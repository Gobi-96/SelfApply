"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from selfapply.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
creative -- unsupported skills are warnings; identity/history/structure remain checked
"""

import re
import logging

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    # Generic labels such as "Note:" and "Important:" are legitimate resume
    # content and are not sufficient evidence of model self-talk.
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


# ── Helpers ───────────────────────────────────────────────────────────────

def _normalized_fact_text(text: str) -> str:
    """Normalize common PDF extraction glyphs before factual comparisons."""
    return text.replace("\ue09d", "+").replace("", "+").casefold()


def build_allowed_skills(profile: dict, original_text: str = "") -> set[str]:
    """Build allowed skills, optionally requiring evidence in the master resume."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    confirmed = {
        str(skill).casefold().strip()
        for field in (
            "transferable_familiarity", "confirmed_experience_skills",
        )
        for skill in boundary.get(field, [])
    }
    for field, category in boundary.items():
        if field == "target_learning_skills":
            continue
        if isinstance(category, list):
            allowed.update(s.casefold().strip() for s in category)
        elif isinstance(category, set):
            allowed.update(s.casefold().strip() for s in category)
    if original_text:
        original = _normalized_fact_text(original_text)
        allowed = {
            skill for skill in allowed
            if skill in confirmed
            or re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", original)
        }
    return allowed


def _skill_items(skills: object) -> list[str]:
    """Flatten the model's categorized skills into individual canonical items."""
    values = skills.values() if isinstance(skills, dict) else []
    items: list[str] = []
    for value in values:
        raw_values = value if isinstance(value, list) else [value]
        for raw in raw_values:
            items.extend(
                part.strip() for part in re.split(
                    r"[,;|\n]|\s+(?:and|&)\s+", str(raw), flags=re.IGNORECASE,
                ) if part.strip()
            )
    return items


def _unsupported_skills(skills: object, profile: dict,
                        original_text: str = "") -> list[str]:
    allowed = build_allowed_skills(profile, original_text)
    original = _normalized_fact_text(original_text)
    return [
        item for item in _skill_items(skills)
        if item.casefold() not in allowed
        and not re.search(
            r"(?<!\w)" + re.escape(item.casefold()) + r"(?!\w)", original,
        )
    ]


def remove_unsupported_skills(
    skills: object, profile: dict, original_text: str = "",
) -> tuple[dict[str, str], list[str]]:
    """Remove unsupported model-added skill-list items before validation."""
    if not isinstance(skills, dict):
        return {}, []
    unsupported = {
        item.casefold() for item in _unsupported_skills(
            skills, profile, original_text=original_text,
        )
    }
    cleaned: dict[str, str] = {}
    removed: list[str] = []
    for category, value in skills.items():
        items = _skill_items({str(category): value})
        kept: list[str] = []
        for item in items:
            if item.casefold() in unsupported:
                removed.append(item)
            else:
                kept.append(item)
        if kept:
            cleaned[str(category)] = ", ".join(kept)
    return cleaned, removed


_PLACEHOLDER_PATTERNS = (
    re.compile(r"\b(?:tbd|to be determined|placeholder)\b", re.IGNORECASE),
    re.compile(r"^\s*tech(?:nologies)?\s*(?:\||$)", re.IGNORECASE),
    re.compile(r"(?:^|\|)\s*dates?\s*(?:\||$)", re.IGNORECASE),
    re.compile(r"^\s*n/?a\s*$", re.IGNORECASE),
)


def _contains_placeholder(value: object) -> bool:
    return any(pattern.search(str(value)) for pattern in _PLACEHOLDER_PATTERNS)


def _preserved_schools(profile: dict) -> list[str]:
    value = profile.get("resume_facts", {}).get("preserved_school", "")
    if isinstance(value, list):
        return [str(school).strip() for school in value if str(school).strip()]
    return [school.strip() for school in str(value).split(",") if school.strip()]


