"""SQLite storage.

Stock model (same as the V1 digitized logbook):
  every item:  Starting - Usage = End
  cup items:   Opening + Stocks = Starting     (counter cups + reserve)
               Opening - Usage  = Closing      (cups left at the counter)
               Stocks + Closing = End
Usage and End stay empty (NULL) until they are entered, so a half-filled day
(morning count only) is saved without pretending nothing was used.
"""
import json
import os
import sqlite3
from collections import OrderedDict
from datetime import datetime

from flask import g

import config

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  code TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  display_name TEXT,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('owner','manager','staff')),
  branch_id INTEGER REFERENCES branches(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS items(
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  unit TEXT NOT NULL,
  unit_cost REAL NOT NULL DEFAULT 0,
  unit_price REAL NOT NULL DEFAULT 0,
  reorder_point REAL NOT NULL DEFAULT 0,
  lead_time_days INTEGER NOT NULL DEFAULT 2,
  active INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_logs(
  branch_id INTEGER NOT NULL REFERENCES branches(id),
  log_date TEXT NOT NULL,
  sales REAL NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  updated_by TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(branch_id, log_date)
);
CREATE TABLE IF NOT EXISTS log_items(
  branch_id INTEGER NOT NULL,
  log_date TEXT NOT NULL,
  item_code TEXT NOT NULL,
  opening REAL,
  stocks REAL,
  starting REAL NOT NULL DEFAULT 0,
  usage REAL,
  ending REAL,
  closing REAL,
  restock REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(branch_id, log_date, item_code),
  FOREIGN KEY(branch_id, log_date) REFERENCES daily_logs(branch_id, log_date) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  branch_id INTEGER NOT NULL,
  event_date TEXT NOT NULL,
  name TEXT NOT NULL,
  UNIQUE(branch_id, event_date)
);
CREATE TABLE IF NOT EXISTS settings(
  branch_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY(branch_id, key)
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  username TEXT,
  branch_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT,
  undone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS forecast_cache(
  branch_id INTEGER PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  is_full INTEGER NOT NULL,
  generated_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions(
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  branch_id INTEGER NOT NULL,
  payload TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

MAX_QTY = 1_000_000
STOCK_FIELDS = ("opening", "stocks", "starting", "usage", "ending", "closing", "restock")


class ValidationError(ValueError):
    pass


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now_iso():
    return config.now_ph().strftime("%Y-%m-%d %H:%M:%S")


def _migrate(conn):
    """Upgrade a database made by an earlier version of this app (keeps all data)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(log_items)")]
    if cols and "opening" not in cols:           # first v2: usage/ending were NOT NULL, no opening/stocks/closing
        conn.execute("ALTER TABLE log_items RENAME TO log_items_old")
        conn.executescript(SCHEMA)               # creates the new log_items
        conn.execute(
            "INSERT INTO log_items(branch_id,log_date,item_code,starting,restock,ending,usage) "
            "SELECT branch_id,log_date,item_code,starting,restock,ending,usage FROM log_items_old")
        conn.execute("DROP TABLE log_items_old")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def init_db(conn):
    conn.executescript(SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _migrate(conn)
    for i, (code, name, unit) in enumerate(config.ITEM_DEFS):
        conn.execute("INSERT OR IGNORE INTO items(code,name,unit,sort) VALUES(?,?,?,?)", (code, name, unit, i))
    if conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 0:
        conn.execute("INSERT INTO branches(name,code,created_at) VALUES(?,?,?)",
                     ("Mad Potato - Teresa", "TERESA", now_iso()))
    conn.commit()


# ---------- settings ----------
def get_settings(conn, branch_id):
    out = dict(config.DEFAULT_SETTINGS)
    for r in conn.execute("SELECT key,value FROM settings WHERE branch_id=?", (branch_id,)):
        out[r["key"]] = r["value"]
    return out


def set_settings(conn, branch_id, values: dict):
    for k, v in values.items():
        if k not in config.DEFAULT_SETTINGS:
            continue
        conn.execute(
            "INSERT INTO settings(branch_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(branch_id,key) DO UPDATE SET value=excluded.value",
            (branch_id, k, str(v)),
        )
    conn.commit()


def thresholds(settings):
    return {
        "deficit": float(settings["tier_deficit"]),
        "under": float(settings["tier_under"]),
        "below": float(settings["tier_below"]),
        "high": float(settings["tier_high"]),
    }


# ---------- items ----------
def get_items(conn, active_only=True):
    q = "SELECT * FROM items" + (" WHERE active=1" if active_only else "") + " ORDER BY sort"
    return [dict(r) for r in conn.execute(q)]


# ---------- validation ----------
def valid_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValidationError("Date must look like 2026-11-09.")


def _num(v, name, default=0.0):
    """Required non-negative number (sales, costs)."""
    if v is None or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number.")
    if x != x or x < 0 or x > MAX_QTY:
        raise ValidationError(f"{name} must be between 0 and {MAX_QTY:,}.")
    return x


def _opt(v, name, negative_ok=False):
    """Optional number. Blank means 'not entered yet' (None). Stock counts may go below zero, as in V1."""
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a number.")
    lo = -MAX_QTY if negative_ok else 0
    if x != x or x < lo or x > MAX_QTY:
        raise ValidationError(f"{name} must be between {lo:,} and {MAX_QTY:,}.")
    return x


# ---------- logs ----------
def _item_row(r):
    return {k: r[k] for k in STOCK_FIELDS}


def load_logs(conn, branch_id, start=None, end=None):
    """OrderedDict date -> {date, sales, notes, items:{code:{opening,stocks,starting,usage,ending,closing,restock}}}."""
    where, args = ["branch_id=?"], [branch_id]
    if start:
        where.append("log_date>=?"); args.append(start)
    if end:
        where.append("log_date<=?"); args.append(end)
    clause = " AND ".join(where)
    logs = OrderedDict()
    for r in conn.execute(f"SELECT * FROM daily_logs WHERE {clause} ORDER BY log_date", args):
        logs[r["log_date"]] = {"date": r["log_date"], "sales": r["sales"], "notes": r["notes"],
                               "updated_by": r["updated_by"], "updated_at": r["updated_at"], "items": {}}
    if logs:
        for r in conn.execute(f"SELECT * FROM log_items WHERE {clause} ORDER BY log_date", args):
            if r["log_date"] in logs:
                logs[r["log_date"]]["items"][r["item_code"]] = _item_row(r)
    return logs


def last_state_before(conn, branch_id, date_str):
    """Latest counted End (and counter Closing) per item before date_str."""
    rows = conn.execute(
        "SELECT li.item_code, li.ending, li.closing FROM log_items li JOIN ("
        " SELECT item_code, MAX(log_date) d FROM log_items WHERE branch_id=? AND log_date<? AND ending IS NOT NULL"
        " GROUP BY item_code) m ON li.item_code=m.item_code AND li.log_date=m.d AND li.branch_id=?",
        (branch_id, date_str, branch_id)).fetchall()
    return {r["item_code"]: {"ending": r["ending"], "closing": r["closing"]} for r in rows}


def blank_items(conn, branch_id, date_str):
    """A fresh day, pre-filled the way the V1 logbook does it:
    Starting = yesterday's End, Opening = yesterday's Closing, Stocks = Starting - Opening."""
    prev = last_state_before(conn, branch_id, date_str)
    items = {}
    for it in get_items(conn):
        code, p = it["code"], prev.get(it["code"])
        start = max(p["ending"], 0.0) if p else 0.0
        opening = stocks = None
        if code in config.CUP_CODES and p and p["closing"] is not None:
            opening = min(max(p["closing"], 0.0), start)
            stocks = start - opening
        items[code] = {"opening": opening, "stocks": stocks, "starting": start, "usage": None,
                       "ending": None, "closing": None, "restock": 0.0}
    return items


def get_log_or_template(conn, branch_id, date_str):
    logs = load_logs(conn, branch_id, date_str, date_str)
    blank = blank_items(conn, branch_id, date_str)
    if date_str in logs:
        log = dict(logs[date_str], exists=True)
        log["items"] = {**blank, **log["items"]}      # items added later still get a row to edit
        return log
    return {"date": date_str, "sales": 0.0, "notes": "", "items": blank, "exists": False}


def upsert_log(conn, branch_id, date_str, sales, items, notes, username, trust_usage=False):
    """Validate and save one day.

    Normal save: End = Starting - Usage, and for cups Closing = Opening - Usage, Stocks = Starting - Opening
    when those aren't given. `trust_usage=True` (imports and restores) stores the numbers exactly as given.
    """
    date_str = valid_date(date_str)
    sales = _num(sales, "Sales")
    notes = str(notes or "")[:500]
    known = {i["code"] for i in get_items(conn, active_only=False)}
    prev = last_state_before(conn, branch_id, date_str)
    clean = {}
    for code, v in (items or {}).items():
        if code not in known:
            raise ValidationError(f"Unknown item code '{code}'.")
        v = v or {}
        starting = _opt(v.get("starting"), f"{code} starting", True) or 0.0
        opening = _opt(v.get("opening"), f"{code} opening", True)
        stocks = _opt(v.get("stocks"), f"{code} stocks", True)
        usage = _opt(v.get("usage"), f"{code} usage")
        ending = _opt(v.get("ending"), f"{code} end", True)
        closing = _opt(v.get("closing"), f"{code} closing", True)
        if trust_usage:
            if usage is None and ending is not None:
                usage = max(0.0, starting - ending)
            if ending is None and usage is not None:
                ending = starting - usage
        else:
            if usage is None and ending is not None:
                usage = max(0.0, starting - ending)                 # older callers send End instead of Usage
            if usage is None and opening is not None and closing is not None:
                usage = max(0.0, opening - closing)                 # cups: typed the counter closing
            ending = starting - usage if usage is not None else None
            if opening is not None and usage is not None:
                closing = opening - usage
            elif usage is None:
                closing = None
            if opening is not None and stocks is None:
                stocks = starting - opening
        # purchases: bought between yesterday's count and today's start, or added during the day
        restock = 0.0
        if code in prev and prev[code]["ending"] is not None:
            restock += max(0.0, starting - prev[code]["ending"])
        if ending is not None and usage is not None:
            restock += max(0.0, ending - (starting - usage))
        clean[code] = (opening, stocks, starting, usage, ending, closing, restock)

    conn.execute(
        "INSERT INTO daily_logs(branch_id,log_date,sales,notes,updated_by,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(branch_id,log_date) DO UPDATE SET sales=excluded.sales, notes=excluded.notes, "
        "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (branch_id, date_str, sales, notes, username, now_iso()))
    conn.execute("DELETE FROM log_items WHERE branch_id=? AND log_date=?", (branch_id, date_str))
    conn.executemany(
        "INSERT INTO log_items(branch_id,log_date,item_code,opening,stocks,starting,usage,ending,closing,restock) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(branch_id, date_str, c, *vals) for c, vals in clean.items()])
    conn.commit()
    return load_logs(conn, branch_id, date_str, date_str)[date_str]


# ---------- audit ----------
def audit(conn, username, branch_id, action, detail=None):
    cur = conn.execute(
        "INSERT INTO audit_log(ts,username,branch_id,action,detail) VALUES(?,?,?,?,?)",
        (now_iso(), username, branch_id, action, json.dumps(detail or {}, default=str)))
    conn.commit()
    return cur.lastrowid


# ---------- events ----------
def get_events(conn, branch_id):
    return [dict(r) for r in conn.execute(
        "SELECT id,event_date,name FROM events WHERE branch_id=? ORDER BY event_date", (branch_id,))]
