import os
import random
import sys
import tempfile
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HDR = {"X-Requested-With": "mapo"}


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("ADMIN_PASSWORD", "ownerpass123")
    import config
    monkeypatch.setattr(config, "DB_PATH", os.path.join(tmp, "t.db"))
    import app as appmod
    app = appmod.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def login(c, user="owner", pw="ownerpass123"):
    return c.post("/api/auth/login", json={"username": user, "password": pw}, headers=HDR)


def seed(c, days=45):
    """Weekday-patterned sales with a payday bump, ending today."""
    import config
    random.seed(1)
    today = config.today_ph()
    base = {0: 3000, 1: 3200, 2: 3400, 3: 3600, 4: 4800, 5: 5200, 6: 4400}
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        sales = base[d.weekday()] + random.randint(-250, 250) + (700 if d.day in (15, 30) else 0)
        items = {"M": {"starting": 100, "restock": 0, "ending": 100 - sales // 90},
                 "G": {"starting": 80, "restock": 0, "ending": 80 - sales // 180}}
        r = c.put(f"/api/logs/{d.isoformat()}", json={"sales": sales, "items": items, "notes": ""}, headers=HDR)
        assert r.status_code == 200, r.get_json()


def test_requires_login(client):
    assert client.get("/api/dashboard").status_code == 401
    assert client.post("/api/auth/login", json={}).status_code == 403  # missing CSRF header


def test_login_throttle_and_wrong_password(client):
    assert login(client, pw="nope").status_code == 401
    assert login(client).status_code == 200
    me = client.get("/api/auth/me").get_json()
    assert me["user"]["role"] == "owner" and me["can_manage"]


def test_roles_and_staff_limits(client):
    login(client)
    r = client.post("/api/users", json={"username": "ana", "password": "staffpass1", "role": "staff", "branch_id": 1}, headers=HDR)
    assert r.status_code == 200
    client.post("/api/auth/logout", headers=HDR)
    assert login(client, "ana", "staffpass1").status_code == 200
    assert client.get("/api/settings").status_code == 403
    assert client.get("/api/audit").status_code == 403
    items = client.get("/api/items").get_json()
    assert "unit_cost" not in items[0]
    import config
    old = (config.today_ph() - timedelta(days=5)).isoformat()
    r = client.put(f"/api/logs/{old}", json={"sales": 1000, "items": {}}, headers=HDR)
    assert r.status_code == 403
    future = (config.today_ph() + timedelta(days=2)).isoformat()
    assert client.put(f"/api/logs/{future}", json={"sales": 1, "items": {}}, headers=HDR).status_code == 400
    today = config.today_ph().isoformat()
    assert client.put(f"/api/logs/{today}", json={"sales": 3500, "items": {"M": {"starting": 50, "restock": 0, "ending": 30}}}, headers=HDR).status_code == 200


def test_validation(client):
    login(client)
    import config
    d = config.today_ph().isoformat()
    assert client.put(f"/api/logs/{d}", json={"sales": -5, "items": {}}, headers=HDR).status_code == 400
    assert client.put(f"/api/logs/{d}", json={"sales": "abc", "items": {}}, headers=HDR).status_code == 400
    assert client.put(f"/api/logs/{d}", json={"sales": 100, "items": {"ZZ": {}}}, headers=HDR).status_code == 400
    assert client.put("/api/logs/not-a-date", json={"sales": 1}, headers=HDR).status_code == 400


def test_v1_logbook_flow_cups_and_carry_forward(client):
    """Opening + Stocks = Starting, Starting - Usage = End, Opening - Usage = Closing (same as the V1 logbook)."""
    login(client)
    import config
    today = config.today_ph()
    y, t = (today - timedelta(days=1)).isoformat(), today.isoformat()
    items = {"M": {"opening": 26, "stocks": 33, "starting": 59, "usage": 20},     # cup item
             "F": {"starting": 100, "usage": 12}}                                  # not a cup item
    log = client.put(f"/api/logs/{y}", json={"sales": 3000, "items": items}, headers=HDR).get_json()
    m, f = log["items"]["M"], log["items"]["F"]
    assert (m["ending"], m["closing"], m["stocks"]) == (39, 6, 33)
    assert f["ending"] == 88 and f["closing"] is None
    # next morning: Starting = yesterday's End, Opening = yesterday's Closing, Stocks = the rest
    tmpl = client.get(f"/api/logs/{t}").get_json()
    assert not tmpl["exists"]
    assert (tmpl["items"]["M"]["starting"], tmpl["items"]["M"]["opening"], tmpl["items"]["M"]["stocks"]) == (39, 6, 33)
    assert tmpl["items"]["M"]["usage"] is None and tmpl["items"]["M"]["ending"] is None
    assert tmpl["items"]["F"]["starting"] == 88 and tmpl["items"]["F"]["opening"] is None


def test_typed_closing_gives_usage_and_partial_day_is_kept(client):
    login(client)
    import config
    t = config.today_ph().isoformat()
    # only the morning count: no usage yet, so End stays empty
    r = client.put(f"/api/logs/{t}", json={"sales": 0, "items": {"G": {"opening": 6, "stocks": 3, "starting": 9}}}, headers=HDR).get_json()
    assert r["items"]["G"]["usage"] is None and r["items"]["G"]["ending"] is None
    # evening: staff type the counter closing instead of usage
    r = client.put(f"/api/logs/{t}", json={"sales": 3500, "items": {"G": {"opening": 26, "stocks": 3, "starting": 29, "closing": 6}}}, headers=HDR).get_json()
    assert r["items"]["G"]["usage"] == 20 and r["items"]["G"]["ending"] == 9
    # usage can't be negative, but a count below zero is allowed (the V1 sheet had them)
    assert client.put(f"/api/logs/{t}", json={"sales": 1, "items": {"G": {"starting": 5, "usage": -1}}}, headers=HDR).status_code == 400
    r = client.put(f"/api/logs/{t}", json={"sales": 1, "items": {"G": {"starting": 5, "usage": 8}}}, headers=HDR).get_json()
    assert r["items"]["G"]["ending"] == -3


def test_old_database_is_upgraded_without_losing_data(tmp_path):
    import sqlite3
    import db
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE branches(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
      INSERT INTO branches(name,code,created_at) VALUES('Mad Potato - Teresa','TERESA','2026-01-01');
      CREATE TABLE daily_logs(branch_id INTEGER NOT NULL, log_date TEXT NOT NULL, sales REAL NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '', updated_by TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(branch_id, log_date));
      CREATE TABLE log_items(branch_id INTEGER NOT NULL, log_date TEXT NOT NULL, item_code TEXT NOT NULL, starting REAL NOT NULL DEFAULT 0, restock REAL NOT NULL DEFAULT 0, ending REAL NOT NULL DEFAULT 0, usage REAL NOT NULL DEFAULT 0, PRIMARY KEY(branch_id, log_date, item_code));
      INSERT INTO daily_logs VALUES(1,'2026-10-01',3500,'','x','2026-10-01 20:00:00');
      INSERT INTO log_items VALUES(1,'2026-10-01','M',100,0,60,40);
    """)
    con.commit(); con.close()
    conn = db.connect(path)
    db.init_db(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(log_items)")]
    assert {"opening", "stocks", "closing"} <= set(cols)
    row = db.load_logs(conn, 1)["2026-10-01"]["items"]["M"]
    assert (row["starting"], row["ending"], row["usage"], row["opening"]) == (100, 60, 40, None)
    conn.close()


def test_forecast_and_model_selection(client):
    login(client)
    seed(client, 45)
    f = client.get("/api/forecast?days=14").get_json()
    assert f["model"] and len(f["forecast"]) == 14
    assert f["accuracy"]["folds"] >= 1
    names = {m["name"] for m in f["accuracy"]["models"]}
    assert {"ma7", "dow_avg", "gbr"} <= names
    row = f["forecast"][0]
    assert row["lower"] <= row["yhat"] <= row["upper"] and row["tier"]
    # cached second call is stable
    assert client.get("/api/forecast?days=14").get_json()["model"] == f["model"]


def test_forecast_small_data_is_honest(client):
    login(client)
    seed(client, 6)
    f = client.get("/api/forecast").get_json()
    assert f["accuracy"] is None and f["notes"]


def test_dashboard_reorder_insights_profit_exports(client):
    login(client)
    seed(client, 40)
    d = client.get("/api/dashboard").get_json()
    assert d["tomorrow"] and d["stock"] and "anomalies" in d
    r = client.get("/api/reorder").get_json()
    assert {x["code"] for x in r["rows"]} == {"M", "G"}
    items = client.get("/api/items").get_json()
    for it in items:
        it["unit_cost"], it["unit_price"] = (12, 40) if it["code"] in ("M", "G") else (0, 0)
    assert client.put("/api/items", json=items, headers=HDR).status_code == 200
    p = client.get("/api/profit").get_json()
    assert p["costs_configured"] and p["totals"]["gross"] > 0 and p["best_sellers"]
    assert client.get("/api/insights").get_json()["weekday"]["best_day"]
    assert client.get("/api/reports?period=week").status_code == 200
    xl = client.get("/api/export/excel")
    assert xl.status_code == 200 and xl.data[:2] == b"PK"
    pdf = client.get("/api/export/pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"


def test_anomaly_detected(client):
    login(client)
    seed(client, 40)
    import config
    d = (config.today_ph() - timedelta(days=1))
    client.put(f"/api/logs/{d.isoformat()}", json={"sales": 12000, "items": {}}, headers=HDR)
    a = client.get("/api/insights").get_json()["anomalies"]
    assert any(x["date"] == d.isoformat() and x["type"] == "sales" for x in a)


def test_settings_validation(client):
    login(client)
    bad = client.put("/api/settings", json={"tier_deficit": 5000}, headers=HDR)
    assert bad.status_code == 400
    ok = client.put("/api/settings", json={"op_cost": 2500, "tier_high": 5000}, headers=HDR)
    assert ok.status_code == 200
    assert client.put("/api/settings", json={"sheets_url": "https://evil.example.com/x"}, headers=HDR).status_code == 400


def test_mapo_confirm_undo_taglish(client):
    login(client)
    seed(client, 20)
    import config
    t = config.today_ph().isoformat()
    before = client.get(f"/api/logs/{t}").get_json()["items"]["M"]["ending"]

    r = client.post("/api/chat", json={"message": "closing medio 5, grande 3"}, headers=HDR).get_json()
    assert r["intent"] == "stock_pending" and r["pending"]
    # nothing written yet
    assert client.get(f"/api/logs/{t}").get_json()["items"]["M"]["ending"] == before
    r2 = client.post("/api/chat/confirm", json={"id": r["pending"]["id"], "accept": True}, headers=HDR).get_json()
    assert r2["intent"] == "stock_saved"
    cur = client.get(f"/api/logs/{t}").get_json()["items"]
    assert cur["M"]["ending"] == 5 and cur["G"]["ending"] == 3
    u = client.post("/api/chat", json={"message": "undo"}, headers=HDR).get_json()
    assert u["intent"] == "undo"
    assert client.get(f"/api/logs/{t}").get_json()["items"]["M"]["ending"] == before

    # cancel path via text
    r = client.post("/api/chat", json={"message": "bawas 2 jumbo"}, headers=HDR).get_json()
    assert r["intent"] == "stock_pending"
    assert client.post("/api/chat", json={"message": "hindi"}, headers=HDR).get_json()["intent"] == "cancel"

    # read intents (including Taglish)
    for msg, intent in [("sales today", "sales"), ("benta kahapon", "sales"), ("forecast bukas", "forecast"),
                        ("forecast this week", "forecast"), ("stock", "inventory"), ("what should i order", "reorder"),
                        ("targets", "targets"), ("profit this week", "profit"), ("best sellers", "best"),
                        ("trend vs last week", "trend"), ("help", "help"), ("blah blah", "unknown")]:
        assert client.post("/api/chat", json={"message": msg}, headers=HDR).get_json()["intent"] == intent, msg


def test_mapo_parsing_units():
    import assistant
    from datetime import date
    q, _ = assistant.extract_item_quantities("add 50 cups of medio")
    assert q == {"M": 50.0}
    q, _ = assistant.extract_item_quantities("closing jumbo juice is 4 and jumbo 7")
    assert q == {"JJ": 4.0, "J": 7.0}
    d, rest = assistant.parse_date("sales nov 5", date(2026, 11, 10))
    assert d == date(2026, 11, 5)
    d, _ = assistant.parse_date("benta kahapon", date(2026, 11, 10))
    assert d == date(2026, 11, 9)


def test_csv_import_and_backup_restore(client):
    login(client)
    import io
    csv_text = "Date,Sales,M_Start,M_End,M_Usage\n2026-10-01,3500,100,60,40\n10/02/2026,4100,60,20,40\nbad,x,1,1,1\n"
    r = client.post("/api/import/csv", data={"file": (io.BytesIO(csv_text.encode()), "s.csv")}, headers=HDR,
                    content_type="multipart/form-data").get_json()
    assert r["imported"] == 2 and r["skipped_count"] == 1
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    res = client.post("/api/restore", data={"file": (io.BytesIO(backup.data), "b.json")}, headers=HDR,
                      content_type="multipart/form-data").get_json()
    assert res["restored_days"] == 2


def test_ph_calendar():
    import ph_calendar
    from datetime import date
    assert ph_calendar.holiday_name(date(2026, 12, 25)) == "Christmas Day"
    assert ph_calendar.holiday_name(date(2026, 4, 3)) == "Good Friday"
    assert ph_calendar.is_payday(date(2026, 11, 30))
    assert ph_calendar.is_payday(date(2026, 8, 14))  # 15th is Saturday -> Friday


def test_csv_import_original_sheet_layout(client):
    """One row per item per day, date only on the first row, Opening written as a sum, count below zero."""
    login(client)
    import io
    csv_text = (
        "Date,Item_Code,Starting_Stock,Ending_Stock,Daily_Usage,Restock_Added,Closing_Stock,Daily_Total_Sales,Opening,Stocks,Closing\r\n"
        "2025-01-01,M,180.0,135.0,45.0,130.0,5.0,3893.0,50.0,130.0,5.0\r\n"
        ",F,100.0,88.0,12.0,0.0,0.0,3893.0,,0.0,0.0\r\n"
        "2025-01-02,M,135.0,92.0,43.0,85.0,7.0,3652.0,50.0,85.0,7.0\r\n"
        ",F,88.0,80.0,8.0,0.0,0.0,3652.0,,0.0,0.0\r\n"
        "2025-01-03,M,142.0,-15.0,157.0,0.0,-15.0,4519.0,6+51,0.0,-15.0\r\n"
        ",F,80.0,70.0,10.0,0.0,0.0,4519.0,,0.0,0.0\r\n")
    r = client.post("/api/import/csv", data={"file": (io.BytesIO(csv_text.encode()), "sales_data.csv")},
                    headers=HDR, content_type="multipart/form-data").get_json()
    assert r["imported"] == 3 and r["negative_stock"] == 1 and r["first_date"] == "2025-01-01" and r["has_cup_columns"]
    m3 = client.get("/api/logs/2025-01-03").get_json()["items"]["M"]
    assert (m3["starting"], m3["usage"], m3["ending"]) == (142, 157, -15)          # kept as written
    assert (m3["opening"], m3["stocks"], m3["closing"]) == (57, 0, -15)            # "6+51" read as 57
    assert m3["restock"] == 50                                                      # 142 - yesterday's End 92
    f1 = client.get("/api/logs/2025-01-01").get_json()["items"]["F"]
    assert f1["opening"] is None and f1["closing"] is None and f1["ending"] == 88   # not a cup item
    d1 = client.get("/api/logs/2025-01-01").get_json()
    assert d1["sales"] == 3893 and d1["items"]["M"]["stocks"] == 130
    again = client.post("/api/import/csv", data={"file": (io.BytesIO(csv_text.encode()), "sales_data.csv")},
                        headers=HDR, content_type="multipart/form-data").get_json()
    assert again["imported"] == 3  # safe to re-import
