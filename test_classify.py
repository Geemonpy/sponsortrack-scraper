"""
Tests for scraper.classify() badge logic and rejection rules.
Run with: python test_classify.py
"""

from scraper import classify, salary_signal, GENERAL_SALARY_THRESHOLD


def make_job(description="", company="SomeCompany", job_id="1"):
    return {
        "id": job_id,
        "title": "Test Role",
        "description": description,
        "company": {"display_name": company},
        "location": {"display_name": "London"},
        "salary_min": None,
        "salary_max": None,
        "created": "2026-05-28T00:00:00Z",
        "redirect_url": "https://example.com/job/1",
    }


REGISTER: dict = {
    "somecompany": {
        "rating": "A",
        "routes": {"Skilled Worker"},
        "is_skilled_worker": True,
    }
}
NO_REGISTER: dict = {}


def test_sponsor_confirmed():
    """on register + positive keyword -> sponsor_confirmed"""
    result = classify(make_job("visa sponsorship available"), REGISTER)
    assert result is not None, "Expected a result, got None"
    assert result["badge"] == "sponsor_confirmed", f"Expected sponsor_confirmed, got {result['badge']}"
    print("PASS test_sponsor_confirmed")


def test_negative_unable_to_offer():
    """'unable to offer visa sponsorship' -> rejected (None)"""
    result = classify(
        make_job("we are unable to offer visa sponsorship for these roles"), REGISTER
    )
    assert result is None, f"Expected None (rejection), got badge={result and result['badge']}"
    print("PASS test_negative_unable_to_offer")


def test_licensed_sponsor():
    """on register, neutral text -> licensed_sponsor"""
    result = classify(make_job("a great opportunity to join our team"), REGISTER)
    assert result is not None, "Expected a result, got None"
    assert result["badge"] == "licensed_sponsor", f"Expected licensed_sponsor, got {result['badge']}"
    print("PASS test_licensed_sponsor")


def test_right_to_work_rejection():
    """'must have right to work in UK' -> rejected (None)"""
    result = classify(make_job("must have right to work in uk"), NO_REGISTER)
    assert result is None, f"Expected None (rejection), got badge={result and result['badge']}"
    print("PASS test_right_to_work_rejection")


def test_sponsorship_mentioned():
    """'we can sponsor skilled workers' + NOT on register -> sponsorship_mentioned"""
    result = classify(make_job("we can sponsor skilled workers"), NO_REGISTER)
    assert result is not None, "Expected a result, got None"
    assert result["badge"] == "sponsorship_mentioned", f"Expected sponsorship_mentioned, got {result['badge']}"
    print("PASS test_sponsorship_mentioned")


def test_do_not_provide_sponsorship_tier2():
    """Promedica Plus UK style: 'we do not provide sponsorship or support for Tier 2' -> rejected"""
    result = classify(
        make_job("we do not provide sponsorship or support for Tier 2 Skilled Worker Visas"),
        REGISTER,
    )
    assert result is None, f"Expected None (rejection), got badge={result and result['badge']}"
    print("PASS test_do_not_provide_sponsorship_tier2")


def test_do_not_provide_sponsorship():
    """'do not provide sponsorship' -> rejected"""
    result = classify(make_job("do not provide sponsorship"), REGISTER)
    assert result is None, f"Expected None (rejection), got badge={result and result['badge']}"
    print("PASS test_do_not_provide_sponsorship")


def test_cannot_provide_visa_sponsorship():
    """'cannot provide visa sponsorship' -> rejected via regex"""
    result = classify(make_job("we cannot provide visa sponsorship for this role"), NO_REGISTER)
    assert result is None, f"Expected None (rejection), got badge={result and result['badge']}"
    print("PASS test_cannot_provide_visa_sponsorship")


def test_genuine_positive_still_passes():
    """'visa sponsorship available' with no negators -> sponsorship_mentioned"""
    result = classify(make_job("visa sponsorship available for the right candidate"), NO_REGISTER)
    assert result is not None, "Expected a result, got None"
    assert result["badge"] == "sponsorship_mentioned", f"Expected sponsorship_mentioned, got {result['badge']}"
    print("PASS test_genuine_positive_still_passes")


PROMEDICA24_DESC = (
    "Join the PROMEDICA24 team and become part of a group of dedicated caregivers where Care is"
    " more than a job its a calling. We are committed to a Positive approach, Empathy, and"
    " striving for Excellence in everything we do. Please Note: We do not provide sponsorship or"
    " support for Tier 2 Skilled Worker Visas. Apply Now: Click the job post link to submit your"
    " application it only takes about 5 minutes! What We Offer: Long-term, stable employment"
    " contracts with career growth opportunities. Daily earni"
)


def test_salary_signal_meets():
    """salary_max >= 41700 -> meets"""
    assert salary_signal({"salary_max": GENERAL_SALARY_THRESHOLD, "salary_min": 20000}) == "meets"
    assert salary_signal({"salary_max": 50000, "salary_min": None}) == "meets"
    print("PASS test_salary_signal_meets")


def test_salary_signal_below():
    """salary_max < 41700 -> below; falls back to salary_min when max is absent"""
    assert salary_signal({"salary_max": 30000, "salary_min": 25000}) == "below"
    assert salary_signal({"salary_max": None, "salary_min": 28000}) == "below"
    print("PASS test_salary_signal_below")


def test_salary_signal_unknown():
    """no usable salary figure -> unknown"""
    assert salary_signal({"salary_max": None, "salary_min": None}) == "unknown"
    assert salary_signal({}) == "unknown"
    print("PASS test_salary_signal_unknown")


def test_classify_includes_salary_signal():
    """classify() row always contains meets_general_threshold"""
    result = classify(make_job("visa sponsorship available"), REGISTER)
    assert result is not None
    assert "meets_general_threshold" in result
    assert result["meets_general_threshold"] == "unknown"  # make_job has no salary
    print("PASS test_classify_includes_salary_signal")


def test_classify_salary_meets():
    """classify() reflects meets when salary_max >= threshold"""
    job = make_job("visa sponsorship available")
    job["salary_max"] = 45000
    result = classify(job, REGISTER)
    assert result is not None
    assert result["meets_general_threshold"] == "meets"
    print("PASS test_classify_salary_meets")


def test_promedica24_full_description_rejected():
    """Full PROMEDICA24 description with 'do not provide sponsorship or support for Tier 2 Skilled Worker Visas' -> rejected"""
    result = classify(make_job(PROMEDICA24_DESC, company="PROMEDICA24"), NO_REGISTER)
    assert result is None, f"Expected None (rejection), got badge={result and result['badge']}"
    print("PASS test_promedica24_full_description_rejected")


if __name__ == "__main__":
    tests = [
        test_sponsor_confirmed,
        test_negative_unable_to_offer,
        test_licensed_sponsor,
        test_right_to_work_rejection,
        test_sponsorship_mentioned,
        test_do_not_provide_sponsorship_tier2,
        test_do_not_provide_sponsorship,
        test_cannot_provide_visa_sponsorship,
        test_genuine_positive_still_passes,
        test_promedica24_full_description_rejected,
        test_salary_signal_meets,
        test_salary_signal_below,
        test_salary_signal_unknown,
        test_classify_includes_salary_signal,
        test_classify_salary_meets,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failures += 1
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(failures)
