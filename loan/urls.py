from django.urls import path

from . import document_views, draw_views, note_views, page_views, task_views, views

app_name = "loan"

urlpatterns = [
    path("", views.LoanListView.as_view(), name="loan_list"),
    path("from-opportunity/", views.LoanFromOpportunityView.as_view(), name="loan_from_opportunity"),
    path("stages/", views.LoanStagesView.as_view(), name="loan_stages"),
    path("tasks/", task_views.TaskListView.as_view(), name="task_list"),
    path("tasks/manual/", task_views.StaffTaskCreateView.as_view(), name="staff_task_create"),
    path("tasks/manual/<int:pk>/", task_views.StaffTaskDetailView.as_view(), name="staff_task_detail"),
    path("<int:pk>/", views.LoanDetailView.as_view(), name="loan_detail"),
    path("<int:pk>/page/", page_views.LoanPageView.as_view(), name="loan_page"),
    # Documents
    path("document-types/", document_views.DocumentTypeListView.as_view(), name="document_type_list"),
    path("document-types/<int:pk>/", document_views.DocumentTypeDetailView.as_view(), name="document_type_detail"),
    path("document-types/<int:pk>/template/", document_views.DocumentTypeTemplateView.as_view(),
         name="document_type_template"),
    path("<int:loan_pk>/document-requests/", document_views.DocumentRequestListView.as_view(),
         name="document_request_list"),
    path("<int:loan_pk>/document-requests/<int:pk>/", document_views.DocumentRequestDetailView.as_view(),
         name="document_request_detail"),
    path("<int:loan_pk>/documents/", document_views.LoanDocumentListView.as_view(), name="loan_document_list"),
    path("<int:loan_pk>/documents/<int:pk>/", document_views.LoanDocumentDetailView.as_view(),
         name="loan_document_detail"),
    path("<int:loan_pk>/documents/<int:pk>/download/", document_views.LoanDocumentDownloadView.as_view(),
         name="loan_document_download"),
    # Draws
    path("<int:loan_pk>/draws/", draw_views.DrawRequestListView.as_view(), name="draw_list"),
    path("<int:loan_pk>/draws/summary/", draw_views.DrawSummaryView.as_view(), name="draw_summary"),
    path("<int:loan_pk>/draws/<int:pk>/", draw_views.DrawRequestDetailView.as_view(), name="draw_detail"),
    path("<int:loan_pk>/draws/<int:pk>/status/", draw_views.DrawRequestStatusView.as_view(), name="draw_status"),
    # Internal notes
    path("<int:loan_pk>/notes/", note_views.LoanNoteListView.as_view(), name="loan_note_list"),
    path("<int:loan_pk>/notes/<int:pk>/", note_views.LoanNoteDetailView.as_view(), name="loan_note_detail"),
]
