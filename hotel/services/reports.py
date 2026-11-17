"""Reporting — every figure is computed from the database. Reports return a uniform dict so the
same data renders on screen and exports to CSV / Excel / PDF."""
import csv
import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from hotel.models import Payment, Reservation, ReservationLine, ReservationRoom, Room, RoomBlock, RoomType

Z = Decimal("0")
SOLD = (Reservation.CONFIRMED, Reservation.CHECKED_IN, Reservation.CHECKED_OUT)


def f2(x):
    return f"{Decimal(x):,.2f}"


def days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _sold_reservations(start, end, room_type_id=None, room_id=None):
    qs = Reservation.objects.filter(status__in=SOLD, check_in__lte=end, check_out__gt=start).select_related("room_type", "guest", "rate_plan")
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    if room_id:
        qs = qs.filter(rooms__room_id=room_id).distinct()
    return qs


def nightly_stats(start, end, room_type_id=None):
    """{date: {'sold': rooms, 'revenue': room revenue, 'by_type': {type_id: [sold, revenue]}}} for stay nights in [start, end]."""
    out = {d: {"sold": 0, "revenue": Z, "by_type": defaultdict(lambda: [0, Z])} for d in days(start, end)}
    for r in _sold_reservations(start, end, room_type_id):
        per_night = (r.room_charges + r.extra_guest_charges - r.discount_total) / max(r.nights, 1)
        for d in days(max(r.check_in, start), min(r.check_out - timedelta(days=1), end)):
            o = out[d]
            o["sold"] += r.room_count
            o["revenue"] += per_night
            bt = o["by_type"][r.room_type_id]
            bt[0] += r.room_count
            bt[1] += per_night
    return out


def available_per_day(start, end, room_type_id=None):
    rooms = Room.objects.filter(active=True)
    if room_type_id:
        rooms = rooms.filter(room_type_id=room_type_id)
    ids = list(rooms.values_list("id", flat=True))
    blocks = list(RoomBlock.objects.filter(room_id__in=ids, released_at__isnull=True, start_date__lte=end, end_date__gte=start))
    res = {}
    for d in days(start, end):
        res[d] = len(ids) - len({b.room_id for b in blocks if b.start_date <= d <= b.end_date})
    return res


def _pct(a, b):
    return (Decimal(a) * 100 / Decimal(b)).quantize(Decimal("0.1")) if b else Decimal("0.0")


# --------------------------------------------------------------------------- reports
def occupancy(start, end, room_type_id=None):
    stats = nightly_stats(start, end, room_type_id)
    avail = available_per_day(start, end, room_type_id)
    rows, tot_sold, tot_av, tot_rev = [], 0, 0, Z
    for d in days(start, end):
        s, a, rev = stats[d]["sold"], avail[d], stats[d]["revenue"]
        tot_sold += s
        tot_av += a
        tot_rev += rev
        rows.append([d.isoformat(), a, s, f"{_pct(s, a)}%", f2(rev), f2(rev / s) if s else "0.00", f2(rev / a) if a else "0.00"])
    return {"title": "Occupancy, ADR & RevPAR", "columns": ["Date", "Rooms available", "Rooms sold", "Occupancy", "Room revenue", "ADR", "RevPAR"], "rows": rows,
            "summary": {"Occupancy": f"{_pct(tot_sold, tot_av)}%", "ADR": f2(tot_rev / tot_sold) if tot_sold else "0.00",
                        "RevPAR": f2(tot_rev / tot_av) if tot_av else "0.00", "Room nights sold": tot_sold, "Room revenue": f2(tot_rev)}}


