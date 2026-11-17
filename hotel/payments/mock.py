"""Built-in SANDBOX provider for local development / demos / tests.

It behaves like a hosted gateway: the guest is sent to a gateway page (/pay/mock/<order>/), which signs a
result with a server-side secret. verify_payment() checks that HMAC, exactly like Razorpay's signature check,
so the booking can never be confirmed by merely claiming success from the browser.
"""
import hashlib
import hmac
import uuid
from decimal import Decimal

from django.conf import settings

from .base import PaymentProvider, ProviderError, VerifiedPayment


def sign(order_id, payment_id, status="PAID"):
    msg = f"{order_id}|{payment_id}|{status}".encode()
    return hmac.new(settings.MOCK_PAYMENT_SECRET.encode(), msg, hashlib.sha256).hexdigest()


class MockProvider(PaymentProvider):
    name = "mock"

    def create_order(self, amount, currency, receipt, notes):
        order_id = f"mock_order_{uuid.uuid4().hex[:16]}"
        return {"order_id": order_id,
                "client": {"provider": self.name, "order_id": order_id, "amount": int(Decimal(amount) * 100), "currency": currency,
                           "checkout_url": f"/pay/mock/{order_id}/"}}

    def verify_payment(self, order_id, payment_id, signature, expected_amount):
        if not signature or not hmac.compare_digest(sign(order_id, payment_id, "PAID"), signature):
            raise ProviderError("Payment signature could not be verified.")
        return VerifiedPayment(order_id, payment_id, "PAID", Decimal(expected_amount), {"provider": "mock", "status": "captured"})

    def parse_webhook(self, body, headers):
        return None  # the sandbox has no asynchronous webhooks

    def refund(self, payment_id, amount, notes):
        return {"refund_id": f"mock_rfnd_{uuid.uuid4().hex[:14]}", "status": "PAID", "raw": {"provider": "mock", "amount": str(amount)}}
