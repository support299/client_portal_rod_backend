from django.urls import path

from . import views

app_name = "ghl"

urlpatterns = [
    path("oauth/start/", views.oauth_start, name="oauth_start"),
    path("oauth/callback/", views.oauth_callback, name="oauth_callback"),
    path("oauth/status/", views.oauth_status, name="oauth_status"),
]
