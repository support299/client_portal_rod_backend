from rest_framework import serializers

from .models import GHLWebhookLog


class WebhookLogListSerializer(serializers.ModelSerializer):
    class Meta:
        model = GHLWebhookLog
        fields = [
            "id", "event_type", "location_id", "webhook_id", "contact_id", "object_id",
            "signature_valid", "status", "error", "processed_at", "received_at",
        ]


class WebhookLogDetailSerializer(WebhookLogListSerializer):
    class Meta(WebhookLogListSerializer.Meta):
        fields = WebhookLogListSerializer.Meta.fields + ["payload"]
