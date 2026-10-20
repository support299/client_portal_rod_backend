import logging
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .client import api_request, get_location_token
from .models import GHLContact, GHLOpportunity, GHLPipelineStage, GHLToken, GHLUser

logger = logging.getLogger(__name__)


class GHLAPIError(Exception):
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"GHL API error ({status_code}): {body}")


class GHLService:
    """Shared helpers for the GHL services."""

    @staticmethod
    def json(resp):
        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise GHLAPIError(resp.status_code, body)
        return resp.json()

    @staticmethod
    def token_for_location(location_id):
        """Return a Location token, minting one from an agency token if needed."""
        token = GHLToken.objects.filter(user_type="Location", location_id=location_id).first()
        if token:
            return token
        company_token = GHLToken.objects.filter(user_type="Company").first()
        if company_token:
            return get_location_token(company_token, location_id)
        raise GHLToken.DoesNotExist(f"No GHL connection for location {location_id}")

    @staticmethod
    def parse_dt(value):
        return parse_datetime(value) if value else None

    @staticmethod
    def parse_d(value):
        return parse_date(value) if value else None


class OpportunityService:
    MAX_LIMIT = 100

    UPDATE_FIELDS = [
        "location_id", "contact", "name", "monetary_value", "pipeline_id",
        "pipeline_stage_id", "stage", "status", "source", "assigned_to", "lost_reason_id",
        "followers", "expected_close_date", "original_close_date",
        "forecast_slippage_count", "forecast_days_slipped", "forecast_probability",
        "effective_probability", "custom_fields", "attributions",
        "last_status_change_at", "last_stage_change_at", "date_added",
        "date_updated", "raw", "synced_at",
    ]

    @staticmethod
    def search(token, **params):
        """Fetch one page of opportunities.

        Supported params (see docs): q, status (open/won/lost/abandoned/all),
        pipelineId, pipelineStageId, contactId, assignedTo, campaignId, date,
        endDate, order, page, limit (max 100), startAfter, startAfterId,
        getTasks, getNotes, getCalendarEvents, country.

        Returns {"opportunities": [...], "meta": {...}, "aggregations": {...}}.
        """
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("limit", OpportunityService.MAX_LIMIT)
        params["locationId"] = token.location_id
        return GHLService.json(api_request(token, "GET", "/opportunities/search", params=params))

    @staticmethod
    def iter_all(token, **params):
        """Yield every opportunity matching params, following startAfter/startAfterId cursors."""
        params.pop("page", None)
        while True:
            data = OpportunityService.search(token, **params)
            opportunities = data.get("opportunities") or []
            yield from opportunities

            meta = data.get("meta") or {}
            if not opportunities or not meta.get("startAfterId") or not meta.get("nextPage"):
                return
            params["startAfter"] = meta.get("startAfter")
            params["startAfterId"] = meta["startAfterId"]

    @staticmethod
    def from_ghl(data, location_id):
        return GHLOpportunity(
            opportunity_id=data["id"],
            location_id=data.get("locationId") or location_id,
            contact_id=data.get("contactId") or None,
            name=data.get("name") or "",
            monetary_value=data.get("monetaryValue") or 0,
            pipeline_id=data.get("pipelineId") or "",
            pipeline_stage_id=data.get("pipelineStageId") or "",
            stage_id=data.get("pipelineStageId") or None,
            status=data.get("status") or "",
            source=data.get("source") or "",
            assigned_to=data.get("assignedTo") or "",
            lost_reason_id=data.get("lostReasonId") or "",
            followers=data.get("followers") or [],
            expected_close_date=GHLService.parse_d(data.get("forecastExpectedCloseDate")),
            original_close_date=GHLService.parse_d(data.get("forecastOriginalCloseDate")),
            forecast_slippage_count=data.get("forecastSlippageCount") or 0,
            forecast_days_slipped=data.get("forecastDaysSlipped") or 0,
            forecast_probability=data.get("forecastProbability"),
            effective_probability=data.get("effectiveProbability"),
            custom_fields=data.get("customFields") or [],
            attributions=data.get("attributions") or [],
            last_status_change_at=GHLService.parse_dt(data.get("lastStatusChangeAt")),
            last_stage_change_at=GHLService.parse_dt(data.get("lastStageChangeAt")),
            date_added=GHLService.parse_dt(data.get("createdAt")),
            date_updated=GHLService.parse_dt(data.get("updatedAt")),
            raw=data,
            synced_at=timezone.now(),
        )

    @staticmethod
    def ensure_contacts(opportunities, location_id):
        """Create placeholder GHLContact rows for opportunity contacts not yet synced.

        Existing contacts are left untouched (ContactService.sync owns their full data).
        """
        contacts = {}
        for data in opportunities:
            contact = data.get("contact") or {}
            contact_id = data.get("contactId") or contact.get("id")
            if not contact_id:
                continue
            contacts[contact_id] = GHLContact(
                ghl_id=contact_id,
                location_id=data.get("locationId") or location_id,
                contact_name=contact.get("name") or "",
                email=contact.get("email") or "",
                phone=contact.get("phone") or "",
                company_name=contact.get("companyName") or "",
                tags=contact.get("tags") or [],
                raw=contact,
            )
        GHLContact.objects.bulk_create(contacts.values(), ignore_conflicts=True)

    @staticmethod
    def upsert(batch, location_id):
        OpportunityService.ensure_contacts(batch, location_id)
        known_stages = PipelineService.ensure_stages(location_id, batch)
        opportunities = [OpportunityService.from_ghl(data, location_id) for data in batch]
        for opportunity in opportunities:
            # A stage GHL no longer returns can't satisfy the FK; the raw id is still kept.
            if opportunity.stage_id not in known_stages:
                opportunity.stage_id = None
        GHLOpportunity.objects.bulk_create(
            opportunities,
            update_conflicts=True,
            unique_fields=["opportunity_id"],
            update_fields=OpportunityService.UPDATE_FIELDS,
        )

    @staticmethod
    def sync(location_id, batch_size=MAX_LIMIT, **params):
        """Fetch every opportunity in a GHL location and upsert it into GHLOpportunity.

        Pipelines/stages referenced by the opportunities that aren't in
        GHLPipelineStage yet are fetched from GHL before each batch is saved.

        Extra params are passed to the search (e.g. pipeline_id=..., status="open").
        Returns the number of opportunities synced.
        """
        token = GHLService.token_for_location(location_id)
        if "pipeline_id" in params:
            params["pipelineId"] = params.pop("pipeline_id")
        batch = []
        total = 0
        for data in OpportunityService.iter_all(token, limit=batch_size, **params):
            batch.append(data)
            if len(batch) >= batch_size:
                OpportunityService.upsert(batch, location_id)
                total += len(batch)
                batch = []
        if batch:
            OpportunityService.upsert(batch, location_id)
            total += len(batch)
        # ensure_contacts may have added placeholder contacts with emails.
        AccountLinkService.link_contacts()
        return total


