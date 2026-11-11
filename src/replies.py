"""Deterministic customer-facing reply composer.

Amounts and order ids come from the policy decision, never from the model, so
the reply cannot drift from the action. The service never sends these replies.
"""
from __future__ import annotations

from .normalize import format_usd, normalize_email


def _hello(ticket: dict) -> str:
    name = ""
    return "Hello," if not name else f"Hello {name},"


def _subject(ticket: dict) -> str:
    subject = (ticket.get("subject") or "Your Larkfield Home request").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _prior_note(ctx: dict) -> str:
    notes = []
    prior = int(ctx.get("prior_cents") or 0)
    if prior > 0:
        notes.append(f"A credit of {format_usd(prior)} has already been issued for the affected line and is not repaid.")
    other = (ctx.get("other_prior_note") or "").strip()
    if other:
        notes.append(other)
    return (" " + " ".join(notes)) if notes else ""


def _partial_note(ctx: dict) -> str:
    notes = []
    if ctx.get("whole_order"):
        notes.append("Only the affected item is eligible, so the rest of the order is not included.")
    if ctx.get("shipping_request"):
        notes.append("The delivery charge is not part of the item value, so it is not included.")
    return (" " + " ".join(notes)) if notes else ""


def compose(ticket: dict, ctx: dict) -> dict:
    outcome = ctx["outcome"]
    order_id = ctx.get("order_id") or "your order"
    item = ctx.get("item") or "the affected item"
    amount = int(ctx.get("amount_cents") or 0)
    amount_text = format_usd(amount)
    days = ctx.get("days")
    delivered = ctx.get("delivered_at") or "the recorded delivery date"
    carrier = ctx.get("carrier") or "the carrier"
    tracking = ctx.get("tracking_no") or "not available"
    last_event = ctx.get("last_event_at") or "not available"
    reason_codes = set(ctx.get("reason_codes") or [])

    if outcome == "ESCALATE" and ctx.get("approver_role") == "TRUST_SAFETY":
        body = (
            f"{_hello(ticket)}\n\nThank you for contacting Larkfield Home. Your message about "
            f"{order_id} has been passed to our Trust & Safety team for review. No refund has been "
            "processed. They will follow up with you directly.\n\nLarkfield Home Customer Care"
        )
    elif outcome == "ESCALATE":
        body = (
            f"{_hello(ticket)}\n\nOrder {order_id} has had no carrier movement for {days} days "
            f"(last tracking update: {last_event}). We have escalated it to Carrier Claims for "
            "investigation. No refund has been processed yet. They will follow up with you.\n\n"
            "Larkfield Home Customer Care"
        )
    elif outcome == "PEND":
        body = (
            f"{_hello(ticket)}\n\nWe have reviewed your request for {item} on order {order_id}. "
            f"A refund of {amount_text} is ready, but because it is above our $150 authority limit "
            "it needs approval from a Support Lead before anything is processed. You do not need to "
            f"do anything; we will follow up once a decision is made.{_prior_note(ctx)}{_partial_note(ctx)}"
            "\n\nLarkfield Home Customer Care"
        )
        if "DAMAGE_EXCEPTION" in reason_codes:
            body = body.replace(
                "We have reviewed your request",
                "We have reviewed your request under the damaged-item photo exception",
            )
    elif outcome == "PROCEED_MODIFIED":
        body = (
            f"{_hello(ticket)}\n\nWe can approve a refund of {amount_text} for {item} on order "
            f"{order_id}, which is the value of the affected line.{_partial_note(ctx)}{_prior_note(ctx)} "
            "It will be processed to the original payment method.\n\nLarkfield Home Customer Care"
        )
    elif outcome == "PROCEED" and amount > 0:
        body = (
            f"{_hello(ticket)}\n\nWe have approved a refund of {amount_text} for {item} on order "
            f"{order_id}. It will be processed to the original payment method.{_prior_note(ctx)}"
            "\n\nLarkfield Home Customer Care"
        )
        if "DAMAGE_EXCEPTION" in reason_codes:
            body = body.replace(
                "\n\nLarkfield Home Customer Care",
                " This is covered by the damaged-item photo exception.\n\nLarkfield Home Customer Care",
            )
    elif outcome == "REJECT" and "OUT_OF_WINDOW" in reason_codes:
        photo_note = ""
        if ctx.get("damage") and not ctx.get("has_photo"):
            photo_note = " A photo attachment is required for the 45-day damaged-item exception."
        body = (
            f"{_hello(ticket)}\n\nWe are unable to refund {item} on order {order_id}. The carrier "
            f"recorded delivery on {delivered}, which is {days} days before your email. Our policy "
            "allows 30 days from delivery (up to 45 days for damaged or faulty items with a photo), "
            f"so this request falls outside the window.{photo_note} No refund has been processed."
            "\n\nLarkfield Home Customer Care"
        )
    elif outcome == "REJECT" and "ALREADY_REFUNDED" in reason_codes:
        prior = int(ctx.get("prior_cents") or 0)
        prior_text = f" ({format_usd(prior)})" if prior > 0 else ""
        body = (
            f"{_hello(ticket)}\n\nOur records show that the affected item on order {order_id} has "
            f"already been refunded{prior_text}. No further refund is due for that line. If you "
            "believe this is wrong, reply with the refund reference and we will check it."
            "\n\nLarkfield Home Customer Care"
        )
    elif outcome == "INVALID_REQUEST" and "CUSTOMER_MISMATCH" in reason_codes:
        body = (
            f"{_hello(ticket)}\n\nWe cannot discuss or refund order {order_id} because it is not "
            "associated with the email address you wrote from. Please contact us from the email used "
            "to place the order. No refund has been processed.\n\nLarkfield Home Customer Care"
        )
    elif outcome == "INVALID_REQUEST":
        reference = ctx.get("requested_ref")
        ref_text = f"order {reference}" if reference else "your order number (LF-######)"
        body = (
            f"{_hello(ticket)}\n\nWe could not find {ref_text} on the email address you wrote from. "
            "Please reply with the order number and the item concerned, and we will look into it. We "
            "cannot act on a different or nearby order. No refund has been processed."
            "\n\nLarkfield Home Customer Care"
        )
    elif outcome == "UNABLE_TO_VERIFY" and "CARRIER_STATUS_UNAVAILABLE" in reason_codes:
        body = (
            f"{_hello(ticket)}\n\nThe carrier has not returned a usable status for order {order_id}, "
            "so we cannot verify the delivery or refund claim yet. We are checking with the carrier "
            "and will follow up. No refund has been processed.\n\nLarkfield Home Customer Care"
        )
    elif outcome == "UNABLE_TO_VERIFY" and ctx.get("delivered_dispute"):
        body = (
            f"{_hello(ticket)}\n\nOur carrier records show order {order_id} was delivered on "
            f"{delivered}. Because you report it did not arrive, we are checking this with the "
            "carrier. No refund has been processed.\n\nLarkfield Home Customer Care"
        )
    elif outcome == "UNABLE_TO_VERIFY":
        body = (
            f"{_hello(ticket)}\n\nWe could not verify the details needed to decide this request "
            "(for example a carrier scan or a single matching order). Please reply with the order "
            "number and the item concerned, and our support team will help. No refund has been "
            "processed.\n\nLarkfield Home Customer Care"
        )
    elif outcome == "PROCEED" and ctx.get("ticket_type") in {"ORDER_STATUS", "DELIVERY_ISSUE"}:
        if ctx.get("shipment_status") == "DELIVERED":
            body = (
                f"{_hello(ticket)}\n\nOur carrier records show order {order_id} was delivered on "
                f"{delivered} ({carrier}, tracking {tracking}).\n\nLarkfield Home Customer Care"
            )
        else:
            body = (
                f"{_hello(ticket)}\n\nOrder {order_id} is in transit with {carrier}. The last "
                f"tracking update was {last_event}. Tracking number: {tracking}. If it does not "
                "move, reply and we will investigate.\n\nLarkfield Home Customer Care"
            )
    elif outcome == "RETURN" and amount == 0:
        body = (
            f"{_hello(ticket)}\n\nWe can help you return {item} from order {order_id}. Please reply "
            "to confirm the return and we will send the next steps. No refund has been processed."
            "\n\nLarkfield Home Customer Care"
        )
    else:
        body = (
            f"{_hello(ticket)}\n\nThanks for contacting Larkfield Home. A support specialist will "
            "follow up on your question. For order-specific help, reply with your order number "
            "(LF-######) and the item concerned.\n\nLarkfield Home Customer Care"
        )

    return {
        "to": normalize_email(ticket.get("from_") or ticket.get("from")),
        "subject": _subject(ticket),
        "body": body,
    }


def generic_fallback(ticket: dict, order_id: str | None = None) -> dict:
    reference = f" order {order_id}" if order_id else ""
    return {
        "to": normalize_email(ticket.get("from_") or ticket.get("from")),
        "subject": _subject(ticket),
        "body": (
            f"{_hello(ticket)}\n\nThanks for contacting Larkfield Home. We could not complete an "
            f"automated review of this request{reference}, so it has been passed to our support "
            "team. No refund has been processed. Please reply with your order number and the item "
            "concerned if you can.\n\nLarkfield Home Customer Care"
        ),
    }
