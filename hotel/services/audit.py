"""Audit trail helper. Every important administrative action goes through log()."""
import json

from django.core.serializers.json import DjangoJSONEncoder


def _clean(value):
    if value is None:
        return None
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, default=str))


def client_ip(request):
    if not request:
        return None
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")) or None


def log(action, entity, entity_id=None, *, user=None, old=None, new=None, summary="", request=None, actor_label=""):
    from hotel.models import AuditLog

    if not isinstance(entity, str):
        entity_id = entity.pk if entity_id is None else entity_id
        entity = entity.__class__.__name__
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    if request is not None and user is None and getattr(request, "user", None) is not None and request.user.is_authenticated:
        user = request.user
    return AuditLog.objects.create(
        user=user,
        actor_label=actor_label or ("" if user else "System"),
        action=action,
        entity_type=entity,
        entity_id=str(entity_id or ""),
        summary=summary[:255],
        old_value=_clean(old),
        new_value=_clean(new),
        ip_address=client_ip(request),
    )
