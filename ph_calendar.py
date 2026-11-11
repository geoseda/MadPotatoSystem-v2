"""Philippine holidays and paydays, used as demand drivers in the forecast.

Fixed-date and Holy Week holidays are built in. Presidential proclamations
(Eid, special non-working days, school events, promos) vary each year, so add
those on the Forecast page as custom events.
"""
import calendar
from datetime import date, timedelta
from functools import lru_cache

FIXED = [
    (1, 1, "New Year's Day"), (2, 25, "EDSA Revolution Anniversary"),
    (4, 9, "Araw ng Kagitingan"), (5, 1, "Labor Day"), (6, 12, "Independence Day"),
    (8, 21, "Ninoy Aquino Day"), (11, 1, "All Saints' Day"), (11, 2, "All Souls' Day"),
    (11, 30, "Bonifacio Day"), (12, 8, "Feast of the Immaculate Conception"),
    (12, 24, "Christmas Eve"), (12, 25, "Christmas Day"), (12, 30, "Rizal Day"),
    (12, 31, "New Year's Eve"),
]


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=32)
def holiday_map(year: int) -> dict:
    out = {date(year, m, d): name for m, d, name in FIXED}
    # National Heroes Day: last Monday of August
    last_aug = date(year, 8, calendar.monthrange(year, 8)[1])
    out[last_aug - timedelta(days=last_aug.weekday())] = "National Heroes Day"
    e = easter(year)
    out[e - timedelta(days=4)] = "Holy Wednesday"
    out[e - timedelta(days=3)] = "Maundy Thursday"
    out[e - timedelta(days=2)] = "Good Friday"
    out[e - timedelta(days=1)] = "Black Saturday"
    return out


def holiday_name(d: date):
    return holiday_map(d.year).get(d)


@lru_cache(maxsize=256)
def payday_dates(year: int, month: int) -> frozenset:
    """Common payroll dates: the 15th and the last day, moved to Friday if on a weekend."""
    last = calendar.monthrange(year, month)[1]
    out = set()
    for day in (15, last):
        d = date(year, month, day)
        if d.weekday() == 5:
            d -= timedelta(days=1)
        elif d.weekday() == 6:
            d -= timedelta(days=2)
        out.add(d)
    return frozenset(out)


def is_payday(d: date) -> bool:
    return d in payday_dates(d.year, d.month)


def is_post_payday(d: date) -> bool:
    """The day after payday often carries the spending."""
    return is_payday(d - timedelta(days=1))
