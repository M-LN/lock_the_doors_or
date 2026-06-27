# Østjylland Ro-index — data pipeline

Daily calm-score for the 7 municipalities in Østjyllands Politi, built on the
public døgnrapporter. Static site + one GitHub Action. No server, no database.

## Two layers (the one architectural decision that matters)

```
politi.dk ──► data/incidents/YYYY-MM-DD.jsonl   ← raw, append-only, immutable
                              │
                   build_scores.py (recompute from scratch)
                              ▼
              web/daily_scores.json              ← derived, disposable
                              │
                         fetch() in the frontend
```

The **raw log** is the source of truth: one line per incident, written once,
never edited. The **scores** are a pure function of that log. When the scoring
model changes (different weights, seasonal correction), you re-run
`build_scores.py` and the entire history is rescored — nothing is lost. If you
stored finished scores instead, old data would be frozen to an old model. That
separation is the difference between a fragile and a robust pipeline.

## Raw incident schema — `data/incidents/YYYY-MM-DD.jsonl`

One JSON object per line:

| field     | type        | notes                                            |
|-----------|-------------|--------------------------------------------------|
| `id`      | string      | `sha1(date\|kommune\|type\|by\|vej\|time)[:12]` — idempotency key |
| `date`    | `YYYY-MM-DD`| report day                                       |
| `kommune` | string      | one of the 7 (resolved via DAWA reverse geocode) |
| `type`    | string      | one of `config.SEVERITY` keys                    |
| `by`      | string\|null| town                                             |
| `vej`     | string\|null| street, no house number                          |
| `time`    | `HH:MM`\|null|                                                 |
| `x`,`y`   | float\|null | WGS84 lon/lat (DAWA)                              |
| `source`  | string      | provenance                                       |

`id` makes re-runs safe: politi.dk edits reports through the day, so the Action
runs with a 2-day overlap and only appends genuinely new ids.

## Score model — `build_scores.py`

For each `(kommune, day d)`:

- **baseline μ** = mean weighted count on the **same weekday**, over the
  trailing `WINDOW_WEEKS`, using only days **strictly before d** (no leakage).
- **σ_pred** = `sqrt(μ + μ/n_samples)` — Poisson observation noise *plus*
  uncertainty in μ. With few samples σ inflates, so early scores are pulled
  toward 50 rather than over-reacting. Honest cold-start, not a cosmetic band.
- **score** = `clamp(round(50 − 13·z), 2, 98)`, where `z = (w − μ)/σ_pred`.
  100 = unusually calm.

Per-weekday baseline is deliberate: reporting volume swings with day of week,
so a flat baseline would score every Monday "busy". Below `MIN_SAMPLES`
same-weekday observations a kommune is flagged `mature: false` — surface that in
the UI ("indsamler baseline") instead of pretending a quiet day is signal.

## Derived schema — `web/daily_scores.json`

```jsonc
{
  "date": "2026-06-27", "days": 56, "window_weeks": 8,
  "dates": ["..."],                          // last OUT_DAYS
  "kommuner": [{ "name", "n", "wsum", "expected", "z", "score",
                 "n_samples", "mature" }],   // today's snapshot
  "district_score": 52,
  "series":  { "Aarhus": [{ "d","w","n","exp","z","score","mature" }], ... },
  "district_series": [{ "d", "score" }],
  "incidents": [{ "type","sev","by","vej","time","x","y","k" }], // latest day
  "types": { "Indbrud": 3, ... }
}
```

`score` is `null` for the first days of a kommune's history (no prior
same-weekday data yet) — the frontend should render that as a gap.

## Frontend — `web/index.html`

`web/index.html` is the live frontend. It inlines the static GeoJSON (geometry
+ centroids) and `fetch`es `daily_scores.json` at load, merging centroids onto
each kommune by name. It handles `null` scores as cold-start gaps: greyed-out
choropleth fill, an em dash in chips/rings, "Afventer baseline" mood text, and
broken (not interpolated) lines in the sparklines and trend chart.

It must be served over HTTP — `fetch` from `file://` is blocked by browsers:

```bash
cd web && python -m http.server 8000   # then open http://localhost:8000
```

The Action commits an updated `daily_scores.json` daily; the page picks it up on
next load (it requests with `cache: 'no-store'`).

## Run locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
export ANTHROPIC_API_KEY=sk-...
python scripts/ingest.py        # appends today's incidents
python scripts/build_scores.py  # writes web/daily_scores.json
```

`data/incidents/` ships with 56 days of **mock** data so `build_scores.py` runs
immediately. Delete it before going live.

## Notes & limits

- **The scrape seam.** The report *list* is client-rendered, so `ingest.py`
  uses Playwright. If you sniff the underlying XHR (DevTools → Network), swap in
  a plain `requests` call — faster and lighter. Individual report pages are
  server-rendered and already parse with `requests`.
- **Resolution & GDPR.** Reports are anonymised to town level; geocoding to a
  point is approximate. An accumulated, searchable incident-per-address history
  edges toward a register, which is heavier under GDPR than a snapshot. Keep
  storage at town level.
- **Maturity.** The first ~8 weeks per kommune are low-confidence by
  construction. Let the UI say so.
