import base64
import binascii
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from django.conf import settings
from django.utils import timezone

from ghl.client import api_request
from ghl.models import GHLOpportunity
from ghl.services import AccountLinkService, GHLAPIError, GHLService, OpportunityService
from loan.services import LoanConversionError, LoanService

logger = logging.getLogger(__name__)

OPPORTUNITY_EVENTS = {
    "OpportunityCreate",
    "OpportunityUpdate",
    "OpportunityDelete",
    "OpportunityStageUpdate",
    "OpportunityStatusUpdate",
    "OpportunityMonetaryValueUpdate",
    "OpportunityAssignedToUpdate",
}


def verify_signature(raw_body, signature):
    """Check the X-GHL-Signature header (Ed25519, base64) against the raw request body.

    Returns True/False, or None when GHL_WEBHOOK_PUBLIC_KEY isn't configured.
    """
    if not settings.GHL_WEBHOOK_PUBLIC_KEY:
        return None
    if not signature:
        return False
    try:
        public_key = load_pem_public_key(settings.GHL_WEBHOOK_PUBLIC_KEY.encode())
        public_key.verify(base64.b64decode(signature), raw_body)
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


def delete_opportunity(opportunity_id):
    """Delete our copy of an opportunity removed in GHL; one with a loan is kept (Loan PROTECTs it)."""
    deleted, _ = GHLOpportunity.objects.filter(opportunity_id=opportunity_id, loan__isnull=True).delete()
    if not deleted and GHLOpportunity.objects.filter(opportunity_id=opportunity_id).exists():
        return "deleted in GHL; kept locally because it has a loan"
    return ""


def process_log(log):
    """Apply one logged opportunity event to GHLOpportunity.

    The payload only carries a few fields, so the opportunity is re-fetched from GHL and
    upserted; that also makes the result independent of the order events arrive in.
    An opportunity in the loan pipeline gets its loan created (see LoanService.ensure_loan).
    """
    note = ""
    if log.event_type not in OPPORTUNITY_EVENTS or not log.object_id:
        log.status = "ignored"
    elif log.event_type == "OpportunityDelete":
        note = delete_opportunity(log.object_id)
        log.status = "processed"
    else:
        location_id = log.location_id or (log.payload or {}).get("locationId", "")
        token = GHLService.token_for_location(location_id)
        try:
            data = GHLService.json(api_request(token, "GET", f"/opportunities/{log.object_id}"))
        except GHLAPIError as exc:
            if exc.status_code != 404:
                raise
            # Already gone in GHL (deleted before we got to it).
            note = delete_opportunity(log.object_id)
        else:
            OpportunityService.upsert([data.get("opportunity", data)], location_id)
            AccountLinkService.link_contacts()
            opportunity = GHLOpportunity.objects.select_related("contact").get(opportunity_id=log.object_id)
            try:
                LoanService.ensure_loan(opportunity)
            except LoanConversionError as exc:
                # The opportunity itself is saved; only the loan is missing.
                note = f"loan not created: {exc}"
        log.status = "processed"
    log.error = note
    log.processed_at = timezone.now()
    log.save(update_fields=["status", "error", "processed_at"])
