from django.urls import path

from . import views

app_name = "loan"

urlpatterns = [
    path("", views.LoanListView.as_view(), name="loan_list"),
    path("from-opportunity/", views.LoanFromOpportunityView.as_view(), name="loan_from_opportunity"),
    path("<int:pk>/", views.LoanDetailView.as_view(), name="loan_detail"),
]