def revenue(start, end, room_type_id=None):
    stats = nightly_stats(start, end, room_type_id)
    pays = defaultdict(lambda: [Z, Z])
    for p in Payment.objects.filter(status=Payment.PAID, paid_at__date__gte=start, paid_at__date__lte=end):
        k = timezone.localtime(p.paid_at).date()
        pays[k][0 if p.kind == Payment.PAYMENT else 1] += p.amount
    created = defaultdict(int)
    for r in Reservation.objects.filter(created_at__date__gte=start, created_at__date__lte=end).exclude(status__in=(Reservation.RELEASED, Reservation.PENDING_PAYMENT)):
        created[timezone.localtime(r.created_at).date()] += 1
    rows, rr, cc, ref = [], Z, Z, Z
    for d in days(start, end):
        c, rf = pays[d]
        rr += stats[d]["revenue"]
        cc += c
        ref += rf
        rows.append([d.isoformat(), f2(stats[d]["revenue"]), created[d], f2(c), f2(rf), f2(c - rf)])
    return {"title": "Revenue", "columns": ["Date", "Room revenue (nights stayed)", "Bookings made", "Payments collected", "Refunds", "Net collected"], "rows": rows,
            "summary": {"Room revenue": f2(rr), "Collected": f2(cc), "Refunded": f2(ref), "Net collected": f2(cc - ref)}}


def _res_rows(qs):
    return [[r.reference, r.guest.name, r.room_type.name, r.assigned_room_numbers or "—", r.check_in.isoformat(), r.check_out.isoformat(), r.nights,
             r.get_status_display(), r.get_source_display(), f2(r.total), f2(r.amount_paid), f2(r.balance)] for r in qs]


RES_COLS = ["Booking", "Guest", "Room type", "Room", "Check-in", "Check-out", "Nights", "Status", "Source", "Total", "Paid", "Balance"]


def reservations(start, end, room_type_id=None):
    qs = Reservation.objects.filter(check_in__gte=start, check_in__lte=end).exclude(status__in=(Reservation.RELEASED, Reservation.PENDING_PAYMENT))
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    qs = qs.select_related("guest", "room_type").prefetch_related("rooms__room").order_by("check_in")
    by_status = qs.values("status").annotate(n=Count("id"))
    return {"title": "Reservations (by arrival date)", "columns": RES_COLS, "rows": _res_rows(qs),
            "summary": {dict(Reservation.STATUS)[x["status"]]: x["n"] for x in by_status}}


def cancellations(start, end, room_type_id=None):
    qs = Reservation.objects.filter(status=Reservation.CANCELLED, cancelled_at__date__gte=start, cancelled_at__date__lte=end)
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    qs = qs.select_related("guest", "room_type")
    rows = [[r.reference, r.guest.name, r.room_type.name, r.check_in.isoformat(), timezone.localtime(r.cancelled_at).strftime("%Y-%m-%d %H:%M"), f2(r.total), f2(r.amount_paid), r.cancellation_reason] for r in qs]
    return {"title": "Cancellations", "columns": ["Booking", "Guest", "Room type", "Check-in", "Cancelled at", "Total", "Retained", "Reason"], "rows": rows,
            "summary": {"Cancellations": len(rows), "Value cancelled": f2(sum((r.total for r in qs), Z)), "Fees retained": f2(sum((r.amount_paid for r in qs), Z))}}


def no_shows(start, end, room_type_id=None):
    qs = Reservation.objects.filter(status=Reservation.NO_SHOW, check_in__gte=start, check_in__lte=end).select_related("guest", "room_type")
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    return {"title": "No-shows", "columns": RES_COLS, "rows": _res_rows(qs), "summary": {"No-shows": qs.count()}}


def room_performance(start, end, room_type_id=None):
    rows = []
    rooms = Room.objects.filter(active=True).select_related("room_type")
    if room_type_id:
        rooms = rooms.filter(room_type_id=room_type_id)
    period = (end - start).days + 1
    sold = defaultdict(lambda: [0, Z])
    for rr in ReservationRoom.objects.filter(reservation__status__in=SOLD, reservation__check_in__lte=end, reservation__check_out__gt=start, room__isnull=False).select_related("reservation"):
        r = rr.reservation
        n = len(list(days(max(r.check_in, start), min(r.check_out - timedelta(days=1), end))))
        per_room = (r.room_charges + r.extra_guest_charges - r.discount_total) / max(r.room_count, 1)
        sold[rr.room_id][0] += n
        sold[rr.room_id][1] += per_room / max(r.nights, 1) * n
    for room in rooms:
        n, rev = sold[room.id]
        rows.append([room.room_number, room.room_type.name, n, f"{_pct(n, period)}%", f2(rev)])
    by_type = defaultdict(lambda: [0, Z])
    for d, o in nightly_stats(start, end, room_type_id).items():
        for tid, (n, rev) in o["by_type"].items():
            by_type[tid][0] += n
            by_type[tid][1] += rev
    names = dict(RoomType.objects.values_list("id", "name"))
    summary = {f"{names[t]}": f"{n} nights · {f2(rev)}" for t, (n, rev) in by_type.items()}
    return {"title": "Room performance", "columns": ["Room", "Type", "Nights sold (assigned)", "Occupancy", "Revenue"], "rows": rows, "summary": summary}


