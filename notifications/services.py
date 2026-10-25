import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from chats.services import user_group

from .models import Notification

logger = logging.getLogger(__name__)


def loan_officer_ids(loan):
    """The loan's active officers. Admins who aren't on the loan aren't notified."""
    return set(loan.officer_assignments.filter(is_deleted=False).values_list("officer_id", flat=True))


def loan_team_ids(loan):
    """Everyone working the loan: the borrower and its officers."""
    return loan_officer_ids(loan) | {loan.borrower_id}


class NotificationService:
    @staticmethod
    def unread_count(user):
        return Notification.objects.filter(recipient=user, read_at__isnull=True).count()

    @staticmethod
    def notify(recipient_ids, kind, title, body="", loan=None, actor=None, data=None):
        """Create a notification for each recipient (except the actor) and push it to their open tabs.

        The push runs once the surrounding transaction commits, so a rolled-back change sends
        nothing. Returns the notifications created.
        """
        recipient_ids = set(recipient_ids) - {None, getattr(actor, "pk", None)}
        if not recipient_ids:
            return []
        notifications = Notification.objects.bulk_create([
            Notification(
                recipient_id=user_id, actor=actor, kind=kind, title=title, body=body, loan=loan,
                data=data or {},
            )
            for user_id in recipient_ids
        ])
        transaction.on_commit(lambda: NotificationService.push(notifications))
        return notifications

    @staticmethod
    def push(notifications):
        """Send each notification down its recipient's websocket (the chat socket, see ChatConsumer).

        A failed push must not fail the request; clients catch up from the REST API.
        """
        from .serializers import NotificationSerializer

        layer = get_channel_layer()
        if layer is None:
            return
        for notification in notifications:
            try:
                event = {
                    "type": "notification",
                    "notification": NotificationSerializer(notification).data,
                    "unread_count": NotificationService.unread_count(notification.recipient_id),
                }
                async_to_sync(layer.group_send)(
                    user_group(notification.recipient_id), {"type": "chat.event", "event": event},
                )
            except Exception:
                logger.exception("Could not push notification %s", notification.pk)

    @staticmethod
    def mark_read(user, ids=None):
        """Mark the user's notifications read: those in ids, or all of them. Returns how many changed."""
        qs = Notification.objects.filter(recipient=user, read_at__isnull=True)
        if ids is not None:
            qs = qs.filter(pk__in=ids)
        return qs.update(read_at=timezone.now())
