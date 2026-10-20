import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from loan.models import DocumentRequest, Loan, LoanDocument, LoanOfficer

from .models import Chat, ChatMember, ChatRead, Message

logger = logging.getLogger(__name__)


class ChatError(Exception):
    pass


def user_group(user_id):
    """Channel-layer group every websocket of one user joins; chat events are fanned out to these."""
    return f"chat_user_{user_id}"


def admin_ids():
    User = get_user_model()
    return set(
        User.objects.filter(Q(is_staff=True) | Q(role=User.Role.ADMIN), is_active=True)
        .values_list("pk", flat=True)
    )


def visible_chats(user):
    """Admins see every chat; borrowers their loans' chats; other staff the chats of loans they're
    on plus the groups and direct messages they're a member of."""
    qs = Chat.objects.filter(is_deleted=False).exclude(kind=Chat.Kind.LOAN, loan__is_deleted=True)
    if user.is_app_admin:
        return qs
    if user.role == user.Role.BORROWER:
        return qs.filter(kind=Chat.Kind.LOAN, loan__borrower=user)
    on_loan = LoanOfficer.objects.filter(loan=OuterRef("loan"), officer=user, is_deleted=False)
    member = ChatMember.objects.filter(chat=OuterRef("pk"), user=user, is_deleted=False)
    return qs.filter((Q(kind=Chat.Kind.LOAN) & Exists(on_loan)) | Exists(member))


def with_inbox_fields(qs, user):
    """Add what the inbox shows: unread_count, last message preview, active members, loan name."""
    last_message = Message.objects.filter(chat=OuterRef("pk"), is_deleted=False).order_by("-created_at", "-id")
    # Messages from others after the user's last read (all of them if they never opened the chat).
    unread = (
        Message.objects.filter(chat=OuterRef("pk"), is_deleted=False)
        .exclude(sender=user)
        .annotate(cutoff=Subquery(
            ChatRead.objects.filter(chat=OuterRef("chat"), user=user).values("last_read_at")[:1]
        ))
        .filter(Q(cutoff__isnull=True) | Q(created_at__gt=F("cutoff")))
        .order_by().values("chat").annotate(n=Count("pk")).values("n")
    )
    return (
        qs.select_related("loan__opportunity", "loan__borrower")
        .prefetch_related(Prefetch(
            "members", queryset=ChatMember.objects.filter(is_deleted=False).select_related("user"),
            to_attr="active_members",
        ))
        .annotate(
            last_message_body=Subquery(last_message.values("body")[:1]),
            last_message_kind=Subquery(last_message.values("kind")[:1]),
            unread_count=Coalesce(Subquery(unread), 0),
        )
    )


def inbox_order(qs):
    """Newest activity first; chats with no messages yet go last."""
    return qs.order_by(F("last_message_at").desc(nulls_last=True), "-id")


def messages_queryset():
    return (
        Message.objects.filter(is_deleted=False)
        .select_related("sender")
        .prefetch_related(
            Prefetch("attachments", queryset=LoanDocument.objects.filter(is_deleted=False)),
            Prefetch(
                "document_requests",
                queryset=DocumentRequest.objects.filter(is_deleted=False).select_related("form_document"),
            ),
        )
    )


