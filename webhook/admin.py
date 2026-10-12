from django.contrib import admin

from .models import GHLWebhookLog


class ReadOnlyLogAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GHLWebhookLog)
class GHLWebhookLogAdmin(ReadOnlyLogAdmin):
    list_display = ("received_at", "event_type", "object_id", "status", "signature_valid")
    list_filter = ("event_type", "status", "signature_valid")
    search_fields = ("object_id", "contact_id", "webhook_id", "location_id")
    date_hierarchy = "received_at"
