"""MAPO, the chat assistant.

Rule-based parsing handles English and Taglish, several items per message
("closing medio 5, grande 3"), and dates. Stock changes are never written
immediately: MAPO shows what will change and waits for a confirmation, and the
last change can be undone. An optional trained intent model (nlp_pipeline.joblib)
is used as a fallback for questions the rules don't recognise.
"""
import json
import re
import uuid
from datetime import date, datetime, timedelta

import analytics
import auth
import config
import db
import forecast_service

MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MON = "|".join(MONTHS)
ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in sorted(config.ITEM_ALIASES, key=len, reverse=True)) + r")\b")
UNIT_WORDS = r"(?:cups?|pcs|pieces?|packs?|bottles?)"

SET_WORDS = r"\b(closing|close|ending|end|set|update|natira|natitira|nalabi|count)\b"
ADD_WORDS = r"\b(add|added|restock|restocked|dagdag|dagdagan|nagdagdag|plus|delivery|delivered)\b"
REMOVE_WORDS = r"\b(remove|deduct|subtract|reduce|minus|bawas|bawasan|used|spoiled|nasira|sira)\b"

HELP = (
    "Here is what I can do:\n"
    "- **Stock:** \"closing medio 5, grande 3\" (cups left at the counter), \"add 50 medio\", \"bawas 2 jumbo\" (I ask you to confirm first)\n"
    "- **Sales:** \"sales today\", \"benta kahapon\", \"sales nov 5\"\n"
    "- **Forecast:** \"forecast tomorrow\", \"forecast this week\", \"forecast month\"\n"
    "- **Inventory:** \"stock\", \"how many medio left\", \"what should I order\"\n"
    "- **Business:** \"profit this week\", \"best sellers\", \"trend vs last week\", \"targets\"\n"
    "- **Undo:** \"undo\" reverses your last stock change."
)


class Ctx:
    def __init__(self, conn, branch_id, branch_name, user, settings, after_save=None):
        self.conn, self.branch_id, self.branch_name = conn, branch_id, branch_name
        self.user, self.settings, self.after_save = user, settings, after_save
        self.today = config.today_ph()
        self.th = db.thresholds(settings)


# ------------------------------------------------------------------ parsing
def parse_date(text, today):
    """Return (date | None, text with the date words removed)."""
    for word, delta in (("tomorrow", 1), ("bukas", 1), ("yesterday", -1), ("kahapon", -1),
                        ("today", 0), ("ngayon", 0)):
        if re.search(rf"\b{word}\b", text):
            return today + timedelta(days=delta), re.sub(rf"\b{word}\b", " ", text)

    def fix_year(m, d, y):
        try:
            if y:
                y = int(y)
                return date(y if y > 99 else 2000 + y, m, d)
            cand = date(today.year, m, d)
            return cand if (cand - today).days <= 30 else date(today.year - 1, m, d)
        except ValueError:
            return None

    pats = [
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda g: date(int(g[0]), int(g[1]), int(g[2]))),
        (rf"\b({_MON})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b",
         lambda g: fix_year(MONTHS[g[0]], int(g[1]), g[2])),
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})[a-z]*\b(?:,?\s+(\d{{4}}))?",
         lambda g: fix_year(MONTHS[g[1]], int(g[0]), g[2])),
        (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lambda g: fix_year(int(g[0]), int(g[1]), g[2])),
    ]
    for pat, build in pats:
        m = re.search(pat, text)
        if m:
            try:
                d = build(m.groups())
            except (ValueError, KeyError):
                d = None
            if d:
                return d, text[:m.start()] + " " + text[m.end():]
    return None, text


