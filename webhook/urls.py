from django.urls import path

from . import views

app_name = "webhook"

urlpatterns = [
    path("", views.webhook, name="receive"),
    path("logs/", views.WebhookLogListView.as_view(), name="log_list"),
    path("logs/<int:pk>/", views.WebhookLogDetailView.as_view(), name="log_detail"),
]
