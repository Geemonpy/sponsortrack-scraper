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
from datetime import date, datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SPONSOR_CSV_URL = os.environ.get("SPONSOR_CSV_URL", "").strip()

ADZUNA_COUNTRY = "gb"
RESULTS_PER_PAGE = 50          # Adzuna max
PAGES_PER_QUERY = 2            # 2 x 50 = up to 100 jobs per search term
MAX_DAYS_OLD = 30             # ignore anything older than this

# gov.uk page that always links to the latest register CSV
SPONSOR_REGISTER_PAGE = (
    "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
)

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


def load_sponsor_index() -> set[str]:
    """Download the register CSV and return a set of normalised company keys."""
    url = discover_sponsor_csv_url()
    log("Downloading sponsor register (~10 MB) ...")
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()

    text = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    # The org-name column is usually the first; find it defensively.
    org_idx = 0
    for i, col in enumerate(header):
        if "organisation" in col.lower() or "name" in col.lower():
            org_idx = i
            break

    index: set[str] = set()
    for row in reader:
        if len(row) > org_idx:
            key = normalise_company(row[org_idx])
            if key:
                index.add(key)
    log(f"Loaded {len(index):,} unique sponsor organisations")
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


def parse_posted_date(job: dict) -> date | None:
    created = job.get("created")
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def classify(job: dict, sponsor_index: set[str]) -> dict | None:
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

    sponsor_match = normalise_company(company) in sponsor_index

    if sponsor_match and positives:
        badge = "sponsor_confirmed"
    elif sponsor_match:
        badge = "licensed_sponsor"
    elif positives:
        badge = "sponsorship_mentioned"
    else:
        return None  # not on register, no positive keyword

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

    rows = list(rows_by_id.values())
    log(f"Collected {len(rows)} jobs ({rejected} auto-rejected)")

    if not rows:
        log("Nothing to upsert.")
        return

    # Upsert in batches; dedupe on the unique external_id column.
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        supabase.table("jobs").upsert(chunk, on_conflict="external_id").execute()
        log(f"  upserted {i + len(chunk)}/{len(rows)}")

    # Remove rows from previous runs that are no longer valid this scrape.
    # Only reached when rows is non-empty, so we never wipe the table on a failed run.
    valid_ids_list = list(rows_by_id.keys())
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
    log(f"Done. sponsor_confirmed={counts['sponsor_confirmed']} "
        f"licensed_sponsor={counts['licensed_sponsor']} "
        f"sponsorship_mentioned={counts['sponsorship_mentioned']}")


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
