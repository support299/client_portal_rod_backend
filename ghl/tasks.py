import logging
from datetime import timedelta

from celery import shared_task
from loan.services import LoanService

from .client import GHLAuthError, get_access_token
from .models import GHLToken
from .services import ContactService, GHLAPIError, OpportunityService, UserService

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


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_ghl_location(self, location_id):
    """Sync users, contacts, then opportunities (and any missing pipeline stages) for one location.

    Afterwards every loan-pipeline opportunity without a loan gets one (see LoanService.ensure_pipeline_loans).
    """
    try:
        users = UserService.sync(location_id)
        contacts = ContactService.sync(location_id)
        opportunities = OpportunityService.sync(location_id)
    except (GHLAuthError, GHLAPIError) as exc:
        logger.warning('GHL sync failed for location %s: %s', location_id, exc)
        raise self.retry(exc=exc)
    loans, skipped = LoanService.ensure_pipeline_loans()
    logger.info('Synced %d user(s), %d contact(s) and %d opportunity(ies) for location %s; '
                'created %d loan(s), skipped %d',
                users, contacts, opportunities, location_id, len(loans), len(skipped))
    return {'users': users, 'contacts': contacts, 'opportunities': opportunities,
            'loans_created': len(loans), 'loans_skipped': len(skipped)}


@shared_task
def sync_all_ghl_users():
    """Queue a user sync for every connected GHL location."""
    location_ids = list(
        GHLToken.objects.filter(user_type='Location')
        .exclude(location_id='')
        .values_list('location_id', flat=True)
        .distinct()
    )
    for location_id in location_ids:
        sync_ghl_users.delay(location_id)
    logger.info('Queued user sync for %d GHL location(s)', len(location_ids))
    return len(location_ids)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_ghl_users(self, location_id):
    try:
        count = UserService.sync(location_id)
    except (GHLAuthError, GHLAPIError) as exc:
        logger.warning('GHL user sync failed for location %s: %s', location_id, exc)
        raise self.retry(exc=exc)
    logger.info('Synced %d user(s) for location %s', count, location_id)
    return count