def extract_item_quantities(text):
    """{'M': 5.0, 'G': 3.0} from 'medio 5, grande is 3' or 'add 50 cups of medio'."""
    found, matches = {}, list(ALIAS_RE.finditer(text))
    for i, m in enumerate(matches):
        code = config.ITEM_ALIASES[m.group(1)]
        after = text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        before = text[matches[i - 1].end() if i else 0: m.start()]
        n = re.match(r"\s*(?:is|are|to|=|:|at|na|ay)?\s*(\d+(?:\.\d+)?)\b", after)
        if not n:
            n = re.search(rf"(\d+(?:\.\d+)?)\s*{UNIT_WORDS}?\s*(?:of|ng)?\s*$", before)
        if n:
            found[code] = float(n.group(1))
    if matches and not found:
        nums = re.findall(r"\b\d+(?:\.\d+)?\b", text)
        if len(nums) == 1:
            found = {config.ITEM_ALIASES[m.group(1)]: float(nums[0]) for m in matches}
    return found, [config.ITEM_ALIASES[m.group(1)] for m in matches]


def stock_verb(text):
    if re.search(ADD_WORDS, text):
        return "add"
    if re.search(REMOVE_WORDS, text):
        return "remove"
    if re.search(SET_WORDS, text):
        return "set"
    return None


# --------------------------------------------------------------- formatting
def peso(x):
    return f"₱{x:,.0f}"


def nice_date(d):
    d = d if isinstance(d, date) else datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%a, %b %d").replace(" 0", " ")


def reply(text, intent, **extra):
    return {"reply": text, "intent": intent, **extra}


# ------------------------------------------------------------- stock change
def _item_names(conn):
    return {i["code"]: i["name"] for i in db.get_items(conn, active_only=False)}


def _fmt(x):
    return "-" if x is None else f"{x:g}"


def plan_stock_change(ctx, verb, qtys, d):
    """Work out exactly what a command would change. Nothing is written here.

    closing N -> cups: usage = Opening - N (N is what's left at the counter); others: usage = Starting - N
    add N     -> Starting (and Stocks for cups) go up by N
    remove N  -> Usage goes up by N (used, spoiled or thrown away)
    """
    tmpl = db.get_log_or_template(ctx.conn, ctx.branch_id, d.isoformat())
    changes, problems = [], []
    for code, q in qtys.items():
        cur = tmpl["items"].get(code)
        if cur is None:
            continue
        new, cup = dict(cur), code in config.CUP_CODES
        if verb == "set":
            base = cur["opening"] if cup and cur["opening"] is not None else cur["starting"]
            if q > base + 1e-9:
                problems.append(f"{code}: closing {q:g} is more than the {'opening' if cup and cur['opening'] is not None else 'starting'} count ({base:g})")
                continue
            new["usage"] = base - q
        elif verb == "add":
            new["starting"] = cur["starting"] + q
            if cup and cur["stocks"] is not None:
                new["stocks"] = cur["stocks"] + q
        else:
            new["usage"] = (cur["usage"] or 0.0) + q
        if new["usage"] is not None:
            new["ending"] = new["starting"] - new["usage"]
            if cup and new["opening"] is not None:
                new["closing"] = new["opening"] - new["usage"]
        changes.append({"code": code, "before": cur, "after": new})
    return {"date": d.isoformat(), "verb": verb, "existed": tmpl["exists"], "changes": changes, "problems": problems}


def describe_plan(plan, names):
    verb = {"set": "Set closing count", "add": "Add stock", "remove": "Remove stock (counts as used)"}[plan["verb"]]
    lines = [f"{verb} for **{nice_date(plan['date'])}**:"]
    for c in plan["changes"]:
        b, a = c["before"], c["after"]
        bits = []
        if a["starting"] != b["starting"]:
            bits.append(f"starting {_fmt(b['starting'])} → **{a['starting']:g}**")
        if a["usage"] != b["usage"]:
            bits.append(f"used {_fmt(b['usage'])} → **{a['usage']:g}**")
        if a["ending"] is not None and a["ending"] != b["ending"]:
            bits.append(f"end → **{a['ending']:g}**")
        if a["closing"] is not None and a["closing"] != b["closing"]:
            bits.append(f"closing {a['closing']:g}")
        lines.append(f"- {names.get(c['code'], c['code'])}: " + ", ".join(bits))
    for pr in plan.get("problems", []):
        lines.append(f"- skipped, {pr}")
    lines.append("Confirm this change?")
    return "\n".join(lines)


