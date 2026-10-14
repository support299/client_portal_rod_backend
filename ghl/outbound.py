"""Write-back to GHL: push local changes to an opportunity, log the call, mirror it locally."""

import time

import requests

from .client import api_request
from .models import GHLOutboundLog, GHLPipelineStage
from .services import GHLAPIError, GHLService

# Local field -> GHL payload key, for the fields that can be pushed back to GHL.
OPPORTUNITY_PUSH_FIELDS = {
    "name": "name",
    "monetary_value": "monetaryValue",
    "expected_close_date": "forecastExpectedCloseDate",
    "status": "status",
    "pipeline_stage_id": "pipelineStageId",
    "assigned_to": "assignedTo",
    "pipeline_id": "pipelineId",
    "lost_reason_id": "lostReasonId",
    # List of {"id": <custom field id>, "field_value": ...}
    "custom_fields": "customFields",
}


def _payload_value(field, value):
    if field == "monetary_value":
        return float(value)
    return value.isoformat() if hasattr(value, "isoformat") else value


def update_opportunity_in_ghl(opportunity, triggered_by=None, **fields):
    """Update an opportunity in GHL, then mirror the change locally.

    GHL is written first, so a failure raises GHLAPIError and leaves our copy untouched.
    `fields` are GHLOpportunity attribute names (see OPPORTUNITY_PUSH_FIELDS). Every call
    is recorded in GHLOutboundLog, including failures.
    """
    payload = {OPPORTUNITY_PUSH_FIELDS[f]: _payload_value(f, v) for f, v in fields.items()}
    if not payload:
        return opportunity

    endpoint = f"/opportunities/{opportunity.opportunity_id}"
    log = GHLOutboundLog(
        action="opportunity.update",
        object_id=opportunity.opportunity_id,
        location_id=opportunity.location_id,
        triggered_by=triggered_by,
        method="PUT",
        endpoint=endpoint,
        request_body=payload,
    )
    started = time.monotonic()
    try:
        token = GHLService.token_for_location(opportunity.location_id)
        resp = api_request(token, "PUT", endpoint, json=payload)
        log.http_status = resp.status_code
        try:
            log.response_body = resp.json()
        except ValueError:
            log.response_body = {"text": resp.text}
        data = GHLService.json(resp)
        log.success = True
    except requests.RequestException as exc:
        log.error = str(exc)
        raise GHLAPIError(502, str(exc)) from exc
    except Exception as exc:
        log.error = str(exc)
        raise
    finally:
        log.duration_ms = int((time.monotonic() - started) * 1000)
        log.save()

    updated = (data.get("opportunity") if isinstance(data, dict) else None) or {}
    local = {f: v for f, v in fields.items() if f != "custom_fields"}
    for field, value in local.items():
        setattr(opportunity, field, value)
    update_fields = list(local)
    # Pushed custom fields use a different shape than the stored ones, so take GHL's copy.
    if "custom_fields" in fields and updated.get("customFields") is not None:
        opportunity.custom_fields = updated["customFields"]
        update_fields.append("custom_fields")
    if "pipeline_stage_id" in fields:
        opportunity.stage = GHLPipelineStage.objects.filter(stage_id=fields["pipeline_stage_id"]).first()
        update_fields.append("stage")
    opportunity.save(update_fields=update_fields)
    return opportunity
