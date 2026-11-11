"""Business analytics on top of the daily logs."""
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

import config
import forecasting

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
CUP_CODES = ("M", "G", "J", "MG", "2XL", "3XL")
Z80 = 1.2816


def d2date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def tier_for(amount, th):
    labels = dict(config.TIERS)
    if amount < th["deficit"]:
        key = "deficit"
    elif amount < th["under"]:
        key = "bad"
    elif amount < th["below"]:
        key = "below-avg"
    elif amount <= th["high"]:
        key = "acceptable"
    else:
        key = "successful"
    return key, labels[key]


# ---------------------------------------------------------------- patterns
def weekday_profile(logs):
    buckets = defaultdict(list)
    for d, e in logs.items():
        if e["sales"] > 0:
            buckets[d2date(d).weekday()].append(e["sales"])
    rows = [{"dow": i, "day": DAYS[i], "avg": round(sum(v) / len(v), 2) if v else 0, "days": len(v)}
            for i, v in ((i, buckets.get(i, [])) for i in range(7))]
    best = max((r for r in rows if r["days"]), key=lambda r: r["avg"], default=None)
    return {"rows": rows, "best_day": best["day"] if best else None}


def sales_stats(logs, today, days=7):
    """Average over the last `days` logged days and the change vs the prior window."""
    vals = [(d, e["sales"]) for d, e in logs.items() if e["sales"] > 0 and d2date(d) <= today]
    last = [v for _, v in vals[-days:]]
    prev = [v for _, v in vals[-2 * days:-days]]
    avg = sum(last) / len(last) if last else 0
    pavg = sum(prev) / len(prev) if prev else None
    change = ((avg - pavg) / pavg * 100) if pavg else None
    return {"avg": round(avg, 2), "prev_avg": round(pavg, 2) if pavg else None,
            "change_pct": round(change, 1) if change is not None else None, "days": len(last)}


# --------------------------------------------------------------- anomalies
def _robust_z(x, peers):
    med = statistics.median(peers)
    mad = statistics.median([abs(p - med) for p in peers]) * 1.4826
    if mad <= 0:
        mad = statistics.pstdev(peers) or 0
    return (x - med) / mad if mad else 0.0, med


def find_anomalies(logs, today, limit=25):
    out = []
    dates = sorted(logs)
    by_dow = defaultdict(list)
    for d in dates:
        if logs[d]["sales"] > 0:
            by_dow[d2date(d).weekday()].append((d, logs[d]["sales"]))

    # 1) Sales far from the usual for that weekday
    for dow, rows in by_dow.items():
        for d, s in rows:
            peers = [x for dd, x in rows if dd != d]
            if len(peers) < 4:
                continue
            z, med = _robust_z(s, peers)
            if abs(z) >= 3:
                high = z > 0
                out.append({"date": d, "severity": "info" if high else "warn", "type": "sales",
                            "title": "Unusually strong sales" if high else "Unusually weak sales",
                            "detail": f"₱{s:,.0f} on a {DAYS[dow]}; a typical {DAYS[dow]} is about ₱{med:,.0f}. "
                                      "Check for an event, promo, weather or a data entry slip."})

    # 2) Cups used vs sales: a big mismatch can mean waste, miscounts or unrecorded sales
    ratios = []
    for d in dates:
        e = logs[d]
        cups = sum((e["items"][c]["usage"] or 0) for c in CUP_CODES if c in e["items"])
        if e["sales"] > 0 and cups > 0:
            ratios.append((d, e["sales"] / cups, cups))
    if len(ratios) >= 8:
        vals = [r for _, r, _ in ratios]
        for d, r, cups in ratios:
            peers = [x for dd, x, _ in ratios if dd != d]
            z, med = _robust_z(r, peers)
            if abs(z) >= 3 and abs(r - med) / med > 0.25:
                low = r < med
                out.append({"date": d, "severity": "alert" if low else "warn", "type": "usage_mismatch",
                            "title": "Cups used don't match sales" if low else "Sales high for cups used",
                            "detail": f"₱{r:,.0f} per cup vs the usual ₱{med:,.0f} ({cups:,.0f} cups used). "
                                      + ("Possible waste, spoilage, miscount or unrecorded sales."
                                         if low else "Usage may be under-counted.")})

    # 3) Data quality (recent days only, so old history doesn't flood the list)
    for d in dates:
        e = logs[d]
        recent = (today - d2date(d)).days <= 30
        if recent:
            neg = [c for c, v in e["items"].items() if v["ending"] is not None and v["ending"] < 0]
            if neg:
                out.append({"date": d, "severity": "warn", "type": "data",
                            "title": "Stock count went below zero",
                            "detail": f"{', '.join(neg)}: usage was higher than the starting count. "
                                      "Recount, or add the purchase to Starting (for example 53+50)."})
            if d2date(d) < today and any(v["usage"] is None for v in e["items"].values()) and e["sales"] > 0:
                miss = [c for c, v in e["items"].items() if v["usage"] is None]
                out.append({"date": d, "severity": "info", "type": "data", "title": "Usage not entered",
                            "detail": f"No usage for {', '.join(miss[:6])}{'…' if len(miss) > 6 else ''}, so stock and forecasts skip them for this day."})
        if e["sales"] <= 0 and e["items"] and d2date(d) < today and any(v["usage"] is not None for v in e["items"].values()):
            out.append({"date": d, "severity": "warn", "type": "data", "title": "Sales not entered",
                        "detail": "Stock was logged but the day's sales total is 0."})
    if len(dates) >= 2:
        first, last = d2date(dates[0]), d2date(dates[-1])
        have = {d2date(d) for d in dates}
        missing = [first + timedelta(days=i) for i in range((last - first).days + 1)
                   if first + timedelta(days=i) not in have]
        recent = [m for m in missing if (today - m).days <= 30]
        if recent:
            out.append({"date": recent[-1].isoformat(), "severity": "info", "type": "gaps",
                        "title": f"{len(recent)} day(s) with no log in the last 30 days",
                        "detail": "Missing days: " + ", ".join(m.strftime("%b %d") for m in recent[:8])
                                  + ("…" if len(recent) > 8 else "")})
    out.sort(key=lambda a: a["date"], reverse=True)
    return out[:limit]