def rate_plan_performance(start, end, room_type_id=None):
    qs = Reservation.objects.filter(status__in=SOLD, check_in__gte=start, check_in__lte=end)
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    agg = qs.values("rate_plan__room_type__name", "rate_plan__name").annotate(n=Count("id"), nights=Sum("nights"), rev=Sum("total")).order_by("-rev")
    rows = [[a["rate_plan__room_type__name"], a["rate_plan__name"], a["n"], a["nights"], f2(a["rev"] or 0), f2((a["rev"] or 0) / a["n"])] for a in agg]
    return {"title": "Rate plan performance", "columns": ["Room type", "Rate plan", "Bookings", "Nights", "Revenue (incl. tax)", "Avg booking value"], "rows": rows, "summary": {"Bookings": sum(r[2] for r in rows)}}


def guest_stats(start, end, room_type_id=None):
    qs = Reservation.objects.filter(status__in=SOLD, check_in__gte=start, check_in__lte=end).select_related("guest")
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    agg = defaultdict(lambda: [0, 0, Z])
    for r in qs:
        a = agg[r.guest_id]
        a[0] += 1
        a[1] += r.nights
        a[2] += r.total
    from hotel.models import Guest

    guests = {g.id: g for g in Guest.objects.filter(id__in=agg.keys())}
    repeat = sum(1 for gid in agg if Reservation.objects.filter(guest_id=gid, status__in=SOLD, check_in__lt=start).exists() or agg[gid][0] > 1)
    rows = sorted(([guests[g].name, guests[g].email, guests[g].country or "—", v[0], v[1], f2(v[2])] for g, v in agg.items()), key=lambda r: -Decimal(r[5].replace(",", "")))
    return {"title": "Guest statistics", "columns": ["Guest", "Email", "Country", "Bookings", "Nights", "Spend"], "rows": rows,
            "summary": {"Unique guests": len(agg), "Repeat guests": repeat, "New guests": len(agg) - repeat}}


def pending_payments(start, end, room_type_id=None):
    qs = Reservation.objects.filter(status__in=(Reservation.CONFIRMED, Reservation.CHECKED_IN, Reservation.CHECKED_OUT), check_in__lte=end, check_out__gte=start)
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    qs = [r for r in qs.select_related("guest", "room_type") if r.balance > 0]
    rows = [[r.reference, r.guest.name, r.guest.phone, r.check_in.isoformat(), r.get_status_display(), f2(r.total), f2(r.amount_paid), f2(r.balance)] for r in qs]
    return {"title": "Pending payments", "columns": ["Booking", "Guest", "Phone", "Check-in", "Status", "Total", "Paid", "Balance due"], "rows": rows,
            "summary": {"Bookings with balance": len(rows), "Total outstanding": f2(sum((r.balance for r in qs), Z))}}


def tax_collection(start, end, room_type_id=None):
    qs = ReservationLine.objects.filter(category="TAX", reservation__status__in=SOLD, reservation__check_in__gte=start, reservation__check_in__lte=end)
    if room_type_id:
        qs = qs.filter(reservation__room_type_id=room_type_id)
    agg = defaultdict(lambda: Z)
    for ln in qs:
        agg[ln.description.split(" on ")[0]] += ln.amount
    rows = [[k, f2(v)] for k, v in sorted(agg.items())]
    return {"title": "Tax collection (by arrival date)", "columns": ["Tax", "Amount"], "rows": rows, "summary": {"Total tax": f2(sum(agg.values(), Z))}}


def booking_source(start, end, room_type_id=None):
    qs = Reservation.objects.filter(check_in__gte=start, check_in__lte=end).exclude(status__in=(Reservation.RELEASED, Reservation.PENDING_PAYMENT))
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    agg = qs.values("source").annotate(n=Count("id"), rev=Sum("total"))
    rows = [[dict(Reservation.SOURCES)[a["source"]], a["n"], f2(a["rev"] or 0)] for a in agg]
    return {"title": "Booking sources", "columns": ["Source", "Bookings", "Value"], "rows": rows, "summary": {"Bookings": sum(r[1] for r in rows)}}