class ChatService:
    @staticmethod
    def participant_ids(chat):
        """Users who get live events for a chat: its participants plus admins."""
        if chat.kind == Chat.Kind.LOAN:
            ids = {chat.loan.borrower_id}
            ids.update(
                LoanOfficer.objects.filter(loan_id=chat.loan_id, is_deleted=False)
                .values_list("officer_id", flat=True)
            )
        else:
            ids = set(chat.members.filter(is_deleted=False).values_list("user_id", flat=True))
        return ids | admin_ids()

    @staticmethod
    def broadcast(user_ids, event):
        """Push an event to every open websocket of these users (no-op without a channel layer)."""
        layer = get_channel_layer()
        if layer is None:
            return
        for user_id in user_ids:
            try:
                async_to_sync(layer.group_send)(user_group(user_id), {"type": "chat.event", "event": event})
            except Exception:
                # A failed push must not fail the request; clients catch up from the REST API.
                logger.exception("Could not push chat event to user %s", user_id)

    @staticmethod
    def ensure_loan_chat(loan):
        chat = Chat.objects.filter(loan=loan).first()
        if chat:
            return chat
        try:
            with transaction.atomic():
                return Chat.objects.create(kind=Chat.Kind.LOAN, loan=loan)
        except IntegrityError:
            # Created concurrently.
            return Chat.objects.get(loan=loan)

    @staticmethod
    def ensure_loan_chats(user):
        """Create the missing chats for the loans this user can see, so they show in the inbox."""
        loans = Loan.objects.filter(is_deleted=False, chat__isnull=True)
        if user.role == user.Role.BORROWER:
            loans = loans.filter(borrower=user)
        elif not user.is_app_admin:
            loans = loans.filter(officer_assignments__officer=user, officer_assignments__is_deleted=False)
        Chat.objects.bulk_create(
            [Chat(kind=Chat.Kind.LOAN, loan_id=pk) for pk in loans.values_list("pk", flat=True).distinct()],
            ignore_conflicts=True,
        )

    @staticmethod
    def send_message(chat, sender, body, kind=Message.Kind.TEXT, attachments=(), document_request=None):
        """Save a message and push it to the chat's participants once committed.

        attachments (LoanDocuments) and document_request are linked to the message before the
        push, so the pushed message carries them.
        """
        from .serializers import MessageSerializer

        body = (body or "").strip()
        if kind == Message.Kind.TEXT and not body and not attachments:
            raise ChatError("Message is empty.")
        with transaction.atomic():
            message = Message.objects.create(chat=chat, sender=sender, kind=kind, body=body)
            if attachments:
                LoanDocument.objects.filter(pk__in=[d.pk for d in attachments]).update(message=message)
            if document_request is not None:
                DocumentRequest.objects.filter(pk=document_request.pk).update(message=message)
                document_request.message = message
            Chat.objects.filter(pk=chat.pk).update(last_message_at=message.created_at)
            if sender is not None:
                ChatService.mark_read(chat, sender, at=message.created_at, notify=False)
            message = messages_queryset().get(pk=message.pk)
            event = {"type": "message", "chat": chat.pk, "message": MessageSerializer(message).data}
            recipients = ChatService.participant_ids(chat)
            transaction.on_commit(lambda: ChatService.broadcast(recipients, event))
        return message

    @staticmethod
    def mark_read(chat, user, at=None, notify=True):
        """Record that the user has read the chat up to `at` (default now); never moves backwards."""
        at = at or timezone.now()
        read, created = ChatRead.objects.get_or_create(chat=chat, user=user, defaults={"last_read_at": at})
        if not created and read.last_read_at < at:
            read.last_read_at = at
            read.save(update_fields=["last_read_at"])
        if notify:
            # Only the reader's own tabs need to clear their unread badge.
            event = {"type": "read", "chat": chat.pk, "last_read_at": read.last_read_at.isoformat()}
            transaction.on_commit(lambda: ChatService.broadcast([user.pk], event))
        return read

    @staticmethod
    def check_staff(*users):
        for user in users:
            if user.role == user.Role.BORROWER:
                raise ChatError("Borrowers can only chat in their loan chats.")
            if not user.is_active:
                raise ChatError(f"{user.email} is not active.")

    @staticmethod
    @transaction.atomic
    def get_or_create_direct(user, other):
        """The one direct chat between two staff members, created on first use."""
        if user.pk == other.pk:
            raise ChatError("You can't start a direct message with yourself.")
        ChatService.check_staff(user, other)
        existing = (
            Chat.objects.filter(kind=Chat.Kind.DIRECT, is_deleted=False)
            .filter(members__user=user, members__is_deleted=False)
            .filter(members__user=other, members__is_deleted=False)
            .first()
        )
        if existing:
            return existing, False
        chat = Chat.objects.create(kind=Chat.Kind.DIRECT, created_by=user)
        ChatMember.objects.bulk_create([ChatMember(chat=chat, user=user), ChatMember(chat=chat, user=other)])
        return chat, True

    @staticmethod
    @transaction.atomic
    def create_group(creator, name, members):
        """A staff-only group; the creator is always a member."""
        name = (name or "").strip()
        if not name:
            raise ChatError("A group needs a name.")
        members = {u.pk: u for u in [creator, *members]}.values()
        ChatService.check_staff(*members)
        chat = Chat.objects.create(kind=Chat.Kind.GROUP, name=name, created_by=creator)
        ChatMember.objects.bulk_create([ChatMember(chat=chat, user=u) for u in members])
        return chat

    @staticmethod
    def check_can_manage(chat, user):
        if chat.kind != Chat.Kind.GROUP:
            raise ChatError("Only groups have editable members.")
        if not (user.is_app_admin or chat.members.filter(user=user, is_deleted=False).exists()):
            raise ChatError("Only the group's members can change it.")

    @staticmethod
    @transaction.atomic
    def add_members(chat, actor, users):
        ChatService.check_can_manage(chat, actor)
        ChatService.check_staff(*users)
        current = set(chat.members.filter(is_deleted=False).values_list("user_id", flat=True))
        added = [u for u in users if u.pk not in current]
        ChatMember.objects.bulk_create([ChatMember(chat=chat, user=u) for u in added])
        if added:
            names = ", ".join(u.get_full_name() or u.email for u in added)
            ChatService.send_message(chat, None, f"{actor.get_full_name() or actor.email} added {names}",
                                     kind=Message.Kind.SYSTEM)
        return added

    @staticmethod
    @transaction.atomic
    def remove_member(chat, actor, user):
        ChatService.check_can_manage(chat, actor)
        removed = chat.members.filter(user=user, is_deleted=False).update(is_deleted=True)
        if not removed:
            raise ChatError("That user isn't in this group.")
        text = (
            f"{user.get_full_name() or user.email} left" if actor.pk == user.pk
            else f"{actor.get_full_name() or actor.email} removed {user.get_full_name() or user.email}"
        )
        ChatService.send_message(chat, None, text, kind=Message.Kind.SYSTEM)
        # The removed user's open tabs should drop the chat.
        transaction.on_commit(lambda: ChatService.broadcast([user.pk], {"type": "removed", "chat": chat.pk}))
