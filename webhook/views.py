import json
import logging

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import GHLWebhookLog
from .services import verify_signature
from .tasks import process_ghl_webhook

logger = logging.getLogger(__name__)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def webhook(request):
    """Receive GHL webhook events: log first, then process in the background."""
    raw = request.body
    try:
        payload = json.loads(raw)
    except ValueError:
        return Response({"error": "invalid json"}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return Response({"error": "invalid payload"}, status=status.HTTP_400_BAD_REQUEST)

    webhook_id = str(payload.get("webhookId") or "")
    if webhook_id and GHLWebhookLog.objects.filter(webhook_id=webhook_id).exists():
        return Response({"status": "duplicate"})

    valid = verify_signature(raw, request.headers.get("X-GHL-Signature", ""))
    log = GHLWebhookLog.objects.create(
        event_type=str(payload.get("type") or "")[:100],
        location_id=str(payload.get("locationId") or ""),
        webhook_id=webhook_id,
        contact_id=str(payload.get("contactId") or ""),
        object_id=str(payload.get("id") or ""),
        payload=payload,
        signature_valid=bool(valid),
        status="ignored" if valid is False else "received",
        error="invalid signature" if valid is False else "",
    )
    if valid is False:
        return Response({"error": "invalid signature"}, status=status.HTTP_401_UNAUTHORIZED)
    if valid is None:
        logger.warning("GHL_WEBHOOK_PUBLIC_KEY is not set; webhook signatures are not being verified")

    def enqueue():
        try:
            process_ghl_webhook.delay(log.pk)
        except Exception:
            # The row stays "received", so it can be replayed once the broker is back.
            logger.exception("Could not queue GHL webhook %s", log.pk)

    transaction.on_commit(enqueue)
    return Response({"status": "received"})
