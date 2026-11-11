"""Caches forecasts in the database and refreshes the slow models in the background.

Before: Prophet refit on every request. Now: fast models answer immediately,
heavy models (Prophet, SARIMAX) run in a background thread and the result is cached
until the logged data changes. Run gunicorn with 1 worker + threads (see Procfile).
"""
import json
import threading

import config
import db
import forecasting

_running = set()
_lock = threading.Lock()


def _events_set(conn, branch_id):
    return {e["event_date"] for e in db.get_events(conn, branch_id)}


def _store(conn, branch_id, fp, is_full, payload):
    conn.execute(
        "INSERT INTO forecast_cache(branch_id,fingerprint,is_full,generated_at,payload) VALUES(?,?,?,?,?) "
        "ON CONFLICT(branch_id) DO UPDATE SET fingerprint=excluded.fingerprint, is_full=excluded.is_full, "
        "generated_at=excluded.generated_at, payload=excluded.payload",
        (branch_id, fp, int(is_full), db.now_iso(), json.dumps(payload)),
    )
    conn.commit()


def _background(branch_id):
    conn = db.connect()
    try:
        logs = db.load_logs(conn, branch_id)
        events = _events_set(conn, branch_id)
        fp = forecasting.fingerprint(logs, events)
        payload = forecasting.compute_forecast(logs, events, config.today_ph(), include_slow=True)
        _store(conn, branch_id, fp, True, payload)
    except Exception:  # never let a worker thread crash the app
        pass
    finally:
        conn.close()
        with _lock:
            _running.discard(branch_id)


def get_forecast(conn, branch_id, force=False, wait_for_full=False):
    """Return (payload, refreshing). Fast answer now; heavy models fill in later."""
    logs = db.load_logs(conn, branch_id)
    events = _events_set(conn, branch_id)
    fp = forecasting.fingerprint(logs, events)
    n_obs = sum(1 for e in logs.values() if e["sales"] and e["sales"] > 0)

    row = conn.execute("SELECT * FROM forecast_cache WHERE branch_id=?", (branch_id,)).fetchone()
    if row and row["fingerprint"] == fp and not force:
        with _lock:
            running = branch_id in _running
        if row["is_full"] or not forecasting.slow_models_available(n_obs):
            return json.loads(row["payload"]), False
        if running:
            return json.loads(row["payload"]), True

    slow = forecasting.slow_models_available(n_obs)
    if wait_for_full or not slow:
        payload = forecasting.compute_forecast(logs, events, config.today_ph(), include_slow=slow)
        _store(conn, branch_id, fp, True, payload)
        return payload, False

    payload = forecasting.compute_forecast(logs, events, config.today_ph(), include_slow=False)
    _store(conn, branch_id, fp, False, payload)
    with _lock:
        start = branch_id not in _running
        if start:
            _running.add(branch_id)
    if start:
        threading.Thread(target=_background, args=(branch_id,), daemon=True).start()
    return payload, True


def invalidate(conn, branch_id):
    conn.execute("DELETE FROM forecast_cache WHERE branch_id=?", (branch_id,))
    conn.commit()
