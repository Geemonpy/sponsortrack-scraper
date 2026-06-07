"""
Fantastic Jobs source module
----------------------------
Fetches from the Fantastic.jobs direct API (data.fantastic.jobs/v1/active-ats)
and maps jobs into the Adzuna-compatible shape expected by classify() in scraper.py.
"""

import re
import time

import httpx

FANTASTIC_ENDPOINT = "https://data.fantastic.jobs/v1/active-ats"
FANTASTIC_LIMIT = 50          # was 100 — 1 credit per job, so keep pulls small on the trial
FANTASTIC_MAX_PAGES = 1       # 1 page x 50 = max 50 jobs/run (preserve trial credits)

# Hard guard: counts how many real API requests we've made this process.
# Even if scraper.py loops more than FANTASTIC_MAX_PAGES, this module will refuse
# to make another billed request and return empty, ending pagination cleanly.
_pages_fetched = 0

# Strings that strongly suggest a non-UK job despite "United Kingdom" geocoding
_NON_UK_SIGNALS = ["canberra", "australia", "act health"]

# Bare dollar amounts (not GBP) — strong non-UK signal
_DOLLAR_RE = re.compile(r"\$\s*\d")

# AUD as a standalone currency word — catches "AUD 50,000" while avoiding "auditor"
_AUD_RE = re.compile(r"\baud\b", re.IGNORECASE)

_JUNIOR_POSITIVE = [
    "junior", "graduate", "trainee", "entry-level", "entry level", "apprentice"
]
_JUNIOR_NEGATIVE = [
    "senior", "head of", "lead ", "principal", "director", "manager"
]


def is_uk_job(job: dict) -> bool:
    """Return True only if the job is genuinely UK-based."""
    countries = [c.lower() for c in (job.get("countries_derived") or [])]
    if "united kingdom" not in countries:
        return False
    text = (
        (job.get("title") or "") + " " +
        (job.get("description") or job.get("description_text") or "")
    ).lower()
    for signal in _NON_UK_SIGNALS:
        if signal in text:
            return False
    if _DOLLAR_RE.search(text):
        return False
    if _AUD_RE.search(text):
        return False
    return True


def is_junior_friendly(title: str, description: str) -> bool:
    """True if the role targets junior/graduate candidates and isn't senior-level."""
    combined = (title + " " + description).lower()
    has_junior = any(kw in combined for kw in _JUNIOR_POSITIVE)
    has_senior = any(kw in combined for kw in _JUNIOR_NEGATIVE)
    return has_junior and not has_senior


def map_to_adzuna_shape(job: dict) -> dict | None:
    """
    Map a Fantastic Jobs API job dict into the Adzuna-compatible shape that
    classify() expects. Returns None if the job has no usable location.
    """
    locations = job.get("locations_derived") or []
    if not locations or not locations[0]:
        return None
    location_str = locations[0]

    # date_posted is "YYYY-MM-DD"; parse_posted_date() reads the "created" key
    date_posted = (job.get("date_posted") or "")[:10]
    description = job.get("description") or job.get("description_text") or ""

    return {
        "id": str(job.get("id", "")),
        "title": (job.get("title") or "").strip(),
        "company": {"display_name": job.get("organization") or "Unknown"},
        "location": {"display_name": location_str},
        "description": description,
        "created": date_posted,
        "redirect_url": job.get("url"),
        "salary_min": None,
        "salary_max": None,
    }


def fetch_fantastic_page(fantastic_key: str, offset: int) -> tuple[list[dict], dict]:
    """
    Fetch one page from the Fantastic.jobs direct API.
    Returns (jobs, response_headers) — caller logs the credit headers.
    Raises httpx.HTTPStatusError on non-200 after one retry on 429.

    Hard-capped at FANTASTIC_MAX_PAGES billed requests per process to protect
    the trial credit balance.
    """
    global _pages_fetched
    if _pages_fetched >= FANTASTIC_MAX_PAGES:
        # Already hit the per-run request cap — make no further billed calls.
        return [], {}
    _pages_fetched += 1

    params = {
        "apiKey": fantastic_key,
        "time_frame": "24h",
        "location": "United Kingdom",
        "description_format": "text",
        "limit": FANTASTIC_LIMIT,
        "offset": offset,
    }
    resp = httpx.get(FANTASTIC_ENDPOINT, params=params, timeout=60)
    if resp.status_code == 429:
        time.sleep(3)
        resp = httpx.get(FANTASTIC_ENDPOINT, params=params, timeout=60)
    resp.raise_for_status()

    headers = dict(resp.headers)
    data = resp.json()
    if isinstance(data, list):
        return data, headers
    for key in ("data", "jobs", "results"):
        if isinstance(data.get(key), list):
            return data[key], headers
    return [], headers