# ------------------------------------------------------------------ profit
def profit_report(logs, items, op_cost, start=None, end=None):
    by_code = {i["code"]: i for i in items}
    costs_set = any(i["unit_cost"] > 0 for i in items)
    days, tot = [], {"sales": 0.0, "cogs": 0.0}
    units = defaultdict(float)
    for d, e in logs.items():
        if (start and d < start) or (end and d > end) or e["sales"] <= 0:
            continue
        cogs = sum((v["usage"] or 0) * by_code[c]["unit_cost"] for c, v in e["items"].items() if c in by_code)
        for c, v in e["items"].items():
            units[c] += v["usage"] or 0
        gross = e["sales"] - cogs
        days.append({"date": d, "sales": round(e["sales"], 2), "cogs": round(cogs, 2),
                     "gross": round(gross, 2), "net": round(gross - op_cost, 2)})
        tot["sales"] += e["sales"]
        tot["cogs"] += cogs
    n = len(days)
    gross = tot["sales"] - tot["cogs"]
    margin = gross / tot["sales"] if tot["sales"] else 0
    breakeven = (op_cost / margin) if (costs_set and margin > 0) else op_cost
    items_out = []
    for c, u in units.items():
        it = by_code.get(c)
        if not it:
            continue
        rev = u * it["unit_price"]
        cost = u * it["unit_cost"]
        items_out.append({"code": c, "name": it["name"], "units": round(u, 1), "unit": it["unit"],
                          "est_revenue": round(rev, 2), "est_margin": round(rev - cost, 2)})
    items_out.sort(key=lambda r: r["units"], reverse=True)
    return {
        "costs_configured": costs_set,
        "days": days,
        "totals": {"days": n, "sales": round(tot["sales"], 2), "cogs": round(tot["cogs"], 2),
                   "gross": round(gross, 2), "margin_pct": round(margin * 100, 1),
                   "op_cost": round(op_cost * n, 2), "net": round(gross - op_cost * n, 2),
                   "avg_net_per_day": round((gross - op_cost * n) / n, 2) if n else 0},
        "breakeven_sales_per_day": round(breakeven, 2),
        "days_below_breakeven": sum(1 for x in days if x["sales"] < breakeven),
        "best_sellers": items_out[:10],
    }


# ----------------------------------------------------------------- reorder
STATUS_ORDER = {"critical": 0, "reorder": 1, "ok": 2, "unknown": 3}


