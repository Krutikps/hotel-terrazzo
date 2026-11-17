"""Pluggable payment providers. Add a provider by subclassing PaymentProvider and registering it below."""
from django.conf import settings

from .base import PaymentProvider, ProviderError, VerifiedPayment  # noqa: F401
from .mock import MockProvider
from .razorpay import RazorpayProvider

PROVIDERS = {"mock": MockProvider, "razorpay": RazorpayProvider}


def get_provider(name=None):
    if not name:
        from hotel.models import Hotel

        hotel = Hotel.get()
        name = (hotel.payment_provider if hotel and hotel.payment_provider else settings.PAYMENT_PROVIDER) or "mock"
    if name.lower() == "mock" and not (settings.DEBUG or settings.ALLOW_MOCK_PAYMENTS):
        raise ProviderError("The sandbox payment provider is disabled in production. Configure Razorpay.")
    try:
        return PROVIDERS[name.lower()]()
    except KeyError:
        raise ProviderError(f"Unknown payment provider '{name}'.")
