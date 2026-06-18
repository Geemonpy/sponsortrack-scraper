"""
SponsorTrack scraper
--------------------
Fetches UK jobs from the Adzuna API, cross-references each employer against the
Home Office "Register of licensed sponsors: workers", applies badge logic, and
upserts the results into Supabase.

Run modes:
    python scraper.py              # run one full scrape now (ideal for cron)
    python scraper.py --schedule   # stay alive, scrape every day at 10:00

Environment variables (see .env.example):
    ADZUNA_APP_ID, ADZUNA_APP_KEY, SUPABASE_URL, SUPABASE_KEY
    SPONSOR_CSV_URL   (optional) pin a specific register CSV instead of auto-discovery
"""

import argparse
import csv
import io
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from supabase import create_client

from activejobs_source import (
    FANTASTIC_LIMIT,
    FANTASTIC_MAX_PAGES,
    fetch_fantastic_page,
    is_junior_friendly,
    is_uk_job,
    map_to_adzuna_shape,
)

load_dotenv()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SPONSOR_CSV_URL = os.environ.get("SPONSOR_CSV_URL", "").strip()
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
FANTASTIC_API_KEY = os.environ.get("FANTASTIC_API_KEY", "")

ADZUNA_COUNTRY = "gb"
RESULTS_PER_PAGE = 50          # Adzuna max
PAGES_PER_QUERY = 2            # 2 x 50 = up to 100 jobs per search term
MAX_DAYS_OLD = 30             # ignore anything older than this

# Current general Skilled Worker salary threshold (Apr 2024 onward).
# Health/care, new-entrant, and national-pay-scale roles have lower legitimate
# thresholds — this constant is used for an INFORMATIONAL signal only.
GENERAL_SALARY_THRESHOLD = 41_700

# Lower legitimate threshold for health/care roles (Health and Care Worker visa route).
HEALTH_CARE_SALARY_THRESHOLD = 25_000

# gov.uk page that always links to the latest register CSV
SPONSOR_REGISTER_PAGE = (
    "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
)

SPONSORSHIP_QUERIES = [
    "visa sponsorship",
    "sponsorship available",
    "skilled worker visa",
    "tier 2 sponsorship",
    "certificate of sponsorship",
    "we offer sponsorship",
    "visa sponsorship available",
]

SEARCH_QUERIES = {
    "IT": [
        "junior software developer",
        "graduate software developer",
        "junior data analyst",
        "associate software engineer",
        "junior DevOps engineer",
        "IT support analyst",
        "technical support engineer",
        "junior cyber security analyst",
        "graduate cloud engineer",
        "junior automation tester",
    ],
    "care": [
        "care assistant",
        "support worker",
        "healthcare assistant",
        "residential support worker",
        "domiciliary care worker",
        "learning disability support worker",
        "live in carer",
        "mental health support worker",
        "autism support worker",
    ],
    "sponsorship": SPONSORSHIP_QUERIES,
}

POSITIVE_KEYWORDS = [
    "visa sponsorship", "skilled worker visa", "sponsorship available",
    "certificate of sponsorship", "cos available", "tier 2 sponsorship",
    "work visa sponsorship", "uk visa sponsorship", "health and care worker visa",
    "will sponsor", "can sponsor", "we sponsor", "sponsorship provided",
    "relocation support", "international applicants welcome",
]

NEGATIVE_KEYWORDS = [
    "no sponsorship", "cannot sponsor", "sponsorship not available",
    "unable to sponsor", "uk citizens only", "no overseas applicants",
    "right to work only", "must have right to work", "sponsorship is not available",
    "we are unable to sponsor", "british citizens only",
    "does not meet the requirements for sponsorship",
    "unable to offer visa sponsorship", "unable to offer sponsorship",
    "cannot offer sponsorship", "no visa sponsorship",
    "not able to offer sponsorship", "without sponsorship",
    "do not offer sponsorship", "does not offer sponsorship",
    "visa sponsorship is not available",
    "do not provide sponsorship", "does not provide sponsorship",
    "not provide sponsorship", "cannot provide sponsorship",
    "no sponsorship or support", "do not provide visa",
    "not provide visa sponsorship", "without visa sponsorship",
    "unable to provide sponsorship",
]