def apply_plan(ctx, plan):
    d = plan["date"]
    if not auth.can_edit_date(ctx.user, d, ctx.today):
        return reply("Staff can only change today's or yesterday's log. Ask a manager to edit older days.", "denied")
    tmpl = db.get_log_or_template(ctx.conn, ctx.branch_id, d)
    items = {c: dict(v) for c, v in tmpl["items"].items()}
    for ch in plan["changes"]:
        items[ch["code"]] = dict(ch["after"])
    log = db.upsert_log(ctx.conn, ctx.branch_id, d, tmpl["sales"], items, tmpl["notes"], ctx.user["username"])
    db.audit(ctx.conn, ctx.user["username"], ctx.branch_id, "mapo_stock",
             {"date": d, "existed": plan["existed"],
              "changes": [{"code": c["code"], "before": c["before"]} for c in plan["changes"]]})
    if ctx.after_save:
        ctx.after_save(log)
    names = _item_names(ctx.conn)
    done = ", ".join(
        f"{names.get(c['code'], c['code'])} " + (f"end {c['after']['ending']:g}" if c["after"]["ending"] is not None
                                                  else f"starting {c['after']['starting']:g}") for c in plan["changes"])
    note = "" if tmpl["exists"] or tmpl["sales"] else " Remember to enter the day's sales in the Logbook."
    return reply(f"Saved for {nice_date(d)}: {done}.{note} Say \"undo\" to reverse it.", "stock_saved", refresh=True)


def undo_last(ctx):
    row = ctx.conn.execute(
        "SELECT * FROM audit_log WHERE username=? AND branch_id=? AND action='mapo_stock' AND undone=0 "
        "ORDER BY id DESC LIMIT 1", (ctx.user["username"], ctx.branch_id)).fetchone()
    if not row:
        return reply("There is no recent stock change of yours to undo.", "undo")
    detail = json.loads(row["detail"])
    d = detail["date"]
    if not auth.can_edit_date(ctx.user, d, ctx.today):
        return reply("That change is too old for you to undo. Ask a manager.", "denied")
    if not detail["existed"]:
        ctx.conn.execute("DELETE FROM daily_logs WHERE branch_id=? AND log_date=?", (ctx.branch_id, d))
        ctx.conn.execute("DELETE FROM log_items WHERE branch_id=? AND log_date=?", (ctx.branch_id, d))
    else:
        cur = db.load_logs(ctx.conn, ctx.branch_id, d, d).get(d)
        if cur:
            items = cur["items"]
            for ch in detail["changes"]:
                items[ch["code"]] = dict(ch["before"])
            db.upsert_log(ctx.conn, ctx.branch_id, d, cur["sales"], items, cur["notes"], ctx.user["username"])
    ctx.conn.execute("UPDATE audit_log SET undone=1 WHERE id=?", (row["id"],))
    ctx.conn.commit()
    db.audit(ctx.conn, ctx.user["username"], ctx.branch_id, "mapo_undo", {"of": row["id"], "date": d})
    return reply(f"Undone. The {nice_date(d)} stock change is reversed.", "undo", refresh=True)


def confirm(ctx, pending_id, accept):
    row = ctx.conn.execute("SELECT * FROM pending_actions WHERE id=? AND username=? AND branch_id=?",
                           (pending_id, ctx.user["username"], ctx.branch_id)).fetchone()
    if not row or row["expires_at"] < db.now_iso():
        return reply("That request expired. Please send the command again.", "expired")
    ctx.conn.execute("DELETE FROM pending_actions WHERE id=?", (pending_id,))
    ctx.conn.commit()
    if not accept:
        return reply("Cancelled. Nothing was changed.", "cancel")
    return apply_plan(ctx, json.loads(row["payload"]))