def average_stay(start, end, room_type_id=None):
    qs = Reservation.objects.filter(status__in=SOLD, check_in__gte=start, check_in__lte=end)
    if room_type_id:
        qs = qs.filter(room_type_id=room_type_id)
    dist = defaultdict(int)
    for n in qs.values_list("nights", flat=True):
        dist[n] += 1
    total = sum(dist.values())
    avg = Decimal(sum(k * v for k, v in dist.items())) / total if total else Z
    rows = [[f"{k} night(s)", v, f"{_pct(v, total)}%"] for k, v in sorted(dist.items())]
    return {"title": "Average length of stay", "columns": ["Length", "Bookings", "Share"], "rows": rows, "summary": {"Average stay (nights)": f"{avg:.2f}", "Bookings": total}}


REPORTS = {
    "occupancy": ("Occupancy / ADR / RevPAR", occupancy), "revenue": ("Revenue", revenue), "reservations": ("Reservations", reservations),
    "cancellations": ("Cancellations", cancellations), "no_shows": ("No-shows", no_shows), "room_performance": ("Room performance", room_performance),
    "rate_plans": ("Rate plan performance", rate_plan_performance), "guests": ("Guest statistics", guest_stats),
    "pending": ("Pending payments", pending_payments), "tax": ("Tax collection", tax_collection),
    "sources": ("Booking source", booking_source), "avg_stay": ("Average stay", average_stay),
}


def run(key, start, end, room_type_id=None):
    return REPORTS[key][1](start, end, room_type_id or None)