# Catches negated sponsorship/visa patterns not covered by static keywords.
# Two branches so "no" keeps a tight window (avoids false positives like
# "No experience needed - visa sponsorship available") while the stronger
# negators (do not / cannot / won't / unable to) are allowed up to 8 filler
# words to bridge constructions like:
#   "do not provide sponsorship or support for Tier 2 Skilled Worker Visas"
# Targets extended to include tier 2 and skilled worker visa so that jobs
# whose only positive keyword is one of those are also caught.
_NEGATIVE_PATTERN = re.compile(
    r"\b(?:do not|does not|don't|doesn't|cannot|can't|won't|unable to|not able to)\b"
    r"(?:\s+\w+){0,8}\s+"
    r"(?:sponsor(?:ship)?|visa\s+sponsorship|tier\s*2|skilled\s+worker\s+visa)\b"
    r"|"
    r"\bno\b(?:\s+\w+){0,3}\s+"
    r"(?:sponsor(?:ship)?|visa\s+sponsorship|tier\s*2|skilled\s+worker\s+visa)\b",
    re.IGNORECASE,
)

# Company-name suffixes/noise stripped before matching against the register
_COMPANY_NOISE = re.compile(
    r"\b(ltd|limited|plc|llp|llc|lp|inc|incorporated|company|co|group|holdings|"
    r"services|recruitment|uk|the)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NEGATORS = re.compile(r"\b(no|not|unable|cannot|can't|won't|without)\b")

# Matches health/care terms in a job title to select the lower salary threshold.
# Scoped to titles (not descriptions) to avoid false positives like "customer care".
_HEALTH_CARE_TITLE_RE = re.compile(
    r"\b(?:"
    r"nurs(?:e|ing)"
    r"|carer"
    r"|care\s+(?:worker|assistant|home)"
    r"|healthcare(?:\s+assistant)?"
    r"|health\s+care"
    r"|support\s+worker"
    r"|nhs"
    r"|social\s+care"
    r"|midwife"
    r"|physiotherapist"
    r"|paramedic"
    r"|occupational\s+therapist"
    r")\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def require_env() -> None:
    missing = [
        name for name, val in {
            "ADZUNA_APP_ID": ADZUNA_APP_ID,
            "ADZUNA_APP_KEY": ADZUNA_APP_KEY,
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_KEY": SUPABASE_KEY,
        }.items() if not val
    ]
    if missing:
        log(f"ERROR: missing environment variables: {', '.join(missing)}")
        sys.exit(1)


def normalise_company(name: str) -> str:
    """lowercase, drop common suffixes/punctuation -> comparable key."""
    if not name:
        return ""
    name = name.lower()
    name = _COMPANY_NOISE.sub(" ", name)
    name = _NON_ALNUM.sub("", name)
    return name


def discover_sponsor_csv_url() -> str:
    """Find the current register CSV link on the gov.uk publication page."""
    if SPONSOR_CSV_URL:
        log(f"Using pinned SPONSOR_CSV_URL: {SPONSOR_CSV_URL}")
        return SPONSOR_CSV_URL
    log("Discovering latest sponsor register CSV from gov.uk ...")
    resp = httpx.get(SPONSOR_REGISTER_PAGE, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    matches = re.findall(
        r"https://assets\.publishing\.service\.gov\.uk/media/[^\"'\s]+?"
        r"Worker_and_Temporary_Worker\.csv",
        resp.text,
    )
    if not matches:
        raise RuntimeError("Could not find the register CSV link on gov.uk")
    url = matches[0]
    log(f"Found register CSV: {url}")
    return url


def load_sponsor_index() -> dict[str, dict]:
    """Download the register CSV and return a sponsor info dict.

    Keyed by normalised org name. Each value contains:
        rating:            "A" | "B" | "unknown"
        routes:            set[str] of all licensed routes for that org
        is_skilled_worker: True if "Skilled Worker" is among the routes
    """
    url = discover_sponsor_csv_url()
    log("Downloading sponsor register (~10 MB) ...")
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()

    text = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])

    org_col    = next((h for h in headers if "organisation" in h.lower() or "name" in h.lower()), None)
    rating_col = next((h for h in headers if "rating" in h.lower() or "type" in h.lower()), None)
    route_col  = next((h for h in headers if "route" in h.lower()), None)

    if not org_col:
        raise RuntimeError("Cannot identify organisation column in sponsor register CSV")

    index: dict[str, dict] = {}
    for row in reader:
        org = (row.get(org_col) or "").strip()
        key = normalise_company(org)
        if not key:
            continue

        rating_raw = (row.get(rating_col) or "").strip() if rating_col else ""
        route_raw  = (row.get(route_col)  or "").strip() if route_col  else ""

        m = re.search(r"\b([AB])\s*[Rr]ating", rating_raw)
        row_rating = m.group(1).upper() if m else "unknown"

        if key not in index:
            index[key] = {"rating": row_rating, "routes": set(), "is_skilled_worker": False}
        else:
            # A beats B beats unknown when the same org appears on multiple routes.
            existing = index[key]["rating"]
            if row_rating == "A" or (row_rating == "B" and existing == "unknown"):
                index[key]["rating"] = row_rating

        if route_raw:
            index[key]["routes"].add(route_raw)
            if "skilled worker" in route_raw.lower():
                index[key]["is_skilled_worker"] = True

    skilled_count = sum(1 for v in index.values() if v["is_skilled_worker"])
    log(f"Loaded {len(index):,} unique sponsor organisations "
        f"({skilled_count:,} with Skilled Worker licence)")
    return index


def contains_any(haystack: str, needles: list[str]) -> list[str]:
    return [n for n in needles if n in haystack]


def has_negated_positive(description: str, positives: list[str]) -> bool:
    """Return True if any positive keyword occurrence is preceded by a negator within ~60 chars."""
    for kw in positives:
        idx = description.find(kw)
        while idx != -1:
            window = description[max(0, idx - 60):idx]
            if _NEGATORS.search(window):
                return True
            idx = description.find(kw, idx + 1)
    return False


def format_salary(job: dict) -> str:
    smin = job.get("salary_min")
    smax = job.get("salary_max")
    if not smin and not smax:
        return "Not specified"
    if smin and smax and smin != smax:
        return f"£{int(smin):,} – £{int(smax):,}"
    return f"£{int(smin or smax):,}"


def is_health_care(job: dict) -> bool:
    """Return True if the job is in the health/care sector.

    Checks the category field first (set on classified rows), then falls back
    to title keyword matching to catch raw API jobs before category is assigned.
    """
    if (job.get("category") or "").lower() == "care":
        return True
    return bool(_HEALTH_CARE_TITLE_RE.search(job.get("title") or ""))


def salary_signal(job: dict) -> str:
    """Return meets/below/unknown vs. the sector-appropriate salary threshold.

    Health/care jobs are compared against HEALTH_CARE_SALARY_THRESHOLD (£25,000);
    all other jobs against GENERAL_SALARY_THRESHOLD (£41,700). Uses salary_max
    as the best available figure; falls back to salary_min. Returns "unknown"
    when no usable figure exists.
    This is informational only — it must not affect badge logic or filtering.
    """
    best = job.get("salary_max") or job.get("salary_min")
    if not best:
        return "unknown"
    threshold = HEALTH_CARE_SALARY_THRESHOLD if is_health_care(job) else GENERAL_SALARY_THRESHOLD
    return "meets" if best >= threshold else "below"


def parse_posted_date(job: dict) -> date | None:
    created = job.get("created")
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def classify(job: dict, sponsor_index: dict[str, dict]) -> dict | None:
    """Apply badge logic. Returns a row dict, or None if the job is auto-rejected."""
    description = (job.get("description") or "").lower()
    company = (job.get("company") or {}).get("display_name", "") or "Unknown"

    # Explicit negative keywords always override positives
    negatives = contains_any(description, NEGATIVE_KEYWORDS)
    if negatives:
        return None

    # Regex-based negative pattern catches novel "do not / cannot / won't ... sponsor" wordings
    if _NEGATIVE_PATTERN.search(description):
        return None

    positives = contains_any(description, POSITIVE_KEYWORDS)

    # A positive keyword preceded by a negator within ~30 chars is a rejection signal
    if positives and has_negated_positive(description, positives):
        return None

    sponsor_info = sponsor_index.get(normalise_company(company))
    sponsor_match = sponsor_info is not None
    is_skilled_worker_sponsor = sponsor_info["is_skilled_worker"] if sponsor_info else False
    sponsor_rating = sponsor_info["rating"] if sponsor_info else None
    sponsor_routes_str = ", ".join(sorted(sponsor_info["routes"])) if sponsor_info else None

    # Strong badges require a Skilled Worker licence; everything else is "mentioned".
    if sponsor_match and is_skilled_worker_sponsor and positives:
        badge = "sponsor_confirmed"
    elif sponsor_match and is_skilled_worker_sponsor:
        badge = "licensed_sponsor"
    elif positives:
        # Covers: off-register orgs AND on-register orgs licensed only for other routes
        # (Creative Worker, Global Business Mobility, Ministers of Religion, etc.)
        badge = "sponsorship_mentioned"
    else:
        return None  # not a Skilled Worker sponsor and no positive keyword

    posted = parse_posted_date(job)
    if posted and posted < (datetime.now(timezone.utc).date() - timedelta(days=MAX_DAYS_OLD)):
        return None

    return {
        "external_id": f"adzuna:{job.get('id')}",
        "title": job.get("title", "").strip() or "Untitled role",
        "company": company,
        "location": (job.get("location") or {}).get("display_name"),
        "salary": format_salary(job),
        "description": job.get("description"),
        "source": "Adzuna",
        "apply_url": job.get("redirect_url"),
        "sponsor_match": sponsor_match,
        "badge": badge,
        "positive_keywords": positives,
        "negative_keywords": negatives,
        "posted_date": posted.isoformat() if posted else None,
        "sponsor_rating": sponsor_rating,
        "sponsor_routes": sponsor_routes_str,
        "is_skilled_worker_sponsor": is_skilled_worker_sponsor,
        "meets_general_threshold": salary_signal(job),
    }


# --------------------------------------------------------------------------- #
# Adzuna fetch
# --------------------------------------------------------------------------- #
def fetch_adzuna(query: str, page: int) -> list[dict]:
    url = f"https://api.adzuna.com/v1/api/jobs/{ADZUNA_COUNTRY}/search/{page}"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PER_PAGE,
        "what": query,
        "max_days_old": MAX_DAYS_OLD,
        "content-type": "application/json",
    }
    resp = httpx.get(url, params=params, timeout=60)
    if resp.status_code != 200:
        log(f"  ! Adzuna {resp.status_code} for '{query}' p{page}: {resp.text[:120]}")
        return []
    return resp.json().get("results", [])


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #
def deduplicate_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Collapse same-title+company listings to one representative row each.

    Representative = most recent by posted_date.  If the group spans multiple
    locations the representative's location is rewritten as
    "First Location +N more locations".
    Returns (deduped_rows, n_collapsed).
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        title_key = re.sub(r"\s+", " ", (row.get("title") or "").lower().strip())
        company_key = re.sub(r"\s+", " ", (row.get("company") or "").lower().strip())
        groups[f"{title_key}||{company_key}"].append(row)

    result: list[dict] = []
    collapsed = 0
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        collapsed += len(group) - 1
        # Most-recent first; rows without a date sort to the end.
        group.sort(key=lambda r: r.get("posted_date") or "0000-00-00", reverse=True)
        rep = dict(group[0])  # shallow copy so we don't mutate the original

        # Collect unique locations in order of recency.
        seen: set[str] = set()
        unique_locs: list[str] = []
        for r in group:
            loc = (r.get("location") or "").strip()
            if loc and loc not in seen:
                seen.add(loc)
                unique_locs.append(loc)

        if len(unique_locs) > 1:
            rep["location"] = f"{unique_locs[0]} +{len(unique_locs) - 1} more locations"

        result.append(rep)

    return result, collapsed


