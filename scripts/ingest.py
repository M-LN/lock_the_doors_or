#!/usr/bin/env python3
"""
ingest.py — fetch yesterday's døgnrapporter, extract structured incidents,
geocode them, and append to the immutable daily log.

Pipeline per run:
    report URLs  ->  prose text  ->  [LLM extraction]  ->  geocode (DAWA)
                 ->  upsert into data/incidents/YYYY-MM-DD.jsonl (idempotent)

Idempotency: every incident gets a stable id = sha1(date|kommune|type|by|vej|time).
Re-running a day never double-counts; politi.dk edits/corrects reports during the
day, so this script is safe to run repeatedly. Only genuinely new ids are appended.

Requires: ANTHROPIC_API_KEY in the environment for the extraction step.

THE ONE SEAM: the report *list* on politi.dk is rendered client-side
(the HTML ships {{article.Link}} placeholders), so you cannot get the links
with plain requests. Two robust options, pick one in `iter_report_urls`:
  1. Sniff the XHR the page fires (DevTools -> Network -> filter doegnrapport)
     and hit that JSON endpoint directly. Cleanest once you know the URL.
  2. Render the list page with Playwright and read the <a> hrefs.
The individual report PAGES are server-rendered, so parse_report() below works
with plain requests once you have a URL.
"""
import os, re, json, sys, time, hashlib, datetime as dt
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from config import KOMMUNER, SEVERITY, KOMKODE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INCIDENT_DIR = os.path.join(ROOT, "data", "incidents")
DISTRICT = "OEstjyllands-Politi"
UA = {"User-Agent": "ostjylland-ro-index/1.0 (personal research; contact: you@example.com)"}
DAWA = "https://api.dataforsyningen.dk"


# --------------------------------------------------------------------------
# 1. discover report URLs  (THE SEAM — see module docstring)
# --------------------------------------------------------------------------
def iter_report_urls(since: dt.date):
    """
    Yield politi.dk report page URLs published on/after `since`.

    Implement ONE of the two strategies. The Playwright version below is the
    portable default; uncomment and `playwright install chromium` to use it.
    """
    # --- Option 2: Playwright (portable, no endpoint guessing) ---------------
    from playwright.sync_api import sync_playwright
    url = f"https://politi.dk/doegnrapporter?district={DISTRICT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="load", timeout=60000)
        page.wait_for_selector("a[href*='/doegnrapporter/']", timeout=30000)
        hrefs = page.eval_on_selector_all(
            "a[href*='/doegnrapporter/']",
            "els => els.map(e => e.href)")
        browser.close()
    seen = set()
    for h in hrefs:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})$", h)
        if not m:
            continue
        d = dt.date(int(m[1]), int(m[2]), int(m[3]))
        if d >= since and h not in seen:
            seen.add(h)
            yield h, d


def parse_report(url: str) -> str:
    """Fetch a server-rendered report page and return its prose body."""
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    main = soup.select_one("#main-body-content") or soup.find("main") or soup
    # Reports separate entries with '**'; keep paragraphs, drop boilerplate.
    text = "\n".join(p.get_text(" ", strip=True) for p in main.find_all(["p", "li"]))
    return text


# --------------------------------------------------------------------------
# 2. LLM extraction: prose -> structured incidents
# --------------------------------------------------------------------------
EXTRACT_SYS = (
    "Du er en præcis informationsudtrækker. Du får et uddrag af en dansk "
    "politidøgnrapport. Returnér KUN et JSON-array (intet andet, ingen markdown). "
    "Hvert element: {\"type\": en af "
    + ", ".join(f'\"{t}\"' for t in SEVERITY) + ", "
    "\"by\": bynavn eller null, \"vej\": vejnavn uden husnummer eller null, "
    "\"time\": \"HH:MM\" eller null}. "
    "Én post pr. konkret hændelse. Spring grundlovsforhør, generelle "
    "opfordringer og rene statusbeskeder over. Vælg den bedst passende type; "
    "brug \"Andet\" hvis intet passer."
)


