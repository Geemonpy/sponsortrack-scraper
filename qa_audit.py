"""
qa_audit.py  —  Read-only QA audit for the SponsorTrack jobs table.

Connects to Supabase (same credentials as scraper.py), pulls every row, and
prints a structured report covering:
  1. Totals by badge
  2. Suspected missed rejections (jobs whose description contains a refusal signal)
  3. Data-quality issues (missing apply_url, likely title+company duplicates)
  4. One-line summary
"""

import os
import re
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# ---------------------------------------------------------------------------
# Refusal signals used in section 2
# ---------------------------------------------------------------------------
REFUSAL_PHRASES = [
    "no sponsorship",
    "not provide sponsorship",
    "unable to offer sponsorship",
    "unable to offer visa sponsorship",
    "unable to provide sponsorship",
    "unable to provide visa sponsorship",
    "cannot sponsor",
    "cannot offer sponsorship",
    "cannot provide sponsorship",
    "do not offer sponsorship",
    "does not offer sponsorship",
    "do not provide sponsorship",
    "does not provide sponsorship",
    "without sponsorship",
    "without visa sponsorship",
    "no visa sponsorship",
    "no visa",
    "right to work",
    "british citizens only",
    "uk citizens only",
    "no overseas applicants",
    "uk citizens only",
    "must have right to work",
    "sponsorship is not available",
    "sponsorship not available",
]

# Catches "do not / does not / cannot / won't / no + provide|offer|sponsor|tier 2|skilled worker visa"
_REFUSAL_PATTERN = re.compile(
    r"\b(?:do not|does not|don't|doesn't|cannot|can't|won't|unable to|not able to)\b"
    r"(?:\s+\w+){0,8}\s+"
    r"(?:sponsor(?:ship)?|visa\s+sponsorship|tier\s*2|skilled\s+worker\s+visa)\b"
    r"|"
    r"\bno\b(?:\s+\w+){0,3}\s+"
    r"(?:sponsor(?:ship)?|visa\s+sponsorship|tier\s*2|skilled\s+worker\s+visa)\b",
    re.IGNORECASE,
)

BADGE_ORDER = ["sponsor_confirmed", "licensed_sponsor", "sponsorship_mentioned",
               "verified", "likely", "hidden"]  # cover both old and new badge names


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_all_rows(supabase) -> list[dict]:
    """Pull every row, paginating in chunks of 1 000 (Supabase default limit)."""
    rows: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        result = (
            supabase.table("jobs")
            .select("external_id,title,company,location,badge,description,apply_url")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def find_refusal_match(description: str) -> str | None:
    """Return the first matching refusal signal (phrase or regex), or None."""
    lower = description.lower()
    for phrase in REFUSAL_PHRASES:
        if phrase in lower:
            return phrase
    m = _REFUSAL_PATTERN.search(description)
    if m:
        return m.group(0)
    return None


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set in .env")
        raise SystemExit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    log("Fetching all rows from jobs table ...")
    rows = fetch_all_rows(supabase)
    log(f"Fetched {len(rows)} rows.")

    # -------------------------------------------------------------------
    # 1. TOTALS
    # -------------------------------------------------------------------
    section("1. TOTALS")
    badge_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        badge_counts[r.get("badge") or "(null)"] += 1

    print(f"  Total jobs : {len(rows)}")
    for badge in BADGE_ORDER:
        if badge in badge_counts:
            print(f"    {badge:<28} {badge_counts[badge]:>5}")
    for badge, count in sorted(badge_counts.items()):
        if badge not in BADGE_ORDER:
            print(f"    {badge:<28} {count:>5}")

    # -------------------------------------------------------------------
    # 2. SUSPECTED MISSED REJECTIONS
    # -------------------------------------------------------------------
    section("2. SUSPECTED MISSED REJECTIONS")
    missed: list[tuple[dict, str]] = []
    for r in rows:
        desc = r.get("description") or ""
        match = find_refusal_match(desc)
        if match:
            missed.append((r, match))

    if not missed:
        print("  None found.")
    else:
        print(f"  {len(missed)} job(s) contain a refusal signal:\n")
        for r, phrase in missed:
            print(f"  [{r.get('badge','?'):>22}]  {r.get('title','?')[:60]}")
            print(f"    Company : {r.get('company','?')}")
            print(f"    Matched : \"{phrase}\"")
            print()

    # -------------------------------------------------------------------
    # 3. DATA QUALITY
    # -------------------------------------------------------------------
    section("3. DATA QUALITY")

    # 3a. Missing apply_url
    no_url = [r for r in rows if not (r.get("apply_url") or "").strip()]
    print(f"  Missing apply_url: {len(no_url)}")
    if no_url:
        for r in no_url[:20]:
            print(f"    - [{r.get('badge','?')}] {r.get('title','?')[:55]}  |  {r.get('company','?')}")
        if len(no_url) > 20:
            print(f"    ... and {len(no_url) - 20} more")

    # 3b. Likely duplicates: same (normalised title, normalised company) >= 3 times
    print()
    key_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        title_key = re.sub(r"\s+", " ", (r.get("title") or "").lower().strip())
        company_key = re.sub(r"\s+", " ", (r.get("company") or "").lower().strip())
        key_groups[(title_key, company_key)].append(r)

    dup_groups = {k: v for k, v in key_groups.items() if len(v) >= 3}
    print(f"  Duplicate groups (same title+company, ≥3 occurrences): {len(dup_groups)}")
    if dup_groups:
        for (title_key, company_key), group in sorted(dup_groups.items(),
                                                       key=lambda x: -len(x[1])):
            locations = [r.get("location") or "(no location)" for r in group]
            print(f"\n    \"{title_key[:55]}\" @ {company_key[:35]}")
            print(f"    {len(group)} occurrences — locations: {', '.join(locations[:6])}")

    # -------------------------------------------------------------------
    # 4. SUMMARY
    # -------------------------------------------------------------------
    section("4. SUMMARY")
    print(
        f"  {len(rows)} total jobs"
        f" | {len(missed)} suspected missed rejections"
        f" | {len(no_url)} missing apply_url"
        f" | {len(dup_groups)} duplicate group(s)"
    )
    print()


if __name__ == "__main__":
    main()