class PipelineService:
    STAGE_UPDATE_FIELDS = [
        "pipeline_id", "pipeline_name", "location_id", "name", "position",
        "win_probability", "show_in_funnel", "show_in_pie_chart", "raw", "synced_at",
    ]

    @staticmethod
    def get(token, pipeline_id):
        """Fetch one pipeline with its stages.

        Returns {"pipeline": {"id", "name", "stages": [...], "locationId", ...}}.
        """
        return GHLService.json(api_request(token, "GET", f"/opportunities/pipelines/{pipeline_id}"))

    @staticmethod
    def sync_stages(location_id, pipeline_id):
        """Fetch a pipeline from GHL and upsert its stages into GHLPipelineStage.

        Stages that no longer exist in the pipeline are deleted.
        Returns the number of stages synced.
        """
        token = GHLService.token_for_location(location_id)
        pipeline = PipelineService.get(token, pipeline_id)["pipeline"]
        now = timezone.now()
        stages = [
            GHLPipelineStage(
                stage_id=stage["id"],
                pipeline_id=pipeline["id"],
                pipeline_name=pipeline.get("name") or "",
                location_id=pipeline.get("locationId") or location_id,
                name=stage.get("name") or "",
                position=stage.get("position") or 0,
                win_probability=stage.get("stageWinProbability"),
                show_in_funnel=stage.get("showInFunnel", True),
                show_in_pie_chart=stage.get("showInPieChart", True),
                raw=stage,
                synced_at=now,
            )
            for stage in pipeline.get("stages") or []
        ]
        GHLPipelineStage.objects.bulk_create(
            stages,
            update_conflicts=True,
            unique_fields=["stage_id"],
            update_fields=PipelineService.STAGE_UPDATE_FIELDS,
        )
        GHLPipelineStage.objects.filter(pipeline_id=pipeline["id"]).exclude(
            stage_id__in=[s.stage_id for s in stages]
        ).delete()
        return len(stages)

    @staticmethod
    def ensure_stages(location_id, opportunities):
        """Make sure every stage the opportunities point to is in GHLPipelineStage.

        Re-syncs any pipeline that has a stage we don't have yet (a new pipeline,
        or a stage added in GHL). Returns the set of referenced stage ids that exist.
        """
        stage_to_pipeline = {
            data["pipelineStageId"]: data.get("pipelineId")
            for data in opportunities
            if data.get("pipelineStageId")
        }
        existing = set(
            GHLPipelineStage.objects.filter(stage_id__in=stage_to_pipeline)
            .values_list("stage_id", flat=True)
        )
        missing_pipelines = {
            pipeline_id
            for stage_id, pipeline_id in stage_to_pipeline.items()
            if stage_id not in existing and pipeline_id
        }
        if not missing_pipelines:
            return existing
        for pipeline_id in missing_pipelines:
            PipelineService.sync_stages(location_id, pipeline_id)
        return set(
            GHLPipelineStage.objects.filter(stage_id__in=stage_to_pipeline)
            .values_list("stage_id", flat=True)
        )


