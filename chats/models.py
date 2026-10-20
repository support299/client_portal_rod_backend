from django.conf import settings
from django.db import models


class Chat(models.Model):
    """A chat: one per loan, or a staff group / direct message with no loan.

    Who can see a loan chat follows the loan (its borrower, current loan officers and admins),
    so reassigning a loan needs no membership changes. Groups and direct messages list their
    members in ChatMember.
    """

    class Kind(models.TextChoices):
        LOAN = "loan", "Loan chat"
        GROUP = "group", "Internal group"
        DIRECT = "direct", "Direct message"

    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    # Loan chats only, one per loan.
    loan = models.OneToOneField(
        "loan.Loan", on_delete=models.CASCADE, null=True, blank=True, related_name="chat",
    )
    name = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-last_message_at", "-id"]
        constraints = [
            # A loan chat has a loan; groups and direct messages never do.
            models.CheckConstraint(
                condition=(
                    models.Q(kind="loan", loan__isnull=False)
                    | (~models.Q(kind="loan") & models.Q(loan__isnull=True))
                ),
                name="chat_loan_matches_kind",
            ),
        ]

    def __str__(self):
        if self.kind == self.Kind.LOAN:
            return f"Chat: {self.loan}"
        return self.name or f"{self.get_kind_display()} {self.pk}"


class ChatMember(models.Model):
    """A member of a group or direct message; loan chats get their access from the loan."""

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_memberships",
    )
    is_deleted = models.BooleanField(default=False, db_index=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["joined_at", "id"]
        constraints = [
            # Only one active membership per user; removed ones are kept (is_deleted) and may be re-added.
            models.UniqueConstraint(
                fields=["chat", "user"], condition=models.Q(is_deleted=False),
                name="unique_chat_member",
            ),
        ]

    def __str__(self):
        return f"{self.user} in {self.chat}"


class ChatRead(models.Model):
    """How far one user has read in one chat, for unread counts in every kind of chat."""

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    last_read_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["chat", "user"], name="unique_chat_read"),
        ]

    def __str__(self):
        return f"{self.user} read {self.chat} at {self.last_read_at}"


class Message(models.Model):
    """One message in a chat; its files are loan.LoanDocument rows (message.attachments)."""

    class Kind(models.TextChoices):
        TEXT = "text", "Text"
        DOCUMENT_REQUEST = "document_request", "Document request"
        SYSTEM = "system", "System"

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    # Empty for system messages.
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.TEXT)
    body = models.TextField(blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["chat", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} in {self.chat}"