def _latest_pending(ctx):
    return ctx.conn.execute(
        "SELECT id FROM pending_actions WHERE username=? AND branch_id=? AND expires_at>? "
        "ORDER BY expires_at DESC LIMIT 1", (ctx.user["username"], ctx.branch_id, db.now_iso())).fetchone()


# ------------------------------------------------------------- info intents
def answer_sales(ctx, d):
    log = db.load_logs(ctx.conn, ctx.branch_id, d.isoformat(), d.isoformat()).get(d.isoformat())
    if not log or log["sales"] <= 0:
        when = "today" if d == ctx.today else nice_date(d)
        return reply(f"No sales are logged for {when} yet.", "sales")
    key, label = analytics.tier_for(log["sales"], ctx.th)
    return reply(f"Sales for **{nice_date(d)}**: **{peso(log['sales'])}** ({label}).", "sales")


def answer_forecast(ctx, scope, d=None):
    payload, refreshing = forecast_service.get_forecast(ctx.conn, ctx.branch_id)
    rows = [r for r in payload["forecast"] if r["date"] > ctx.today.isoformat() or r["date"] == ctx.today.isoformat()]
    if not rows:
        return reply(payload["notes"][0] if payload["notes"] else "No forecast yet.", "forecast")
    model = payload.get("model_label")
    if scope == "tomorrow":
        want = (d or ctx.today + timedelta(days=1)).isoformat()
        r = next((x for x in rows if x["date"] == want), None)
        if not r:
            return reply("I can't forecast that date yet.", "forecast")
        key, label = analytics.tier_for(r["yhat"], ctx.th)
        return reply(f"Forecast for **{nice_date(r['date'])}**: **{peso(r['yhat'])}** "
                     f"(likely {peso(r['lower'])} to {peso(r['upper'])}), {label}. Model: {model}.", "forecast")
    n = 7 if scope == "week" else 30
    start = ctx.today + timedelta(days=1)
    sel = [r for r in rows if r["date"] >= start.isoformat()][:n]
    total = sum(r["yhat"] for r in sel)
    best = max(sel, key=lambda r: r["yhat"])
    return reply(f"Next {len(sel)} days: about **{peso(total)}** in total, {peso(total / len(sel))} per day. "
                 f"Strongest day: {nice_date(best['date'])} ({peso(best['yhat'])}). Model: {model}.", "forecast")


def answer_inventory(ctx, codes):
    logs = db.load_logs(ctx.conn, ctx.branch_id)
    rec = analytics.reorder_suggestions(logs, db.get_items(ctx.conn), ctx.settings)
    if not rec["rows"]:
        return reply("No stock counts are logged yet.", "inventory")
    rows = [r for r in rec["rows"] if not codes or r["code"] in codes]
    lines = [f"Stock as of **{nice_date(rec['stock_date'])}**:"]
    for r in rows:
        extra = f", about {r['days_cover']:g} days left" if r["days_cover"] is not None else ""
        flag = {"critical": " ⚠ will run out before a restock arrives", "reorder": " (reorder soon)"}.get(r["status"], "")
        lines.append(f"- {r['name']}: **{r['stock']:g}** {r['unit']}{extra}{flag}")
    return reply("\n".join(lines), "inventory")


def answer_reorder(ctx):
    logs = db.load_logs(ctx.conn, ctx.branch_id)
    rec = analytics.reorder_suggestions(logs, db.get_items(ctx.conn), ctx.settings)
    todo = [r for r in rec["rows"] if r["order_qty"] > 0]
    if not todo:
        return reply("Nothing needs ordering right now. Stock covers the forecast demand.", "reorder")
    lines = ["Suggested order:"] + [
        f"- {r['name']}: **{r['order_qty']}** {r['unit']} ({r['stock']:g} left)" for r in todo]
    return reply("\n".join(lines), "reorder")