def _technical_skills_block(text: str) -> str:
    """Return only the real Technical Skills section, using heading boundaries."""
    start = re.search(
        r"(?im)^\s*(?:TECHNICAL\s+SKILLS|SKILLS|TECH\s+STACK|CORE\s+SKILLS|TECHNOLOGIES)\s*$",
        text,
    )
    if not start:
        return ""
    remainder = text[start.end():]
    end = re.search(
        r"(?im)^\s*(?:TARGETED\s+TECHNICAL\s+EXPOSURE|(?:PROFESSIONAL\s+|WORK\s+)?EXPERIENCE|PROJECTS|EDUCATION(?:\s*&\s*CERTIFICATIONS)?)\s*$",
        remainder,
    )
    return remainder[:end.start()] if end else remainder


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    for phrase, replacement in {
        "scalable solutions": "maintainable systems",
        "proven track record": "experience",
        "seamless": "reliable",
        "robust": "reliable",
    }.items():
        text = re.sub(r"\b" + re.escape(phrase) + r"\b", replacement, text,
                      flags=re.IGNORECASE)
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def validate_json_fields(data: dict, profile: dict, mode: str = "normal",
                         original_text: str = "") -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", "lenient", or "creative".
                 strict  → banned words are errors (trigger retries)
                 normal  → banned words are warnings (no retry)
                 lenient → banned words ignored entirely

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Projects are optional because many valid resumes (especially uploaded by
    # first-time users) contain only work experience and education. Requiring
    # a non-empty project list made every tailored resume fall back to the
    # master for those users.
    for key in ("title", "summary", "skills", "experience", "education"):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    if "projects" not in data:
        errors.append("Missing required field: projects")
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # Collect all text for bulk checks
    all_text_parts: list[str] = [data["summary"]]

    # Skills: every item must be explicitly supported by the master resume.
    if isinstance(data["skills"], dict):
        for skill in _unsupported_skills(
            data["skills"], profile, original_text=original_text,
        ):
            message = f"Unsupported skill (not in master resume): '{skill}'"
            (warnings if mode == "creative" else errors).append(message)
    else:
        errors.append("Skills must be a categorized JSON object")

    # Experience: preserved companies must be present (always enforced)
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        seen_companies: set[str] = set()
        for company in preserved_companies:
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            if not has_company:
                errors.append(f"Company '{company}' missing from experience")
        for entry in data["experience"]:
            header_lower = str(entry.get("header", "")).casefold()
            matches = [c for c in preserved_companies if c.casefold() in header_lower]
            if len(matches) != 1:
                errors.append(
                    f"Experience header must identify exactly one preserved company: "
                    f"'{entry.get('header', '')}'"
                )
            elif matches[0].casefold() in seen_companies:
                errors.append(f"Duplicate experience company: '{matches[0]}'")
            else:
                seen_companies.add(matches[0].casefold())
            if not entry.get("bullets"):
                errors.append(f"Experience entry has no bullets: '{entry.get('header', '')}'")
            if _contains_placeholder(entry.get("header", "")) or _contains_placeholder(
                entry.get("subtitle", "")
            ):
                errors.append("Experience contains a placeholder header or date")
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Projects: collect bullets
    if isinstance(data["projects"], list):
        preserved_projects = resume_facts.get("preserved_projects", [])
        for project in preserved_projects:
            if not any(
                project.casefold() in str(entry.get("header", "")).casefold()
                for entry in data["projects"]
            ):
                errors.append(f"Preserved project missing: '{project}'")
        for entry in data["projects"]:
            header_lower = str(entry.get("header", "")).casefold()
            if preserved_projects and not any(
                project.casefold() in header_lower for project in preserved_projects
            ):
                errors.append(f"Unknown or renamed project: '{entry.get('header', '')}'")
            if not entry.get("bullets"):
                errors.append(f"Project entry has no bullets: '{entry.get('header', '')}'")
            if _contains_placeholder(entry.get("header", "")) or _contains_placeholder(
                entry.get("subtitle", "")
            ):
                errors.append("Project contains a placeholder header or date")
            for b in entry.get("bullets", []):
                all_text_parts.append(b)

    # Education: preserved school must be present (always enforced)
    edu = str(data.get("education", ""))
    for school in _preserved_schools(profile):
        if school.casefold() not in edu.casefold():
            errors.append(f"Education '{school}' missing")
    if _contains_placeholder(data.get("education", "")):
        errors.append("Education contains placeholder text")

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    if mode == "creative":
        structural_markers = (
            "Missing required field:", "Skills must be", "has no bullets",
            "placeholder", "LLM self-talk",
        )
        factual = [
            error for error in errors
            if not any(marker.casefold() in error.casefold() for marker in structural_markers)
        ]
        errors = [error for error in errors if error not in factual]
        warnings.extend(factual)
    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def validate_tailored_resume(text: str, profile: dict, original_text: str = "",
                             mode: str = "normal") -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching)
    section_variants: dict[str, list[str]] = {
        "SUMMARY": ["summary", "professional summary", "profile"],
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "PROJECTS": ["projects", "personal projects", "key projects", "selected projects"],
        "EDUCATION": ["education", "academic background"],
    }
    projects_expected = bool(resume_facts.get("preserved_projects")) or any(
        re.search(rf"(?im)^\s*{re.escape(variant)}\s*:?[ \t]*$", original_text)
        for variant in section_variants["PROJECTS"]
    )
    for section, variants in section_variants.items():
        if section == "PROJECTS" and not projects_expected:
            continue
        if not any(
            re.search(rf"(?im)^\s*{re.escape(v)}\s*:?[ \t]*$", text)
            for v in variants
        ):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    full_name = personal.get("full_name", "")
    if full_name and full_name.lower() not in text_lower:
        warnings.append(f"Name '{full_name}' missing -- will be injected")

    # 3. Check companies preserved
    for company in resume_facts.get("preserved_companies", []):
        if company.lower() not in text_lower:
            errors.append(f"Company '{company}' missing -- cannot remove real experience")

    # 4. Check projects preserved
    for project in resume_facts.get("preserved_projects", []):
        if project.lower() not in text_lower:
            errors.append(f"Project '{project}' missing -- cannot remove a preserved project")

    # 5. Check immutable experience and education metadata.
    for school in _preserved_schools(profile):
        if school.casefold() not in text.casefold():
            errors.append(f"Education '{school}' missing")
    for metric in resume_facts.get("real_metrics", []):
        if str(metric).casefold() not in text.casefold():
            errors.append(f"Immutable metric missing or changed: '{metric}'")
    for entry in resume_facts.get("experience_entries", []):
        for field in ("header", "subtitle"):
            value = str(entry.get(field, "")).strip()
            if value and value.casefold() not in text.casefold():
                errors.append(f"Immutable experience {field} missing: '{value}'")
    for education in resume_facts.get("education_entries", []):
        if str(education).casefold() not in text.casefold():
            errors.append(f"Immutable education entry missing: '{education}'")

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    # 7. Every listed skill must be supported by the master resume.
    skills_block = _technical_skills_block(text)
    if skills_block:
        skill_values: dict[str, str] = {}
        for line in skills_block.splitlines():
            if ":" in line:
                category, value = line.split(":", 1)
                skill_values[category] = value
        for skill in _unsupported_skills(
            skill_values, profile, original_text=original_text,
        ):
            message = f"Unsupported skill in Technical Skills: '{skill}'"
            (warnings if mode == "creative" else errors).append(message)

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = _normalized_fact_text(original_text)
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                warnings.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # Placeholder and malformed-contact safety checks are always blocking.
    if any(_contains_placeholder(line) for line in text.splitlines()):
        errors.append("Contains placeholder text (for example Tech, Dates, TBD, or N/A)")
    if "\\http://" in text_lower or "\\https://" in text_lower:
        errors.append("Contains malformed URL beginning with a backslash")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned and mode != "lenient":
        message = f"Banned words: {', '.join(found_banned[:5])}"
        if mode == "strict":
            errors.append(message)
        else:
            warnings.append(message)

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = len(re.findall(
            rf"(?im)^\s*(?:professional\s+|work\s+|selected\s+|key\s+|personal\s+)?"
            rf"{re.escape(section_name)}\s*:?[ \t]*$",
            text,
        ))
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    if mode == "creative":
        structural_markers = (
            "Missing required section:", "placeholder", "malformed URL",
            "LLM self-talk", "appears ", "em dash", "en dash",
        )
        factual = [
            error for error in errors
            if not any(marker.casefold() in error.casefold() for marker in structural_markers)
        ]
        errors = [error for error in errors if error not in factual]
        warnings.extend(factual)
    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(text: str, mode: str = "normal") -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", "lenient", or "creative".
              strict  → banned words are errors (trigger retries); word limit enforced
              normal  → banned words are warnings; word limit is soft (+25 words)
              lenient → banned words ignored; word count not checked

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Word count
    words = len(text.split())
    if mode == "strict" and words > 250:
        errors.append(f"Too long ({words} words). Max 250.")
    elif mode == "normal" and words > 275:
        warnings.append(f"Long ({words} words). Target 250.")
    # lenient: no word count check

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" — always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
