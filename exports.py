"""Excel and PDF exports for owners and managers."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import analytics

HEAD_FILL = PatternFill("solid", fgColor="1F1B16")
HEAD_FONT = Font(bold=True, color="F4B223")


def _sheet(wb, title, headers, rows, widths=None, money_cols=()):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append(r)
    for col in money_cols:
        for cell in ws[get_column_letter(col)][1:]:
            cell.number_format = '"₱"#,##0.00'
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, max(12, len(h) + 3))
    ws.freeze_panes = "A2"
    return ws


def build_excel(branch_name, logs, items, th, forecast, reorder, profit, periods):
    wb = Workbook()
    wb.remove(wb.active)
    rows = []
    for d, e in logs.items():
        key, label = analytics.tier_for(e["sales"], th) if e["sales"] > 0 else ("", "")
        rows.append([d, analytics.DAYS[analytics.d2date(d).weekday()], e["sales"], label, e["notes"], e["updated_by"]])
    _sheet(wb, "Daily sales", ["Date", "Day", "Sales", "Tier", "Notes", "Updated by"], rows, {5: 40}, money_cols=(3,))

    codes = [i["code"] for i in items]
    stock_rows = []
    for d, e in logs.items():
        for c in codes:
            v = e["items"].get(c)
            if v:
                stock_rows.append([d, c, v["opening"], v["stocks"], v["starting"], v["usage"], v["ending"], v["closing"], v["restock"]])
    _sheet(wb, "Stock log", ["Date", "Item", "Opening", "Stocks", "Starting", "Usage", "End", "Closing", "Bought"], stock_rows)

    _sheet(wb, "Profit", ["Date", "Sales", "Ingredient cost", "Gross", "After operating cost"],
           [[x["date"], x["sales"], x["cogs"], x["gross"], x["net"]] for x in profit["days"]], money_cols=(2, 3, 4, 5))
    _sheet(wb, "Periods", ["Period", "From", "To", "Days", "Sales", "Avg / day", "Best day", "Best sales"],
           [[p["period"], p["from"], p["to"], p["days"], p["sales"], p["avg"], p["best_day"], p["best_sales"]]
            for p in periods], money_cols=(5, 6, 8))
    if forecast and forecast["forecast"]:
        _sheet(wb, "Forecast", ["Date", "Forecast", "Low", "High", "Tier", "Note"],
               [[f["date"], f["yhat"], f["lower"], f["upper"], analytics.tier_for(f["yhat"], th)[1],
                 f["holiday"] or ("Payday" if f["payday"] else ("Event" if f["event"] else ""))]
                for f in forecast["forecast"]], money_cols=(2, 3, 4))
    _sheet(wb, "Reorder", ["Item", "In stock", "Avg daily use", "Days left", "Status", "Order qty"],
           [[r["name"], r["stock"], r["avg_daily"], r["days_cover"], r["status"], r["order_qty"]]
            for r in reorder["rows"]])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_pdf(branch_name, stats, periods, forecast, th, reorder, anomalies, profit, generated):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=f"{branch_name} report")
    ss = getSampleStyleSheet()
    h1, h2, body = ss["Title"], ss["Heading2"], ss["BodyText"]
    story = [Paragraph(f"{branch_name}: business report", h1),
             Paragraph(f"Generated {generated}", body), Spacer(1, 6)]

    def table(data, widths=None):
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F1B16")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#F4B223")),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C9CCD1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")]),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ]))
        return t

    t = profit["totals"]
    story += [Paragraph("Summary", h2), Paragraph(
        f"Last 7 logged days average ₱{stats['avg']:,.0f} per day"
        + (f" ({stats['change_pct']:+.1f}% vs the week before)." if stats["change_pct"] is not None else ".")
        + f" Logged period: {t['days']} days, sales ₱{t['sales']:,.0f}."
        + (f" Gross margin {t['margin_pct']}%; after operating cost ₱{t['net']:,.0f}." if profit["costs_configured"]
           else " Item costs are not set, so margin is not shown."), body)]

    if periods:
        story += [Paragraph("Weekly results", h2), table(
            [["Week", "Days", "Sales", "Avg / day", "Best day"]] +
            [[p["period"], p["days"], f"₱{p['sales']:,.0f}", f"₱{p['avg']:,.0f}", f"{p['best_day']} (₱{p['best_sales']:,.0f})"]
             for p in periods[:8]])]
    if forecast and forecast["forecast"]:
        nxt = forecast["forecast"][:7]
        story += [Paragraph(f"Next 7 days ({forecast['model_label']})", h2), table(
            [["Date", "Forecast", "Range", "Tier"]] +
            [[f["date"], f"₱{f['yhat']:,.0f}", f"₱{f['lower']:,.0f} to ₱{f['upper']:,.0f}",
              analytics.tier_for(f["yhat"], th)[1]] for f in nxt])]
    todo = [r for r in reorder["rows"] if r["order_qty"] > 0]
    story += [Paragraph("Reorder list", h2)]
    story += [table([["Item", "In stock", "Days left", "Order"]] +
                    [[r["name"], f"{r['stock']:g}", r["days_cover"] if r["days_cover"] is not None else "-",
                      f"{r['order_qty']} {r['unit']}"] for r in todo])] if todo else [Paragraph("Nothing to order right now.", body)]
    if anomalies:
        story += [Paragraph("Things to check", h2)] + [
            Paragraph(f"<b>{a['date']}</b>: {a['title']}. {a['detail']}", body) for a in anomalies[:8]]
    doc.build(story)
    buf.seek(0)
    return buf
