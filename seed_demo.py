"""Fill an EMPTY database with made-up demo data, so you can try every feature.

    python seed_demo.py            # refuses if the database already has sales
    python seed_demo.py --force    # adds demo days anyway (don't do this on real data)

The numbers are invented (weekday pattern + payday bump + noise). Item costs and
prices are placeholders: replace them in Settings with your real ones.
"""
import random
import sys
from datetime import timedelta

import config
import db

PLACEHOLDER_COSTS = {  # code: (cost, price) per unit, demo only
    "M": (14, 59), "G": (22, 89), "J": (30, 119), "MG": (38, 149), "2XL": (46, 179), "3XL": (56, 209),
    "JC": (10, 39), "F": (8, 25), "CP": (12, 39), "JJ": (16, 49), "BW": (7, 20),
}
SHARE = {"M": 0.30, "G": 0.22, "J": 0.14, "MG": 0.08, "2XL": 0.04, "3XL": 0.03, "JC": 0.10, "F": 0.05, "CP": 0.07, "JJ": 0.05, "BW": 0.04}
PRICE_AVG = 78


def main(force=False, days=75):
    conn = db.connect()
    db.init_db(conn)
    branch = conn.execute("SELECT id FROM branches ORDER BY id LIMIT 1").fetchone()["id"]
    if not force and conn.execute("SELECT COUNT(*) FROM daily_logs").fetchone()[0]:
        sys.exit("The database already has logs. Use --force to add demo data anyway.")
    for code, (cost, price) in PLACEHOLDER_COSTS.items():
        conn.execute("UPDATE items SET unit_cost=?, unit_price=?, lead_time_days=2 WHERE code=?", (cost, price, code))
    conn.commit()

    random.seed(7)
    today = config.today_ph()
    base = {0: 3050, 1: 3150, 2: 3350, 3: 3500, 4: 4300, 5: 4900, 6: 4200}
    end = {c: 150.0 for c in PLACEHOLDER_COSTS}
    closing = {c: 6.0 for c in config.CUP_CODES}
    for i in range(days, 0, -1):               # up to yesterday, leaving today open to log
        d = today - timedelta(days=i)
        sales = base[d.weekday()] * random.uniform(0.9, 1.1) + (650 if d.day in (15, 30) else 0)
        if random.random() < 0.04:
            sales *= 0.55                       # a rainy day
        items = {}
        for c, share in SHARE.items():
            used = round(sales * share / PRICE_AVG * random.uniform(0.9, 1.1))
            start = end[c]
            if start - used < used * 2.5:       # buy more when about 2 days remain
                start += round(used * 5, 0)
            row = {"starting": start, "usage": used}
            if c in config.CUP_CODES:
                opening = min(closing[c] + 20, start)   # counter cups: yesterday's closing + 20 prepared
                row.update(opening=opening, stocks=start - opening, closing=opening - used)
                closing[c] = max(opening - used, 0.0)
            items[c] = row
            end[c] = start - used
        db.upsert_log(conn, branch, d.isoformat(), round(sales), items, "demo data", "demo")
    print(f"Added {days} days of demo data for branch {branch}.")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
