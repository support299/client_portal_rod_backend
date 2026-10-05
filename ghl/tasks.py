import logging
from datetime import timedelta

from celery import shared_task

from .client import GHLAuthError, get_access_token
from .models import GHLToken

logger = logging.getLogger(__name__)

# A margin larger than any token lifetime, so get_access_token always refreshes.
FORCE_REFRESH = timedelta.max


@shared_task
def refresh_all_ghl_tokens():
    """Queue a refresh for every stored GHL token."""
    token_ids = list(GHLToken.objects.values_list('pk', flat=True))
    for token_id in token_ids:
        refresh_ghl_token.delay(token_id)
    logger.info('Queued refresh for %d GHL token(s)', len(token_ids))
    return len(token_ids)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def refresh_ghl_token(self, token_id):
    try:
        token = GHLToken.objects.get(pk=token_id)
    except GHLToken.DoesNotExist:
        return
    try:
        get_access_token(token, margin=FORCE_REFRESH)
    except GHLAuthError as exc:
        logger.warning('GHL token refresh failed for %s: %s', token, exc)
        raise self.retry(exc=exc)