# --------------------------------------------------------------------------- #
# Main scrape
# --------------------------------------------------------------------------- #
def run_scrape() -> None:
    require_env()
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    sponsor_index = load_sponsor_index()

    rows_by_id: dict[str, dict] = {}
    rejected = 0

    for category, queries in SEARCH_QUERIES.items():
        for query in queries:
            for page in range(1, PAGES_PER_QUERY + 1):
                jobs = fetch_adzuna(query, page)
                if not jobs:
                    break
                for job in jobs:
                    row = classify(job, sponsor_index)
                    if row is None:
                        rejected += 1
                        continue
                    row["category"] = category
                    rows_by_id[row["external_id"]] = row  # dedupe across queries
            log(f"  {category:<4} '{query}' -> running total {len(rows_by_id)}")

    # ------------------------------------------------------------------ #
    # Fantastic Jobs (direct API)
    # ------------------------------------------------------------------ #
    if not FANTASTIC_API_KEY:
        log("FANTASTIC_API_KEY not set — skipping Fantastic Jobs")
    else:
        fantastic_fetched = 0
        fantastic_rejected_uk = 0
        fantastic_kept = 0
        log("Fetching Fantastic Jobs (direct API, time_frame=24h) ...")
        try:
            for page_num in range(FANTASTIC_MAX_PAGES):
                offset = page_num * FANTASTIC_LIMIT
                raw_jobs, resp_headers = fetch_fantastic_page(FANTASTIC_API_KEY, offset)
                if page_num == 0:
                    jobs_rem = resp_headers.get("x-api-jobs-remaining", "?")
                    req_rem = resp_headers.get("x-api-requests-remaining", "?")
                    log(f"  Fantastic Jobs credits — jobs-remaining={jobs_rem}, requests-remaining={req_rem}")
                log(f"  Fantastic Jobs offset={offset} -> {len(raw_jobs)} raw")
                if not raw_jobs:
                    break
                for job in raw_jobs:
                    fantastic_fetched += 1
                    if not is_uk_job(job):
                        fantastic_rejected_uk += 1
                        continue
                    adzuna_like = map_to_adzuna_shape(job)
                    if adzuna_like is None:
                        continue
                    row = classify(adzuna_like, sponsor_index)
                    if row is None:
                        rejected += 1
                        continue
                    job_id = str(job.get("id", ""))
                    row["external_id"] = f"fantastic:{job_id}"
                    row["source"] = "Fantastic Jobs"
                    row["apply_url"] = job.get("url")
                    row["category"] = "ats"
                    row["junior_friendly"] = is_junior_friendly(
                        job.get("title", ""),
                        job.get("description") or job.get("description_text", ""),
                    )
                    rows_by_id[row["external_id"]] = row
                    fantastic_kept += 1
            log(
                f"Fantastic Jobs: fetched={fantastic_fetched}, "
                f"rejected_non_uk={fantastic_rejected_uk}, "
                f"kept={fantastic_kept}"
            )
        except Exception as exc:
            log(
                f"WARNING: Fantastic Jobs fetch failed ({exc!r}) — "
                "continuing with Adzuna jobs only"
            )

    rows = list(rows_by_id.values())
    log(f"Collected {len(rows)} jobs ({rejected} auto-rejected)")

    if not rows:
        log("Nothing to upsert.")
        return

    rows, collapsed = deduplicate_rows(rows)
    log(f"Collapsed {len(rows) + collapsed} jobs into {len(rows)} unique listings "
        f"({collapsed} duplicates removed)")

    # Upsert in batches; dedupe on the unique external_id column.
    # If the junior_friendly column is missing, log the SQL to add it and retry
    # the affected batches without that field.
    BATCH = 500
    drop_junior_col = False
    drop_salary_col = False
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        if drop_junior_col:
            chunk = [{k: v for k, v in r.items() if k != "junior_friendly"} for r in chunk]
        if drop_salary_col:
            chunk = [{k: v for k, v in r.items() if k != "meets_general_threshold"} for r in chunk]
        try:
            supabase.table("jobs").upsert(chunk, on_conflict="external_id").execute()
        except Exception as exc:
            exc_str = str(exc)
            if "junior_friendly" in exc_str and not drop_junior_col:
                drop_junior_col = True
                log("WARNING: 'junior_friendly' column not found in Supabase.")
                log("  Run this SQL to add it:")
                log("    ALTER TABLE jobs ADD COLUMN junior_friendly BOOLEAN;")
                log("  Retrying upsert without junior_friendly ...")
                chunk = [{k: v for k, v in r.items() if k != "junior_friendly"} for r in chunk]
                supabase.table("jobs").upsert(chunk, on_conflict="external_id").execute()
            elif "meets_general_threshold" in exc_str and not drop_salary_col:
                drop_salary_col = True
                log("WARNING: 'meets_general_threshold' column not found in Supabase.")
                log("  Run this SQL to add it:")
                log("    ALTER TABLE jobs ADD COLUMN meets_general_threshold TEXT;")
                log("  Retrying upsert without meets_general_threshold ...")
                chunk = [{k: v for k, v in r.items() if k != "meets_general_threshold"} for r in chunk]
                supabase.table("jobs").upsert(chunk, on_conflict="external_id").execute()
            else:
                raise
        log(f"  upserted {i + len(chunk)}/{len(rows)}")

    # Remove rows from previous runs that are no longer valid this scrape.
    # Only reached when rows is non-empty, so we never wipe the table on a failed run.
    # Use deduped ids so displaced duplicate rows are also swept from the DB.
    valid_ids_list = [r["external_id"] for r in rows]
    del_result = (
        supabase.table("jobs")
        .delete()
        .not_.in_("external_id", valid_ids_list)
        .execute()
    )
    deleted = len(del_result.data) if del_result.data else 0
    log(f"Deleted {deleted} stale row(s) not present in this scrape")

    counts = {b: sum(1 for r in rows if r["badge"] == b)
              for b in ("sponsor_confirmed", "licensed_sponsor", "sponsorship_mentioned")}
    skilled_matched = sum(1 for r in rows if r.get("is_skilled_worker_sponsor"))
    on_register_not_skilled = sum(
        1 for r in rows if r.get("sponsor_match") and not r.get("is_skilled_worker_sponsor")
    )
    salary_counts = {s: sum(1 for r in rows if r.get("meets_general_threshold") == s)
                     for s in ("meets", "below", "unknown")}
    health_care_count = sum(1 for r in rows if is_health_care(r))
    general_count = len(rows) - health_care_count
    log(
        f"Done. total_kept={len(rows)} | "
        f"sponsor_confirmed={counts['sponsor_confirmed']} "
        f"licensed_sponsor={counts['licensed_sponsor']} "
        f"sponsorship_mentioned={counts['sponsorship_mentioned']} | "
        f"skilled_worker_matched={skilled_matched} "
        f"on_register_not_skilled_worker={on_register_not_skilled} | "
        f"salary_sector=health_care:{health_care_count}/general:{general_count} "
        f"salary_meets={salary_counts['meets']} "
        f"salary_below={salary_counts['below']} "
        f"salary_unknown={salary_counts['unknown']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SponsorTrack scraper")
    parser.add_argument("--schedule", action="store_true",
                        help="run daily at 10:00 instead of once")
    args = parser.parse_args()

    if args.schedule:
        from apscheduler.schedulers.blocking import BlockingScheduler
        scheduler = BlockingScheduler(timezone="Europe/London")
        scheduler.add_job(run_scrape, "cron", hour=10, minute=0)
        log("Scheduler started — scraping daily at 10:00 Europe/London. Ctrl+C to stop.")
        run_scrape()  # run once on boot too
        scheduler.start()
    else:
        run_scrape()


if __name__ == "__main__":
    main()
