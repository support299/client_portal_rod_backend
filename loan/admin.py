from django.contrib import admin

from .models import Loan, LoanOfficer


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
        return ", ".join(u.email for u in obj.loan_officers.all())

    @admin.display(description="Stage", ordering="opportunity__stage__position")
    def stage(self, obj):
        return obj.opportunity.stage.name.strip() if obj.opportunity.stage else ""
