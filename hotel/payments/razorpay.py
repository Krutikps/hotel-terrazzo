"""Razorpay provider (https://razorpay.com/docs/api). Works with test keys (rzp_test_…) as a sandbox.

Money flow: Orders API -> Checkout.js in the browser -> we verify the signature AND re-fetch the payment
from Razorpay's API server-side before anything is confirmed. Webhooks are an independent second path.
"""
import hashlib
import hmac
import json
import logging
from decimal import Decimal

import requests
from django.conf import settings

from .base import PaymentProvider, ProviderError, VerifiedPayment

log = logging.getLogger("hotel.payments")
API = "https://api.razorpay.com/v1"


def _paise(amount):
    return int((Decimal(amount) * 100).quantize(Decimal("1")))


class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def __init__(self):
        self.key_id = settings.RAZORPAY_KEY_ID
        self.key_secret = settings.RAZORPAY_KEY_SECRET
        if not (self.key_id and self.key_secret):
            raise ProviderError("Razorpay is not configured (set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET).")

    def _call(self, method, path, **kw):
        try:
            r = requests.request(method, f"{API}{path}", auth=(self.key_id, self.key_secret), timeout=20, **kw)
        except requests.RequestException as e:
            log.exception("Razorpay network error")
            raise ProviderError("Could not reach the payment gateway.") from e
        if r.status_code >= 400:
            log.warning("Razorpay %s %s -> %s %s", method, path, r.status_code, r.text[:300])
            raise ProviderError("The payment gateway rejected the request.")
        return r.json()

    def create_order(self, amount, currency, receipt, notes):
        data = self._call("POST", "/orders", json={"amount": _paise(amount), "currency": currency, "receipt": receipt[:40], "notes": notes})
        return {"order_id": data["id"],
                "client": {"provider": self.name, "key": self.key_id, "order_id": data["id"], "amount": data["amount"], "currency": currency}}

    def verify_payment(self, order_id, payment_id, signature, expected_amount):
        expected = hmac.new(self.key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise ProviderError("Payment signature could not be verified.")
        # Never trust the browser alone: confirm status/amount/order with Razorpay directly.
        pay = self._call("GET", f"/payments/{payment_id}")
        if pay.get("order_id") != order_id:
            raise ProviderError("Payment does not belong to this order.")
        if pay.get("status") == "authorized":
            pay = self._call("POST", f"/payments/{payment_id}/capture", json={"amount": pay["amount"], "currency": pay["currency"]})
        status = {"captured": "PAID", "failed": "FAILED"}.get(pay.get("status"), "AUTHORIZED")
        amount = Decimal(pay.get("amount", 0)) / 100
        if status == "PAID" and amount != Decimal(expected_amount):
            raise ProviderError("Paid amount does not match the booking amount.")
        return VerifiedPayment(order_id, payment_id, status, amount, {k: pay.get(k) for k in ("id", "status", "method", "amount", "currency", "order_id", "bank", "wallet", "vpa", "error_code", "error_description")})

    def parse_webhook(self, body, headers):
        secret = settings.RAZORPAY_WEBHOOK_SECRET
        if not secret:
            raise ProviderError("Webhook secret not configured.")
        sig = headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(expected, sig):
            raise ProviderError("Invalid webhook signature.")
        evt = json.loads(body)
        ent = (evt.get("payload", {}).get("payment", {}) or {}).get("entity")
        if not ent:
            return None
        kind = evt.get("event")
        if kind in ("payment.captured", "order.paid"):
            status = "PAID"
        elif kind == "payment.failed":
            status = "FAILED"
        else:
            return None
        return VerifiedPayment(ent.get("order_id", ""), ent.get("id", ""), status, Decimal(ent.get("amount", 0)) / 100,
                               {k: ent.get(k) for k in ("id", "status", "method", "amount", "currency", "order_id", "error_code", "error_description")})

    def refund(self, payment_id, amount, notes):
        data = self._call("POST", f"/payments/{payment_id}/refund", json={"amount": _paise(amount), "notes": notes})
        return {"refund_id": data["id"], "status": "PAID" if data.get("status") in ("processed", "created", "pending") else "PENDING",
                "raw": {k: data.get(k) for k in ("id", "status", "amount", "payment_id")}}
