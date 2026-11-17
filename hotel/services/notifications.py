"""Notification architecture: events -> channels (email / SMS / WhatsApp / in-app), all logged in the Notification table.

Channels are switched on/off in Settings → Notifications; credentials live in environment variables.
A failing channel never breaks a booking.
"""
import logging

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction

log = logging.getLogger("hotel.notifications")

GUEST_EVENTS = {
    "BOOKING_CONFIRMED": ("Booking confirmed – {ref}",
                          "Dear {guest},\n\nThank you for booking with {hotel}. Your reservation is confirmed.\n\nBooking number: {ref}\nRoom: {room} ({plan})\n"
                          "Check-in: {check_in} (from {ci_time})\nCheck-out: {check_out} (until {co_time})\nGuests: {adults} adult(s), {children} child(ren)\n"
                          "Total: {total}   Paid: {paid}   Balance: {balance}\n\nCancellation policy: {policy}\n\nManage your booking: {manage_url}\n\nWe look forward to welcoming you."),
    "PAYMENT_CONFIRMED": ("Payment received – {ref}", "Dear {guest},\n\nWe have received your payment for booking {ref}.\nAmount paid so far: {paid}\nBalance due: {balance}\n\n{hotel}"),
    "CANCELLED": ("Booking cancelled – {ref}", "Dear {guest},\n\nYour booking {ref} has been cancelled.\n{cancel_detail}\n\nWe hope to host you another time.\n{hotel}"),
    "MODIFIED": ("Booking updated – {ref}", "Dear {guest},\n\nYour booking {ref} has been updated.\nRoom: {room} ({plan})\nCheck-in: {check_in}\nCheck-out: {check_out}\nTotal: {total}   Balance: {balance}\n\nManage your booking: {manage_url}\n\n{hotel}"),
    "PRE_ARRIVAL": ("Your stay at {hotel} starts tomorrow", "Dear {guest},\n\nWe are looking forward to welcoming you tomorrow ({check_in}). Check-in is from {ci_time}.\nBooking number: {ref}. Balance due at arrival: {balance}\n\n{hotel}, {hotel_phone}"),
    "CHECKOUT_REMINDER": ("Check-out reminder – {ref}", "Dear {guest},\n\nA gentle reminder that check-out today is by {co_time}. We hope you enjoyed your stay!\n\n{hotel}"),
}
ADMIN_EVENTS = {
    "NEW_BOOKING": ("New booking {ref}", "{guest} booked {room} ({plan}) {check_in} → {check_out}, {adults}A/{children}C. Total {total}, paid {paid}. Source: {source}."),
    "CANCELLATION": ("Cancellation {ref}", "Booking {ref} ({guest}) was cancelled. {cancel_detail}"),
    "PAYMENT_FAILED": ("Payment failed {ref}", "A payment for {guest} ({ref}) did not complete. {reason}"),
    "PAYMENT_PENDING": ("Payment pending {ref}", "Booking {ref} ({guest}) has a balance of {balance} due."),
    "SPECIAL_REQUEST": ("Special request – {ref}", "{guest} ({ref}, arriving {check_in}) requested: {requests}"),
    "MAINTENANCE_ALERT": ("Maintenance – room {room_no}", "Room {room_no}: {problem} (priority {priority})."),
}
ADMIN_AFTER_GUEST = {"CANCELLED": "CANCELLATION"}


class _D(dict):
    def __missing__(self, key):
        return ""


def _fmt(tpl, ctx):
    return tpl[0].format_map(_D(ctx)), tpl[1].format_map(_D(ctx))


def _context(res, extra=None):
    extra = extra or {}
    h = res.hotel if res else None
    ctx = {"hotel": h.name if h else "", "hotel_phone": h.phone if h else ""}
    if res:
        mm = h.money
        reqs = "; ".join(filter(None, [", ".join(r.request_type + (f" ({r.notes})" if r.notes else "") for r in res.requests.all()), res.special_requests])) or "—"
        ctx.update(
            guest=res.guest.name, ref=res.reference, room=res.room_type.name, plan=res.rate_plan.name,
            check_in=f"{res.check_in:%a %d %b %Y}", check_out=f"{res.check_out:%a %d %b %Y}", ci_time=f"{h.check_in_time:%H:%M}",
            co_time=f"{h.check_out_time:%H:%M}", adults=res.adults, children=res.children, total=mm(res.total), paid=mm(res.amount_paid),
            balance=mm(res.balance), policy=res.cancellation_policy, source=res.get_source_display(), requests=reqs,
            manage_url=f"{settings.SITE_URL}/my-booking/?ref={res.booking_number or ''}",
        )
        fee, refund = extra.get("fee"), extra.get("refund")
        ctx["cancel_detail"] = (f"Cancellation charge: {mm(fee)}. Refund: {mm(refund)} (to the original payment method, 5–7 working days)." if fee is not None else "")
    t = extra.get("ticket")
    if t:
        ctx.update(room_no=t.room.room_number, problem=t.problem, priority=t.get_priority_display())
    ctx["reason"] = extra.get("reason", "")
    return ctx