class ContactService:
    MAX_PAGE_LIMIT = 500

    UPDATE_FIELDS = [
        "location_id", "first_name", "last_name", "contact_name", "email", "phone",
        "company_name", "type", "source", "assigned_to", "tags", "custom_fields",
        "address1", "city", "state", "postal_code", "country", "dnd",
        "date_added", "date_updated", "raw", "synced_at",
    ]

    @staticmethod
    def search(token, filters=None, sort=None, query=None, page=None,
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
            "pageLimit": min(page_limit, ContactService.MAX_PAGE_LIMIT),
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
        return GHLService.json(api_request(token, "POST", "/contacts/search", json=body))

    @staticmethod
    def iter_all(token, filters=None, sort=None, query=None, page_limit=MAX_PAGE_LIMIT):
        """Yield every contact matching the search, following searchAfter cursors."""
        search_after = None
        page = 1
        while True:
            data = ContactService.search(token, filters=filters, sort=sort, query=query,
                                         page=page, page_limit=page_limit, search_after=search_after)
            contacts = data.get("contacts") or []
            yield from contacts

            if len(contacts) < page_limit:
                return
            search_after = contacts[-1].get("searchAfter")
            if not search_after:
                page += 1

    @staticmethod
    def from_ghl(data, location_id):
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
            date_added=GHLService.parse_dt(data.get("dateAdded")),
            date_updated=GHLService.parse_dt(data.get("dateUpdated")),
            raw=data,
            synced_at=timezone.now(),
        )

    @staticmethod
    def upsert(contacts):
        GHLContact.objects.bulk_create(
            contacts,
            update_conflicts=True,
            unique_fields=["ghl_id"],
            update_fields=ContactService.UPDATE_FIELDS,
        )

    @staticmethod
    def sync(location_id, batch_size=MAX_PAGE_LIMIT):
        """Fetch every contact in a GHL location and upsert it into GHLContact.

        Returns the number of contacts synced.
        """
        token = GHLService.token_for_location(location_id)
        batch = []
        total = 0
        for data in ContactService.iter_all(token, page_limit=batch_size):
            batch.append(ContactService.from_ghl(data, location_id))
            if len(batch) >= batch_size:
                ContactService.upsert(batch)
                total += len(batch)
                batch = []
        if batch:
            ContactService.upsert(batch)
            total += len(batch)
        AccountLinkService.link_contacts()
        return total


