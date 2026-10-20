from django.contrib import admin

from .models import DocumentRequest, DocumentType, Loan, LoanDocument, LoanOfficer


class LoanOfficerInline(admin.TabularInline):
    model = LoanOfficer
    extra = 0
    raw_id_fields = ["officer"]


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ["id", "opportunity", "borrower", "officers", "stage", "created_at"]
    list_filter = ["opportunity__stage"]
    search_fields = ["opportunity__name", "borrower__email", "opportunity__opportunity_id"]
    raw_id_fields = ["opportunity", "borrower", "created_by"]
    list_select_related = ["opportunity__stage", "borrower"]
    inlines = [LoanOfficerInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("officer_assignments__officer")

    @admin.display(description="Loan officers")
    def officers(self, obj):
        return ", ".join(u.email for u in obj.loan_officers)

    @admin.display(description="Stage", ordering="opportunity__stage__position")
    def stage(self, obj):
        return obj.opportunity.stage.name.strip() if obj.opportunity.stage else ""


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "kind", "is_default", "position", "is_active"]
    list_filter = ["category", "kind", "is_default", "is_active"]
    search_fields = ["name"]


@admin.register(DocumentRequest)
class DocumentRequestAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "loan", "kind", "source", "status", "requested_at", "is_deleted"]
    list_filter = ["status", "kind", "source", "is_deleted"]
    search_fields = ["name", "loan__opportunity__name", "loan__borrower__email"]
    raw_id_fields = ["loan", "document_type", "form_document", "message", "requested_by", "reviewed_by"]


@admin.register(LoanDocument)
class LoanDocumentAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "loan", "direction", "origin", "review_status", "uploaded_at", "is_deleted"]
    list_filter = ["origin", "direction", "review_status", "visible_to_borrower", "is_deleted"]
    search_fields = ["name", "ghl_media_id", "loan__opportunity__name", "loan__borrower__email"]
    raw_id_fields = ["loan", "request", "message", "document_type", "uploaded_by", "reviewed_by"]
    readonly_fields = ["ghl_media_id", "raw", "synced_at"]