def notify_after_commit(event, res, extra=None, guest=True):
    res_id = res.pk if res else None
    transaction.on_commit(lambda: send(event, res_id, extra=extra, guest=guest))


def send(event, reservation_id=None, extra=None, guest=True):
    """Dispatch an event. Never raises."""
    try:
        from hotel.models import Hotel, Reservation

        res = (Reservation.objects.select_related("hotel", "guest", "room_type", "rate_plan").filter(pk=reservation_id).first()
               if reservation_id else None)
        hotel = res.hotel if res else Hotel.get()
        if not hotel:
            return
        ctx = _context(res, extra)
        if event in GUEST_EVENTS and guest and res:
            subj, body = _fmt(GUEST_EVENTS[event], ctx)
            _deliver_guest(hotel, res, event, subj, body)
            admin_ev = ADMIN_AFTER_GUEST.get(event)
            if admin_ev:
                _deliver_admin(hotel, res, admin_ev, *_fmt(ADMIN_EVENTS[admin_ev], ctx))
        elif event in ADMIN_EVENTS:
            _deliver_admin(hotel, res, event, *_fmt(ADMIN_EVENTS[event], ctx))
    except Exception:  # noqa: BLE001 - notifications must never break the booking flow
        log.exception("Notification %s failed", event)


def _record(**kw):
    from hotel.models import Notification

    return Notification.objects.create(**kw)


def _deliver_guest(hotel, res, event, subject, body):
    g = res.guest
    if hotel.notify_email and g.email:
        _email(res, event, g.email, subject, body, audience="GUEST", attach=(event == "BOOKING_CONFIRMED"))
    elif g.email:
        _record(audience="GUEST", channel="EMAIL", event=event, recipient=g.email, subject=subject, body=body,
                status="SKIPPED", error="Email disabled", reservation=res)
    if hotel.notify_sms and g.phone:
        _twilio(res, event, "SMS", g.phone, f"{subject}. {hotel.name}")
    if hotel.notify_whatsapp and g.phone:
        _twilio(res, event, "WHATSAPP", g.phone, f"{subject}\n\n{body}"[:1500])


def _deliver_admin(hotel, res, event, subject, body):
    _record(audience="ADMIN", channel="INAPP", event=event, recipient="staff", subject=subject, body=body, status="SENT", reservation=res)
    to = hotel.admin_notification_email or hotel.email
    if hotel.notify_email and to:
        _email(res, event, to, f"[{hotel.name}] {subject}", body, audience="ADMIN")


def _email(res, event, to, subject, body, audience, attach=False):
    n = _record(audience=audience, channel="EMAIL", event=event, recipient=to, subject=subject, body=body, reservation=res)
    try:
        msg = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
        if attach and res:
            from .documents import confirmation_pdf

            msg.attach(f"booking-{res.reference}.pdf", confirmation_pdf(res), "application/pdf")
        msg.send(fail_silently=False)
        n.status = "SENT"
    except Exception as e:  # noqa: BLE001
        log.warning("Email to %s failed: %s", to, e)
        n.status, n.error = "FAILED", str(e)[:300]
    n.save(update_fields=["status", "error"])


def _twilio(res, event, channel, phone, text):
    n = _record(audience="GUEST", channel=channel, event=event, recipient=phone, body=text, reservation=res)
    sid, tok = settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN
    sender = settings.TWILIO_WHATSAPP_FROM if channel == "WHATSAPP" else settings.TWILIO_SMS_FROM
    if not (sid and tok and sender):
        n.status, n.error = "SKIPPED", "Twilio credentials not configured"
    else:
        to = f"whatsapp:{phone}" if channel == "WHATSAPP" else phone
        frm = f"whatsapp:{sender}" if channel == "WHATSAPP" and not sender.startswith("whatsapp:") else sender
        try:
            r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, tok),
                              data={"To": to, "From": frm, "Body": text}, timeout=15)
            n.status = "SENT" if r.status_code < 300 else "FAILED"
            n.error = "" if r.status_code < 300 else r.text[:300]
        except requests.RequestException as e:
            n.status, n.error = "FAILED", str(e)[:300]
    n.save(update_fields=["status", "error"])