def answer_profit(ctx, d=None):
    items = db.get_items(ctx.conn)
    start = (ctx.today - timedelta(days=6)).isoformat()
    rep = analytics.profit_report(db.load_logs(ctx.conn, ctx.branch_id, start, ctx.today.isoformat()),
                                  items, float(ctx.settings["op_cost"]))
    t = rep["totals"]
    if not t["days"]:
        return reply("No sales logged in the last 7 days.", "profit")
    if not rep["costs_configured"]:
        return reply(f"Last {t['days']} days: sales **{peso(t['sales'])}**, operating cost {peso(t['op_cost'])}. "
                     "Set item costs in Settings to see gross margin and profit.", "profit")
    return reply(f"Last {t['days']} days: sales **{peso(t['sales'])}**, ingredient cost {peso(t['cogs'])}, "
                 f"margin {t['margin_pct']}%, after operating cost **{peso(t['net'])}**. "
                 f"Break-even is about {peso(rep['breakeven_sales_per_day'])} per day.", "profit")


def answer_best(ctx):
    start = (ctx.today - timedelta(days=29)).isoformat()
    rep = analytics.profit_report(db.load_logs(ctx.conn, ctx.branch_id, start, ctx.today.isoformat()),
                                  db.get_items(ctx.conn), float(ctx.settings["op_cost"]))
    top = rep["best_sellers"][:5]
    if not top:
        return reply("Not enough logged usage yet.", "best")
    return reply("Top sellers (last 30 days, by units used):\n" + "\n".join(
        f"- {r['name']}: {r['units']:g} {r['unit']}" for r in top), "best")


def answer_trend(ctx):
    s = analytics.sales_stats(db.load_logs(ctx.conn, ctx.branch_id), ctx.today, 7)
    if s["change_pct"] is None:
        return reply(f"The last {s['days']} logged days average {peso(s['avg'])}. I need two weeks to compare.", "trend")
    way = "up" if s["change_pct"] >= 0 else "down"
    return reply(f"The last 7 logged days average **{peso(s['avg'])}**, {way} {abs(s['change_pct'])}% "
                 f"from {peso(s['prev_avg'])} the week before.", "trend")


def answer_targets(ctx):
    t = ctx.th
    return reply("Daily sales tiers:\n"
                 f"- Operating Deficit: below {peso(t['deficit'])}\n- Underperforming: {peso(t['deficit'])} to {peso(t['under'])}\n"
                 f"- Below Average: {peso(t['under'])} to {peso(t['below'])}\n- Acceptable: {peso(t['below'])} to {peso(t['high'])}\n"
                 f"- High-Performing: above {peso(t['high'])}", "targets")


# ------------------------------------------------------- optional ML fallback
_ml = {"loaded": False, "pipe": None}
ML_MAP = [("forecast", "forecast"), ("predict", "forecast"), ("sale", "sales"), ("inventory", "inventory"),
          ("stock", "inventory"), ("target", "targets"), ("tier", "targets")]


def ml_intent(text):
    """Best-effort use of the trained TF-IDF + logistic regression model, if it is present."""
    if not _ml["loaded"]:
        _ml["loaded"] = True
        try:
            import joblib
            _ml["pipe"] = joblib.load(config.NLP_MODEL_FILE)
        except Exception:
            _ml["pipe"] = None
    pipe = _ml["pipe"]
    if pipe is None:
        return None
    try:
        probs = pipe.predict_proba([text])[0]
        label = str(pipe.classes_[probs.argmax()]).lower()
        if probs.max() < 0.55:
            return None
        return next((v for k, v in ML_MAP if k in label), None)
    except Exception:
        return None