def _latest_counts(logs):
    """code -> (date, ending) from the most recent day each item's End was counted."""
    latest = {}
    for d in sorted(logs):
        for code, v in logs[d]["items"].items():
            if v["ending"] is not None:
                latest[code] = (d, v["ending"])
    return latest


def reorder_suggestions(logs, items, settings):
    latest = _latest_counts(logs)
    if not latest:
        return {"stock_date": None, "rows": []}
    stock_date = max(d for d, _ in latest.values())
    cover = max(1, int(float(settings.get("cover_days", 7))))
    rows = []
    for it in items:
        code = it["code"]
        if code not in latest:
            continue
        d_str, ending = latest[code]
        last, stock = d2date(d_str), max(float(ending), 0.0)
        lead = max(1, int(it["lead_time_days"]))
        horizon = lead + cover
        dates = [last + timedelta(days=i) for i in range(1, horizon + 1)]
        pred, sigma = forecasting.forecast_item_usage(logs, code, dates)
        row = {"code": code, "name": it["name"], "unit": it["unit"], "stock": stock, "counted": d_str,
               "lead_days": lead, "status": "unknown", "avg_daily": None, "days_cover": None,
               "order_qty": 0, "stockout_date": None, "reorder_point": None}
        if pred is not None:
            avg = sum(pred[:7]) / min(7, len(pred))
            demand_lead, demand_cover = sum(pred[:lead]), sum(pred[:horizon])
            safety = Z80 * sigma * math.sqrt(lead)
            rop = max(demand_lead + safety, float(it["reorder_point"] or 0))
            if avg <= 0.01:
                status = "ok"
            elif stock < demand_lead:
                status = "critical"
            elif stock <= rop:
                status = "reorder"
            else:
                status = "ok"
            days_cover = stock / avg if avg > 0.01 else None
            row.update(
                status=status, avg_daily=round(avg, 1), reorder_point=round(rop, 1),
                days_cover=round(days_cover, 1) if days_cover is not None else None,
                order_qty=int(math.ceil(max(0.0, demand_cover + safety - stock))) if status != "ok" else 0,
                stockout_date=(last + timedelta(days=int(days_cover))).isoformat() if days_cover is not None else None)
        rows.append(row)
    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r["days_cover"] if r["days_cover"] is not None else 1e9))
    return {"stock_date": stock_date, "rows": rows}


def order_message(branch_name, stock_date, rows):
    todo = [r for r in rows if r["order_qty"] > 0]
    if not todo:
        return ""
    lines = [f"Stock order request - {branch_name}", f"Counted on {stock_date}", ""]
    lines += [f"- {r['name']}: {r['order_qty']} {r['unit']}" for r in todo]
    return "\n".join(lines)


# --------------------------------------------------------------- summaries
def period_summary(logs, th, items, period="week", count=8):
    """Aggregate by ISO week or calendar month, newest first."""
    by_code = {i["code"]: i for i in items}
    groups = defaultdict(list)
    for d, e in logs.items():
        if e["sales"] <= 0:
            continue
        dt = d2date(d)
        key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}" if period == "week" else dt.strftime("%Y-%m")
        groups[key].append((d, e))
    out = []
    for key in sorted(groups, reverse=True)[:count]:
        rows = groups[key]
        sales = [e["sales"] for _, e in rows]
        best = max(rows, key=lambda r: r[1]["sales"])
        tiers = defaultdict(int)
        for s in sales:
            tiers[tier_for(s, th)[0]] += 1
        cogs = sum((v["usage"] or 0) * by_code[c]["unit_cost"] for _, e in rows
                   for c, v in e["items"].items() if c in by_code)
        out.append({"period": key, "from": rows[0][0], "to": rows[-1][0], "days": len(rows),
                    "sales": round(sum(sales), 2), "avg": round(sum(sales) / len(sales), 2),
                    "best_day": best[0], "best_sales": round(best[1]["sales"], 2),
                    "tiers": dict(tiers), "cogs": round(cogs, 2)})
    for i, r in enumerate(out):
        nxt = out[i + 1] if i + 1 < len(out) else None
        r["avg_change_pct"] = round((r["avg"] - nxt["avg"]) / nxt["avg"] * 100, 1) if nxt and nxt["avg"] else None
    return out
