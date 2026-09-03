from selfapply.eligibility import early_ineligibility_reason


PROFILE = {
    "personal": {"city": "Portland", "province_state": "Oregon", "country": "United States"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": True},
}
SEARCH = {
    "location": {
        "accept_patterns": ["Portland", "Portland Metro", "Beaverton", "Hillsboro"],
        "aliases": {"Portland Metro": ["Beaverton", "Hillsboro"]},
    }
}


def test_rejects_explicit_no_sponsorship_before_scoring():
    job = {
        "location": "Remote, US",
        "full_description": "This role is remote. We are unable to provide visa sponsorship.",
    }
    assert early_ineligibility_reason(job, PROFILE, SEARCH) == "not_eligible_work_auth"


def test_rejects_clear_nonlocal_onsite_role():
    job = {
        "location": "Dallas, TX",
        "full_description": "Employees must work onsite five days each week. No remote option is available.",
    }
    assert early_ineligibility_reason(job, PROFILE, SEARCH) == "not_eligible_location"


def test_keeps_remote_role_regardless_of_headquarters_location():
    job = {
        "location": "New York, NY",
        "full_description": "This is a fully remote role open anywhere in the United States.",
    }
    assert early_ineligibility_reason(job, PROFILE, SEARCH) is None


def test_keeps_metro_city_alias_and_ambiguous_posting():
    local = {"location": "Beaverton, OR", "full_description": "Hybrid, three days in office."}
    ambiguous = {"location": "United States", "full_description": "Work arrangement discussed in interview."}
    assert early_ineligibility_reason(local, PROFILE, SEARCH) is None
    assert early_ineligibility_reason(ambiguous, PROFILE, SEARCH) is None


def test_no_sponsorship_is_irrelevant_when_candidate_does_not_need_it():
    profile = {**PROFILE, "work_authorization": {"require_sponsorship": False}}
    job = {"location": "Remote", "full_description": "We cannot sponsor visas."}
    assert early_ineligibility_reason(job, profile, SEARCH) is None
