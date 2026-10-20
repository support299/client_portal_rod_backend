from django.urls import path

from . import views

app_name = "chats"

urlpatterns = [
    path("", views.ChatListCreateView.as_view(), name="chat_list"),
    path("people/", views.ChatPeopleView.as_view(), name="chat_people"),
    path("loans/<int:loan_id>/", views.LoanChatView.as_view(), name="loan_chat"),
    path("<int:pk>/", views.ChatDetailView.as_view(), name="chat_detail"),
    path("<int:pk>/messages/", views.ChatMessagesView.as_view(), name="chat_messages"),
    path("<int:pk>/read/", views.ChatReadView.as_view(), name="chat_read"),
    path("<int:pk>/members/", views.ChatMembersView.as_view(), name="chat_members"),
    path("<int:pk>/members/<int:user_id>/", views.ChatMemberDetailView.as_view(), name="chat_member_detail"),
]