class UserService:
    PAGE_LIMIT = 100

    UPDATE_FIELDS = [
        "company_id", "first_name", "last_name", "name", "email", "phone",
        "extension", "role_type", "role", "location_ids", "permissions",
        "deleted", "date_added", "date_updated", "raw", "synced_at",
    ]

    @staticmethod
    def search(token, skip=0, limit=PAGE_LIMIT, **params):
        """Fetch one page of users in the token's location.

        Returns {"users": [...], "count": int}.
        """
        if not token.company_id:
            raise ValueError(f"GHL token {token} has no company_id; reconnect the location")
        params = {k: v for k, v in params.items() if v is not None}
        params.update({
            "companyId": token.company_id,
            "locationId": token.location_id,
            "skip": skip,
            "limit": limit,
        })
        return GHLService.json(api_request(token, "GET", "/users/search", params=params))

    @staticmethod
    def iter_all(token, limit=PAGE_LIMIT, **params):
        """Yield every user in the token's location, following skip/limit paging."""
        skip = 0
        while True:
            users = UserService.search(token, skip=skip, limit=limit, **params).get("users") or []
            yield from users
            if len(users) < limit:
                return
            skip += limit

    @staticmethod
    def from_ghl(data, company_id):
        roles = data.get("roles") or {}
        return GHLUser(
            user_id=data["id"],
            company_id=company_id or "",
            first_name=data.get("firstName") or "",
            last_name=data.get("lastName") or "",
            name=data.get("name") or "",
            email=data.get("email") or "",
            phone=data.get("phone") or "",
            extension=data.get("extension") or "",
            role_type=roles.get("type") or "",
            role=roles.get("role") or "",
            location_ids=roles.get("locationIds") or [],
            permissions=data.get("permissions") or {},
            deleted=bool(data.get("deleted")),
            date_added=GHLService.parse_dt(data.get("dateAdded")),
            date_updated=GHLService.parse_dt(data.get("dateUpdated")),
            raw=data,
            synced_at=timezone.now(),
        )

    @staticmethod
    def sync(location_id):
        """Fetch every user in a GHL location and upsert it into GHLUser.

        Returns the number of users synced.
        """
        token = GHLService.token_for_location(location_id)
        users = {
            data["id"]: UserService.from_ghl(data, token.company_id)
            for data in UserService.iter_all(token)
        }
        GHLUser.objects.bulk_create(
            users.values(),
            update_conflicts=True,
            unique_fields=["user_id"],
            update_fields=UserService.UPDATE_FIELDS,
        )
        AccountLinkService.link_ghl_users()
        return len(users)

    @staticmethod
    def ensure(location_id, user_ids):
        """Fetch and save the GHL users in `user_ids` we don't have yet (e.g. a new opportunity assignee).

        Users GHL no longer has (404) are skipped. Returns the number saved.
        """
        user_ids = set(filter(None, user_ids))
        missing = user_ids - set(GHLUser.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True))
        if not missing:
            return 0
        token = GHLService.token_for_location(location_id)
        users = []
        for user_id in missing:
            try:
                data = GHLService.json(api_request(token, "GET", f"/users/{user_id}"))
            except GHLAPIError as exc:
                if exc.status_code != 404:
                    raise
                continue
            users.append(UserService.from_ghl(data.get("user", data), token.company_id))
        GHLUser.objects.bulk_create(
            users,
            update_conflicts=True,
            unique_fields=["user_id"],
            update_fields=UserService.UPDATE_FIELDS,
        )
        AccountLinkService.link_ghl_users()
        return len(users)


