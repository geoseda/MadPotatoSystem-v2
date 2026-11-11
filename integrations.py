"""Outbound integrations. Both are optional and fail quietly (the app keeps working)."""
import json
import threading
import urllib.request
from urllib.parse import urlparse

import config
import db


def _post_json(url, payload, timeout=15):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # Apps Script redirects; urllib follows
        return resp.status


def valid_sheets_url(url):
    p = urlparse(url or "")
    return p.scheme == "https" and p.netloc.endswith("script.google.com")


def sync_log_async(branch_id, branch_name, url, log, username):
    """Send one saved day to a Google Apps Script web app, with 3 attempts."""
    if not valid_sheets_url(url):
        return

    def work():
        status = "error"
        payload = {
            "branch": branch_name, "date": log["date"], "sales": log["sales"], "notes": log["notes"],
            "updated_by": username,
            "items": [{"code": c, **v} for c, v in log["items"].items()],
        }
        for _ in range(3):
            try:
                _post_json(url, payload)
                status = "ok"
                break
            except Exception:
                continue
        conn = db.connect()
        try:
            db.set_settings(conn, branch_id, {"sheets_last_status": f"{status} {db.now_iso()}"})
        finally:
            conn.close()

    threading.Thread(target=work, daemon=True).start()


def send_telegram(text):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return False
    try:
        _post_json(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                   {"chat_id": config.TELEGRAM_CHAT_ID, "text": text})
        return True
    except Exception:
        return False


def low_stock_alert_async(branch_id, branch_name, rows, stock_date):
    """Alert once per branch per day when items are critical or due for reorder."""
    urgent = [r for r in rows if r["status"] in ("critical", "reorder")]
    if not urgent or not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return

    def work():
        conn = db.connect()
        try:
            key = f"alert_sent_{stock_date}"
            sent = conn.execute("SELECT 1 FROM settings WHERE branch_id=? AND key=?", (branch_id, key)).fetchone()
            if sent:
                return
            lines = [f"Low stock - {branch_name} ({stock_date})"]
            for r in urgent:
                tag = "OUT BEFORE RESTOCK" if r["status"] == "critical" else "reorder"
                lines.append(f"- {r['name']}: {r['stock']:g} left, order {r['order_qty']} ({tag})")
            if send_telegram("\n".join(lines)):
                conn.execute("INSERT OR REPLACE INTO settings(branch_id,key,value) VALUES(?,?,?)",
                             (branch_id, key, "1"))
                conn.commit()
        finally:
            conn.close()

    threading.Thread(target=work, daemon=True).start()
