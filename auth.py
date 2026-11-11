"""Login, roles and branch access control."""
import time
from functools import wraps

from flask import g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db

RANK = {"staff": 1, "manager": 2, "owner": 3}
MIN_PASSWORD = 8

_attempts = {}  # ip -> [timestamps]
MAX_ATTEMPTS, WINDOW = 5, 600


def hash_password(pw):
    return generate_password_hash(pw)


def check_password_strength(pw):
    if not pw or len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    return None


def throttled(ip):
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < WINDOW]
    _attempts[ip] = recent
    return len(recent) >= MAX_ATTEMPTS


def record_failure(ip):
    _attempts.setdefault(ip, []).append(time.time())


def clear_failures(ip):
    _attempts.pop(ip, None)


def verify_login(conn, username, password):
    row = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if row and check_password_hash(row["pw_hash"], password):
        return row
    return None


def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        row = db.get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if row:
            g.user = dict(row)
        else:
            session.clear()


def api_error(message, status=400):
    return jsonify({"error": message}), status


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return api_error("Please sign in.", 401)
        return fn(*a, **kw)
    return wrapper


def role_required(min_role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return api_error("Please sign in.", 401)
            if RANK[g.user["role"]] < RANK[min_role]:
                return api_error("Your role can't do this.", 403)
            return fn(*a, **kw)
        return wrapper
    return deco


def has_role(min_role):
    return bool(g.user) and RANK[g.user["role"]] >= RANK[min_role]


def resolve_branch(conn):
    """Owners may pick any branch (?branch=ID); everyone else is locked to their own."""
    user = g.user
    if user["role"] == "owner":
        requested = request.args.get("branch") or (request.get_json(silent=True) or {}).get("branch")
        if requested:
            row = conn.execute("SELECT id FROM branches WHERE id=?", (requested,)).fetchone()
            if row:
                return row["id"]
        row = conn.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()
        return row["id"]
    return user["branch_id"]


def csrf_guard():
    """Cookie sessions + a custom header that browsers won't send cross-site without CORS."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if request.headers.get("X-Requested-With") != "mapo":
            return api_error("Blocked request.", 403)


def bootstrap_owner(conn, env):
    """Create the first owner account if there are no users yet."""
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return None
    username = env.get("ADMIN_USERNAME", "owner")
    password = env.get("ADMIN_PASSWORD")
    generated = False
    if not password:
        import secrets
        password = secrets.token_urlsafe(10)
        generated = True
    conn.execute(
        "INSERT INTO users(username,display_name,pw_hash,role,branch_id,created_at) VALUES(?,?,?,?,?,?)",
        (username, "Owner", hash_password(password), "owner", None, db.now_iso()),
    )
    conn.commit()
    return (username, password) if generated else (username, None)


def can_edit_date(user, date_str, today):
    """Managers and owners can fix any day; staff can only touch today and yesterday."""
    if RANK[user["role"]] >= RANK["manager"]:
        return True
    from datetime import datetime
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return 0 <= (today - d).days <= 1
