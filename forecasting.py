"""Sales forecasting engine.

Several models compete on a rolling-origin backtest (train on the past, predict
the next 7 days, repeat). The model with the lowest error is used for the
forecast. The 7-day moving average is always in the race, so if nothing beats it
the system says so instead of pretending a fancier model is better.
"""
import hashlib
import json
import logging
import warnings
from datetime import timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor

import ph_calendar

try:  # optional heavy dependencies
    from prophet import Prophet
except Exception:  # pragma: no cover
    Prophet = None
try:
    from statsmodels.tsa.statespace.sarimax import SARIMAX
except Exception:  # pragma: no cover
    SARIMAX = None

MODEL_VERSION = "v2"
Z80 = 1.2816          # 80% prediction band
BT_HORIZON = 7        # backtest predicts one week ahead
MIN_BACKTEST_TRAIN = 14
REGRESSORS = ["payday", "post_payday", "event"]


# ---------------------------------------------------------------- data prep
def observed_frame(logs):
    rows = [(d, e["sales"]) for d, e in logs.items() if e["sales"] and e["sales"] > 0]
    df = pd.DataFrame(rows, columns=["ds", "y"])
    if df.empty:
        return df
    df["ds"] = pd.to_datetime(df["ds"])
    return df.sort_values("ds").reset_index(drop=True)


def calendar_frame(dates, events):
    """Calendar drivers for each date. events: set of ISO date strings."""
    idx = pd.DatetimeIndex(dates)
    ds = [d.date() for d in idx]
    return pd.DataFrame({
        "ds": idx,
        "dow": idx.dayofweek,
        "dom": idx.day,
        "month": idx.month,
        "payday": [int(ph_calendar.is_payday(d)) for d in ds],
        "post_payday": [int(ph_calendar.is_post_payday(d)) for d in ds],
        "holiday": [int(ph_calendar.holiday_name(d) is not None) for d in ds],
        "event": [int(d.isoformat() in events) for d in ds],
    })


def with_cal(df, events):
    cal = calendar_frame(df["ds"], events)
    out = df.reset_index(drop=True).copy()
    for c in cal.columns:
        if c != "ds":
            out[c] = cal[c].values
    return out


