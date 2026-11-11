"""Import history from the old sales_data.csv (or any similar CSV) into the database.

Command line:   python migrate_csv.py sales_data.csv [branch_code]
Also used by the Settings page (Import history).

Two layouts are understood:

1. ONE ROW PER ITEM PER DAY (the original Mad Potato sheet). The date is written
   only on the first row of each day and the daily sales total is repeated on every
   row. Columns used: Date, Item_Code, Starting_Stock, Ending_Stock, Daily_Usage,
   Daily_Total_Sales, plus Opening, Stocks and Closing for the cup items.
   Purchases are worked out from the jump between yesterday's End and today's Starting.
2. ONE ROW PER DAY with item columns such as M_Start, M_End, M_Usage.

Rows that can't be read are skipped and reported. Importing the same file twice is
safe: a day that already exists is replaced.
"""
import csv
import io
import re
import sys
from collections import OrderedDict
from datetime import datetime

import config
import db

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y")


def _parse_date(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _num(s):
    s = re.sub(r"[₱,\s]", "", str(s or ""))
    try:
        return float(s)
    except ValueError:
        return None


def _sum(s):
    """'6+20' -> 26.0. Opening is often written as a sum of what was carried and what was added."""
    s = str(s or "").replace(" ", "").replace(",", "")
    if not s:
        return None
    if re.fullmatch(r"-?[\d.]+(\+[\d.]+)*", s):
        try:
            return float(sum(float(t) for t in s.split("+")))
        except ValueError:
            return None
    return None


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def map_headers(headers):
    """Find the columns we need. Returns a dict; 'long' is True for one-row-per-item files."""
    norm = {h: _norm(h) for h in headers}
    first = lambda pred: next((h for h, n in norm.items() if pred(n)), None)  # noqa: E731
    out = {
        "date": first(lambda n: "date" in n),
        "sales": first(lambda n: "sales" in n),
        "notes": first(lambda n: n in ("notes", "note", "remarks")),
        "item": first(lambda n: n in ("itemcode", "item", "code", "itemname")),
        "start": first(lambda n: n.startswith("start")),
        "end": first(lambda n: n.startswith("end")),
        "usage": first(lambda n: "usage" in n or n.startswith("used")),
        "opening": first(lambda n: n == "opening"),
        "stocks": first(lambda n: n == "stocks"),
        "closing": first(lambda n: n == "closing"),
        "wide": {},
    }
    out["long"] = bool(out["item"])
    for code in config.ITEM_CODES:
        c = code.lower()
        for h, n in norm.items():
            if not n.startswith(c):
                continue
            rest = n[len(c):]
            kind = ("starting" if rest.startswith(("start", "open", "begin")) else
                    "restock" if rest.startswith(("rest", "add", "deliv")) else
                    "ending" if rest.startswith(("end", "clos", "left")) else
                    "usage" if rest.startswith(("usage", "used", "daily")) else None)
            if kind:
                out["wide"].setdefault(code, {})[kind] = h
    return out


def _import_long(conn, branch_id, reader, h, username):
    days = OrderedDict()
    skipped, unknown, current = [], set(), None
    for i, row in enumerate(reader, start=2):
        raw = (row.get(h["date"]) or "").strip()
        if raw:
            current = _parse_date(raw)          # a new day starts here
            if not current:
                skipped.append(f"row {i}: unreadable date '{raw}'")
                continue
        if not current:
            continue                            # rows before the first date
        code = (row.get(h["item"]) or "").strip().upper()
        if code not in config.ITEM_CODES:
            if code:
                unknown.add(code)
            continue
        day = days.setdefault(current, {"sales": None, "items": {}})
        sales = _num(row.get(h["sales"])) if h["sales"] else None
        if day["sales"] is None and sales is not None:
            day["sales"] = sales
        cup = code in config.CUP_CODES
        day["items"][code] = {
            "starting": _num(row.get(h["start"])), "ending": _num(row.get(h["end"])),
            "usage": _num(row.get(h["usage"])) if h["usage"] else None,
            # Opening / Stocks / Closing only exist for cup items
            "opening": _sum(row.get(h["opening"])) if cup and h["opening"] else None,
            "stocks": _num(row.get(h["stocks"])) if cup and h["stocks"] else None,
            "closing": _num(row.get(h["closing"])) if cup and h["closing"] else None,
        }

    imported, negative, mismatch, neg_usage = 0, 0, 0, 0
    first_day = last_day = None
    for d in sorted(days):
        day = days[d]
        if day["sales"] is None or day["sales"] <= 0:
            skipped.append(f"{d}: no sales total")
            continue
        items = {}
        for code, v in day["items"].items():
            s0, e0, u0 = v["starting"] or 0.0, v["ending"] or 0.0, v["usage"]
            if u0 is None:
                u0 = max(0.0, s0 - e0)
            if u0 < 0:
                neg_usage += 1
                u0 = 0.0
            negative += (e0 < 0)
            mismatch += abs((s0 - u0) - e0) > 0.011
            items[code] = {**v, "starting": s0, "ending": e0, "usage": u0}
        try:
            db.upsert_log(conn, branch_id, d, day["sales"], items, "", username, trust_usage=True)
            imported += 1
            first_day, last_day = first_day or d, d
        except db.ValidationError as exc:
            skipped.append(f"{d}: {exc}")
    return {"imported": imported, "skipped": skipped[:20], "skipped_count": len(skipped), "error": None,
            "matched_items": sorted({c for d in days.values() for c in d["items"]}),
            "format": "one row per item per day", "first_date": first_day, "last_date": last_day,
            "negative_stock": negative, "usage_mismatch": mismatch, "negative_usage_set_to_zero": neg_usage,
            "has_cup_columns": bool(h["opening"] and h["stocks"] and h["closing"]),
            "unknown_items": sorted(unknown)}


def _import_wide(conn, branch_id, reader, h, username):
    imported, skipped = 0, []
    for i, row in enumerate(reader, start=2):
        d, sales = _parse_date(row.get(h["date"])), _num(row.get(h["sales"]))
        if not d or sales is None or sales < 0:
            skipped.append(f"row {i}: bad date or sales")
            continue
        items = {}
        for code, cols in h["wide"].items():
            v = {k: _num(row.get(col)) for k, col in cols.items()}
            if all(x is None for x in v.values()):
                continue
            items[code] = {k: x for k, x in v.items() if x is not None}
        try:
            db.upsert_log(conn, branch_id, d, sales, items, row.get(h["notes"], "") if h["notes"] else "",
                          username, trust_usage=True)
            imported += 1
        except db.ValidationError as exc:
            skipped.append(f"row {i}: {exc}")
    return {"imported": imported, "skipped": skipped[:20], "skipped_count": len(skipped), "error": None,
            "matched_items": sorted(h["wide"]), "format": "one row per day"}


def import_csv(conn, branch_id, text, username="import"):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return {"imported": 0, "skipped": [], "error": "The file has no header row."}
    h = map_headers(reader.fieldnames)
    if not h["date"] or not h["sales"]:
        return {"imported": 0, "skipped": [], "error":
                "Couldn't find a Date column and a Sales column. Found: " + ", ".join(reader.fieldnames[:12])}
    if h["long"]:
        if not (h["start"] and h["end"]):
            return {"imported": 0, "skipped": [], "error":
                    "Found an item column but no Starting and Ending stock columns. Found: "
                    + ", ".join(reader.fieldnames[:12])}
        return _import_long(conn, branch_id, reader, h, username)
    return _import_wide(conn, branch_id, reader, h, username)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else config.LEGACY_CSV
    code = sys.argv[2] if len(sys.argv) > 2 else None
    conn = db.connect()
    db.init_db(conn)
    branch = (conn.execute("SELECT id FROM branches WHERE code=?", (code,)).fetchone() if code
              else conn.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone())
    if not branch:
        sys.exit("Branch not found.")
    with open(path, encoding="utf-8-sig") as f:
        print(import_csv(conn, branch["id"], f.read()))
