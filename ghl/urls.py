from django.urls import path

from . import views

app_name = "ghl"

urlpatterns = [
    path("oauth/start/", views.oauth_start, name="oauth_start"),
    path("oauth/callback/", views.oauth_callback, name="oauth_callback"),
    path("oauth/status/", views.oauth_status, name="oauth_status"),
    path("opportunities/", views.OpportunityListView.as_view(), name="opportunity_list"),
    path("opportunities/filters/", views.OpportunityFiltersView.as_view(), name="opportunity_filters"),
    path("users/", views.UserListView.as_view(), name="user_list"),
]
