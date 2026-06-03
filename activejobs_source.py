"""
Active Jobs DB source module
----------------------------
Fetches from the RapidAPI "Active Jobs DB" endpoint and maps jobs into the
Adzuna-compatible shape expected by classify() in scraper.py.
"""

import re
import time

import httpx

ACTIVEJOBS_HOST = "active-jobs-db.p.rapidapi.com"
ACTIVEJOBS_ENDPOINT = f"https://{ACTIVEJOBS_HOST}/active-ats-7d"
ACTIVEJOBS_LIMIT = 100
ACTIVEJOBS_MAX_PAGES = 1  # free tier: 1 × 100 = 100 jobs max

# Strings that strongly suggest a non-UK job despite "United Kingdom" geocoding
_NON_UK_SIGNALS = ["canberra", "australia", "act health"]

# Bare dollar amounts (not £) — strong non-UK signal
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
        (job.get("title") or "") + " " + (job.get("description_text") or "")
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
    Map an Active Jobs DB job dict into the Adzuna-compatible shape that
    classify() expects. Returns None if the job has no usable location.
    """
    locations = job.get("locations_derived") or []
    if not locations or not locations[0]:
        return None
    location_str = locations[0]

    # date_posted is "YYYY-MM-DD"; parse_posted_date() reads the "created" key
    date_posted = (job.get("date_posted") or "")[:10]  # keep date portion only

    return {
        "id": job.get("id", ""),
        "title": (job.get("title") or "").strip(),
        "company": {"display_name": job.get("organization") or "Unknown"},
        "location": {"display_name": location_str},
        "description": job.get("description_text") or "",
        "created": date_posted,
        "redirect_url": job.get("url"),
        "salary_min": None,
        "salary_max": None,
    }


def fetch_activejobs_page(rapidapi_key: str, offset: int, date_filter: str) -> list[dict]:
    """
    Fetch one page from the Active Jobs DB endpoint.
    Raises httpx.HTTPStatusError on non-200; raises on network failure.
    Caller is responsible for catching and logging.
    """
    headers = {
        "x-rapidapi-host": ACTIVEJOBS_HOST,
        "x-rapidapi-key": rapidapi_key,
    }
    params = {
        "location_filter": "United Kingdom",
        "description_type": "text",
        "include_ai": "true",
        "limit": ACTIVEJOBS_LIMIT,
        "offset": offset,
        "date_filter": date_filter,
    }
    resp = httpx.get(ACTIVEJOBS_ENDPOINT, headers=headers, params=params, timeout=60)
    if resp.status_code == 429:
        time.sleep(3)
        resp = httpx.get(ACTIVEJOBS_ENDPOINT, headers=headers, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    # Handle wrapped responses e.g. {"data": [...]}
    for key in ("data", "jobs", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    return []
