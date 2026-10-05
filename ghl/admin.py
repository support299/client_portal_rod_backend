from django.contrib import admin

from .models import GHLToken


@admin.register(GHLToken)
class GHLTokenAdmin(admin.ModelAdmin):
    list_display = ("user_type", "location_id", "company_id", "expires_at", "updated_at")
    search_fields = ("location_id", "company_id")
    exclude = ("access_token", "refresh_token")
    readonly_fields = ("expires_at", "scope", "created_at", "updated_at")
