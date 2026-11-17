"""Shared API plumbing: JSON responses, friendly error mapping, permissions, rate limiting."""
import functools
import json
import logging
from datetime import date, datetime

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse

from hotel.payments import ProviderError
from hotel.services import audit, availability, pricing, reservations

log = logging.getLogger("hotel.api")


class Enc(DjangoJSONEncoder):
    pass


def ok(data=None, status=200, **extra):
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return JsonResponse(payload, status=status, encoder=Enc, json_dumps_params={"ensure_ascii": False})


def fail(message, status=400, code="error", field=None, **extra):
    payload = {"ok": False, "error": {"message": message, "code": code}}
    if field:
        payload["error"]["field"] = field
    payload.update(extra)
    return JsonResponse(payload, status=status, encoder=Enc)


def body(request):
    if request.content_type and "json" in request.content_type:
        try:
            return json.loads(request.body or b"{}")
        except ValueError:
            raise pricing.PricingError("Invalid request.")
    return request.POST.dict()


def parse_date(v, name="date"):
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise pricing.PricingError(f"Please enter a valid {name}.", name)


def as_int(v, default=0, name="number", lo=0, hi=1000):
    try:
        n = int(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise pricing.PricingError(f"Please enter a valid {name}.", name)
    if n < lo or n > hi:
        raise pricing.PricingError(f"Please enter a valid {name}.", name)
    return n


def client_ip(request):
    return audit.client_ip(request) or "unknown"


def rate_limit(bucket):
    """Fixed-window per-IP limiter (cache-backed; use Redis in production for multi-process accuracy)."""
    def deco(view):
        @functools.wraps(view)
        def inner(request, *a, **kw):
            limits = settings.RATE_LIMITS.get(bucket)
            if limits:
                max_calls, window = limits
                key = f"rl:{bucket}:{client_ip(request)}"
                cache.add(key, 0, window)
                try:
                    n = cache.incr(key)
                except ValueError:
                    cache.set(key, 1, window)
                    n = 1
                if n > max_calls:
                    return fail("Too many requests. Please wait a moment and try again.", 429, "rate_limited")
            return view(request, *a, **kw)
        return inner
    return deco


def api(view):
    """Map domain exceptions to friendly JSON errors; never leak internals."""
    @functools.wraps(view)
    def inner(request, *a, **kw):
        try:
            return view(request, *a, **kw)
        except pricing.CouponError as e:
            return fail(e.message, 400, "coupon_invalid", field="coupon")
        except pricing.PricingError as e:
            return fail(e.message, 400, "invalid", field=e.field)
        except availability.Unavailable as e:
            return fail(str(e), 409, "unavailable")
        except reservations.HoldExpired as e:
            return fail("Your booking session has expired. Please search again.", 410, "hold_expired")
        except reservations.BookingError as e:
            return fail(e.message, 409 if e.code in ("unavailable", "invalid_state") else 400, e.code)
        except ProviderError as e:
            return fail(str(e) or "Payment could not be completed.", 402, "payment_error")
        except ValidationError as e:
            return fail(" ".join(e.messages), 400, "validation")
        except (KeyError, ValueError, TypeError):
            log.exception("Bad API request %s", request.path)
            return fail("Some of the information provided is invalid. Please check and try again.", 400, "bad_request")
        except Exception:  # noqa: BLE001
            log.exception("Unhandled API error %s", request.path)
            return fail("Something went wrong on our side. Please try again.", 500, "server_error")
    return inner


def staff_api(perm=None):
    """Require a logged-in staff user with the permission (else JSON 401/403)."""
    def deco(view):
        @functools.wraps(view)
        @api
        def inner(request, *a, **kw):
            if not request.user.is_authenticated:
                return fail("Please sign in.", 401, "auth_required")
            if perm and not request.user.can(perm):
                return fail("You don't have permission to do that.", 403, "forbidden")
            return view(request, *a, **kw)
        return inner
    return deco
