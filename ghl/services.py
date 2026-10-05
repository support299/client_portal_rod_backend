"""GHL data fetchers (opportunities, contacts) built on client.api_request.

Docs:
  https://marketplace.gohighlevel.com/docs/ghl/opportunities/search-opportunity
  https://marketplace.gohighlevel.com/docs/ghl/contacts/search-contacts-advanced
"""

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .client import api_request, get_location_token
from .models import GHLContact, GHLToken

OPPORTUNITIES_MAX_LIMIT = 100
CONTACTS_MAX_PAGE_LIMIT = 500


class GHLAPIError(Exception):
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"GHL API error ({status_code}): {body}")


def _json(resp):
    if not resp.ok:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        raise GHLAPIError(resp.status_code, body)
    return resp.json()


def token_for_location(location_id):
    """Return a Location token, minting one from an agency token if needed."""
    token = GHLToken.objects.filter(user_type="Location", location_id=location_id).first()
    if token:
        return token
    company_token = GHLToken.objects.filter(user_type="Company").first()
    if company_token:
        return get_location_token(company_token, location_id)
    raise GHLToken.DoesNotExist(f"No GHL connection for location {location_id}")


# Opportunities

def search_opportunities(token, **params):
    """Fetch one page of opportunities.

    Supported params (see docs): q, status (open/won/lost/abandoned/all),
    pipelineId, pipelineStageId, contactId, assignedTo, campaignId, date,
    endDate, order, page, limit (max 100), startAfter, startAfterId,
    getTasks, getNotes, getCalendarEvents, country.

    Returns {"opportunities": [...], "meta": {...}, "aggregations": {...}}.
    """
    params = {k: v for k, v in params.items() if v is not None}
    params.setdefault("limit", OPPORTUNITIES_MAX_LIMIT)
    params["locationId"] = token.location_id
    return _json(api_request(token, "GET", "/opportunities/search", params=params))


def iter_opportunities(token, **params):
    """Yield every opportunity matching params, following startAfter/startAfterId cursors."""
    params.pop("page", None)
    while True:
        data = search_opportunities(token, **params)
        opportunities = data.get("opportunities") or []
        yield from opportunities

        meta = data.get("meta") or {}
        if not opportunities or not meta.get("startAfterId") or not meta.get("nextPage"):
            return
        params["startAfter"] = meta.get("startAfter")
        params["startAfterId"] = meta["startAfterId"]


# Contacts

def search_contacts(token, filters=None, sort=None, query=None, page=None,
                    page_limit=100, search_after=None):
    """Fetch one page of contacts using advanced search.

    filters: e.g. [{"field": "tags", "operator": "contains", "value": "loan"}]
    sort:    e.g. [{"field": "dateAdded", "direction": "desc"}]
    Use `page` for simple paging, or `search_after` (the last contact's
    "searchAfter" value) for deep paging.

    Returns {"contacts": [...], "total": int}.
    """
    body = {
        "locationId": token.location_id,
        "pageLimit": min(page_limit, CONTACTS_MAX_PAGE_LIMIT),
    }
    if filters:
        body["filters"] = filters
    if sort:
        body["sort"] = sort
    if query:
        body["query"] = query
    if search_after:
        body["searchAfter"] = search_after
    elif page:
        body["page"] = page
    return _json(api_request(token, "POST", "/contacts/search", json=body))


def iter_contacts(token, filters=None, sort=None, query=None, page_limit=CONTACTS_MAX_PAGE_LIMIT):
    """Yield every contact matching the search, following searchAfter cursors."""
    search_after = None
    page = 1
    while True:
        data = search_contacts(token, filters=filters, sort=sort, query=query,
                               page=page, page_limit=page_limit, search_after=search_after)
        contacts = data.get("contacts") or []
        yield from contacts

        if len(contacts) < page_limit:
            return
        search_after = contacts[-1].get("searchAfter")
        if not search_after:
            page += 1


CONTACT_UPDATE_FIELDS = [
    "location_id", "first_name", "last_name", "contact_name", "email", "phone",
    "company_name", "type", "source", "assigned_to", "tags", "custom_fields",
    "address1", "city", "state", "postal_code", "country", "dnd",
    "date_added", "date_updated", "raw", "synced_at",
]


def _contact_from_ghl(data, location_id):
    return GHLContact(
        ghl_id=data["id"],
        location_id=data.get("locationId") or location_id,
        first_name=data.get("firstName") or "",
        last_name=data.get("lastName") or "",
        contact_name=data.get("contactName") or "",
        email=data.get("email") or "",
        phone=data.get("phone") or "",
        company_name=data.get("companyName") or "",
        type=data.get("type") or "",
        source=data.get("source") or "",
        assigned_to=data.get("assignedTo") or "",
        tags=data.get("tags") or [],
        custom_fields=data.get("customFields") or [],
        address1=data.get("address1") or "",
        city=data.get("city") or "",
        state=data.get("state") or "",
        postal_code=data.get("postalCode") or "",
        country=data.get("country") or "",
        dnd=bool(data.get("dnd")),
        date_added=parse_datetime(data["dateAdded"]) if data.get("dateAdded") else None,
        date_updated=parse_datetime(data["dateUpdated"]) if data.get("dateUpdated") else None,
        raw=data,
        synced_at=timezone.now(),
    )


def _upsert_contacts(contacts):
    GHLContact.objects.bulk_create(
        contacts,
        update_conflicts=True,
        unique_fields=["ghl_id"],
        update_fields=CONTACT_UPDATE_FIELDS,
    )


def sync_contacts(location_id, batch_size=CONTACTS_MAX_PAGE_LIMIT):
    """Fetch every contact in a GHL location and upsert it into GHLContact.

    Returns the number of contacts synced.
    """
    token = token_for_location(location_id)
    batch = []
    total = 0
    for data in iter_contacts(token, page_limit=batch_size):
        batch.append(_contact_from_ghl(data, location_id))
        if len(batch) >= batch_size:
            _upsert_contacts(batch)
            total += len(batch)
            batch = []
    if batch:
        _upsert_contacts(batch)
        total += len(batch)
    return total
