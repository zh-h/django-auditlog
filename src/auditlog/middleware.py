from __future__ import unicode_literals

import threading
import time

from .registry import auditlog
from django.conf import settings
from django.db.models.signals import pre_save
from django.utils.functional import curry
from django.apps import apps
from auditlog.models import LogEntry

# Use MiddlewareMixin when present (Django >= 1.10)
try:
    from django.utils.deprecation import MiddlewareMixin
except ImportError:
    MiddlewareMixin = object

from django.apps import apps

threadlocal = threading.local()


def get_log_entry_object(log_entry):
    """
    Get log entry related Model object
    :param log_entry: LogEntry instance
    :return: Model object which pk is log_entry.object_pk
    :rtype models.Model
    """
    app_label = log_entry.content_type.app_label
    model_name = log_entry.content_type.model
    object_model = apps.get_model(app_label, model_name)
    object = object_model.objects.get(pk=log_entry.object_pk)
    return object


def save_changes(instance):
    """
    Save changes into model object
    :param instance:
    """

    changes = instance.foreign_key_changes_dict
    object = get_log_entry_object(instance)
    for attname in changes.keys():
        value = changes[attname][1]
        # TODO fetch object
        setattr(object, attname, value)
    pre_save.disconnect(sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'])
    auditlog.unregister(object._meta.model)
    object.save()
    auditlog.register(object._meta.model)


class AuditlogMiddleware(MiddlewareMixin):
    """
    Middleware to couple the request's user to log items. This is accomplished by currying the signal receiver with the
    user from the request (or None if the user is not authenticated).
    """

    def process_request(self, request):
        """
        Gets the current user from the request and prepares and connects a signal receiver with the user already
        attached to it.
        """
        # Initialize thread local storage
        threadlocal.auditlog = {
            'signal_duid': (self.__class__, time.time()),
            'remote_addr': request.META.get('REMOTE_ADDR'),
        }

        # In case of proxy, set 'original' address
        if request.META.get('HTTP_X_FORWARDED_FOR'):
            threadlocal.auditlog['remote_addr'] = request.META.get('HTTP_X_FORWARDED_FOR').split(',')[0]

        # Connect signal for automatic logging
        if hasattr(request, 'user') and hasattr(request.user, 'is_authenticated') and request.user.is_authenticated():
            set_actor = curry(self.set_actor, user=request.user, signal_duid=threadlocal.auditlog['signal_duid'])
            pre_save.connect(set_actor, sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'], weak=False)

    def process_response(self, request, response):
        """
        Disconnects the signal receiver to prevent it from staying active.
        """
        if hasattr(threadlocal, 'auditlog'):
            pre_save.disconnect(sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'])

        return response

    def process_exception(self, request, exception):
        """
        Disconnects the signal receiver to prevent it from staying active in case of an exception.
        """
        if hasattr(threadlocal, 'auditlog'):
            pre_save.disconnect(sender=LogEntry, dispatch_uid=threadlocal.auditlog['signal_duid'])

        return None



    @staticmethod
    def set_actor(user, sender, instance, signal_duid, **kwargs):
        """
        Signal receiver with an extra, required 'user' kwarg. This method becomes a real (valid) signal receiver when
        it is curried with the actor.
        """
        if signal_duid != threadlocal.auditlog['signal_duid']:
            return
        try:
            app_label, model_name = settings.AUTH_USER_MODEL.split('.')
            auth_user_model = apps.get_model(app_label, model_name)
        except ValueError:
            auth_user_model = apps.get_model('auth', 'user')
        if sender == LogEntry and isinstance(user, auth_user_model) and instance.actor is None:
            instance.actor = user
        if hasattr(threadlocal, 'auditlog'):
            instance.remote_addr = threadlocal.auditlog['remote_addr']
        review_permission_name = 'review_%s' % instance.content_type.model
        if user.has_perm(review_permission_name):
            instance.reviewer = user
            save_changes(instance)
