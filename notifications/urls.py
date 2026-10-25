from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notification_list"),
    path("unread-count/", views.NotificationUnreadCountView.as_view(), name="notification_unread_count"),
    path("read-all/", views.NotificationReadAllView.as_view(), name="notification_read_all"),
    path("<int:pk>/read/", views.NotificationReadView.as_view(), name="notification_read"),
]
