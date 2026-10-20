from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .serializers import MessageSerializer
from .services import ChatError, ChatService, user_group, visible_chats


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """One websocket per signed-in tab, carrying events for every chat the user can see.

    Client -> server (each may carry a "ref" that is echoed back in the ack or error):
        {"action": "send", "chat": <id>, "body": "..."}
        {"action": "read", "chat": <id>}
    Server -> client:
        {"type": "message", "chat": <id>, "message": {...}}   new message, incl. your own
        {"type": "read", "chat": <id>, "last_read_at": "..."}  you read it in another tab
        {"type": "removed", "chat": <id>}                      you were removed from a group
        {"type": "ack", "ref": ..., ...} / {"type": "error", "ref": ..., "error": "..."}
    """

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        self.group_name = user_group(user.pk)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        ref = content.get("ref")
        handlers = {"send": self.send_message, "read": self.mark_read}
        handler = handlers.get(content.get("action"))
        try:
            if handler is None:
                raise ChatError("Unknown action.")
            result = await database_sync_to_async(handler)(content)
        except ChatError as exc:
            await self.send_json({"type": "error", "ref": ref, "error": str(exc)})
            return
        await self.send_json({"type": "ack", "ref": ref, **result})

    def get_chat(self, content):
        chat = visible_chats(self.user).select_related("loan").filter(pk=content.get("chat")).first()
        if chat is None:
            raise ChatError("Chat not found.")
        return chat

    def send_message(self, content):
        message = ChatService.send_message(self.get_chat(content), self.user, content.get("body"))
        return {"message": MessageSerializer(message).data}

    def mark_read(self, content):
        read = ChatService.mark_read(self.get_chat(content), self.user)
        return {"chat": read.chat_id, "last_read_at": read.last_read_at.isoformat()}

    async def chat_event(self, event):
        """Events fanned out by ChatService.broadcast."""
        await self.send_json(event["event"])
