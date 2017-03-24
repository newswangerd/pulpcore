"""
A module designed to handle celery events related to workers.

Two celery events that need processing are the 'worker-heartbeat' and 'worker-offline'
events. Each 'worker-heartbeat' event is passed to handle_worker_heartbeat() as an event for
handling. Each 'worker-offline' event is passed to handle_worker_offline() for handling.
See the individual function docblocks for more detail on how each event type is handled.

The use of an 'event' or 'celery event' throughout this module refers to a dict built by celery
that contains event information. Read more about this in the docs for celery.events.

Other functions in this module are helper functions designed to deduplicate the amount of shared
code between the event handlers.
"""

from datetime import datetime
from gettext import gettext as _
import logging

from django.utils import timezone

from pulp.app.models import Worker, TaskLock
from pulp.common import TASK_INCOMPLETE_STATES
from pulp.tasking.constants import TASKING_CONSTANTS
from pulp.tasking.util import cancel


_logger = logging.getLogger(__name__)


def handle_worker_heartbeat(worker_name):
    """
    This is a generic function for updating worker heartbeat records.

    Existing Worker objects are searched for one to update. If an existing one is found, it is
    updated. Otherwise a new Worker entry is created. Logging at the info level is also done.

    :param worker_name: The hostname of the worker
    :type  worker_name: basestring
    """
    existing_worker, created = Worker.objects.get_or_create(name=worker_name)
    if created:
        msg = _("New worker '{name}' discovered").format(name=worker_name)
        _logger.info(msg)
    else:
        existing_worker.save_heartbeat()

    msg = _("Worker heartbeat from '{name}' at time {timestamp}").format(
        timestamp=existing_worker.last_heartbeat,
        name=worker_name
    )

    _logger.debug(msg)



def handle_worker_offline(worker_name):
    """
    This is a generic function for handling workers going offline.

    _delete_worker() task is called to handle any work cleanup associated with a worker going
    offline. Logging at the info level is also done.

    :param worker_name: The hostname of the worker
    :type  worker_name: basestring
    """
    msg = _("Worker '%s' shutdown") % worker_name
    _logger.info(msg)
    delete_worker(worker_name, normal_shutdown=True)


def delete_worker(name, normal_shutdown=False):
    """
    Delete the :class:`~pulp.app.models.Worker` from the database and cancel associated tasks.

    If the worker shutdown normally, no message is logged, otherwise an error level message is
    logged. Default is to assume the worker did not shut down normally.

    Any resource reservations associated with this worker are cleaned up by this function.

    Any tasks associated with this worker are explicitly canceled.

    :param name:            The name of the worker you wish to delete.
    :type  name:            basestring
    :param normal_shutdown: True if the worker shutdown normally, False otherwise. Defaults to
                            False.
    :type normal_shutdown:  bool
    """
    if not normal_shutdown:
        msg = _('The worker named %(name)s is missing. Canceling the tasks in its queue.')
        msg = msg % {'name': name}
        _logger.error(msg)
    else:
        msg = _("Cleaning up shutdown worker '%s'.") % name
        _logger.info(msg)

    try:
        worker = Worker.objects.get(name=name)
    except Worker.DoesNotExist:
        pass
    else:
        # Cancel all of the tasks that were assigned to this worker's queue
        for task_status in worker.tasks.filter(state__in=TASK_INCOMPLETE_STATES):
            cancel(task_status.pk)
        worker.delete()

    is_resource_manager = name.startswith(TASKING_CONSTANTS.RESOURCE_MANAGER_WORKER_NAME)
    is_celerybeat = name.startswith(TASKING_CONSTANTS.CELERYBEAT_WORKER_NAME)
    if is_celerybeat or is_resource_manager:
        TaskLock.objects.filter(name=name).delete()
