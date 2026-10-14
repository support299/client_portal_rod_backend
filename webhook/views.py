import json
import logging

from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.views import IsAppAdmin

from .models import GHLWebhookLog
from .serializers import WebhookLogDetailSerializer, WebhookLogListSerializer
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


class LogPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class WebhookLogListView(ListAPIView):
    """List received GHL webhook deliveries, newest first. Admins only; payload is only in the detail view.

    Query params (all optional): event_type, status, location_id, object_id,
    contact_id, webhook_id, signature_valid (true/false), search (object / contact /
    webhook / location id), date_from / date_to (YYYY-MM-DD, on received_at),
    page, page_size.
    """

    serializer_class = WebhookLogListSerializer
    pagination_class = LogPagination
    permission_classes = [IsAppAdmin]

    FILTER_PARAMS = ("event_type", "status", "location_id", "object_id", "contact_id", "webhook_id")

    def get_queryset(self):
        params = self.request.query_params
        qs = GHLWebhookLog.objects.defer("payload")

        for field in self.FILTER_PARAMS:
            if params.get(field):
                qs = qs.filter(**{field: params[field]})

        valid = params.get("signature_valid", "").lower()
        if valid in ("true", "false"):
            qs = qs.filter(signature_valid=valid == "true")

        search = params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(object_id__icontains=search)
                | Q(contact_id__icontains=search)
                | Q(webhook_id__icontains=search)
                | Q(location_id__icontains=search)
            )

        date_from = parse_date(params.get("date_from", ""))
        date_to = parse_date(params.get("date_to", ""))
        if date_from:
            qs = qs.filter(received_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(received_at__date__lte=date_to)
        return qs


class WebhookLogDetailView(RetrieveAPIView):
    """One webhook delivery including its payload. Admins only."""

    queryset = GHLWebhookLog.objects.all()
    serializer_class = WebhookLogDetailSerializer
    permission_classes = [IsAppAdmin]
