"""PDF generation (booking confirmation, invoice, generic report tables) using ReportLab."""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor("#0E283C")      # brand navy
ACCENT = colors.HexColor("#BE9D61")   # brand gold
SAND = colors.HexColor("#F4F0EE")     # brand cream
_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Title"], fontName="Helvetica-Bold", fontSize=20, textColor=INK, alignment=0, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName="Helvetica-Bold", fontSize=11, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
P = ParagraphStyle("p", parent=_ss["Normal"], fontName="Helvetica", fontSize=9, leading=12, textColor=INK)
SMALL = ParagraphStyle("s", parent=P, fontSize=8, textColor=colors.HexColor("#6b6b6b"))


def m(v, sym="INR "):
    """ReportLab's core fonts lack the rupee glyph, so PDFs use the currency code."""
    from decimal import Decimal

    return f"{sym}{Decimal(str(v)):,.2f}"


def _table(rows, widths, header=True, align_right_from=None, total_rows=()):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
             ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9d2c5"))]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), SAND), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5)]
    if align_right_from is not None:
        style.append(("ALIGN", (align_right_from, 0), (-1, -1), "RIGHT"))
    for r in total_rows:
        style += [("FONT", (0, r), (-1, r), "Helvetica-Bold", 9)]
    t.setStyle(TableStyle(style))
    return t


def _header(h, title):
    addr = ", ".join(x for x in [h.address, h.city, h.state, h.postal_code] if x)
    left = [Paragraph(h.name, H1), Paragraph(addr, SMALL), Paragraph(f"{h.phone}  ·  {h.email}" + (f"  ·  GSTIN {h.gstin}" if h.gstin else ""), SMALL)]
    right = Paragraph(f"<b>{title}</b>", ParagraphStyle("r", parent=H1, alignment=2, fontSize=14, textColor=ACCENT))
    t = Table([[left, right]], colWidths=[110 * mm, 70 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT), ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))
    return t


def confirmation_pdf(res):
    h = res.hotel
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Booking {res.reference}")
    s = [_header(h, "BOOKING CONFIRMATION"), Spacer(1, 8)]
    s.append(Paragraph(f"Booking number: <b>{res.reference}</b> &nbsp;&nbsp; Status: <b>{res.get_status_display()}</b> &nbsp;&nbsp; Payment: <b>{res.get_payment_status_display()}</b>", P))
    s.append(Paragraph("Guest", H2))
    g = res.guest
    s.append(Paragraph(f"{g.name}<br/>{g.email}  {g.phone}", P))
    s.append(Paragraph("Stay", H2))
    rows = [["Hotel", h.name], ["Room category", res.room_type.name], ["Rate plan", res.rate_plan.name],
            ["Check-in", f"{res.check_in:%A, %d %B %Y} (from {h.check_in_time:%H:%M})"],
            ["Check-out", f"{res.check_out:%A, %d %B %Y} (until {h.check_out_time:%H:%M})"],
            ["Nights", str(res.nights)], ["Guests", f"{res.adults} adult(s), {res.children} child(ren)"], ["Rooms", str(res.room_count)]]
    if res.purpose:
        rows.append(["Purpose of stay", res.purpose])
    s.append(_table(rows, [40 * mm, 140 * mm], header=False))
    s.append(Paragraph("Charges", H2))
    rows = [["Description", "Amount"]] + [[ln.description, m(ln.amount)] for ln in res.lines.all()]
    rows += [["Total", m(res.total)], ["Amount paid", m(res.amount_paid)], ["Balance due", m(res.balance)]]
    s.append(_table(rows, [140 * mm, 40 * mm], align_right_from=1, total_rows=(len(rows) - 3, len(rows) - 2, len(rows) - 1)))
    reqs = [r.request_type for r in res.requests.all()]
    if reqs or res.special_requests:
        s.append(Paragraph("Special requests", H2))
        s.append(Paragraph(", ".join(reqs) + ("<br/>" if reqs and res.special_requests else "") + (res.special_requests or ""), P))
    s.append(Paragraph("Cancellation policy", H2))
    s.append(Paragraph(res.cancellation_policy or "—", P))
    doc.build(s)
    return buf.getvalue()


def invoice_pdf(inv):
    d = inv.data
    hh = d["hotel"]
    sym = "INR "

    class H:  # tiny shim so _header can reuse hotel-like data
        name, address, city, state, postal_code, phone, email, gstin = (hh["name"], hh["address"], hh["city"], hh["state"], hh["postal_code"], hh["phone"], hh["email"], hh["gstin"])

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm, title=f"Invoice {inv.number}")
    s = [_header(H, "TAX INVOICE"), Spacer(1, 8)]
    s.append(Paragraph(f"Invoice no: <b>{inv.number}</b> &nbsp; Date: {inv.issued_at:%d %b %Y} &nbsp; Booking: <b>{d['booking_number']}</b>", P))
    s.append(Paragraph("Billed to", H2))
    g = d["guest"]
    s.append(Paragraph(f"{g['name']}<br/>{g['address']}<br/>{g['email']} {g['phone']}", P))
    st = d["stay"]
    s.append(Paragraph("Stay", H2))
    s.append(Paragraph(f"{st['room_type']} ({st['rate_plan']}) — Room(s): {st['rooms']}<br/>{st['check_in']} → {st['check_out']} · {st['nights']} night(s) · {st['adults']} adult(s), {st['children']} child(ren)", P))
    s.append(Paragraph("Charges", H2))
    rows = [["Description", "Qty", "Amount"]]
    for l in d["lines"]:
        rows.append([l["description"], f"{float(l['quantity']):g}", m(l["amount"], sym)])
    rows += [["", "Total", m(d["total"], sym)], ["", "Paid", m(d["paid"], sym)], ["", "Balance", m(d["balance"], sym)]]
    s.append(_table(rows, [115 * mm, 20 * mm, 45 * mm], align_right_from=1, total_rows=(len(rows) - 3, len(rows) - 2, len(rows) - 1)))
    if d["payments"]:
        s.append(Paragraph("Payments", H2))
        rows = [["Date", "Method", "Reference", "Amount"]] + [
            [p["date"][:10], p["method"] + (" (refund)" if p["kind"] == "REFUND" else ""), p["reference"], m(("-" if p["kind"] == "REFUND" else "") + str(p["amount"]), sym)]
            for p in d["payments"]]
        s.append(_table(rows, [30 * mm, 45 * mm, 60 * mm, 45 * mm], align_right_from=3))
    s.append(Spacer(1, 10))
    s.append(Paragraph("This is a computer-generated invoice and does not require a signature.", SMALL))
    doc.build(s)
    return buf.getvalue()


def table_pdf(title, subtitle, columns, rows):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm, title=title)
    data = [columns] + [[str(c) for c in r] for r in rows]
    width = (297 - 24) * mm / max(len(columns), 1)
    s = [Paragraph(title, H1), Paragraph(subtitle, SMALL), Spacer(1, 8), _table(data, [width] * len(columns))]
    doc.build(s)
    return buf.getvalue()
