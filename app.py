"""Mad Potato Management System: Flask backend."""
import json
import os
import re
from datetime import timedelta

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session

import analytics
import assistant
import auth
import config
import db
import exports
import forecast_service
import integrations
import migrate_csv
import ph_calendar
from auth import api_error, login_required, role_required


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="/static", template_folder="templates")
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    )

    conn = db.connect()
    db.init_db(conn)
    created = auth.bootstrap_owner(conn, os.environ)
    conn.close()
    if created:
        user, pw = created
        if pw:
            print(f"\n*** First run: owner account created. Username: {user}  Password: {pw}\n"
                  "*** Sign in and change it in Settings > Users. Set ADMIN_PASSWORD to choose your own.\n", flush=True)
    if config.SECRET_KEY_IS_EPHEMERAL:
        print("WARNING: SECRET_KEY is not set; sessions reset on every restart.", flush=True)

    app.teardown_appcontext(db.close_db)

    @app.before_request
    def _before():
        blocked = auth.csrf_guard()
        if blocked:
            return blocked
        auth.load_user()

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(404)
    def _404(_e):
        if request.path.startswith("/api/"):
            return api_error("Not found.", 404)
        return send_from_directory("templates", "index.html")

    @app.errorhandler(413)
    def _413(_e):
        return api_error("That file is too large.", 413)

    @app.errorhandler(500)
    def _500(_e):
        return api_error("Something went wrong on our side. Try again.", 500)

    # ------------------------------------------------------------- helpers
    def branch_scope():
        conn = db.get_db()
        bid = auth.resolve_branch(conn)
        branch = dict(conn.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone())
        return conn, bid, branch, db.get_settings(conn, bid)

    def tier_rows(rows, th):
        out = []
        for r in rows:
            key, label = analytics.tier_for(r["yhat"], th)
            out.append({**r, "tier": key, "tier_label": label})
        return out

    def after_save(branch, settings):
        def hook(log):
            integrations.sync_log_async(branch["id"], branch["name"], settings.get("sheets_url"), log, g.user["username"])
            if settings.get("alerts_enabled") == "1":
                conn = db.get_db()
                rec = analytics.reorder_suggestions(db.load_logs(conn, branch["id"]), db.get_items(conn), settings)
                if rec["stock_date"]:
                    integrations.low_stock_alert_async(branch["id"], branch["name"], rec["rows"], rec["stock_date"])
        return hook

    def clamp_staff_range(start, end):
        if g.user["role"] == "staff":
            floor = (config.today_ph() - timedelta(days=14)).isoformat()
            start = max(start or floor, floor)
        return start, end

    # ----------------------------------------------------------------- pages
    @app.route("/")
    def index():
        return send_from_directory("templates", "index.html")

    @app.route("/sw.js")
    def sw():
        resp = send_from_directory("static", "sw.js", mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/manifest.webmanifest")
    def manifest():
        return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")

    @app.route("/healthz")
    def healthz():
        db.get_db().execute("SELECT 1")
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    def login():
        conn = db.get_db()
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        if auth.throttled(ip):
            return api_error("Too many attempts. Wait 10 minutes and try again.", 429)
        body = request.get_json(silent=True) or {}
        user = auth.verify_login(conn, str(body.get("username", "")).strip(), str(body.get("password", "")))
        if not user:
            auth.record_failure(ip)
            return api_error("Wrong username or password.", 401)
        auth.clear_failures(ip)
        session.clear()
        session.permanent = True
        session["uid"] = user["id"]
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (db.now_iso(), user["id"]))
        conn.commit()
        db.audit(conn, user["username"], user["branch_id"], "login")
        return jsonify({"ok": True})

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/auth/me")
    def me():
        if not g.user:
            return api_error("Please sign in.", 401)
        conn = db.get_db()
        u = g.user
        branches = [dict(r) for r in conn.execute(
            "SELECT id,name,code FROM branches" + ("" if u["role"] == "owner" else " WHERE id=?") + " ORDER BY id",
            () if u["role"] == "owner" else (u["branch_id"],))]
        return jsonify({
            "user": {"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"],
                     "role": u["role"], "branch_id": u["branch_id"]},
            "branches": branches, "today": config.today_ph().isoformat(),
            "can_manage": auth.has_role("manager"), "is_owner": auth.has_role("owner"),
        })

    @app.post("/api/auth/password")
    @login_required
    def change_password():
        body = request.get_json(silent=True) or {}
        conn = db.get_db()
        row = conn.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
        if not auth.check_password_hash(row["pw_hash"], str(body.get("current", ""))):
            return api_error("Current password is wrong.")
        err = auth.check_password_strength(str(body.get("new", "")))
        if err:
            return api_error(err)
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(body["new"]), g.user["id"]))
        conn.commit()
        db.audit(conn, g.user["username"], g.user["branch_id"], "password_change")
        return jsonify({"ok": True})

    # ---------------------------------------------------- branches and users
    @app.post("/api/branches")
    @role_required("owner")
    def add_branch():
        body = request.get_json(silent=True) or {}
        name, code = str(body.get("name", "")).strip()[:60], re.sub(r"[^A-Z0-9]", "", str(body.get("code", "")).upper())[:12]
        if not name or not code:
            return api_error("Give the branch a name and a short code.")
        conn = db.get_db()
        try:
            cur = conn.execute("INSERT INTO branches(name,code,created_at) VALUES(?,?,?)", (name, code, db.now_iso()))
        except Exception:
            return api_error("That branch code is already used.")
        conn.commit()
        db.audit(conn, g.user["username"], cur.lastrowid, "branch_create", {"name": name})
        return jsonify({"id": cur.lastrowid, "name": name, "code": code})

    @app.get("/api/users")
    @role_required("manager")
    def list_users():
        conn = db.get_db()
        if g.user["role"] == "owner":
            rows = conn.execute("SELECT u.*, b.name AS branch_name FROM users u LEFT JOIN branches b ON b.id=u.branch_id ORDER BY u.id")
        else:
            rows = conn.execute("SELECT u.*, b.name AS branch_name FROM users u LEFT JOIN branches b ON b.id=u.branch_id "
                                "WHERE u.branch_id=? AND u.role='staff' ORDER BY u.id", (g.user["branch_id"],))
        return jsonify([{k: r[k] for k in ("id", "username", "display_name", "role", "branch_id", "branch_name", "active", "last_login")}
                        for r in rows])

    @app.post("/api/users")
    @role_required("manager")
    def add_user():
        body = request.get_json(silent=True) or {}
        conn = db.get_db()
        username = re.sub(r"[^a-zA-Z0-9_.-]", "", str(body.get("username", "")))[:30]
        role = body.get("role", "staff")
        branch_id = body.get("branch_id")
        if g.user["role"] == "manager":
            role, branch_id = "staff", g.user["branch_id"]
        if role not in config.ROLES or not username:
            return api_error("Pick a username and a valid role.")
        if role != "owner" and not conn.execute("SELECT 1 FROM branches WHERE id=?", (branch_id,)).fetchone():
            return api_error("Pick a branch for this user.")
        err = auth.check_password_strength(str(body.get("password", "")))
        if err:
            return api_error(err)
        try:
            cur = conn.execute(
                "INSERT INTO users(username,display_name,pw_hash,role,branch_id,created_at) VALUES(?,?,?,?,?,?)",
                (username, str(body.get("display_name") or username)[:40], auth.hash_password(body["password"]),
                 role, None if role == "owner" else branch_id, db.now_iso()))
        except Exception:
            return api_error("That username is taken.")
        conn.commit()
        db.audit(conn, g.user["username"], branch_id, "user_create", {"username": username, "role": role})
        return jsonify({"id": cur.lastrowid})

    @app.patch("/api/users/<int:uid>")
    @role_required("manager")
    def update_user(uid):
        conn = db.get_db()
        target = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not target:
            return api_error("User not found.", 404)
        if g.user["role"] == "manager" and not (target["role"] == "staff" and target["branch_id"] == g.user["branch_id"]):
            return api_error("Your role can't do this.", 403)
        body = request.get_json(silent=True) or {}
        if "active" in body:
            if uid == g.user["id"]:
                return api_error("You can't deactivate your own account.")
            conn.execute("UPDATE users SET active=? WHERE id=?", (1 if body["active"] else 0, uid))
        if body.get("password"):
            err = auth.check_password_strength(str(body["password"]))
            if err:
                return api_error(err)
            conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (auth.hash_password(body["password"]), uid))
        if "role" in body and g.user["role"] == "owner" and body["role"] in config.ROLES and uid != g.user["id"]:
            conn.execute("UPDATE users SET role=? WHERE id=?", (body["role"], uid))
        conn.commit()
        db.audit(conn, g.user["username"], target["branch_id"], "user_update",
                 {"username": target["username"], "fields": [k for k in body if k != "password"] + (["password"] if body.get("password") else [])})
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ items
    @app.get("/api/items")
    @login_required
    def items():
        rows = db.get_items(db.get_db(), active_only=False)
        for r in rows:
            r["cup"] = r["code"] in config.CUP_CODES
        if not auth.has_role("manager"):
            rows = [{"code": r["code"], "name": r["name"], "unit": r["unit"], "active": r["active"], "cup": r["cup"]}
                    for r in rows if r["active"]]
        return jsonify(rows)

    @app.put("/api/items")
    @role_required("manager")
    def update_items():
        conn = db.get_db()
        body = request.get_json(silent=True) or []
        known = {r["code"] for r in db.get_items(conn, active_only=False)}
        try:
            for r in body:
                if r.get("code") not in known:
                    continue
                vals = [db._num(r.get("unit_cost"), "Cost"), db._num(r.get("unit_price"), "Price"),
                        db._num(r.get("reorder_point"), "Minimum stock"),
                        int(db._num(r.get("lead_time_days"), "Lead time", 2)), 1 if r.get("active", 1) else 0]
                if not 0 <= vals[3] <= 30:
                    raise db.ValidationError("Lead time must be 0 to 30 days.")
                conn.execute("UPDATE items SET unit_cost=?,unit_price=?,reorder_point=?,lead_time_days=?,active=? WHERE code=?",
                             (*vals, r["code"]))
        except db.ValidationError as exc:
            conn.rollback()
            return api_error(str(exc))
        conn.commit()
        db.audit(conn, g.user["username"], None, "items_update", {"count": len(body)})
        return jsonify({"ok": True})

    # ------------------------------------------------------------------- logs
    @app.get("/api/logs")
    @login_required
    def logs_range():
        conn, bid, _, settings = branch_scope()
        last_n = request.args.get("last", type=int)
        if last_n:                                   # "the last N logged days", however old they are
            last_n = max(1, min(last_n, 14 if g.user["role"] == "staff" else 120))
            start, end = request.args.get("start"), request.args.get("end")
        else:
            start, end = clamp_staff_range(request.args.get("start"), request.args.get("end"))
        try:
            start = db.valid_date(start) if start else None
            end = db.valid_date(end) if end else None
        except db.ValidationError as exc:
            return api_error(str(exc))
        th = db.thresholds(settings)
        out = []
        for d, e in db.load_logs(conn, bid, start, end).items():
            key, label = analytics.tier_for(e["sales"], th) if e["sales"] > 0 else (None, None)
            out.append({"date": d, "sales": e["sales"], "tier": key, "tier_label": label, "notes": e["notes"],
                        "updated_by": e["updated_by"]})
        return jsonify(out[-last_n:] if last_n else out)

    @app.get("/api/logs/<date_str>")
    @login_required
    def log_get(date_str):
        conn, bid, _, settings = branch_scope()
        try:
            date_str = db.valid_date(date_str)
        except db.ValidationError as exc:
            return api_error(str(exc))
        log = db.get_log_or_template(conn, bid, date_str)
        log["thresholds"] = db.thresholds(settings)
        log["editable"] = auth.can_edit_date(g.user, date_str, config.today_ph()) and date_str <= config.today_ph().isoformat()
        return jsonify(log)

    @app.put("/api/logs/<date_str>")
    @login_required
    def log_put(date_str):
        conn, bid, branch, settings = branch_scope()
        try:
            date_str = db.valid_date(date_str)
        except db.ValidationError as exc:
            return api_error(str(exc))
        today = config.today_ph()
        if date_str > today.isoformat():
            return api_error("You can't log a future date.")
        if not auth.can_edit_date(g.user, date_str, today):
            return api_error("Staff can only edit today's and yesterday's log. Ask a manager.", 403)
        body = request.get_json(silent=True) or {}
        before = db.load_logs(conn, bid, date_str, date_str).get(date_str)
        try:
            log = db.upsert_log(conn, bid, date_str, body.get("sales"), body.get("items"), body.get("notes"),
                                g.user["username"])
        except db.ValidationError as exc:
            return api_error(str(exc))
        db.audit(conn, g.user["username"], bid, "log_save",
                 {"date": date_str, "created": before is None,
                  "sales": [before["sales"] if before else None, log["sales"]],
                  "items_changed": sorted(c for c, v in log["items"].items()
                                          if not before or before["items"].get(c) != v)})
        after_save(branch, settings)(log)
        return jsonify(log)

    @app.delete("/api/logs/<date_str>")
    @role_required("manager")
    def log_delete(date_str):
        conn, bid, _, _ = branch_scope()
        try:
            date_str = db.valid_date(date_str)
        except db.ValidationError as exc:
            return api_error(str(exc))
        conn.execute("DELETE FROM log_items WHERE branch_id=? AND log_date=?", (bid, date_str))
        conn.execute("DELETE FROM daily_logs WHERE branch_id=? AND log_date=?", (bid, date_str))
        conn.commit()
        db.audit(conn, g.user["username"], bid, "log_delete", {"date": date_str})
        return jsonify({"ok": True})

    # -------------------------------------------------------------- forecast
    @app.get("/api/forecast")
    @login_required
    def forecast():
        conn, bid, _, settings = branch_scope()
        th = db.thresholds(settings)
        days = max(1, min(60, int(request.args.get("days", 30))))
        payload, refreshing = forecast_service.get_forecast(conn, bid)
        today = config.today_ph()
        future = [r for r in payload["forecast"] if r["date"] >= today.isoformat()][:days]
        logs = db.load_logs(conn, bid, (today - timedelta(days=60)).isoformat())
        history = [{"date": d, "sales": e["sales"]} for d, e in logs.items() if e["sales"] > 0]
        upcoming = [{"date": d.isoformat(), "name": n} for d, n in sorted(
            (d, n) for yr in (today.year, today.year + 1) for d, n in ph_calendar.holiday_map(yr).items())
            if today <= d <= today + timedelta(days=60)]
        out = {k: payload.get(k) for k in ("model", "model_label", "n_obs", "notes", "last_actual_date", "accuracy")}
        out.update(forecast=tier_rows(future, th), history=history, refreshing=refreshing,
                   events=db.get_events(conn, bid), holidays=upcoming)
        if not auth.has_role("manager") and out["accuracy"]:
            out["accuracy"] = None
        return jsonify(out)

    @app.post("/api/forecast/refresh")
    @role_required("manager")
    def forecast_refresh():
        conn, bid, _, _ = branch_scope()
        forecast_service.get_forecast(conn, bid, force=True)
        return jsonify({"ok": True})

    @app.post("/api/events")
    @role_required("manager")
    def add_event():
        conn, bid, _, _ = branch_scope()
        body = request.get_json(silent=True) or {}
        try:
            d = db.valid_date(body.get("date"))
        except db.ValidationError as exc:
            return api_error(str(exc))
        name = str(body.get("name", "")).strip()[:60]
        if not name:
            return api_error("Name the event (for example, school fair or promo).")
        conn.execute("INSERT INTO events(branch_id,event_date,name) VALUES(?,?,?) "
                     "ON CONFLICT(branch_id,event_date) DO UPDATE SET name=excluded.name", (bid, d, name))
        conn.commit()
        db.audit(conn, g.user["username"], bid, "event_add", {"date": d, "name": name})
        return jsonify({"ok": True})

    @app.delete("/api/events/<int:eid>")
    @role_required("manager")
    def del_event(eid):
        conn, bid, _, _ = branch_scope()
        conn.execute("DELETE FROM events WHERE id=? AND branch_id=?", (eid, bid))
        conn.commit()
        return jsonify({"ok": True})

    # ------------------------------------------------------------- dashboard
    @app.get("/api/dashboard")
    @login_required
    def dashboard():
        conn, bid, branch, settings = branch_scope()
        th, today = db.thresholds(settings), config.today_ph()
        logs = db.load_logs(conn, bid)
        items_ = db.get_items(conn)
        payload, refreshing = forecast_service.get_forecast(conn, bid)
        fc = [r for r in payload["forecast"] if r["date"] > today.isoformat()]
        tomorrow = tier_rows(fc[:1], th)[0] if fc else None
        week = fc[:7]
        rec = analytics.reorder_suggestions(logs, items_, settings)
        t = logs.get(today.isoformat())
        today_info = {"logged": bool(t and t["sales"] > 0), "sales": t["sales"] if t else 0}
        if t and t["sales"] > 0:
            today_info["tier"], today_info["tier_label"] = analytics.tier_for(t["sales"], th)
        recent = []
        for d, e in list(logs.items())[-14:]:
            if e["sales"] > 0:
                key, label = analytics.tier_for(e["sales"], th)
                recent.append({"date": d, "sales": e["sales"], "tier": key, "tier_label": label})
        counts = {}
        for d, e in logs.items():
            if e["sales"] > 0 and (today - analytics.d2date(d)).days < 30:
                k = analytics.tier_for(e["sales"], th)[0]
                counts[k] = counts.get(k, 0) + 1
        out = {
            "branch": branch, "today": today.isoformat(), "today_info": today_info,
            "stats": analytics.sales_stats(logs, today), "tomorrow": tomorrow,
            "week_total": round(sum(r["yhat"] for r in week), 2) if week else None,
            "week_days": len(week), "model_label": payload.get("model_label"), "refreshing": refreshing,
            "stock": rec["rows"], "stock_date": rec["stock_date"], "recent": recent, "tier_counts": counts,
            "notes": payload.get("notes", []),
        }
        if auth.has_role("manager"):
            out["anomalies"] = analytics.find_anomalies(logs, today, limit=5)
            prof = analytics.profit_report(logs, items_, float(settings["op_cost"]),
                                           (today - timedelta(days=6)).isoformat(), today.isoformat())
            out["profit_week"] = prof["totals"]
            out["costs_configured"] = prof["costs_configured"]
        return jsonify(out)

    # ------------------------------------------------- inventory / insights
    @app.get("/api/reorder")
    @login_required
    def reorder():
        conn, bid, branch, settings = branch_scope()
        rec = analytics.reorder_suggestions(db.load_logs(conn, bid), db.get_items(conn), settings)
        rec["message"] = analytics.order_message(branch["name"], rec["stock_date"], rec["rows"])
        return jsonify(rec)

    @app.get("/api/insights")
    @role_required("manager")
    def insights():
        conn, bid, _, settings = branch_scope()
        logs, today = db.load_logs(conn, bid), config.today_ph()
        return jsonify({"anomalies": analytics.find_anomalies(logs, today), "weekday": analytics.weekday_profile(logs),
                        "stats": analytics.sales_stats(logs, today)})

    @app.get("/api/profit")
    @role_required("manager")
    def profit():
        conn, bid, _, settings = branch_scope()
        today = config.today_ph()
        start = request.args.get("start") or (today - timedelta(days=29)).isoformat()
        end = request.args.get("end") or today.isoformat()
        try:
            start, end = db.valid_date(start), db.valid_date(end)
        except db.ValidationError as exc:
            return api_error(str(exc))
        return jsonify(analytics.profit_report(db.load_logs(conn, bid, start, end), db.get_items(conn),
                                               float(settings["op_cost"]), start, end))

    @app.get("/api/reports")
    @role_required("manager")
    def reports():
        conn, bid, _, settings = branch_scope()
        period = "month" if request.args.get("period") == "month" else "week"
        return jsonify(analytics.period_summary(db.load_logs(conn, bid), db.thresholds(settings), db.get_items(conn), period))

    def _export_context():
        conn, bid, branch, settings = branch_scope()
        logs, th, today = db.load_logs(conn, bid), db.thresholds(settings), config.today_ph()
        items_ = db.get_items(conn)
        payload, _ = forecast_service.get_forecast(conn, bid, wait_for_full=False)
        fc = dict(payload, forecast=[r for r in payload["forecast"] if r["date"] > today.isoformat()][:30])
        return (branch, logs, th, items_, fc, analytics.reorder_suggestions(logs, items_, settings),
                analytics.profit_report(logs, items_, float(settings["op_cost"])),
                analytics.period_summary(logs, th, items_, "week"), analytics.sales_stats(logs, today),
                analytics.find_anomalies(logs, today))

    @app.get("/api/export/excel")
    @role_required("manager")
    def export_excel():
        branch, logs, th, items_, fc, rec, prof, periods, _, _ = _export_context()
        buf = exports.build_excel(branch["name"], logs, items_, th, fc, rec, prof, periods)
        db.audit(db.get_db(), g.user["username"], branch["id"], "export", {"type": "excel"})
        return send_file(buf, as_attachment=True, download_name=f"mad-potato-{config.today_ph()}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/export/pdf")
    @role_required("manager")
    def export_pdf():
        branch, logs, th, items_, fc, rec, prof, periods, stats, anomalies = _export_context()
        buf = exports.build_pdf(branch["name"], stats, periods, fc, th, rec, anomalies, prof,
                                config.now_ph().strftime("%b %d, %Y %I:%M %p"))
        db.audit(db.get_db(), g.user["username"], branch["id"], "export", {"type": "pdf"})
        return send_file(buf, as_attachment=True, download_name=f"mad-potato-report-{config.today_ph()}.pdf",
                         mimetype="application/pdf")

    # -------------------------------------------------------------- settings
    @app.get("/api/settings")
    @role_required("manager")
    def settings_get():
        conn, bid, branch, settings = branch_scope()
        return jsonify({"branch": branch, "settings": settings,
                        "telegram_configured": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)})

    @app.put("/api/settings")
    @role_required("manager")
    def settings_put():
        conn, bid, _, current = branch_scope()
        body = request.get_json(silent=True) or {}
        merged = {**current, **{k: str(v) for k, v in body.items() if k in config.DEFAULT_SETTINGS}}
        try:
            tiers = [float(merged[k]) for k in ("tier_deficit", "tier_under", "tier_below", "tier_high")]
            op = float(merged["op_cost"])
            cover = int(float(merged["cover_days"]))
        except ValueError:
            return api_error("Amounts must be numbers.")
        if any(t < 0 for t in tiers) or op < 0 or not 1 <= cover <= 30:
            return api_error("Amounts can't be negative, and stock cover must be 1 to 30 days.")
        if not (tiers[0] < tiers[1] < tiers[2] < tiers[3]):
            return api_error("Tier amounts must increase: deficit < underperforming < below average < high-performing.")
        url = merged["sheets_url"].strip()
        if url and not integrations.valid_sheets_url(url):
            return api_error("The Sheets backup link must be a Google Apps Script web app URL (https://script.google.com/...).")
        merged["sheets_url"], merged["cover_days"] = url, str(cover)
        merged["alerts_enabled"] = "1" if merged["alerts_enabled"] in ("1", "true", "True") else "0"
        changed = {k: merged[k] for k in config.DEFAULT_SETTINGS if k in body and merged[k] != current.get(k)}
        db.set_settings(conn, bid, {k: merged[k] for k in config.DEFAULT_SETTINGS if k not in ("sheets_last_status",)})
        forecast_service.invalidate(conn, bid)
        db.audit(conn, g.user["username"], bid, "settings_update", {k: ("(set)" if k == "sheets_url" else v) for k, v in changed.items()})
        return jsonify({"ok": True})

    @app.post("/api/settings/test-sheets")
    @role_required("manager")
    def test_sheets():
        conn, bid, branch, settings = branch_scope()
        if not integrations.valid_sheets_url(settings.get("sheets_url")):
            return api_error("Save a valid Apps Script link first.")
        try:
            integrations._post_json(settings["sheets_url"], {"test": True, "branch": branch["name"]})
        except Exception:
            return api_error("Couldn't reach that link. Check the deployment is set to 'Anyone' and try again.")
        return jsonify({"ok": True})

    @app.post("/api/import/csv")
    @role_required("manager")
    def import_csv_route():
        conn, bid, _, _ = branch_scope()
        f = request.files.get("file")
        if not f:
            return api_error("Choose a CSV file.")
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return api_error("The file must be a UTF-8 CSV.")
        res = migrate_csv.import_csv(conn, bid, text, g.user["username"])
        if res["error"]:
            return api_error(res["error"])
        db.audit(conn, g.user["username"], bid, "import_csv", {"imported": res["imported"]})
        return jsonify(res)

    @app.get("/api/audit")
    @role_required("manager")
    def audit_log():
        conn, bid, _, _ = branch_scope()
        limit = max(1, min(200, int(request.args.get("limit", 60))))
        rows = conn.execute("SELECT ts,username,action,detail FROM audit_log WHERE branch_id=? OR branch_id IS NULL "
                            "ORDER BY id DESC LIMIT ?", (bid, limit)).fetchall()
        return jsonify([{"ts": r["ts"], "username": r["username"], "action": r["action"],
                         "detail": json.loads(r["detail"] or "{}")} for r in rows])

    # ------------------------------------------------------------------ chat
    def _ctx():
        conn, bid, branch, settings = branch_scope()
        return assistant.Ctx(conn, bid, branch["name"], g.user, settings, after_save(branch, settings))

    @app.post("/api/chat")
    @login_required
    def chat():
        body = request.get_json(silent=True) or {}
        return jsonify(assistant.handle(_ctx(), str(body.get("message", ""))))

    @app.post("/api/chat/confirm")
    @login_required
    def chat_confirm():
        body = request.get_json(silent=True) or {}
        return jsonify(assistant.confirm(_ctx(), str(body.get("id", "")), bool(body.get("accept"))))

    # ---------------------------------------------------------------- backup
    @app.get("/api/backup")
    @role_required("owner")
    def backup():
        conn = db.get_db()
        data = {"version": 2, "exported": db.now_iso(),
                "branches": [dict(r) for r in conn.execute("SELECT * FROM branches")],
                "items": db.get_items(conn, active_only=False),
                "logs": [], "events": [dict(r) for r in conn.execute("SELECT branch_id,event_date,name FROM events")]}
        for b in data["branches"]:
            for d, e in db.load_logs(conn, b["id"]).items():
                data["logs"].append({"branch_id": b["id"], **e})
        db.audit(conn, g.user["username"], None, "backup_download")
        return Response(json.dumps(data), mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename=mad-potato-backup-{config.today_ph()}.json"})

    @app.post("/api/restore")
    @role_required("owner")
    def restore():
        conn = db.get_db()
        f = request.files.get("file")
        try:
            data = json.loads(f.read().decode("utf-8"))
            assert data.get("version") == 2
        except Exception:
            return api_error("That isn't a Mad Potato backup file.")
        valid = {r["id"] for r in conn.execute("SELECT id FROM branches")}
        count = 0
        for log in data.get("logs", []):
            if log.get("branch_id") in valid:
                try:
                    db.upsert_log(conn, log["branch_id"], log["date"], log["sales"], log.get("items", {}),
                                  log.get("notes", ""), g.user["username"], trust_usage=True)
                    count += 1
                except db.ValidationError:
                    continue
        for ev in data.get("events", []):
            if ev.get("branch_id") in valid:
                conn.execute("INSERT OR IGNORE INTO events(branch_id,event_date,name) VALUES(?,?,?)",
                             (ev["branch_id"], ev["event_date"], ev["name"]))
        conn.commit()
        db.audit(conn, g.user["username"], None, "restore", {"logs": count})
        return jsonify({"restored_days": count})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
