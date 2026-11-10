from django.urls import path

from . import views

app_name = "report"

urlpatterns = [
    path("loans/summary/", views.LoanReportSummaryView.as_view(), name="loan_report_summary"),
    path("loans/export/", views.LoanReportExportView.as_view(), name="loan_report_export"),
]
