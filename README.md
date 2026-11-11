# Mad Potato Management System (v2)

A web system for running a Mad Potato branch day to day: a digital logbook, stock tracking with reorder suggestions, a sales forecast that checks itself, profit tracking, reports, and MAPO, a chat assistant that understands English and Taglish.

Built with Flask, SQLite, scikit-learn, Prophet and plain JavaScript. It installs on a phone like an app.

## What's new in v2

**Safe and reliable**
- Sign-in with three roles (staff, manager, owner). The first version had no login.
- SQLite database instead of an in-memory dict and a CSV file.
- Audit trail: who saved, changed, deleted or exported what, and when.
- Input checks, login throttling, CSRF header check, secure session cookies, security headers, no `debug=True`, no open CORS.
- Philippine time (UTC+8) everywhere. On a UTC server the old "today" was wrong for 8 hours a day.

**Better forecasting**
- Models compete in a rolling backtest: 7-day average, weekday pattern, gradient boosting, SARIMAX, Prophet (basic) and Prophet (tuned), plus an ensemble of the best two. The lowest error wins.
- Payday, Philippine holidays and your own events (school fair, promo) are used as forecast drivers.
- The plain 7-day average is always in the race. If nothing beats it, the app says so.
- The Forecast page shows accuracy for every model (average miss, % off, improvement over the 7-day average).
- Fast models answer immediately. Prophet and SARIMAX run in the background and the result is cached until new data arrives.
- Forecast ranges (likely low and high) come from backtest error, not a guess.
- Per-item demand forecast, which feeds the reorder list.

**Business features**
- Reorder list: forecast use over the supplier lead time plus a safety cushion, minus stock on hand. "Copy supplier message" for Messenger or SMS.
- Optional Telegram alert once a day when something will run out before a restock arrives.
- Profit tracking: ingredient cost per item, gross margin, break-even sales per day, best sellers.
- "Things to check": unusual sales days, cups used that don't match sales (possible waste or miscount), and data entry slips.
- Weekly and monthly reports. One-page PDF and a full Excel workbook.
- Multiple branches with a branch switcher for owners.
- Starting stock carries over from the previous day automatically.

**MAPO**
- Several items per message: `closing medio 5, grande 3`.
- Taglish: `benta kahapon`, `forecast bukas`, `bawas 2 jumbo`, `dagdag 50 medio`.
- Stock changes show what will change and wait for Confirm. `undo` reverses your last change.
- Also answers: sales, forecast (tomorrow, week, month), stock, what to order, profit, best sellers, trend, targets.
- The original TF-IDF + logistic regression model (`nlp_pipeline.joblib`) is still used as a fallback for questions the rules don't recognise, if the file is present.

**Logbook (same flow as the V1 digitized logbook)**
- Two cards: **Usage count** (Starting, Usage, End) and **Cup stock** (Opening, Stocks, Closing).
- Every item: `Starting − Usage = End`. Cup items: `Opening + Stocks = Starting`, `Opening − Usage = Closing`, `Stocks + Closing = End`.
- Type the usage and End is worked out for you. For cups you can type the Closing instead and the usage is worked out.
- Starting accepts sums like `12+8`. Enter jumps to the next item.
- A new day is pre-filled from yesterday: Starting = yesterday's End, Opening = yesterday's Closing, Stocks = the rest. Yellow cells were calculated; type over them.
- A half-filled day (morning count only) can be saved. Usage and End stay empty until you enter them.
- Stock counts may go below zero, as in the V1 sheet. The row turns red so it gets rechecked.

**Interface**
- Green and yellow theme.
- New design, mobile first, with a bottom tab bar on phones.
- Installable (PWA). Log entries made while offline are stored on the device and uploaded when the connection returns.

## Roles

| | Staff | Manager | Owner |
|---|---|---|---|
| Log today and yesterday, use MAPO, see stock and forecast | yes | yes | yes |
| Edit older days, delete a day | | yes | yes |
| Profit, reports, insights, settings, costs, audit trail, exports | | yes | yes |
| Add staff in their branch | | yes | yes |
| Add managers, owners, branches; backup and restore | | | yes |

## Run it on your computer

```bash
python -m venv venv
venv\Scripts\activate            # Windows   (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
set ADMIN_PASSWORD=choose-something-strong    # PowerShell: $env:ADMIN_PASSWORD="..."
python app.py
```

Open http://localhost:5000 and sign in as `owner`. If you don't set `ADMIN_PASSWORD`, a random one is printed in the terminal on first run.