class AccountLinkService:
    """Link GHL rows to dashboard logins by email (case-insensitive).

    Staff logins link to a GHLUser, borrower logins to their GHLContact rows
    (one per location). Sync upserts never touch `account`, so links survive re-syncs.
    """

    @staticmethod
    def link_ghl_users():
        """Link every unlinked GHLUser to the staff login with the same email. Returns the count linked."""
        User = get_user_model()
        taken = GHLUser.objects.filter(account__isnull=False).values("account_id")
        staff = {
            email.lower(): pk
            for pk, email in User.objects.exclude(role=User.Role.BORROWER)
            .exclude(pk__in=taken).values_list("pk", "email")
        }
        linked = 0
        for pk, email in (
            GHLUser.objects.filter(account__isnull=True, deleted=False)
            .exclude(email="").values_list("pk", "email")
        ):
            account_id = staff.pop(email.lower(), None)
            if account_id:
                GHLUser.objects.filter(pk=pk).update(account_id=account_id)
                linked += 1
        return linked

    @staticmethod
    def create_assignee_accounts():
        """Create a staff login for every GHL user assigned to a loan-pipeline opportunity who has none.

        The login is a Loan Officer with no password: it can't sign in until an admin sets one,
        but it puts them on their loans' teams (the post_save signal links it and adds them).
        An email that already belongs to a borrower is skipped. Returns the logins created.
        """
        User = get_user_model()
        AccountLinkService.link_ghl_users()
        assignees = (
            GHLOpportunity.objects.filter(pipeline_id=settings.LOAN_PIPELINE_ID)
            .exclude(assigned_to="").values("assigned_to")
        )
        created = []
        for ghl_user in (
            GHLUser.objects.filter(user_id__in=assignees, account__isnull=True, deleted=False).exclude(email="")
        ):
            if User.objects.filter(email__iexact=ghl_user.email).exists():
                logger.warning("No login created for GHL user %s: %s is already in use", ghl_user.user_id, ghl_user.email)
                continue
            created.append(User.objects.create_user(
                email=ghl_user.email,
                first_name=ghl_user.first_name,
                last_name=ghl_user.last_name,
                phone=ghl_user.phone[:20],
                role=User.Role.LOAN_OFFICER,
            ))
        return created

    @staticmethod
    def link_contacts():
        """Link every unlinked GHLContact to the borrower login with the same email. Returns the count linked."""
        User = get_user_model()
        borrower = User.objects.filter(
            role=User.Role.BORROWER, email__iexact=OuterRef("email"),
        ).values("pk")[:1]
        return (
            GHLContact.objects.filter(account__isnull=True).exclude(email="")
            .filter(Exists(borrower))
            .update(account=Subquery(borrower))
        )

    @staticmethod
    def link_account(user):
        """Re-link one login after it is created or its email/role changes."""
        User = get_user_model()
        GHLUser.objects.filter(account=user).update(account=None)
        GHLContact.objects.filter(account=user).update(account=None)
        if user.role == User.Role.BORROWER:
            GHLContact.objects.filter(account__isnull=True, email__iexact=user.email).update(account=user)
        else:
            ghl_user = (
                GHLUser.objects.filter(account__isnull=True, deleted=False, email__iexact=user.email)
                .order_by("-date_updated").first()
            )
            if ghl_user:
                GHLUser.objects.filter(pk=ghl_user.pk).update(account=user)


