"""Central configuration for the Mad Potato Management System."""
import os
import secrets
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Point DB_PATH at a persistent disk in production (e.g. /var/data/mapo.db on Render).
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "mapo.db"))
LEGACY_CSV = os.path.join(BASE_DIR, "sales_data.csv")
NLP_MODEL_FILE = os.path.join(BASE_DIR, "nlp_pipeline.joblib")

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
SECRET_KEY_IS_EPHEMERAL = not os.environ.get("SECRET_KEY")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"

# Philippines has no daylight saving, so a fixed UTC+8 offset is always correct.
# (Render servers run in UTC, which made "today" wrong for 8 hours a day.)
PH_TZ = timezone(timedelta(hours=8))


def now_ph() -> datetime:
    return datetime.now(PH_TZ)


def today_ph():
    return now_ph().date()


# code, display name, unit, sort order
ITEM_DEFS = [
    ("M", "Medio (M)", "cups"),
    ("G", "Grande (G)", "cups"),
    ("J", "Jumbo (J)", "cups"),
    ("MG", "Mega (MG)", "cups"),
    ("2XL", "2XL", "cups"),
    ("3XL", "3XL", "cups"),
    ("JC", "Juice Cups (JC)", "cups"),
    ("F", "Fries (F)", "pcs"),
    ("CP", "Chicken Poppers (CP)", "pcs"),
    ("JJ", "Jumbo Juice (JJ)", "pcs"),
    ("BW", "Bottled Water (BW)", "pcs"),
]
ITEM_CODES = [c for c, _, _ in ITEM_DEFS]
# Items counted at the counter AND in reserve: Opening + Stocks = Starting, Opening - Usage = Closing.
CUP_CODES = ("M", "G", "J", "MG", "2XL", "3XL", "JC")

# Longest aliases first so "jumbo juice" wins over "jumbo".
ITEM_ALIASES = {
    "jumbo juice": "JJ", "juice cups": "JC", "juice cup": "JC", "chicken poppers": "CP",
    "bottled water": "BW", "medio": "M", "grande": "G", "jumbo": "J", "mega": "MG",
    "2xl": "2XL", "3xl": "3XL", "fries": "F", "fry": "F", "poppers": "CP",
    "water": "BW", "tubig": "BW", "juice": "JC",
}

ROLES = ("staff", "manager", "owner")

# Per-branch defaults. Tier thresholds keep the original business rules.
DEFAULT_SETTINGS = {
    "op_cost": "2000",         # minimum daily operating cost (PHP)
    "tier_deficit": "2000",    # below this = Operating Deficit
    "tier_under": "3000",      # below this = Underperforming
    "tier_below": "3800",      # below this = Below Average
    "tier_high": "4800",       # above this = High-Performing
    "cover_days": "7",         # how many days of stock a reorder should cover
    "alerts_enabled": "1",
    "sheets_url": "",
    "sheets_last_status": "",
}

TIERS = [
    # key, label
    ("deficit", "Operating Deficit"),
    ("bad", "Underperforming"),
    ("below-avg", "Below Average"),
    ("acceptable", "Acceptable"),
    ("successful", "High-Performing"),
]

# Optional alert channel (set both to enable Telegram low-stock alerts).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
