from django.conf import settings
from django.db import models


class Notification(models.Model):
    """One in-app notification for one user. Title and body are written when the event happens,
    so they still read correctly if the draw or document changes later.

    The frontend picks where a notification leads from its kind, loan and data (e.g. {"draw": 5}),
    since borrowers and staff open the same loan on different pages.
    """

    class Kind(models.TextChoices):
        DRAW_SUBMITTED = "draw_submitted", "Draw submitted"
        DRAW_STATUS = "draw_status", "Draw status changed"
        DOCUMENT_REQUESTED = "document_requested", "Document requested"
        DOCUMENT_UPLOADED = "document_uploaded", "Document uploaded"
        DOCUMENT_REVIEWED = "document_reviewed", "Document reviewed"
        LOAN_ASSIGNED = "loan_assigned", "Added to a loan"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications",
    )
    # Who caused it; never notified about their own action.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    loan = models.ForeignKey(
        "loan.Loan", on_delete=models.CASCADE, null=True, blank=True, related_name="notifications",
    )
    # Ids of what it's about, e.g. {"draw": 5} or {"document_request": 9}.
    data = models.JSONField(default=dict, blank=True)
    # Null while unread.
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["recipient", "read_at", "-created_at"], name="notification_inbox")]

    def __str__(self):
        return f"{self.title} -> {self.recipient}"
