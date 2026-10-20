from django.contrib.auth import get_user_model
from rest_framework import serializers

from loan.models import LoanDocument

from .models import Chat, Message

User = get_user_model()


class ChatUserSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "full_name", "email", "role"]


class AttachmentSerializer(serializers.ModelSerializer):
    """A file sent with a message. The GHL url is left out on purpose: files are public there."""

    class Meta:
        model = LoanDocument
        fields = ["id", "name", "content_type", "size", "loan"]


class MessageSerializer(serializers.ModelSerializer):
    sender = ChatUserSerializer(read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Message
        fields = ["id", "chat", "sender", "kind", "body", "attachments", "created_at"]
        read_only_fields = fields


class ChatSerializer(serializers.ModelSerializer):
    """One inbox row. Expects a queryset from services.with_inbox_fields."""

    title = serializers.SerializerMethodField()
    loan_name = serializers.CharField(source="loan.opportunity.name", read_only=True, default=None)
    # Loan chats only: who the borrower is, so staff can see and search by their email.
    borrower = ChatUserSerializer(source="loan.borrower", read_only=True, default=None)
    members = serializers.SerializerMethodField()
    unread_count = serializers.IntegerField(read_only=True, default=0)
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = Chat
        fields = [
            "id", "kind", "title", "name", "loan", "loan_name", "borrower", "members",
            "unread_count", "last_message", "last_message_at", "created_at",
        ]
        read_only_fields = fields

    def _members(self, chat):
        members = getattr(chat, "active_members", None)
        if members is None:
            members = list(chat.members.filter(is_deleted=False).select_related("user"))
        return [m.user for m in members]

    def get_title(self, chat):
        if chat.kind == Chat.Kind.LOAN:
            return chat.loan.opportunity.name or f"Loan {chat.loan_id}"
        if chat.kind == Chat.Kind.GROUP:
            return chat.name
        request = self.context.get("request")
        others = [u for u in self._members(chat) if not request or u.pk != request.user.pk]
        return ", ".join(u.get_full_name() or u.email for u in others)

    def get_members(self, chat):
        # Loan chat access follows the loan, so it has no member list.
        if chat.kind == Chat.Kind.LOAN:
            return []
        return ChatUserSerializer(self._members(chat), many=True).data

    def get_last_message(self, chat):
        if not hasattr(chat, "last_message_body") or chat.last_message_at is None:
            return None
        return {"kind": chat.last_message_kind, "body": chat.last_message_body, "at": chat.last_message_at}


class ChatCreateSerializer(serializers.Serializer):
    """Start a direct message (kind=direct, user) or a group (kind=group, name, members)."""

    kind = serializers.ChoiceField(choices=[Chat.Kind.DIRECT, Chat.Kind.GROUP])
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), required=False)
    name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    members = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), many=True, required=False)

    def validate(self, attrs):
        if attrs["kind"] == Chat.Kind.DIRECT and not attrs.get("user"):
            raise serializers.ValidationError({"user": "Choose who to message."})
        if attrs["kind"] == Chat.Kind.GROUP and not (attrs.get("name") or "").strip():
            raise serializers.ValidationError({"name": "A group needs a name."})
        return attrs


class ChatUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Chat
        fields = ["name"]


class MembersSerializer(serializers.Serializer):
    users = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), many=True)


class SendMessageSerializer(serializers.Serializer):
    body = serializers.CharField(allow_blank=False, trim_whitespace=True)
