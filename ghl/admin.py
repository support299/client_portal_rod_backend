from django.contrib import admin

from .models import GHLOutboundLog, GHLToken


@admin.register(GHLToken)
class GHLTokenAdmin(admin.ModelAdmin):
    list_display = ("user_type", "location_id", "company_id", "expires_at", "updated_at")
    search_fields = ("location_id", "company_id")
    exclude = ("access_token", "refresh_token")
    readonly_fields = ("expires_at", "scope", "created_at", "updated_at")


class ReadOnlyLogAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GHLOutboundLog)
class GHLOutboundLogAdmin(ReadOnlyLogAdmin):
    list_display = ("created_at", "action", "object_id", "http_status", "success", "triggered_by")
    list_filter = ("action", "success")
    search_fields = ("object_id", "location_id")
    date_hierarchy = "created_at"
