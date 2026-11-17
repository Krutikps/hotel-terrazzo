"""Payment orchestration: online intents, server-side verification, offline payments, refunds."""
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from hotel.models import Payment, Reservation
from hotel.payments import ProviderError, get_provider
from . import audit, notifications, reservations

log = logging.getLogger("hotel.payments")


def online_amount(res):
    pct = Decimal(res.hotel.online_payment_percent or 100)
    return (res.total * pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def start_online_payment(res, request=None):
    """Create the provider order + a PENDING Payment row for a held reservation. Returns browser config."""
    if not res.is_hold_alive:
        raise reservations.HoldExpired("Your booking session has expired.", "hold_expired")
    provider = get_provider()
    amount = online_amount(res)
    order = provider.create_order(amount, res.hotel.currency, receipt=str(res.access_token),
                                  notes={"reservation": str(res.access_token), "guest": res.guest.name})
    Payment.objects.create(reservation=res, kind=Payment.PAYMENT, amount=amount, method="ONLINE", provider=provider.name,
                           provider_order_id=order["order_id"], status=Payment.PENDING)
    reservations.recompute(res)
    audit.log("payment.initiated", res, request=request, actor_label="Guest (website)", new={"order": order["order_id"], "amount": amount},
              summary=f"Online payment of {amount} initiated")
    return {"order": order, "amount": amount}


@transaction.atomic
def apply_result(provider_name, order_id, payment_id, status, amount, raw, request=None):
    """Idempotently apply a VERIFIED provider result (from checkout verify or webhook)."""
    pay = Payment.objects.select_for_update().filter(provider=provider_name, provider_order_id=order_id, kind=Payment.PAYMENT).first()
    if not pay:
        raise ProviderError("Unknown payment order.")
    res = Reservation.objects.select_for_update().get(pk=pay.reservation_id)
    if pay.status == Payment.PAID:
        return pay, res, "already_paid"
    pay.raw_response = raw or {}
    if status == "PAID":
        if Decimal(amount) != pay.amount:
            pay.status = Payment.FAILED
            pay.note = "Amount mismatch — not confirmed"
            pay.save()
            raise ProviderError("Paid amount does not match the booking amount.")
        pay.status = Payment.PAID
        pay.provider_payment_id = payment_id
        pay.transaction_id = payment_id
        pay.paid_at = timezone.now()
        pay.save()
        reservations.recompute(res)
        try:
            res = reservations.confirm_after_payment(res.id, request=request)
            audit.log("payment.paid", res, request=request, actor_label="Payment provider", new={"payment": payment_id, "amount": amount},
                      summary=f"Online payment {payment_id} verified")
            return pay, res, "confirmed"
        except reservations.HoldExpired:
            # money received but inventory is gone: refund automatically, never leave the guest hanging
            audit.log("payment.paid_after_expiry", res, actor_label="System", new={"payment": payment_id}, summary="Payment received after hold expired; refunding")
            refund_payment(pay, pay.amount, user=None, reason="Room no longer available after session expired", request=request)
            notifications.notify_after_commit("PAYMENT_FAILED", res, extra={"reason": "Paid after the hold expired; refunded automatically"})
            return pay, Reservation.objects.get(pk=res.pk), "expired_refunded"
    if status == "FAILED":
        pay.status = Payment.FAILED
        pay.note = (raw or {}).get("error_description", "Payment failed")[:255] if isinstance(raw, dict) else "Payment failed"
        pay.save()
        reservations.release_hold(res.id, "Payment failed")
        notifications.notify_after_commit("PAYMENT_FAILED", res, extra={"reason": pay.note})
        audit.log("payment.failed", res, request=request, actor_label="Payment provider", summary=pay.note)
        return pay, Reservation.objects.get(pk=res.pk), "failed"
    pay.status = Payment.AUTHORIZED
    pay.save()
    reservations.recompute(res)
    return pay, res, "authorized"


def verify_checkout(order_id, payment_id, signature, request=None):
    """Called when the browser reports a result. We verify server-side before trusting anything."""
    pay = Payment.objects.filter(provider_order_id=order_id, kind=Payment.PAYMENT).first()
    if not pay:
        raise ProviderError("Unknown payment order.")
    provider = get_provider(pay.provider)
    v = provider.verify_payment(order_id, payment_id, signature, pay.amount)
    return apply_result(provider.name, v.order_id, v.payment_id, v.status, v.amount, v.raw, request=request)


def fail_checkout(order_id, reason="Payment cancelled or failed", request=None):
    pay = Payment.objects.filter(provider_order_id=order_id, kind=Payment.PAYMENT).first()
    if not pay or pay.status == Payment.PAID:
        return None
    return apply_result(pay.provider, order_id, "", "FAILED", 0, {"error_description": reason}, request=request)


def handle_webhook(provider_name, body, headers):
    provider = get_provider(provider_name)
    v = provider.parse_webhook(body, headers)
    if v is None:
        return "ignored"
    try:
        _, _, outcome = apply_result(provider.name, v.order_id, v.payment_id, v.status, v.amount, v.raw)
    except ProviderError as e:
        log.warning("Webhook ignored: %s", e)
        return "ignored"
    return outcome


# --------------------------------------------------------------------------- offline payments & refunds
@transaction.atomic
def record_offline(res, amount, method, user=None, reference="", note="", request=None):
    amount = Decimal(amount)
    res = Reservation.objects.select_for_update().get(pk=res.pk)
    if res.status in (Reservation.PENDING_PAYMENT, Reservation.RELEASED, Reservation.CANCELLED, Reservation.NO_SHOW):
        raise reservations.BookingError("Payments can only be added to confirmed, checked-in or checked-out bookings.")
    if amount <= 0:
        raise reservations.BookingError("Payment amount must be greater than zero.")
    if amount > res.balance:
        raise reservations.BookingError(f"Amount exceeds the balance due ({res.hotel.money(res.balance)}).")
    if method not in dict(Payment.METHODS) or method == "ONLINE":
        raise reservations.BookingError("Choose a payment method: cash, card, UPI, bank transfer or other.")
    pay = Payment.objects.create(reservation=res, kind=Payment.PAYMENT, amount=amount, method=method, status=Payment.PAID,
                                 transaction_id=reference[:100], note=note[:255], created_by=user, paid_at=timezone.now())
    reservations.recompute(res)
    audit.log("payment.added", res, user=user, request=request, new={"amount": amount, "method": method, "reference": reference},
              summary=f"{method} payment {amount} recorded on {res.reference}")
    notifications.notify_after_commit("PAYMENT_CONFIRMED", res)
    return pay


@transaction.atomic
def refund_payment(payment, amount, user=None, reason="", request=None):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    amount = Decimal(amount)
    if payment.kind != Payment.PAYMENT or payment.status != Payment.PAID:
        raise reservations.BookingError("Only paid payments can be refunded.")
    remaining = payment.amount - payment.refunded_amount
    if amount <= 0 or amount > remaining:
        raise reservations.BookingError(f"Refund must be between 0.01 and {remaining}.")
    ref = Payment(reservation=payment.reservation, kind=Payment.REFUND, amount=amount, method=payment.method, provider=payment.provider,
                  parent=payment, note=(reason or "")[:255], created_by=user)
    if payment.provider and payment.provider_payment_id:
        try:
            out = get_provider(payment.provider).refund(payment.provider_payment_id, amount, {"reason": reason[:100]})
            ref.provider_payment_id = out["refund_id"]
            ref.transaction_id = out["refund_id"]
            ref.status = Payment.PAID if out["status"] == "PAID" else Payment.PENDING
            ref.raw_response = out["raw"]
        except ProviderError as e:
            ref.status = Payment.FAILED
            ref.note = f"Gateway refund failed: {e}"[:255]
    else:
        ref.status = Payment.PAID
    ref.paid_at = timezone.now() if ref.status == Payment.PAID else None
    ref.save()
    res = payment.reservation
    reservations.recompute(res)
    audit.log("payment.refunded", res, user=user, request=request, new={"amount": amount, "status": ref.status, "reason": reason},
              summary=f"Refund {amount} ({ref.status}) on {res.reference}")
    return ref


def refund_reservation(res, amount, user=None, reason="", request=None):
    """Refund `amount` across the booking's paid payments, newest first."""
    left = Decimal(amount)
    out = []
    for p in res.payments.filter(kind=Payment.PAYMENT, status=Payment.PAID).order_by("-created_at"):
        if left <= 0:
            break
        take = min(left, p.amount - p.refunded_amount)
        if take > 0:
            out.append(refund_payment(p, take, user=user, reason=reason, request=request))
            left -= take
    return out
