from django.db import transaction
from django.db.models import Max

from chats.models import Message
from chats.services import ChatService
from notifications.models import Notification
from notifications.services import NotificationService, loan_team_ids

from .models import DrawRequest, DrawRequestStatusChange, Loan

Status = DrawRequest.Status


class DrawError(Exception):
    pass


class DrawService:
    """Draw requests against a loan's holdback. Every status change goes through change_status,
    which logs it in DrawRequestStatusChange and posts it in the loan chat.
    """

    # Where staff may move a draw from each status. approved -> inspected undoes an approval;
    # rejected -> submitted reopens a draw. Funded and cancelled draws are final.
    STAFF_TRANSITIONS = {
        Status.SUBMITTED: [Status.NEEDS_INFO, Status.INSPECTION_ORDERED, Status.INSPECTED, Status.APPROVED,
                           Status.REJECTED, Status.CANCELLED],
        Status.NEEDS_INFO: [Status.SUBMITTED, Status.REJECTED, Status.CANCELLED],
        Status.INSPECTION_ORDERED: [Status.INSPECTED, Status.NEEDS_INFO, Status.REJECTED, Status.CANCELLED],
        Status.INSPECTED: [Status.APPROVED, Status.INSPECTION_ORDERED, Status.NEEDS_INFO, Status.REJECTED],
        Status.APPROVED: [Status.FUNDED, Status.INSPECTED],
        Status.REJECTED: [Status.SUBMITTED],
        Status.FUNDED: [],
        Status.CANCELLED: [],
    }
    # Borrowers may only resubmit after needs_info, or withdraw before the inspection is ordered.
    BORROWER_TRANSITIONS = {
        Status.SUBMITTED: [Status.CANCELLED],
        Status.NEEDS_INFO: [Status.SUBMITTED, Status.CANCELLED],
    }
    # Statuses in which the borrower may still edit the amount and comment.
    BORROWER_EDITABLE = [Status.SUBMITTED, Status.NEEDS_INFO]

    @staticmethod
    def allowed_transitions(draw, user):
        transitions = DrawService.BORROWER_TRANSITIONS if user.role == user.Role.BORROWER \
            else DrawService.STAFF_TRANSITIONS
        return transitions.get(draw.status, [])

    @staticmethod
    def check_available(loan, amount, exclude=None):
        """Raise DrawError if `amount` is more than the loan has left; exclude a draw being changed.

        Call it with the loan row locked (select_for_update) so two draws can't both take the
        same money.
        """
        if loan.holdback_amount is None:
            raise DrawError("This loan has no holdback set, so draws can't be requested yet.")
        if amount <= 0:
            raise DrawError("The amount must be more than zero.")
        summary = loan.draw_summary(exclude=exclude)
        if amount > summary["available"]:
            raise DrawError(f"Only ${summary['available']:,.2f} is still available on this loan.")

    @staticmethod
    def status_message(draw, to_status, note=""):
        amount = draw.amount_approved if to_status in (Status.APPROVED, Status.FUNDED) else draw.amount_requested
        lines = [f"Draw #{draw.number} (${amount:,.2f}): {Status(to_status).label}"]
        if to_status == Status.FUNDED and draw.funding_reference:
            lines.append(f"Reference: {draw.funding_reference}")
        if note:
            lines.append(note)
        return "\n".join(lines)

    @staticmethod
    def post_update(draw, to_status, note=""):
        """Post the draw's status in the loan chat; the draw's message moves to it."""
        message = ChatService.send_message(
            ChatService.ensure_loan_chat(draw.loan), None,
            DrawService.status_message(draw, to_status, note), kind=Message.Kind.SYSTEM,
        )
        draw.message = message
        draw.save(update_fields=["message", "updated_at"])

    @staticmethod
    @transaction.atomic
    def create(loan, user, amount_requested, borrower_comment=""):
        """Submit a draw (by the borrower, or staff for them); numbered after the loan's last draw."""
        loan = Loan.objects.select_for_update().get(pk=loan.pk)
        DrawService.check_available(loan, amount_requested)
        last = loan.draws.aggregate(last=Max("number"))["last"] or 0
        draw = DrawRequest.objects.create(
            loan=loan,
            number=last + 1,
            amount_requested=amount_requested,
            borrower_comment=borrower_comment,
            submitted_by=user,
        )
        DrawRequestStatusChange.objects.create(draw=draw, to_status=Status.SUBMITTED, changed_by=user)
        DrawService.post_update(draw, Status.SUBMITTED, borrower_comment)
        NotificationService.notify(
            loan_team_ids(loan), Notification.Kind.DRAW_SUBMITTED,
            f"Draw #{draw.number} submitted", f"${amount_requested:,.2f} requested on {loan}",
            loan=loan, actor=user, data={"draw": draw.pk},
        )
        return draw

    @staticmethod
    @transaction.atomic
    def update(draw, **fields):
        """Edit a draw's details (not its status). A new amount_requested must still fit the holdback."""
        loan = Loan.objects.select_for_update().get(pk=draw.loan_id)
        if "amount_requested" in fields and fields["amount_requested"] != draw.amount_requested:
            DrawService.check_available(loan, fields["amount_requested"], exclude=draw)
        if fields.get("amount_approved") is not None and fields["amount_approved"] != draw.amount_approved:
            DrawService.check_available(loan, fields["amount_approved"], exclude=draw)
        for field, value in fields.items():
            setattr(draw, field, value)
        draw.save(update_fields=[*fields, "updated_at"])
        return draw

    @staticmethod
    @transaction.atomic
    def change_status(draw, to_status, user, note="", amount_approved=None, funding_reference=None):
        """Move a draw to `to_status` if the user may, log it, and post it in the loan chat.

        Approving needs an approved amount (sent here, or already on the draw) that fits the
        holdback. A staff note is saved on the draw for needs_info and rejected, which the
        borrower sees.
        """
        loan = Loan.objects.select_for_update().get(pk=draw.loan_id)
        draw = DrawRequest.objects.select_for_update().get(pk=draw.pk)
        draw.loan = loan
        if to_status not in DrawService.allowed_transitions(draw, user):
            raise DrawError(f"A {draw.get_status_display().lower()} draw can't be moved to "
                            f"{Status(to_status).label.lower()}.")
        from_status = draw.status
        fields = ["status"]
        if amount_approved is not None:
            draw.amount_approved = amount_approved
            fields.append("amount_approved")
        if to_status == Status.APPROVED:
            if draw.amount_approved is None:
                raise DrawError("Enter the approved amount to approve the draw.")
            DrawService.check_available(loan, draw.amount_approved, exclude=draw)
        elif from_status not in DrawRequest.OPEN_STATUSES and to_status in DrawRequest.OPEN_STATUSES:
            # Reopening a rejected draw takes its money out of the holdback again.
            DrawService.check_available(loan, draw.amount_requested, exclude=draw)
        if funding_reference is not None:
            draw.funding_reference = funding_reference
            fields.append("funding_reference")
        if note and to_status in (Status.NEEDS_INFO, Status.REJECTED):
            draw.staff_note = note
            fields.append("staff_note")
        draw.status = to_status
        draw.save(update_fields=[*fields, "updated_at"])
        DrawRequestStatusChange.objects.create(
            draw=draw, from_status=from_status, to_status=to_status,
            amount_approved=draw.amount_approved, changed_by=user, note=note,
        )
        DrawService.post_update(draw, to_status, note)
        NotificationService.notify(
            loan_team_ids(loan), Notification.Kind.DRAW_STATUS,
            f"Draw #{draw.number}: {Status(to_status).label}",
            DrawService.status_message(draw, to_status, note), loan=loan, actor=user,
            data={"draw": draw.pk, "status": to_status},
        )
        return draw

    @staticmethod
    def delete(draw):
        """Hide a draw (admins); its files and history stay. It no longer counts against the holdback."""
        draw.is_deleted = True
        draw.save(update_fields=["is_deleted", "updated_at"])
