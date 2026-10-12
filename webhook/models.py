from django.db import models


class GHLWebhookLog(models.Model):
    """One row per webhook delivery from GHL, saved before any processing."""

    STATUS_CHOICES = [
        ("received", "Received"),
        ("processed", "Processed"),
        ("failed", "Failed"),
        ("ignored", "Ignored"),
    ]

    # Pulled out of the payload so they can be filtered on quickly.
    event_type = models.CharField(max_length=100, db_index=True)
    location_id = models.CharField(max_length=64, blank=True, db_index=True)
    webhook_id = models.CharField(max_length=128, blank=True, db_index=True)
    contact_id = models.CharField(max_length=64, blank=True, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, db_index=True)

    # Nulled out by the retention cleanup once the row is old and processed.
    payload = models.JSONField(default=dict, blank=True, null=True)
    signature_valid = models.BooleanField(default=False)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="received", db_index=True)
    error = models.TextField(blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["event_type", "-received_at"]),
            models.Index(fields=["status", "-received_at"]),
        ]

    def __str__(self):
        return f"{self.event_type} @ {self.received_at:%Y-%m-%d %H:%M:%S} [{self.status}]"