def extract_incidents(text: str, model="claude-haiku-4-5-20251001"):
    """Call the Anthropic API and return a list of incident dicts."""
    from anthropic import Anthropic
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is not set; skipping incident extraction.")
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model, max_tokens=2000,
        system=EXTRACT_SYS,
        messages=[{"role": "user", "content": text[:12000]}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        print("  ! extraction returned non-JSON, skipping report", file=sys.stderr)
        return []
    return [i for i in items if isinstance(i, dict) and i.get("type") in SEVERITY]


# --------------------------------------------------------------------------
# 3. geocoding via DAWA (no API key required)
# --------------------------------------------------------------------------
def geocode(vej, by):
    """(vej, by) -> (x, y, kommune) using DAWA. Falls back to town centroid."""
    try:
        if vej and by:
            r = requests.get(f"{DAWA}/adgangsadresser", headers=UA, timeout=20,
                             params={"vejnavn": vej, "postnrnavn": by, "per_side": 1,
                                     "struktur": "mini"})
            hits = r.json()
            if hits:
                h = hits[0]
                return float(h["x"]), float(h["y"]), kommune_for_point(h["x"], h["y"])
        if by:  # town-level fallback — matches the reports' real resolution anyway
            r = requests.get(f"{DAWA}/stednavne2", headers=UA, timeout=20,
                             params={"navn": by, "per_side": 1})
            hits = r.json()
            if hits and hits[0].get("geometri"):
                lon, lat = _centroid(hits[0]["geometri"]["coordinates"])
                return lon, lat, kommune_for_point(lon, lat)
    except Exception as e:
        print(f"  ! geocode failed for {vej}, {by}: {e}", file=sys.stderr)
    return None, None, None


def kommune_for_point(x, y):
    """Reverse-geocode a coordinate to one of the 7 kommuner via DAWA."""
    try:
        r = requests.get(f"{DAWA}/kommuner/reverse", headers=UA, timeout=20,
                         params={"x": x, "y": y, "struktur": "mini"})
        return KOMKODE.get(r.json().get("kode"))
    except Exception:
        return None


def _centroid(coords):
    flat = []
    def walk(c):
        if isinstance(c[0], (int, float)):
            flat.append(c)
        else:
            for sub in c:
                walk(sub)
    walk(coords)
    return (sum(p[0] for p in flat) / len(flat), sum(p[1] for p in flat) / len(flat))


# --------------------------------------------------------------------------
# 4. idempotent append
# --------------------------------------------------------------------------
def incident_id(d, kommune, t, by, vej, time):
    return hashlib.sha1(f"{d}|{kommune}|{t}|{by}|{vej}|{time}".encode()).hexdigest()[:12]


def upsert(day: dt.date, records: list):
    path = os.path.join(INCIDENT_DIR, f"{day.isoformat()}.jsonl")
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = {json.loads(l)["id"] for l in fh if l.strip()}
    new = [r for r in records if r["id"] not in existing]
    if new:
        os.makedirs(INCIDENT_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(new), len(records)


def main():
    since = dt.date.today() - dt.timedelta(days=2)  # small overlap = self-healing
    grand_new = 0
    for url, day in iter_report_urls(since):
        print(f"report {day} {url}")
        text = parse_report(url)
        records = []
        for inc in extract_incidents(text):
            x, y, kommune = geocode(inc.get("vej"), inc.get("by"))
            if kommune not in KOMMUNER:
                continue
            records.append({
                "id": incident_id(day.isoformat(), kommune, inc["type"],
                                  inc.get("by"), inc.get("vej"), inc.get("time")),
                "date": day.isoformat(), "kommune": kommune, "type": inc["type"],
                "by": inc.get("by"), "vej": inc.get("vej"), "time": inc.get("time"),
                "x": x, "y": y, "source": "politi.dk", "url": url,
            })
        added, total = upsert(day, records)
        grand_new += added
        print(f"  {added} new / {total} extracted")
        time.sleep(1)  # be polite
    print(f"done: {grand_new} new incidents")


if __name__ == "__main__":
    main()
