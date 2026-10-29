import logging

from celery import shared_task

from .models import Loan
from .services import SurveyAPIError, SurveyService

logger = logging.getLogger(__name__)


@shared_task
def import_all_survey_documents(location_id=None):
    """Queue a survey import for every loan, or only the loans of one GHL location."""
    loans = Loan.objects.filter(is_deleted=False)
    if location_id:
        loans = loans.filter(opportunity__location_id=location_id)
    loan_ids = list(loans.values_list('pk', flat=True))
    for loan_id in loan_ids:
        import_survey_documents.delay(loan_id)
    logger.info('Queued survey import for %d loan(s)', len(loan_ids))
    return len(loan_ids)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def import_survey_documents(self, loan_id):
    try:
        loan = Loan.objects.select_related('opportunity').get(pk=loan_id)
    except Loan.DoesNotExist:
        return 0
    try:
        count = SurveyService.import_documents(loan)
    except SurveyAPIError as exc:
        logger.warning('Survey import failed for loan %s: %s', loan_id, exc)
        raise self.retry(exc=exc)
    logger.info('Imported %d survey file(s) for loan %s', count, loan_id)
    return count
