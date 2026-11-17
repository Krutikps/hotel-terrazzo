from dataclasses import dataclass, field
from decimal import Decimal


class ProviderError(Exception):
    """Raised when a provider call fails or a signature does not verify."""


@dataclass
class VerifiedPayment:
    order_id: str
    payment_id: str
    status: str  # "PAID" | "FAILED" | "AUTHORIZED"
    amount: Decimal
    raw: dict = field(default_factory=dict)  # provider response — never contains card data


class PaymentProvider:
    name = "base"
    needs_redirect = False

    def create_order(self, amount: Decimal, currency: str, receipt: str, notes: dict) -> dict:
        """Create a provider-side order. Returns {'order_id': str, 'client': {...config for the browser...}}."""
        raise NotImplementedError

    def verify_payment(self, order_id: str, payment_id: str, signature: str, expected_amount: Decimal) -> VerifiedPayment:
        """Server-side verification of a checkout result. Must raise ProviderError if it cannot be proven."""
        raise NotImplementedError

    def parse_webhook(self, body: bytes, headers) -> "VerifiedPayment | None":
        """Verify + parse a provider webhook. Return None for events we ignore; raise ProviderError on a bad signature."""
        raise NotImplementedError

    def refund(self, payment_id: str, amount: Decimal, notes: dict) -> dict:
        """Refund (part of) a captured payment. Returns {'refund_id': str, 'status': 'PAID'|'PENDING', 'raw': {...}}."""
        raise NotImplementedError
