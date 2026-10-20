from django.contrib.auth import get_user_model
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveUpdateAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsStaffMember
from loan.views import visible_loans

from .models import Chat
from .serializers import (
    ChatCreateSerializer, ChatSerializer, ChatUpdateSerializer, ChatUserSerializer, MembersSerializer,
    MessageSerializer, SendMessageSerializer,
)

User = get_user_model()
from .services import (
    ChatError, ChatService, inbox_order, messages_queryset, visible_chats, with_inbox_fields,
)


def chat_for(user, pk):
    """The chat if the user can see it, else 404."""
    return get_object_or_404(visible_chats(user).select_related("loan"), pk=pk)


def inbox_row(request, chat):
    chat = with_inbox_fields(Chat.objects.filter(pk=chat.pk), request.user).get()
    return ChatSerializer(chat, context={"request": request}).data


def error(exc):
    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ChatPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 100


class ChatListCreateView(ListAPIView):
    """GET: the user's inbox, newest activity first. Query params: kind (loan/group/direct), page.
    POST: start a direct message {kind: "direct", user} or a group {kind: "group", name, members}.
    A direct message that already exists is returned (200) instead of created (201).
    """

    permission_classes = [IsAuthenticated]
    pagination_class = ChatPagination
    serializer_class = ChatSerializer

    def get_queryset(self):
        user = self.request.user
        ChatService.ensure_loan_chats(user)
        qs = with_inbox_fields(visible_chats(user), user)
        kind = self.request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        return inbox_order(qs)

    def post(self, request):
        serializer = ChatCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if data["kind"] == Chat.Kind.DIRECT:
                chat, created = ChatService.get_or_create_direct(request.user, data["user"])
            else:
                chat = ChatService.create_group(request.user, data["name"], data.get("members", []))
                created = True
        except ChatError as exc:
            return error(exc)
        return Response(inbox_row(request, chat), status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ChatDetailView(RetrieveUpdateAPIView):
    """GET one chat (inbox row). PATCH {name}: rename a group (its members or admins)."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        return with_inbox_fields(visible_chats(self.request.user), self.request.user)

    def get_serializer_class(self):
        return ChatUpdateSerializer if self.request.method == "PATCH" else ChatSerializer

    def update(self, request, *args, **kwargs):
        chat = self.get_object()
        try:
            ChatService.check_can_manage(chat, request.user)
        except ChatError as exc:
            return error(exc)
        serializer = ChatUpdateSerializer(chat, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if not (serializer.validated_data.get("name") or "").strip():
            return error("A group needs a name.")
        serializer.save()
        return Response(inbox_row(request, chat))


class LoanChatView(APIView):
    """GET the chat of a loan the user can see, creating it on first use."""

    permission_classes = [IsAuthenticated]

    def get(self, request, loan_id):
        loan = get_object_or_404(visible_loans(request.user), pk=loan_id)
        return Response(inbox_row(request, ChatService.ensure_loan_chat(loan)))


class ChatMessagesView(APIView):
    """GET messages, oldest first. Query params: before (a message id, for older pages), limit (max 100).
    POST {body}: send a text message (the websocket's "send" action does the same).
    """

    permission_classes = [IsAuthenticated]
    DEFAULT_LIMIT = 50

    def get(self, request, pk):
        chat = chat_for(request.user, pk)
        try:
            limit = min(int(request.query_params.get("limit", self.DEFAULT_LIMIT)), 100)
            before = request.query_params.get("before")
            qs = messages_queryset().filter(chat=chat)
            if before:
                qs = qs.filter(pk__lt=int(before))
        except ValueError:
            return error("before and limit must be numbers.")
        page = list(qs.order_by("-pk")[:limit + 1])
        has_more = len(page) > limit
        page = page[:limit][::-1]
        return Response({"results": MessageSerializer(page, many=True).data, "has_more": has_more})

    def post(self, request, pk):
        chat = chat_for(request.user, pk)
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            message = ChatService.send_message(chat, request.user, serializer.validated_data["body"])
        except ChatError as exc:
            return error(exc)
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)


class ChatReadView(APIView):
    """POST: mark the chat read up to now."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        chat = chat_for(request.user, pk)
        read = ChatService.mark_read(chat, request.user)
        return Response({"chat": chat.pk, "last_read_at": read.last_read_at})


class ChatMembersView(APIView):
    """POST {users: [ids]}: add staff to a group."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        chat = chat_for(request.user, pk)
        serializer = MembersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            ChatService.add_members(chat, request.user, serializer.validated_data["users"])
        except ChatError as exc:
            return error(exc)
        return Response(inbox_row(request, chat))


class ChatPeopleView(ListAPIView):
    """GET ?search=: staff the user can start a direct message or group with (max 20)."""

    permission_classes = [IsStaffMember]
    serializer_class = ChatUserSerializer
    pagination_class = None

    def get_queryset(self):
        qs = User.objects.filter(is_active=True).exclude(role=User.Role.BORROWER).exclude(pk=self.request.user.pk)
        search = self.request.query_params.get("search", "").strip()
        if search:
            qs = qs.annotate(search_name=Concat("first_name", Value(" "), "last_name")).filter(
                Q(search_name__icontains=search) | Q(email__icontains=search)
            )
        return qs.order_by("first_name", "last_name", "email")[:20]


class ChatMemberDetailView(APIView):
    """DELETE: remove a member from a group (or leave it, with your own id)."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, pk, user_id):
        chat = chat_for(request.user, pk)
        member = get_object_or_404(chat.members.select_related("user"), user_id=user_id, is_deleted=False).user
        try:
            ChatService.remove_member(chat, request.user, member)
        except ChatError as exc:
            return error(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)