class MediaService:
    """The location's GHL media library, where loan documents are stored.

    Every location keeps them in one top-level folder, found by name before it is ever created,
    so a re-onboarded location or a wiped database reuses the folder already in GHL. Its id is
    cached on the location's GHLToken (media_folder_id); GHL stays the source of truth.
    """

    FOLDER_NAME = "Loan Dashboard Documents"
    # Uploads are named "<opportunity id>__<file name>": opportunity ids come from GHL and never
    # change, so each file can be matched back to its loan even after a database wipe.
    NAME_SEPARATOR = "__"
    PAGE_LIMIT = 100

    @staticmethod
    def media_name(opportunity_id, filename):
        return f"{opportunity_id}{MediaService.NAME_SEPARATOR}{os.path.basename(filename)}"

    @staticmethod
    def parse_media_name(name):
        """(opportunity id, original file name) for a name made by media_name, else (None, name)."""
        prefix, sep, rest = (name or "").partition(MediaService.NAME_SEPARATOR)
        # GHL ids are 20 alphanumeric characters; a shorter prefix is part of someone's file name.
        if sep and rest and prefix.isalnum() and len(prefix) >= 15:
            return prefix, rest
        return None, name

    @staticmethod
    def list_files(token, **params):
        """One page of the location's media library (files and/or folders)."""
        params = {"altId": token.location_id, "altType": "location", "limit": MediaService.PAGE_LIMIT, **params}
        return GHLService.json(api_request(token, "GET", "/medias/files", params=params)).get("files", [])

    @staticmethod
    def find_folder(token):
        """Our folder's id at the top of the media library, or None; the oldest if there are several."""
        folders = [
            f for f in MediaService.list_files(
                token, type="folder", query=MediaService.FOLDER_NAME, sortBy="createdAt", sortOrder="asc",
            )
            # The search is a partial match, so keep exact top-level matches only.
            if f.get("name") == MediaService.FOLDER_NAME and not f.get("parentId") and not f.get("deleted")
        ]
        if len(folders) > 1:
            logger.warning("Location %s has %d %r folders; using the oldest (%s)",
                           token.location_id, len(folders), MediaService.FOLDER_NAME, folders[0]["_id"])
        return folders[0]["_id"] if folders else None

    @staticmethod
    def create_folder(token):
        data = GHLService.json(api_request(token, "POST", "/medias/folder", json={
            "altId": token.location_id,
            "altType": "location",
            "name": MediaService.FOLDER_NAME,
        }))
        folder_id = data.get("_id") or data.get("id")
        if not folder_id:
            raise GHLAPIError(200, {"error": "GHL created a folder but returned no id", "response": data})
        logger.info("Created media folder %s for location %s", folder_id, token.location_id)
        return folder_id

    @staticmethod
    def ensure_folder(token, refresh=False):
        """Return the location's folder id: the cached one, else found in GHL, else created there.

        refresh=True drops the cached id first (e.g. the folder was deleted in GHL).
        """
        if token.media_folder_id and not refresh:
            return token.media_folder_id
        stale = token.media_folder_id
        with transaction.atomic():
            # Locked so two first uploads don't both create a folder.
            locked = GHLToken.objects.select_for_update().get(pk=token.pk)
            # Skip if another request already looked it up again meanwhile.
            if not locked.media_folder_id or (refresh and locked.media_folder_id == stale):
                locked.media_folder_id = MediaService.find_folder(locked) or MediaService.create_folder(locked)
                locked.save(update_fields=["media_folder_id", "updated_at"])
        token.media_folder_id = locked.media_folder_id
        return token.media_folder_id

    @staticmethod
    def is_invalid_file_type(exc):
        """True when GHL refused an upload because of the file's type (not a folder problem)."""
        return isinstance(exc.body, dict) and exc.body.get("code") == "INVALID_FILE_TYPE"

    @staticmethod
    def upload(token, file, filename, content_type, opportunity_id):
        """Upload a file object into the location's folder as media_name(opportunity_id, filename).

        Returns GHL's response ({fileId, url}). If GHL rejects the folder (deleted there), the
        folder is found or created again and the upload retried once.
        """
        name = MediaService.media_name(opportunity_id, filename)
        for attempt in range(2):
            folder_id = MediaService.ensure_folder(token, refresh=attempt > 0)
            try:
                return GHLService.json(api_request(
                    token, "POST", "/medias/upload-file",
                    data={"name": name, "hosted": "false", "parentId": folder_id},
                    files={"file": (name, file, content_type or "application/octet-stream")},
                    timeout=120,
                ))
            except GHLAPIError as exc:
                if attempt or exc.status_code not in (400, 404) or MediaService.is_invalid_file_type(exc):
                    raise
                logger.warning("Upload to media folder %s failed (%s); looking the folder up again",
                               folder_id, exc.status_code)
                file.seek(0)
