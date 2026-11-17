"""Scheduler hooks for hosts without a cron service (free tiers).

A free external pinger (cron-job.org, UptimeRobot, GitHub Actions…) calls:
    GET /api/cron/holds/?key=<CRON_SECRET>   every 1-5 minutes  -> releases expired payment holds
    GET /api/cron/daily/?key=<CRON_SECRET>   once a day          -> reminders, no-shows, hold sweep
The key may also be sent as "Authorization: Bearer <CRON_SECRET>". Disabled (404) when CRON_SECRET is unset.
"""
import hmac
import io
import logging

from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..services import reservations

log = logging.getLogger("hotel")


def healthz(request):
    """Liveness + DB check for the hosting platform's health probe. Reveals nothing else."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
    except Exception:  # noqa: BLE001
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


def _authorised(request):
    secret = settings.CRON_SECRET
    if not secret:
        return None  # endpoint disabled
    given = request.GET.get("key", "")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    return hmac.compare_digest(given.encode(), secret.encode())


@csrf_exempt
@require_http_methods(["GET", "POST", "HEAD"])
def run(request, task):
    ok = _authorised(request)
    if ok is None:
        raise Http404()
    if not ok:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    if task == "holds":
        return JsonResponse({"ok": True, "released": reservations.expire_holds()})
    if task == "daily":
        out = io.StringIO()
        call_command("daily_jobs", stdout=out)
        log.info("cron daily: %s", out.getvalue().strip())
        return JsonResponse({"ok": True, "result": out.getvalue().strip()})
    raise Http404()
