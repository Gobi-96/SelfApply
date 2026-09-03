"""Conservative eligibility checks that run before LLM scoring.

Discovery results are often incomplete, so these rules only reject a job when
the enriched posting contains an explicit, profile-backed mismatch. Ambiguous
postings continue through the normal pipeline and are checked again in the
browser.
"""

from __future__ import annotations

import re
from typing import Any


_REMOTE_POSITIVE = re.compile(
    r"\b(?:fully\s+remote|remote(?:[- ]first)?|work\s+from\s+home|telecommut\w*)\b",
    re.IGNORECASE,
)
_REMOTE_NEGATIVE = re.compile(
    r"\b(?:not\s+(?:a\s+)?remote|no\s+remote|remote\s+(?:work\s+)?(?:is\s+)?not\s+"
    r"(?:available|offered|permitted)|on[- ]?site\s+only|in[- ]office\s+only)\b",
    re.IGNORECASE,
)
_ONSITE_REQUIRED = re.compile(
    r"\b(?:on[- ]?site|in[- ]office|hybrid|report\s+to\s+(?:the\s+)?office|"
    r"must\s+(?:be\s+)?(?:located|reside|live)\s+in)\b",
    re.IGNORECASE,
)
_NO_SPONSORSHIP = re.compile(
    r"\b(?:no|not|unable\s+to|cannot|can't|will\s+not|won't|does\s+not|do\s+not)\s+"
    r"(?:offer|provide|support)?\s*(?:employment\s+|visa\s+)?sponsor(?:ship|ing)?\b|"
    r"\bwithout\s+(?:requiring\s+)?sponsor(?:ship|ing)?\s+(?:now\s+or\s+in\s+the\s+future|"
    r"now\s+and\s+in\s+the\s+future)\b",
    re.IGNORECASE,
)

def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _accepted_location(
    location: str,
    accept_patterns: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> bool:
    normalized_location = _normalized(location)
    if not normalized_location:
        return False
    normalized_patterns = [_normalized(item) for item in accept_patterns if _normalized(item)]
    if any(pattern in normalized_location for pattern in normalized_patterns):
        return True
    for region, members in (aliases or {}).items():
        if _normalized(region) not in normalized_patterns:
            continue
        if any(_normalized(member) in normalized_location for member in members):
            return True
    return False


def early_ineligibility_reason(
    job: dict,
    profile: dict,
    search_config: dict,
) -> str | None:
    """Return a stable failure code for an explicit mismatch, otherwise None."""
    description = str(job.get("full_description") or job.get("description") or "")
    location = str(job.get("location") or "")
    evidence = f"{location}\n{description}"

    work_auth = profile.get("work_authorization", {})
    if work_auth.get("require_sponsorship") is True and _NO_SPONSORSHIP.search(description):
        return "not_eligible_work_auth"

    remote_is_disallowed = bool(_REMOTE_NEGATIVE.search(evidence))
    remote_is_allowed = bool(_REMOTE_POSITIVE.search(evidence)) and not remote_is_disallowed
    onsite_is_required = bool(_ONSITE_REQUIRED.search(evidence)) or remote_is_disallowed
    if onsite_is_required and not remote_is_allowed:
        location_cfg = search_config.get("location", {}) or {}
        accept_patterns = list(location_cfg.get("accept_patterns", []) or [])
        if not accept_patterns:
            city = str(profile.get("personal", {}).get("city") or "").strip()
            if city:
                accept_patterns = [city]
        # A blank or nationwide location is ambiguous and must not be rejected.
        country = _normalized(profile.get("personal", {}).get("country"))
        generic_locations = {
            "", "multiple locations", "various locations", "nationwide", "remote",
        }
        if country:
            generic_locations.add(country)
        generic = _normalized(location) in generic_locations
        aliases = location_cfg.get("aliases", {})
        if not isinstance(aliases, dict):
            aliases = {}
        if accept_patterns and not generic and not _accepted_location(
            location, accept_patterns, aliases,
        ):
            return "not_eligible_location"

    return None
