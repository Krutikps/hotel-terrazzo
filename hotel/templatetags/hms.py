from django import template

register = template.Library()


@register.filter
def can(user, perm):
    return bool(user and user.is_authenticated and user.can(perm))


@register.filter
def get_item(d, key):
    try:
        return d.get(key, "")
    except AttributeError:
        return ""


@register.filter
def money(v, symbol="₹"):
    try:
        return f"{symbol}{float(v):,.2f}"
    except (TypeError, ValueError):
        return v


@register.filter
def split(value, sep=","):
    return str(value).split(sep)