Want sample data to try everything (or to demo)? `python seed_demo.py` fills an empty database with 75 made-up days. The item costs it sets are placeholders. Replace them in Settings.

To bring in your old history: Settings > Import history, or `python migrate_csv.py sales_data.csv`.

The importer reads the original sheet layout: one row per item per day with the date only on the first row of each day, and the columns `Date, Item_Code, Starting_Stock, Ending_Stock, Daily_Usage, Daily_Total_Sales, Opening, Stocks, Closing`. Opening written as a sum (like `6+20`) is added up. Numbers are kept as written, including counts below zero. It also reads a one-row-per-day layout with columns like `M_Start`, `M_End`, `M_Usage`. Importing the same file again is safe.

After importing, open the Logbook: today's Starting, Opening and Stocks are filled in from the last imported day's End and Closing.

## Deploy on Render

1. Push this folder to your GitHub repo.
2. On Render, create a Web Service from the repo (or use `render.yaml`).
3. Set environment variables: `SECRET_KEY` (long random text), `ADMIN_PASSWORD`, `COOKIE_SECURE=1`, and `DB_PATH=/var/data/mapo.db`.
4. Start command: `gunicorn app:app --workers 1 --threads 4 --timeout 120`. Keep it at one worker, because the background forecast uses a thread.

### Keeping your data safe

SQLite lives in a file. On Render's free plan the disk is wiped when the service restarts or redeploys, which would erase your logs. Pick one:

- Use a paid plan with a persistent disk (`render.yaml` is set up for this), or
- Turn on the Google Sheets backup (below) **and** download a backup from Settings regularly (owner only). Restore merges it back in.

### Google Sheets backup

Open `apps_script/Code.gs`, follow the steps in its header, and paste the web app link into Settings > Google Sheets backup. Replace any older Apps Script, because the data format changed.

### Telegram low-stock alerts (optional)

Create a bot with @BotFather, then set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` on the server.

## Behaviour that changed from v1 (check these against your paper)

- **Stock maths** follows the V1 logbook: `Starting − Usage = End`, with Opening, Stocks and Closing for the cup items. Purchases are written into Starting (for example `53+50`) or into Opening for cups, exactly like the V1 sheet.
- **MAPO `closing`**: for cup items it means the cups left at the counter (Closing), like the V1 sheet. For other items it means the count left.
- **Tier thresholds** (₱2,000 / 3,000 / 3,800 / 4,800) and the ₱2,000 operating cost are now editable in Settings. The defaults are the ones from v1.
- **Staff** can only edit today and yesterday.
- **Timezone** is fixed to Philippine time.

## Project layout

```
app.py              routes and security
db.py               schema, saving and loading logs
auth.py             login, roles, branch access
forecasting.py      models, backtest, model selection
forecast_service.py caching and background refresh
analytics.py        tiers, anomalies, reorder, profit, summaries
assistant.py        MAPO
exports.py          Excel and PDF
integrations.py     Google Sheets and Telegram
ph_calendar.py      holidays and paydays
migrate_csv.py      import old CSV
seed_demo.py        demo data
templates/ static/  the web app (core.js, pages.js, admin.js, app.css, sw.js)
tests/test_api.py   backend tests
```

## Tests

```bash
pip install pytest
pytest
```

The 17 tests cover login and roles, validation, usage maths, carry-forward, forecasting on synthetic data, reorder, profit, exports, anomaly detection, MAPO confirm and undo (including Taglish), the V1 logbook maths, partial days, upgrading an older database, CSV import (both layouts, including the original sheet), and backup and restore.

## Design decisions you can explain

- **Why SQLite?** One small shop, one server, no extra service to run. The code uses plain SQL, so moving to PostgreSQL later is a contained change.
- **Why compare models instead of picking Prophet?** With only weeks of data a simple model can win. The backtest decides, and the 7-day average is the honesty check.
- **Why confirm before MAPO writes?** A chat command can be misheard or mistyped, and the stock count drives the forecast and reorders.
- **Why prediction ranges from backtest error?** They show how wrong the model has really been, not how sure it claims to be.

## Known limits

- With under about 4 weeks of data, weekday patterns are rough and the 7-day average often wins. This improves as you log more days.
- Sales are one total per day, so per-item revenue is an estimate (units × price).
- Days nobody logged are simply missing. The forecast skips them, and the app lists them under "Things to check".
- Item prices and costs are the same for every branch.
- Holy Week and fixed-date holidays are built in. Eid and special days declared each year are not, so add them as events.
- One server process. Many branches logging at once would need PostgreSQL and a job queue.
