import logging

from celery import shared_task
from ghl.client import GHLAuthError
from ghl.models import GHLToken
from ghl.services import GHLAPIError

from .models import GHLWebhookLog
from .services import process_log

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_ghl_webhook(self, log_id):
    """Apply a logged webhook event; the log row records the outcome."""
    try:
        log = GHLWebhookLog.objects.get(pk=log_id)
    except GHLWebhookLog.DoesNotExist:
        return
    try:
        process_log(log)
    except (GHLAuthError, GHLAPIError, GHLToken.DoesNotExist) as exc:
        log.status = 'failed'
        log.error = str(exc)
        log.save(update_fields=['status', 'error'])
        logger.warning('GHL webhook %s failed: %s', log_id, exc)
        if not isinstance(exc, GHLToken.DoesNotExist):
            raise self.retry(exc=exc)
