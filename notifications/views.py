from django.shortcuts import get_object_or_404
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification
from .serializers import NotificationSerializer
from .services import NotificationService


class NotificationPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        response = super().get_paginated_response(data)
        response.data["unread_count"] = NotificationService.unread_count(self.request.user)
        return response


class NotificationListView(ListAPIView):
    """GET the user's own notifications, newest first, with unread_count.

    Query params (all optional): unread (true), kind, loan, page, page_size.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer
    pagination_class = NotificationPagination

    def get_queryset(self):
        params = self.request.query_params
        qs = Notification.objects.filter(recipient=self.request.user).select_related("actor", "loan__opportunity")
        if params.get("unread") == "true":
            qs = qs.filter(read_at__isnull=True)
        if params.get("kind"):
            qs = qs.filter(kind=params["kind"])
        if params.get("loan"):
            qs = qs.filter(loan=params["loan"])
        return qs


class NotificationUnreadCountView(APIView):
    """GET {"unread_count": n} for the bell badge."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"unread_count": NotificationService.unread_count(request.user)})


class NotificationReadView(APIView):
    """POST marks one notification read."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
        NotificationService.mark_read(request.user, ids=[notification.pk])
        return Response({"unread_count": NotificationService.unread_count(request.user)})


class NotificationReadAllView(APIView):
    """POST marks all of the user's notifications read."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        NotificationService.mark_read(request.user)
        return Response({"unread_count": 0})
