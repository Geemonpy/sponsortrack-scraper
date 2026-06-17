"""
analyze_register.py
-------------------
Read-only analysis of the Home Office "Register of Licensed Sponsors: Workers" CSV.

Does NOT touch the database, does NOT modify any existing file, and writes
nothing to disk — stdout only.

Usage:
    python analyze_register.py
    SPONSOR_CSV_URL=<url> python analyze_register.py   # pin a specific CSV
"""

import csv
import io
import os
import re
from collections import Counter

import httpx
from dotenv import load_dotenv

load_dotenv()

SPONSOR_REGISTER_PAGE = (
    "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
)
SPONSOR_CSV_URL = os.environ.get("SPONSOR_CSV_URL", "").strip()


# --------------------------------------------------------------------------- #
# URL discovery — same logic as scraper.py:discover_sponsor_csv_url()
# --------------------------------------------------------------------------- #
def discover_sponsor_csv_url() -> str:
    if SPONSOR_CSV_URL:
        print(f"Using pinned SPONSOR_CSV_URL: {SPONSOR_CSV_URL}")
        return SPONSOR_CSV_URL
    print("Discovering latest sponsor register CSV from gov.uk ...")
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
    print(f"Found register CSV: {url}")
    return url


def _find_col(headers: list[str], *keywords: str) -> str | None:
    """Return the first header whose lowercase form contains any of the keywords."""
    for h in headers:
        if any(k in h.lower() for k in keywords):
            return h
    return None


def main() -> None:
    url = discover_sponsor_csv_url()
    print("Downloading register CSV ...")
    resp = httpx.get(url, timeout=120, follow_redirects=True)
    resp.raise_for_status()
    size_kb = len(resp.content) / 1024
    print(f"Downloaded {size_kb:,.0f} KB\n")

    text = resp.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers: list[str] = list(reader.fieldnames or [])

    # ------------------------------------------------------------------ #
    # 1. Print exact column headers
    # ------------------------------------------------------------------ #
    sep = "=" * 64
    print(sep)
    print("COLUMN HEADERS")
    print(sep)
    for i, h in enumerate(headers):
        print(f"  [{i}] {h!r}")
    print()

    # Identify key columns defensively
    org_col    = _find_col(headers, "organisation", "name")
    rating_col = _find_col(headers, "rating", "type")
    route_col  = _find_col(headers, "route")

    print(f"Column mapping:")
    print(f"  Organisation -> {org_col!r}")
    print(f"  Rating       -> {rating_col!r}")
    print(f"  Route        -> {route_col!r}")
    print()

    if not org_col:
        raise RuntimeError("Cannot identify organisation-name column in CSV headers.")

    # ------------------------------------------------------------------ #
    # 2. Parse rows
    # ------------------------------------------------------------------ #
    total_rows = 0
    rating_counter: Counter[str] = Counter()
    route_counter:  Counter[str] = Counter()

    # Sets keyed on raw org name (case-preserved)
    orgs_all:      set[str] = set()
    orgs_skilled:  set[str] = set()   # licensed for Skilled Worker
    orgs_a_skilled: set[str] = set()  # A-rated AND Skilled Worker

    for row in reader:
        total_rows += 1
        org    = (row.get(org_col)    or "").strip()
        rating = (row.get(rating_col) or "").strip() if rating_col else ""
        route  = (row.get(route_col)  or "").strip() if route_col  else ""

        orgs_all.add(org)

        # Extract A/B label — handles "Worker / A Rating", "A (SME+)", "A Rating", etc.
        m = re.search(r"\b([AB])\s*(?:rating|\()", rating, re.IGNORECASE)
        if m:
            rating_label = f"{m.group(1).upper()} Rating"
        elif rating:
            rating_label = rating
        else:
            rating_label = "(not stated)"
        rating_counter[rating_label] += 1

        route_label = route if route else "(not stated)"
        route_counter[route_label] += 1

        if "skilled worker" in route.lower():
            orgs_skilled.add(org)
            if m and m.group(1).upper() == "A":
                orgs_a_skilled.add(org)

    unique_orgs = len(orgs_all)

    # ------------------------------------------------------------------ #
    # 3. Print breakdowns
    # ------------------------------------------------------------------ #
    print(sep)
    print("TOTALS")
    print(sep)
    print(f"  Total rows (one per org × route):  {total_rows:>8,}")
    print(f"  Unique organisations:               {unique_orgs:>8,}")
    print()

    print(sep)
    print("BREAKDOWN BY RATING")
    print(sep)
    for label, count in sorted(rating_counter.items()):
        pct = count / total_rows * 100
        print(f"  {label:<35} {count:>7,}  ({pct:.1f}%)")
    print()

    print(sep)
    print("BREAKDOWN BY ROUTE  (sorted by frequency)")
    print(sep)
    for route, count in route_counter.most_common():
        pct = count / total_rows * 100
        print(f"  {route:<45} {count:>7,}  ({pct:.1f}%)")
    print()

    print(sep)
    print("SKILLED WORKER FOCUS")
    print(sep)
    print(f"  Unique orgs with a Skilled Worker licence:              {len(orgs_skilled):>7,}")
    print(f"  Unique orgs A-rated AND licensed for Skilled Worker:    {len(orgs_a_skilled):>7,}")
    print()

    # ------------------------------------------------------------------ #
    # 4. Plain-English summary
    # ------------------------------------------------------------------ #
    a_rows = sum(v for k, v in rating_counter.items() if k.startswith("A"))
    b_rows = sum(v for k, v in rating_counter.items() if k.startswith("B"))
    b_pct  = b_rows / total_rows * 100 if total_rows else 0

    print(sep)
    print("PLAIN-ENGLISH SUMMARY")
    print(sep)
    print(
        f"  The register has {total_rows:,} rows spread across {unique_orgs:,} unique organisations.\n"
        f"  Each organisation appears once per licensed route, so the row count is higher\n"
        f"  than the org count when a sponsor holds multiple route licences.\n"
        f"\n"
        f"  Rating split: {a_rows:,} A-rating rows vs {b_rows:,} B-rating rows ({b_pct:.1f}% B).\n"
        f"  B-rated sponsors are under a UKVI action plan and cannot take on new overseas\n"
        f"  workers, so they are effectively paused for sponsorship purposes.\n"
        f"\n"
        f"  {len(orgs_skilled):,} unique employers hold a Skilled Worker licence — the main route\n"
        f"  for professional roles that require visa sponsorship.\n"
        f"\n"
        f"  Of those, {len(orgs_a_skilled):,} are A-rated, meaning they are in good standing and\n"
        f"  can issue Certificates of Sponsorship today.\n"
        f"\n"
        f"  GAP — the scraper currently treats all {unique_orgs:,} registered orgs as equally\n"
        f"  valid sponsors, ignoring rating and route entirely. A fairer check would\n"
        f"  restrict matches to the {len(orgs_a_skilled):,} A-rated Skilled Worker sponsors,\n"
        f"  which is ~{len(orgs_a_skilled)/unique_orgs*100:.0f}% of the full register."
    )
    print()


if __name__ == "__main__":
    main()
