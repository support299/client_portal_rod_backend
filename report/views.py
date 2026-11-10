import csv

from django.db.models import Count, Sum
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.views import IsStaffMember
from loan.models import DrawRequest, Loan
from loan.task_views import user_name
from loan.views import visible_loans


def report_loans(request):
    """The loans a report covers: what the user can see, narrowed by the query params.

    Query params (all optional): start, end (loan created date, YYYY-MM-DD), officer (user id),
    source, status (open/won/lost/abandoned), stage (pipeline stage id).
    Returns a fresh queryset on the matching ids, so later joins can't duplicate rows or sums.
    """
    params = request.query_params
    loans = visible_loans(request.user)
    for param, lookup in (("start", "created_at__date__gte"), ("end", "created_at__date__lte")):
        value = params.get(param)
        if value:
            date = parse_date(value)
            if date is None:
                raise ValidationError({param: "Use YYYY-MM-DD."})
            loans = loans.filter(**{lookup: date})
    if params.get("officer"):
        loans = loans.filter(
            officer_assignments__officer_id=params["officer"], officer_assignments__is_deleted=False,
        )
    if params.get("source"):
        loans = loans.filter(opportunity__source=params["source"])
    if params.get("status"):
        loans = loans.filter(opportunity__status=params["status"])
    if params.get("stage"):
        loans = loans.filter(opportunity__pipeline_stage_id=params["stage"])
    return Loan.objects.filter(pk__in=loans.values("pk"))


def volume(qs):
    return qs.aggregate(total=Sum("opportunity__monetary_value"))["total"] or 0


class LoanReportSummaryView(APIView):
    """GET the KPIs for the loans in our dashboard (not the whole CRM), as counts and $ volume.

    Takes report_loans' query params. Volume is the opportunity's monetary value.
    Returns total, by_stage (pipeline order), by_status (lost/abandoned deals live here),
    by_source, by_officer, and draws (funded and in-progress draw money on these loans).
    """

    permission_classes = [IsStaffMember]

    def get(self, request):
        loans = report_loans(request)

        by_stage = [
            {
                "stage": row["opportunity__stage__name"] or "(no stage)",
                "stage_id": row["opportunity__pipeline_stage_id"],
                "count": row["count"],
                "volume": row["volume"] or 0,
            }
            for row in loans.values("opportunity__pipeline_stage_id", "opportunity__stage__name")
            .annotate(count=Count("id"), volume=Sum("opportunity__monetary_value"))
            .order_by("opportunity__stage__position")
        ]
        by_status = [
            {"status": row["opportunity__status"] or "(none)", "count": row["count"], "volume": row["volume"] or 0}
            for row in loans.values("opportunity__status")
            .annotate(count=Count("id"), volume=Sum("opportunity__monetary_value"))
            .order_by("-count")
        ]
        by_source = [
            {"source": row["opportunity__source"] or "(none)", "count": row["count"], "volume": row["volume"] or 0}
            for row in loans.values("opportunity__source")
            .annotate(count=Count("id"), volume=Sum("opportunity__monetary_value"))
            .order_by("-count")
        ]
        by_officer = [
            {
                "officer_id": row["officer_assignments__officer_id"],
                "officer": (
                    f'{row["officer_assignments__officer__first_name"]} '
                    f'{row["officer_assignments__officer__last_name"]}'
                ).strip() or row["officer_assignments__officer__email"],
                "count": row["count"],
                "volume": row["volume"] or 0,
            }
            for row in loans.filter(officer_assignments__is_deleted=False)
            .values(
                "officer_assignments__officer_id", "officer_assignments__officer__first_name",
                "officer_assignments__officer__last_name", "officer_assignments__officer__email",
            )
            .annotate(count=Count("id", distinct=True), volume=Sum("opportunity__monetary_value"))
            .order_by("-count")
        ]

        draws = DrawRequest.objects.filter(loan__in=loans, is_deleted=False)
        funded = draws.filter(status=DrawRequest.Status.FUNDED).aggregate(
            count=Count("id"), volume=Sum("amount_approved"),
        )
        open_ = draws.filter(status__in=DrawRequest.OPEN_STATUSES).aggregate(count=Count("id"))

        return Response({
            "total": {"count": loans.count(), "volume": volume(loans)},
            "by_stage": by_stage,
            "by_status": by_status,
            "by_source": by_source,
            "by_officer": by_officer,
            "draws": {
                "funded_count": funded["count"],
                "funded_volume": funded["volume"] or 0,
                "open_count": open_["count"],
            },
        })


class LoanReportExportView(APIView):
    """GET the matching loans as a CSV download. Takes report_loans' query params."""

    permission_classes = [IsStaffMember]

    def get(self, request):
        loans = (
            report_loans(request)
            .select_related("borrower", "opportunity__stage")
            .prefetch_related("officer_assignments__officer")
            .order_by("-created_at")
        )
        response = HttpResponse(content_type="text/csv")
        filename = f"loans-report-{timezone.localdate().isoformat()}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        writer = csv.writer(response)
        writer.writerow([
            "ID", "Name", "Borrower", "Loan Officers", "Stage", "Opportunity Status",
            "Lead Source", "Amount", "Expected Close", "Holdback", "Created",
        ])
        for loan in loans:
            opportunity = loan.opportunity
            writer.writerow([
                loan.pk,
                str(loan),
                user_name(loan.borrower),
                "; ".join(user_name(a.officer) for a in loan.officer_assignments.all() if not a.is_deleted),
                opportunity.stage.name if opportunity.stage else "",
                opportunity.status,
                opportunity.source,
                opportunity.monetary_value,
                opportunity.expected_close_date or "",
                loan.holdback_amount if loan.holdback_amount is not None else "",
                loan.created_at.date().isoformat(),
            ])
        return response
