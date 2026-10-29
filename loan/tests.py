from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from ghl.models import GHLContact, GHLOpportunity, GHLUser
from ghl.services import AccountLinkService

from .models import DrawRequest, DrawRequestStatusChange, LoanDocument, LoanNote
from .serializers import LoanSerializer
from .services import LoanService
from .views import visible_loans


@override_settings(LOAN_PIPELINE_ID="p1")
class GHLAssigneeTests(TestCase):
    def setUp(self):
        self.ghl_officer = User.objects.create_user(email="ghl@example.com", role=User.Role.LOAN_OFFICER)
        self.other = User.objects.create_user(email="other@example.com", role=User.Role.PROCESSOR)
        GHLUser.objects.create(user_id="u1", email="ghl@example.com", account=self.ghl_officer)
        contact = GHLContact.objects.create(ghl_id="c1", location_id="loc1", email="jane@example.com")
        self.opportunity = GHLOpportunity.objects.create(
            opportunity_id="opp1", location_id="loc1", contact=contact, pipeline_id="p1", assigned_to="u1",
        )
        self.loan, _ = LoanService.create_from_opportunity(self.opportunity)

    def officers(self):
        return LoanSerializer(visible_loans(self.ghl_officer).get(pk=self.loan.pk)).data["loan_officers"]

    def test_ghl_assignee_is_added_listed_first_and_flagged(self):
        LoanService.set_officers(self.loan, [self.other, self.ghl_officer])
        officers = self.officers()
        self.assertEqual([o["email"] for o in officers], ["ghl@example.com", "other@example.com"])
        self.assertEqual([o["is_ghl_assignee"] for o in officers], [True, False])

    def test_set_officers_keeps_ghl_assignee(self):
        LoanService.set_officers(self.loan, [self.other])
        self.assertEqual({o["email"] for o in self.officers()}, {"ghl@example.com", "other@example.com"})

    def test_reassignment_in_ghl_adds_new_assignee(self):
        GHLUser.objects.create(user_id="u2", email="other@example.com", account=self.other)
        GHLOpportunity.objects.filter(pk=self.opportunity.pk).update(assigned_to="u2")
        self.assertEqual(LoanService.add_ghl_assignees(), 1)
        self.assertEqual(LoanService.add_ghl_assignees(), 0)
        officers = LoanSerializer(visible_loans(self.other).get(pk=self.loan.pk)).data["loan_officers"]
        # The previous assignee stays on; the new one is first.
        self.assertEqual([o["email"] for o in officers], ["other@example.com", "ghl@example.com"])

    def patch_loan(self, user, payload):
        self.client.force_login(user)
        with mock.patch("loan.views.update_opportunity_in_ghl") as push:
            resp = self.client.patch(f"/api/loans/{self.loan.pk}/", payload, content_type="application/json")
        return resp, push

    def test_admin_makes_a_team_member_main_and_it_is_pushed_to_ghl(self):
        GHLUser.objects.create(user_id="u2", email="other@example.com", account=self.other)
        admin = User.objects.create_user(email="admin@example.com", role=User.Role.ADMIN)
        resp, push = self.patch_loan(admin, {"loan_officers": [self.ghl_officer.pk], "main_officer": self.other.pk})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(push.call_args.kwargs["assigned_to"], "u2")
        officers = resp.json()["loan_officers"]
        self.assertEqual([(o["email"], o["is_ghl_assignee"]) for o in officers],
                         [("other@example.com", True), ("ghl@example.com", False)])

    def test_main_officer_needs_a_ghl_user(self):
        admin = User.objects.create_user(email="admin@example.com", role=User.Role.ADMIN)
        resp, push = self.patch_loan(admin, {"main_officer": self.other.pk})
        self.assertEqual(resp.status_code, 400)
        push.assert_not_called()

    def test_sync_creates_logins_for_assignees_without_one(self):
        GHLUser.objects.create(user_id="u4", email="sam@example.com", first_name="Sam")
        GHLUser.objects.create(user_id="u5", email="jane@example.com")  # the borrower's email
        GHLUser.objects.create(user_id="u6", email="idle@example.com")  # assigned to nothing
        GHLOpportunity.objects.filter(pk=self.opportunity.pk).update(assigned_to="u4")
        GHLOpportunity.objects.create(opportunity_id="opp2", location_id="loc1", pipeline_id="p1", assigned_to="u5")
        created = AccountLinkService.create_assignee_accounts()
        self.assertEqual([u.email for u in created], ["sam@example.com"])
        self.assertTrue(self.loan.officer_assignments.filter(officer=created[0], is_deleted=False).exists())

    def test_linking_a_login_adds_them_to_their_ghl_loans(self):
        GHLUser.objects.create(user_id="u3", email="new@example.com")
        GHLOpportunity.objects.filter(pk=self.opportunity.pk).update(assigned_to="u3")
        new = User.objects.create_user(email="new@example.com", role=User.Role.CLOSER)
        self.assertTrue(self.loan.officer_assignments.filter(officer=new, is_deleted=False).exists())


@override_settings(LOAN_PIPELINE_ID="p1")
@mock.patch("ghl.services.CustomFieldService.names", return_value={"cf1": "Property type"})
class LoanPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="admin@example.com", role=User.Role.ADMIN)
        self.borrower_contact = GHLContact.objects.create(
            ghl_id="c1", location_id="loc1", email="jane@example.com", company_name="Jane LLC",
            custom_fields=[{"id": "cf1", "value": "SFH"}],
        )
        opportunity = GHLOpportunity.objects.create(
            opportunity_id="opp1", location_id="loc1", contact=self.borrower_contact, pipeline_id="p1",
        )
        self.loan, _ = LoanService.create_from_opportunity(opportunity)
        self.url = reverse("loan:loan_page", args=[self.loan.pk])
        self.client = APIClient()

    def add_records(self, count):
        start = self.loan.draws.count()
        for i in range(start, start + count):
            draw = DrawRequest.objects.create(loan=self.loan, number=i + 1, amount_requested=100, submitted_by=self.admin)
            DrawRequestStatusChange.objects.create(draw=draw, to_status="submitted", changed_by=self.admin)
            LoanDocument.objects.create(
                loan=self.loan, draw=draw, origin="app", ghl_media_id=f"m{i}", location_id="loc1",
                url="https://example.com/f", name=f"f{i}.pdf", uploaded_by=self.admin, uploaded_at=timezone.now(),
            )
            LoanNote.objects.create(loan=self.loan, body=f"note {i}", author=self.admin)

    def get_page(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_returns_every_section_with_named_contact_fields(self, _names):
        self.add_records(1)
        data = self.get_page()
        self.assertEqual(
            set(data),
            {"loan", "stages", "draws", "draw_summary", "document_requests", "documents", "document_types", "notes"},
        )
        contact = data["loan"]["contact_details"]
        self.assertEqual(contact["company_name"], "Jane LLC")
        self.assertEqual(contact["custom_fields"], [{"id": "cf1", "value": "SFH", "label": "Property type"}])
        self.assertEqual((len(data["draws"]), len(data["documents"]), len(data["notes"])), (1, 1, 1))

    def test_query_count_does_not_grow_with_records(self, _names):
        self.add_records(1)
        self.client.force_authenticate(self.admin)
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url)
        self.add_records(4)
        with CaptureQueriesContext(connection) as many:
            self.client.get(self.url)
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))

    def test_borrowers_are_refused(self, _names):
        self.client.force_authenticate(self.loan.borrower)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_not_on_the_loan_get_404(self, _names):
        other = User.objects.create_user(email="other@example.com", role=User.Role.PROCESSOR)
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
