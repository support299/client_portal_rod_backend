from django.contrib import admin

from .models import Loan


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "borrower", "loan_officer", "loan_amount", "status", "created_at"]
    list_filter = ["status"]
    search_fields = ["name", "borrower__email", "opportunity__opportunity_id"]
    raw_id_fields = ["opportunity", "contact", "borrower", "loan_officer", "created_by"]
