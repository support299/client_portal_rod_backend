import base64
import json
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.test import TestCase, override_settings

from ghl.models import GHLOpportunity

from .models import GHLWebhookLog
from .services import process_log

PRIVATE_KEY = Ed25519PrivateKey.generate()
PUBLIC_PEM = PRIVATE_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()

EVENT = {
    "type": "OpportunityStageUpdate",
    "webhookId": "wh-1",
    "locationId": "loc1",
    "id": "opp1",
    "contactId": "c1",
}

GHL_OPPORTUNITY = {
    "opportunity": {
        "id": "opp1",
        "name": "123 Main St",
        "locationId": "loc1",
        "contactId": "c1",
        "pipelineId": "p1",
        "pipelineStageId": "s1",
        "status": "open",
        "contact": {"id": "c1", "name": "Jane", "email": "jane@example.com"},
    }
}


def post(client, payload, signature=None):
    body = json.dumps(payload).encode()
    headers = {"HTTP_X_GHL_SIGNATURE": signature} if signature else {}
    return client.post("/crm/webhook/", body, content_type="application/json", **headers)


def sign(payload):
    return base64.b64encode(PRIVATE_KEY.sign(json.dumps(payload).encode())).decode()


@mock.patch("webhook.views.process_ghl_webhook")
class WebhookViewTests(TestCase):
    def test_logs_and_queues_when_no_key_configured(self, task):
        with self.captureOnCommitCallbacks(execute=True):
            resp = post(self.client, EVENT)
        self.assertEqual(resp.status_code, 200)
        log = GHLWebhookLog.objects.get()
        self.assertEqual((log.event_type, log.object_id, log.status), ("OpportunityStageUpdate", "opp1", "received"))
        task.delay.assert_called_once_with(log.pk)

    def test_duplicate_webhook_id_is_skipped(self, task):
        post(self.client, EVENT)
        resp = post(self.client, EVENT)
        self.assertEqual(resp.json(), {"status": "duplicate"})
        self.assertEqual(GHLWebhookLog.objects.count(), 1)

    def test_invalid_json(self, task):
        resp = self.client.post("/crm/webhook/", b"nope", content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(GHLWebhookLog.objects.count(), 0)

    @override_settings(GHL_WEBHOOK_PUBLIC_KEY=PUBLIC_PEM)
    def test_valid_signature(self, task):
        with self.captureOnCommitCallbacks(execute=True):
            resp = post(self.client, EVENT, sign(EVENT))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(GHLWebhookLog.objects.get().signature_valid)
        task.delay.assert_called_once()

    @override_settings(GHL_WEBHOOK_PUBLIC_KEY=PUBLIC_PEM)
    def test_bad_or_missing_signature_is_rejected_but_logged(self, task):
        for signature in (sign({**EVENT, "id": "other"}), None):
            GHLWebhookLog.objects.all().delete()
            resp = post(self.client, EVENT, signature)
            self.assertEqual(resp.status_code, 401)
            log = GHLWebhookLog.objects.get()
            self.assertEqual((log.status, log.signature_valid), ("ignored", False))
        task.delay.assert_not_called()

    def test_broker_down_still_returns_200(self, task):
        task.delay.side_effect = ConnectionError("redis down")
        with self.captureOnCommitCallbacks(execute=True):
            resp = post(self.client, EVENT)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(GHLWebhookLog.objects.get().status, "received")


class ProcessLogTests(TestCase):
    def make_log(self, **kw):
        data = {**EVENT, **kw}
        return GHLWebhookLog.objects.create(
            event_type=data["type"], location_id=data["locationId"], object_id=data["id"], payload=data,
        )

    @mock.patch("webhook.services.api_request")
    @mock.patch("webhook.services.GHLService.token_for_location")
    def test_upserts_fetched_opportunity(self, token, api):
        api.return_value = mock.Mock(ok=True, json=lambda: GHL_OPPORTUNITY)
        log = self.make_log()
        with mock.patch("ghl.services.PipelineService.ensure_stages", return_value=set()):
            process_log(log)
        log.refresh_from_db()
        self.assertEqual(log.status, "processed")
        opp = GHLOpportunity.objects.get(opportunity_id="opp1")
        self.assertEqual((opp.name, opp.pipeline_stage_id, opp.contact_id), ("123 Main St", "s1", "c1"))

    def test_delete_event_removes_local_row(self):
        with mock.patch("ghl.services.PipelineService.ensure_stages", return_value=set()):
            GHLOpportunity.objects.create(opportunity_id="opp1", location_id="loc1")
        log = self.make_log(type="OpportunityDelete")
        process_log(log)
        self.assertFalse(GHLOpportunity.objects.exists())
        self.assertEqual(log.status, "processed")

    def test_non_opportunity_event_is_ignored(self):
        log = self.make_log(type="ContactUpdate")
        process_log(log)
        self.assertEqual(log.status, "ignored")