def fingerprint(logs, events):
    blob = json.dumps(
        [MODEL_VERSION, [(d, e["sales"]) for d, e in logs.items()], sorted(events)], default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


# ------------------------------------------------------------------- models
def predict_ma7(train, fut, events):
    return np.full(len(fut), train["y"].tail(7).mean())


def _dow_estimate(y, dows, dow, level):
    same = y[dows == dow][-6:]
    if len(same) >= 2:
        return 0.6 * np.average(same, weights=np.arange(1, len(same) + 1)) + 0.4 * level
    return level


def predict_dow(train, fut, events):
    y, dows = train["y"].values, train["dow"].values
    level = y[-14:].mean()
    return np.array([_dow_estimate(y, dows, d, level) for d in fut["dow"].values])


def _gbr_row(r, level, dm):
    return [r["dow"], r["dom"], r["month"], r["payday"], r["post_payday"], r["holiday"],
            r["event"], dm / level if level else 1.0]


def predict_gbr(train, fut, events):
    t = train.reset_index(drop=True)
    y, dows = t["y"].values, t["dow"].values
    X, T = [], []
    for i in range(7, len(t)):
        level = y[max(0, i - 14):i].mean()
        same = y[:i][dows[:i] == dows[i]][-6:]
        dm = same.mean() if len(same) else level
        X.append(_gbr_row(t.iloc[i], level, dm))
        T.append(y[i] / level)
    if len(X) < 10:
        return predict_dow(train, fut, events)
    model = GradientBoostingRegressor(
        loss="huber", n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=0)
    model.fit(np.array(X, dtype=float), np.array(T))
    level = y[-14:].mean()
    Xf = []
    for _, r in fut.iterrows():
        same = y[dows == r["dow"]][-6:]
        Xf.append(_gbr_row(r, level, same.mean() if len(same) else level))
    return model.predict(np.array(Xf, dtype=float)) * level


def predict_sarimax(train, fut, events):
    s = train.set_index("ds")["y"].asfreq("D").interpolate(limit_direction="both").ffill().bfill()
    ex = calendar_frame(s.index, events)[["payday", "event"]].astype(float).values
    last = s.index[-1]
    steps = int((fut["ds"].max() - last).days)
    fdates = pd.date_range(last + timedelta(days=1), periods=steps)
    fex = calendar_frame(fdates, events)[["payday", "event"]].astype(float).values
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = SARIMAX(s, exog=ex, order=(1, 0, 1), seasonal_order=(1, 0, 0, 7), trend="c",
                      enforce_stationarity=False, enforce_invertibility=False).fit(disp=False, maxiter=60)
        fc = res.forecast(steps=steps, exog=fex)
    fc.index = fdates
    return fc.reindex(fut["ds"]).ffill().bfill().values


def _prophet_predict(train, fut, events, tuned):
    logging.getLogger("cmdstanpy").setLevel(logging.ERROR)
    logging.getLogger("prophet").setLevel(logging.ERROR)
    kw = dict(yearly_seasonality=False, weekly_seasonality=True,
              daily_seasonality=False, uncertainty_samples=0)
    if tuned:
        years = sorted({d.year for d in pd.concat([train["ds"], fut["ds"]])})
        hol = pd.DataFrame([{"holiday": n, "ds": pd.Timestamp(d)}
                            for yr in years for d, n in ph_calendar.holiday_map(yr).items()])
        m = Prophet(holidays=hol, seasonality_mode="additive", changepoint_prior_scale=0.05, **kw)
        for r in REGRESSORS:
            m.add_regressor(r)
        m.fit(train[["ds", "y"] + REGRESSORS].astype({r: float for r in REGRESSORS}))
        return m.predict(fut[["ds"] + REGRESSORS].astype({r: float for r in REGRESSORS}))["yhat"].values
    m = Prophet(seasonality_mode="multiplicative", **kw)
    m.fit(train[["ds", "y"]])
    return m.predict(fut[["ds"]])["yhat"].values


def predict_prophet_basic(train, fut, events):
    return _prophet_predict(train, fut, events, tuned=False)


def predict_prophet_tuned(train, fut, events):
    return _prophet_predict(train, fut, events, tuned=True)


MODELS = {
    "ma7": ("7-day average", predict_ma7, False),
    "dow_avg": ("Weekday pattern", predict_dow, False),
    "gbr": ("Gradient boosting (payday + holiday aware)", predict_gbr, False),
    "sarimax": ("SARIMAX (weekly cycle)", predict_sarimax, True),
    "prophet_basic": ("Prophet (basic)", predict_prophet_basic, True),
    "prophet_tuned": ("Prophet (tuned: payday, holidays, events)", predict_prophet_tuned, True),
}


def candidate_models(n_obs, include_slow):
    names = ["ma7", "dow_avg"]
    if n_obs >= 21:
        names.append("gbr")
    if include_slow:
        if SARIMAX is not None and n_obs >= 28:
            names.append("sarimax")
        if Prophet is not None and n_obs >= 21:
            names += ["prophet_basic", "prophet_tuned"]
    return names


def slow_models_available(n_obs):
    return (SARIMAX is not None and n_obs >= 28) or (Prophet is not None and n_obs >= 21)


# ----------------------------------------------------------------- backtest
def _metrics(actual, pred):
    a, p = np.asarray(actual, float), np.asarray(pred, float)
    err = p - a
    return {
        "mae": float(np.mean(np.abs(err))),
        "rmse": float(np.sqrt(np.mean(err ** 2))),
        "mape": float(np.mean(np.abs(err) / a) * 100),
        "bias": float(np.mean(err)),
    }


def _folds(df, folds):
    """Rolling 7-day test windows, newest first. Windows with too little data (for example after a
    stretch of days nobody logged) are skipped instead of silently weakening the comparison."""
    last = df["ds"].max()
    out = []
    for k in range(folds * 6):
        t_end = last - timedelta(days=k * BT_HORIZON)
        t_start = t_end - timedelta(days=BT_HORIZON - 1)
        train = df[df["ds"] < t_start]
        test = df[(df["ds"] >= t_start) & (df["ds"] <= t_end)]
        if len(train) >= MIN_BACKTEST_TRAIN and len(test) >= 3:
            out.append((train, test))
        if len(out) == folds:
            break
    return out


def backtest(df, events, names, folds=5):
    fl = _folds(df, folds)
    if not fl:
        return None, [], {}
    actual = [t["y"].values for _, t in fl]
    preds, failed = {}, {}
    for name in names:
        fn = MODELS[name][1]
        try:
            per_fold = []
            for train, test in fl:
                per_fold.append(np.asarray(fn(with_cal(train, events), with_cal(test[["ds"]], events), events), float))
            preds[name] = per_fold
        except Exception as exc:  # a broken optional model must never break the forecast
            failed[name] = str(exc)[:120]
    return actual, fl, {"preds": preds, "failed": failed}


def score_models(actual, preds):
    rows = []
    flat_a = np.concatenate(actual)
    for name, per_fold in preds.items():
        m = _metrics(flat_a, np.concatenate(per_fold))
        rows.append({"name": name, "label": MODELS[name][0], **m})
    rows.sort(key=lambda r: r["mae"])
    if len(rows) >= 2:  # ensemble of the two best
        top = [r["name"] for r in rows[:2]]
        ens = [(preds[top[0]][i] + preds[top[1]][i]) / 2 for i in range(len(actual))]
        m = _metrics(flat_a, np.concatenate(ens))
        rows.append({"name": "ensemble", "label": f"Ensemble of {MODELS[top[0]][0]} + {MODELS[top[1]][0]}",
                     "members": top, **m})
        rows.sort(key=lambda r: r["mae"])
    return rows


# -------------------------------------------------------------- main entry
def _fit_predict(name, df, fut, events, members=None):
    train = with_cal(df, events)
    if name == "ensemble":
        a, b = members
        return (MODELS[a][1](train, fut, events) + MODELS[b][1](train, fut, events)) / 2
    return MODELS[name][1](train, fut, events)


def compute_forecast(logs, events, today, horizon_days=30, include_slow=True, folds=5):
    df = observed_frame(logs)
    n = len(df)
    payload = {"model_version": MODEL_VERSION, "n_obs": n, "notes": [], "forecast": [],
               "accuracy": None, "model": None, "model_label": None,
               "last_actual_date": None}
    if n == 0:
        payload["notes"].append("No sales logged yet. Add a few days in the Logbook to start forecasting.")
        return payload

    last = df["ds"].max()
    payload["last_actual_date"] = last.date().isoformat()
    names = candidate_models(n, include_slow)

    actual, fl, bt = backtest(df, events, names, folds)
    chosen, members, rmse = "dow_avg" if n >= 7 else "ma7", None, None
    if bt and bt["preds"]:
        rows = score_models(actual, bt["preds"])
        base = next((r for r in rows if r["name"] == "ma7"), None)
        for r in rows:
            r["skill"] = round((1 - r["mae"] / base["mae"]) * 100, 1) if base and base["mae"] else None
            for k in ("mae", "rmse", "mape", "bias"):
                r[k] = round(r[k], 2)
        best = rows[0]
        chosen, members, rmse = best["name"], best.get("members"), best["rmse"]
        payload["accuracy"] = {"models": rows, "folds": len(fl), "horizon_days": BT_HORIZON,
                               "failed": bt["failed"], "baseline": "ma7"}
        if chosen == "ma7":
            payload["notes"].append(
                "No model has beaten the plain 7-day average in backtests yet. More logged days will help.")
    else:
        payload["notes"].append(
            f"Need at least {MIN_BACKTEST_TRAIN + 3} logged days to compare models. Using a simple estimate for now.")
    if n < 28:
        payload["notes"].append("Under 4 weeks of data: weekday patterns are still rough.")

    start = last + timedelta(days=1)
    end = pd.Timestamp(max(pd.Timestamp(today), last)) + timedelta(days=horizon_days)
    fut = with_cal(pd.DataFrame({"ds": pd.date_range(start, end)}), events)
    try:
        yhat = _fit_predict(chosen, df, fut, events, members)
    except Exception:
        chosen, members = "dow_avg", None
        yhat = _fit_predict(chosen, df, fut, events)
        payload["notes"].append("The best model failed on the final fit; fell back to the weekday pattern.")
    if rmse is None:
        rmse = float(df["y"].tail(14).std(ddof=0) or 0)

    payload["model"] = chosen
    payload["model_label"] = (f"Ensemble of {MODELS[members[0]][0]} + {MODELS[members[1]][0]}"
                              if chosen == "ensemble" else MODELS[chosen][0])
    band = Z80 * rmse
    for i, r in fut.iterrows():
        d = r["ds"].date()
        y = max(0.0, float(yhat[i]))
        payload["forecast"].append({
            "date": d.isoformat(), "yhat": round(y, 2),
            "lower": round(max(0.0, y - band), 2), "upper": round(y + band, 2),
            "payday": bool(r["payday"]), "holiday": ph_calendar.holiday_name(d),
            "event": bool(r["event"]),
        })
    return payload


# ------------------------------------------------------- per-item demand
def forecast_item_usage(logs, code, dates):
    """Expected daily usage for an item on the given dates, plus its recent volatility."""
    rows = [(d, e["items"][code]["usage"]) for d, e in logs.items()
            if code in e["items"] and e["items"][code]["usage"] is not None]
    if len(rows) < 3:
        return None, None
    df = pd.DataFrame(rows, columns=["ds", "y"])
    df["ds"] = pd.to_datetime(df["ds"])
    df = df.sort_values("ds").reset_index(drop=True)
    train = with_cal(df, set())
    fut = with_cal(pd.DataFrame({"ds": pd.to_datetime(list(dates))}), set())
    pred = predict_dow(train, fut, set())
    return [float(max(0.0, p)) for p in pred], float(df["y"].tail(14).std(ddof=0) or 0)