# --------------------------------------------------------------------- main
def handle(ctx, raw):
    text = re.sub(r"\s+", " ", (raw or "").strip().lower())[:300]
    if not text:
        return reply("Type a question or a stock update. Say \"help\" to see examples.", "empty")

    pend = _latest_pending(ctx)
    if pend and re.fullmatch(r"(yes|y|oo|sige|confirm|ok|okay|go|tama)[.!]?", text):
        return confirm(ctx, pend["id"], True)
    if pend and re.fullmatch(r"(no|n|hindi|cancel|huwag|wag|stop)[.!]?", text):
        return confirm(ctx, pend["id"], False)
    if re.search(r"\b(undo|ibalik|i-undo)\b", text):
        return undo_last(ctx)
    if re.fullmatch(r"(hi|hello|hey|kumusta|musta|good (morning|afternoon|evening))[.!]?", text):
        return reply("Hi! I'm MAPO. Ask about sales, forecasts or stock, or tell me a closing count. Say \"help\" for examples.", "greeting")
    if re.search(r"\b(help|tulong|what can you do)\b", text):
        return reply(HELP, "help")

    d, rest = parse_date(text, ctx.today)
    qtys, mentioned = extract_item_quantities(rest)
    verb = stock_verb(rest)

    # Stock commands (writes) need quantities
    if verb and qtys and not re.search(r"\b(how many|ilan|magkano|what|ano)\b", rest):
        target = d or ctx.today
        if target > ctx.today:
            return reply("I can't change stock for a future date.", "denied")
        if not auth.can_edit_date(ctx.user, target.isoformat(), ctx.today):
            return reply("Staff can only change today's or yesterday's log. Ask a manager to edit older days.", "denied")
        plan = plan_stock_change(ctx, verb, qtys, target)
        if not plan["changes"]:
            return reply("Nothing to change. " + ("; ".join(plan["problems"]) + "." if plan["problems"] else "I couldn't match those items."), "unknown")
        pid = uuid.uuid4().hex[:12]
        expires = (config.now_ph() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        ctx.conn.execute("INSERT INTO pending_actions(id,username,branch_id,payload,expires_at) VALUES(?,?,?,?,?)",
                         (pid, ctx.user["username"], ctx.branch_id, json.dumps(plan), expires))
        ctx.conn.commit()
        return reply(describe_plan(plan, _item_names(ctx.conn)), "stock_pending", pending={"id": pid})
    if verb and mentioned and not qtys:
        return reply("I found the item but no quantity. Try \"closing medio 5\".", "unknown")

    low = rest
    if re.search(r"\b(profit|tubo|margin|kita)\b", low) and not re.search(r"\bsales?\b", low):
        return answer_profit(ctx)
    if re.search(r"\b(best.?sell\w*|top (seller|item)s?|pinakamabenta|pinaka ?mabenta)\b", low):
        return answer_best(ctx)
    if re.search(r"\b(forecast|predict\w*|projection|expected|inaasahan|hula|estimate)\b", low):
        if re.search(r"\b(month|buwan)\b", low):
            return answer_forecast(ctx, "month")
        if re.search(r"\b(week|linggo|7 days)\b", low) and not d:
            return answer_forecast(ctx, "week")
        return answer_forecast(ctx, "tomorrow", d)
    if re.search(r"\b(order|reorder|kulang|restock needed|need to buy|bilhin)\b", low):
        return answer_reorder(ctx)
    if re.search(r"\b(trend|compare|comparison|vs|versus|kumpara|last week)\b", low):
        return answer_trend(ctx)
    if re.search(r"\b(target|tier|goal|threshold)s?\b", low):
        return answer_targets(ctx)
    if re.search(r"\b(sales?|benta|nabenta|revenue|income|magkano)\b", low):
        return answer_sales(ctx, d or ctx.today)
    if re.search(r"\b(stock|inventory|natitira|left|remaining|ilan|supply)\b", low) or mentioned:
        return answer_inventory(ctx, mentioned)

    guess = ml_intent(text)
    if guess == "forecast":
        return answer_forecast(ctx, "tomorrow", d)
    if guess == "sales":
        return answer_sales(ctx, d or ctx.today)
    if guess == "inventory":
        return answer_inventory(ctx, mentioned)
    if guess == "targets":
        return answer_targets(ctx)
    return reply("I didn't catch that. Try \"sales today\", \"forecast tomorrow\", \"closing medio 5\", "
                 "\"what should I order\", or say \"help\".", "unknown")