# --------------------------------------------------------------------------- exports
def to_csv(rep):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([rep["title"]])
    for k, v in rep["summary"].items():
        w.writerow([k, v])
    w.writerow([])
    w.writerow(rep["columns"])
    w.writerows(rep["rows"])
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(rep):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = rep["title"][:30]
    ws.append([rep["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in rep["summary"].items():
        ws.append([k, v])
    ws.append([])
    ws.append(rep["columns"])
    hdr = ws.max_row
    for c in ws[hdr]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="F6F1EA")
    for r in rep["rows"]:
        ws.append(r)
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(max(len(str(c.value or "")) for c in col) + 2, 40)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(rep, subtitle=""):
    from .documents import table_pdf

    sub = subtitle + "   " + " · ".join(f"{k}: {v}" for k, v in rep["summary"].items())
    return table_pdf(rep["title"], sub, rep["columns"], rep["rows"])


# --------------------------------------------------------------------------- dashboard
def room_display_status(room, blocked_ids, arriving_room_ids):
    if not room.active:
        return "INACTIVE"
    if room.status in (Room.OCCUPIED, Room.MAINTENANCE, Room.OUT_OF_ORDER, Room.BLOCKED):
        return room.status
    if room.id in blocked_ids:
        return Room.BLOCKED
    if room.status in (Room.DIRTY, Room.CLEANING):
        return room.status
    if room.id in arriving_room_ids:
        return Room.RESERVED
    return Room.AVAILABLE if room.status in (Room.CLEAN, Room.INSPECTED, Room.AVAILABLE, Room.RESERVED) else room.status


def today_room_states(today=None):
    today = today or timezone.localdate()
    blocked = set(RoomBlock.objects.filter(released_at__isnull=True, start_date__lte=today, end_date__gte=today).values_list("room_id", flat=True))
    arriving = set(ReservationRoom.objects.filter(reservation__status=Reservation.CONFIRMED, reservation__check_in=today, room__isnull=False).values_list("room_id", flat=True))
    return {r.id: room_display_status(r, blocked, arriving) for r in Room.objects.filter(active=True)}, blocked


def dashboard(today=None):
    today = today or timezone.localdate()
    states, _ = today_room_states(today)
    counts = defaultdict(int)
    for s in states.values():
        counts[s] += 1
    total_rooms = len(states)
    in_house = Reservation.objects.filter(status=Reservation.CHECKED_IN)
    arrivals = Reservation.objects.filter(check_in=today, status__in=(Reservation.CONFIRMED, Reservation.CHECKED_IN))
    departures = Reservation.objects.filter(check_out=today, status__in=(Reservation.CHECKED_IN, Reservation.CHECKED_OUT))
    pend = [r for r in Reservation.objects.filter(status__in=(Reservation.CONFIRMED, Reservation.CHECKED_IN)) if r.balance > 0]
    pays = Payment.objects.filter(status=Payment.PAID, paid_at__date=today)
    revenue_today = sum((p.amount for p in pays.filter(kind=Payment.PAYMENT)), Z) - sum((p.amount for p in pays.filter(kind=Payment.REFUND)), Z)
    sellable = total_rooms - counts[Room.MAINTENANCE] - counts[Room.OUT_OF_ORDER] - counts[Room.BLOCKED]
    st = nightly_stats(today, today)[today]
    mstart = today.replace(day=1)
    mstats = nightly_stats(mstart, today)
    m_sold = sum(v["sold"] for v in mstats.values())
    m_rev = sum((v["revenue"] for v in mstats.values()), Z)
    m_av = sum(available_per_day(mstart, today).values())
    return {
        "arrivals": arrivals.count(), "arrivals_pending": arrivals.filter(status=Reservation.CONFIRMED).count(),
        "departures": departures.count(), "departures_pending": departures.filter(status=Reservation.CHECKED_IN).count(),
        "current_guests": sum(r.adults + r.children for r in in_house), "in_house": in_house.count(),
        "available": counts[Room.AVAILABLE], "occupied": counts[Room.OCCUPIED], "blocked": counts[Room.BLOCKED],
        "maintenance": counts[Room.MAINTENANCE] + counts[Room.OUT_OF_ORDER], "dirty": counts[Room.DIRTY] + counts[Room.CLEANING],
        "reserved": counts[Room.RESERVED], "total_rooms": total_rooms,
        "pending_payments": len(pend), "pending_amount": sum((r.balance for r in pend), Z), "revenue_today": revenue_today,
        "occupancy_pct": _pct(counts[Room.OCCUPIED], sellable), "adr": (st["revenue"] / st["sold"]) if st["sold"] else Z,
        "revpar": (st["revenue"] / sellable) if sellable else Z,
        "mtd_revenue": m_rev, "mtd_adr": (m_rev / m_sold) if m_sold else Z, "mtd_revpar": (m_rev / m_av) if m_av else Z,
        "mtd_occupancy": _pct(m_sold, m_av),
        "mtd_bookings": Reservation.objects.filter(created_at__date__gte=mstart).exclude(status__in=(Reservation.RELEASED, Reservation.PENDING_PAYMENT)).count(),
        "mtd_cancellations": Reservation.objects.filter(status=Reservation.CANCELLED, cancelled_at__date__gte=mstart).count(),
    }


def dashboard_charts(today=None, span=14):
    today = today or timezone.localdate()
    start = today - timedelta(days=span - 1)
    stats = nightly_stats(start, today)
    avail = available_per_day(start, today)
    created = defaultdict(int)
    for r in Reservation.objects.filter(created_at__date__gte=start).exclude(status__in=(Reservation.RELEASED, Reservation.PENDING_PAYMENT)):
        created[timezone.localtime(r.created_at).date()] += 1
    labels = [d.strftime("%d %b") for d in days(start, today)]
    types = dict(RoomType.objects.values_list("id", "name"))
    tperf = defaultdict(Decimal)
    for o in stats.values():
        for tid, (n, rev) in o["by_type"].items():
            tperf[tid] += rev
    src = Reservation.objects.filter(created_at__date__gte=start).exclude(status__in=(Reservation.RELEASED, Reservation.PENDING_PAYMENT)).values("source").annotate(n=Count("id"))
    return {
        "labels": labels,
        "occupancy": [float(_pct(stats[d]["sold"], avail[d])) for d in days(start, today)],
        "revenue": [float(stats[d]["revenue"]) for d in days(start, today)],
        "reservations": [created[d] for d in days(start, today)],
        "room_types": {"labels": [types[t] for t in tperf], "values": [float(v) for v in tperf.values()]},
        "sources": {"labels": [dict(Reservation.SOURCES)[s["source"]] for s in src], "values": [s["n"] for s in src]},
    }
