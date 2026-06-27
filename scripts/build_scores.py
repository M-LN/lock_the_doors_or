#!/usr/bin/env python3
"""
build_scores.py — derive the ro-index from the raw incident log.

Reads every data/incidents/YYYY-MM-DD.jsonl (append-only, immutable) and
recomputes the calm score from scratch. Nothing here is persisted back into
the raw log; if the scoring model changes, just re-run and the entire history
is rescored. That separation is the whole point: the raw log is the source of
truth, the scores are a disposable derivative.

Score model
-----------
For each (kommune, day d):
  - baseline mu  = mean weighted count on the SAME WEEKDAY over the trailing
                   WINDOW_WEEKS, using only days strictly BEFORE d (no leakage).
  - n_samples    = how many same-weekday observations fed that mean.
  - sigma_pred   = sqrt(mu + mu/n_samples)  -> Poisson observation noise plus
                   uncertainty in the estimate of mu itself. With few samples
                   sigma inflates, so early scores are automatically pulled
                   toward 50 instead of over-reacting. This is the honest
                   cold-start behaviour, not a cosmetic widening band.
  - z            = (w_d - mu) / sigma_pred
  - score        = clamp(round(50 - SLOPE * z), 2, 98)   # 100 = unusually calm

Per-weekday matters: reporting volume swings with the day of week, so a flat
baseline would score every Monday "busy" and every Sunday "calm". Grouping by
weekday measures unrest, not the calendar.
"""
import json, glob, math, os, datetime as dt
from collections import defaultdict
from config import KOMMUNER, SEVERITY, WINDOW_WEEKS, MIN_SAMPLES, SCORE_SLOPE, OUT_DAYS

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
INCIDENT_DIR = os.path.join(ROOT, "data", "incidents")
OUT_FILE = os.path.join(ROOT, "web", "daily_scores.json")


def load_incidents():
    """Yield every incident record from every daily JSONL file."""
    for path in sorted(glob.glob(os.path.join(INCIDENT_DIR, "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)


def daily_weighted(incidents):
    """(kommune, date) -> {'w': weighted count, 'n': raw count}."""
    agg = defaultdict(lambda: {"w": 0, "n": 0})
    seen_dates = set()
    for inc in incidents:
        d = inc["date"]
        seen_dates.add(d)
        cell = agg[(inc["kommune"], d)]
        cell["w"] += SEVERITY.get(inc["type"], 1)
        cell["n"] += 1
    return agg, sorted(seen_dates)


def date_span(dates):
    """Continuous list of dates from first to last observed day."""
    if not dates:
        return []
    lo = dt.date.fromisoformat(dates[0])
    hi = dt.date.fromisoformat(dates[-1])
    return [(lo + dt.timedelta(days=i)).isoformat()
            for i in range((hi - lo).days + 1)]


def trailing_baseline(wseries, target, weekday):
    """
    mu, n_samples for `target` date using same-weekday observations within the
    trailing window and strictly before the target. wseries: {date_iso: w}.
    """
    t = dt.date.fromisoformat(target)
    window_start = t - dt.timedelta(weeks=WINDOW_WEEKS)
    vals = []
    for diso, w in wseries.items():
        d = dt.date.fromisoformat(diso)
        if window_start <= d < t and d.weekday() == weekday:
            vals.append(w)
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def score_day(w, mu, n_samples):
    """Return (score, z, sigma_pred, mature)."""
    if mu is None or n_samples == 0:
        return None, None, None, False
    sigma = math.sqrt(mu + mu / n_samples)
    sigma = max(sigma, 1.0)  # floor: never divide by ~0 on quiet kommuner
    z = (w - mu) / sigma
    score = max(2, min(98, round(50 - SCORE_SLOPE * z)))
    return score, round(z, 2), round(sigma, 2), n_samples >= MIN_SAMPLES


def build_series(wseries, span):
    """List of {d, w, n, score, z, mature} over the last OUT_DAYS of span."""
    out = []
    for diso in span[-OUT_DAYS:]:
        w = wseries.get(diso, 0.0)
        wd = dt.date.fromisoformat(diso).weekday()
        mu, n = trailing_baseline(wseries, diso, wd)
        score, z, sigma, mature = score_day(w, mu, n)
        out.append({
            "d": diso, "w": int(w),
            "exp": round(mu, 1) if mu is not None else None,
            "n_samples": n, "score": score, "z": z, "mature": mature,
        })
    return out


def main():
    incidents = list(load_incidents())
    agg, dates = daily_weighted(incidents)
    span = date_span(dates)
    if not span:
        raise SystemExit("No incident data found in " + INCIDENT_DIR)
    today = span[-1]

    # Per-kommune weighted series across the full continuous span.
    series = {}
    kommuner_today = []
    for k in KOMMUNER:
        wseries = {d: agg.get((k, d), {"w": 0})["w"] for d in span}
        nseries = {d: agg.get((k, d), {"n": 0})["n"] for d in span}
        s = build_series(wseries, span)
        # carry raw n into the published series
        for row in s:
            row["n"] = nseries.get(row["d"], 0)
        series[k] = s
        last = s[-1]
        kommuner_today.append({
            "name": k, "n": last["n"], "wsum": last["w"],
            "expected": last["exp"], "z": last["z"], "score": last["score"],
            "n_samples": last["n_samples"], "mature": last["mature"],
        })

    # District = aggregate weighted series, scored with the same trailing logic.
    dist_w = {d: sum(agg.get((k, d), {"w": 0})["w"] for k in KOMMUNER) for d in span}
    dist_series_full = build_series(dist_w, span)
    district_series = [{"d": r["d"], "score": r["score"]} for r in dist_series_full]
    district_score = dist_series_full[-1]["score"]

    # Individual incidents for the latest day -> map points.
    latest_points = [
        {"type": i["type"], "sev": SEVERITY.get(i["type"], 1), "by": i.get("by"),
         "vej": i.get("vej"), "time": i.get("time"), "x": i.get("x"),
         "y": i.get("y"), "k": i["kommune"]}
        for i in incidents if i["date"] == today and i.get("x") is not None
    ]

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "date": today,
        "window_weeks": WINDOW_WEEKS,
        "days": len(span),
        "dates": span[-OUT_DAYS:],
        "kommuner": kommuner_today,
        "district_score": district_score,
        "series": series,
        "district_series": district_series,
        "incidents": latest_points,
        "types": SEVERITY,
    }
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    mature = sum(1 for k in kommuner_today if k["mature"])
    print(f"{today}: district {district_score} | "
          f"{len(kommuner_today)} kommuner ({mature} mature) | "
          f"{len(span)} days | {len(latest_points)} points -> {OUT_FILE}")


if __name__ == "__main__":
    main()
