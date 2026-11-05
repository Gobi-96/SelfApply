"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import hashlib
import logging
import re
import time
import copy
from datetime import datetime, timezone
from pathlib import Path

from selfapply import config
from selfapply.config import RESUME_PATH, TAILORED_DIR, load_profile
from selfapply.database import get_connection, get_jobs_by_stage
from selfapply.llm import get_client
from selfapply.scoring.ats_matcher import (
    analyze_ats_match,
    extract_job_keywords,
)
from selfapply.scoring.skill_similarity import (
    find_transferable_skills,
    similarity_threshold,
)
from selfapply.scoring.validator import (
    BANNED_WORDS,
    FABRICATION_WATCHLIST,
    build_allowed_skills,
    remove_unsupported_skills,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up

_EXPOSURE_USE_CASES = {
    "kafka": "event-driven messaging and asynchronous service workflows",
    "apache kafka": "event-driven messaging and asynchronous service workflows",
    "redis": "caching and low-latency state management",
    "kubernetes": "container orchestration, deployment, and service operations",
    "terraform": "repeatable infrastructure provisioning and environment management",
    "rust": "memory-safe systems programming and performance-sensitive services",
    "golang": "concurrent backend services and infrastructure tooling",
    "go": "concurrent backend services and infrastructure tooling",
    "spark": "distributed data processing and large-scale transformation",
    "airflow": "scheduled data pipelines and workflow orchestration",
    "graphql": "typed API design and client-driven data access",
    "grpc": "typed service-to-service communication",
    "mongodb": "document-oriented data modeling and application persistence",
    "dynamodb": "managed key-value data modeling and low-latency persistence",
}


def _contains_term(term: str, text: str) -> bool:
    return bool(re.search(
        r"(?<![\w+#])" + re.escape(str(term).strip()) + r"(?![\w+#])",
        text, re.IGNORECASE,
    ))


def _intensity_details(value: float) -> dict:
    """Describe how much of the resume may be reworked at this setting."""
    if value >= 0.95:
        return {
            "label": "Maximum whole-resume tailoring",
            "sections": ["Title", "Summary", "Technical Skills", "Projects", "Work Experience"],
            "instruction": (
                "Comprehensively rewrite every relevant section and bullet. Integrate every "
                "factually supported important JD term in natural context, while preserving all "
                "immutable history. Use confirmed experience skills in relevant work or project "
                "bullets only when the user explicitly attested to real prior use."
            ),
        }
    if value >= 0.75:
        return {
            "label": "Strong whole-resume tailoring",
            "sections": ["Title", "Summary", "Technical Skills", "Projects", "Work Experience"],
            "instruction": (
                "Rewrite the summary, skills ordering, relevant projects, and most experience "
                "bullets aggressively around the JD, without changing the underlying facts."
            ),
        }
    if value >= 0.45:
        return {
            "label": "Moderate whole-resume tailoring",
            "sections": ["Title", "Summary", "Technical Skills", "Most relevant projects and bullets"],
            "instruction": (
                "Make moderate changes throughout the resume: rewrite the summary, reorder skills, "
                "and reframe only the most relevant project and experience bullets."
            ),
        }
    return {
        "label": "Conservative tailoring",
        "sections": ["Title", "Summary", "Technical Skills"],
        "instruction": (
            "Keep most wording intact. Adjust the title and summary, reorder supported skills, and "
            "lightly reframe only the strongest matching bullets."
        ),
    }


def build_tailoring_plan(
    resume_text: str, job_description: str, profile: dict,
    target_aggressiveness: float = 0.5, company_name: str = "",
    skill_expansion_enabled: bool = True,
) -> dict:
    """Classify JD skills before generation so unsupported claims require consent."""
    value = max(0.0, min(1.0, float(target_aggressiveness)))
    extracted = extract_job_keywords(job_description, company_name=company_name)
    transfer = find_transferable_skills(
        resume_text, job_description,
        minimum_similarity=similarity_threshold(value),
    ) if skill_expansion_enabled and value >= 0.5 else []
    supported_keys = {
        skill.casefold() for skill in extracted["skills"] if _contains_term(skill, resume_text)
    }
    supported = [skill for skill in extracted["skills"] if skill.casefold() in supported_keys]
    profile_confirmed = []
    profile_confirmed_keys: set[str] = set()
    transfer_names = {item["skill"].casefold() for item in transfer}
    ranked_skills = list(dict.fromkeys(
        extracted["required_skills"] + extracted["preferred_skills"]
        + extracted["skills"] + extracted["keywords"]
    ))
    unsupported = [
        skill for skill in ranked_skills
        if skill.casefold() not in supported_keys | profile_confirmed_keys | transfer_names
    ]
    exposure_count = 0
    creative_expansion = bool(
        profile.get("skills_boundary", {}).get("allow_unverified_claims", False)
    )
    exposure_threshold = 0.5 if creative_expansion else 0.9
    if skill_expansion_enabled and value >= exposure_threshold and unsupported:
        # Keep transparent learning/exposure useful without turning the resume
        # into a list of every unsupported buzzword in the job description.
        if creative_expansion:
            exposure_count = 1 if value < 0.75 else (2 if value < 0.95 else 3)
            exposure_count = min(exposure_count, len(unsupported))
        else:
            scale = min(1.0, max(0.0, (value - 0.9) / 0.1))
            exposure_count = min(3, max(1, round(len(unsupported) * scale)))
    exposure = unsupported[:exposure_count]
    return {
        "target_aggressiveness": value,
        "target_percent": round(value * 100),
        "intensity": _intensity_details(value),
        "supported_skills": supported,
        "confirmed_profile_skills": profile_confirmed,
        "transferable_skills": transfer,
        "unsupported_skills": unsupported,
        "planned_exposure_skills": exposure,
        "important_keywords": extracted["keywords"],
        "warning": (
            "Aggressive tailoring will introduce skills not present in the master resume. "
            "Unchecked skills remain clearly labeled as exposure or learning and are never "
            "presented as completed employer work."
            if exposure else ""
        ),
    }


def artifact_prefix(job: dict) -> str:
    """Return a readable, collision-resistant artifact prefix for one job."""
    safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
    digest = hashlib.sha256(str(job["url"]).encode("utf-8")).hexdigest()[:10]
    return f"{safe_site}_{safe_title}_{digest}"


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(
    profile: dict, resume_text: str = "", target_aggressiveness: float = 0.5,
    allow_unverified_claims: bool = False,
) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    allowed_skills = build_allowed_skills(profile, resume_text)
    for category, items in boundary.items():
        if category in {
            "confirmed_additional_skills", "transferable_familiarity",
            "target_learning_skills", "confirmed_experience_skills",
        }:
            continue
        if isinstance(items, list) and items:
            evidenced_items = [item for item in items if item.casefold() in allowed_skills]
            if not evidenced_items:
                continue
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(evidenced_items)}")
    skills_block = "\n".join(skills_lines)
    confirmed_note = ""
    transferable_skills = boundary.get("transferable_familiarity", [])
    if transferable_skills:
        confirmed_note += (
            "\nTRANSFERABLE FAMILIARITY: " + ", ".join(transferable_skills) + ".\n"
            "Integrate these directly into the most relevant existing Technical Skills "
            "category, with no separate category or parenthetical label. Where useful, connect "
            "them to an existing project or experience "
            "bullet using qualified foundation/comparison language. Never claim the person "
            "actually deployed, built, operated, mastered, or used these in production.\n"
        )
    target_learning_skills = boundary.get("target_learning_skills", [])
    if target_learning_skills:
        if allow_unverified_claims:
            confirmed_note += (
                "\nUSER-AUTHORIZED JD TECHNOLOGIES: "
                + ", ".join(target_learning_skills) + ".\n"
                "Integrate these into the closest existing Technical Skills categories. Where "
                "the target role benefits, connect them naturally to a relevant existing project "
                "or experience bullet. Never create a separate learning, exposure, related "
                "technologies, or target-skills section.\n"
            )
        else:
            confirmed_note += (
                "\nTARGET SKILLS - LEARNING: " + ", ".join(target_learning_skills) + ".\n"
                "These are unconfirmed JD skills. Do not put them in Summary, Technical Skills, "
                "employer bullets, or completed project claims. Code will place them in a clearly "
                "labeled Targeted Technical Exposure section with non-experience wording.\n"
            )
    experience_skills = boundary.get("confirmed_experience_skills", [])
    if experience_skills:
        confirmed_note += (
            "\nUSER-CONFIRMED REAL EXPERIENCE SKILLS: " + ", ".join(experience_skills) + ".\n"
            "The user explicitly attested to real prior use. Integrate each naturally into the "
            "most relevant existing employer or preserved project bullet, without inventing an "
            "outcome, metric, duration, certification, employer, or new project.\n"
        )
    intensity = _intensity_details(target_aggressiveness)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])
    experience_entries = resume_facts.get("experience_entries", [])
    education_entries = resume_facts.get("education_entries", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    projects_str = ", ".join(projects) if projects else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    experience_json = json.dumps(experience_entries, ensure_ascii=False)
    education_json = json.dumps(education_entries, ensure_ascii=False)
    projects_json = json.dumps(projects, ensure_ascii=False)
    output_skeleton = {
        "title": "",
        "summary": "",
        "skills": {},
        "experience": [
            {
                "header": entry.get("header", ""),
                "subtitle": entry.get("subtitle", ""),
                "bullets": [],
            }
            for entry in experience_entries
        ],
        "projects": [
            {"header": project, "subtitle": "", "bullets": []}
            for project in projects
        ],
        "education": "\n".join(str(entry) for entry in education_entries),
    }
    output_skeleton_json = json.dumps(output_skeleton, ensure_ascii=False)

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    creative_override = ""
    if allow_unverified_claims:
        creative_override = """

## USER-AUTHORIZED CREATIVE TAILORING
The user explicitly asked for a speculative, JD-aligned resume. You may add
plausible job-description skills, workflows, and responsibilities to the
Summary, Technical Skills, Projects, and relevant Experience bullets. Make the
result read naturally rather than placing everything in a learning section.
Still preserve every supplied employer, title, date, school, contact detail,
and numerical metric exactly; never add a new employer, degree, certification,
date, or numerical achievement. Unverified additions will be disclosed to the
user as warnings after generation. This section overrides only the factual-skill
and factual-responsibility restrictions above, not the immutable-history rules.
"""

    return f"""You are a senior technical recruiter rewriting a resume to get this person an interview.

Take the base resume and job description. Return a tailored resume as a JSON object.

## RECRUITER SCAN (6 seconds):
1. Title -- matches what they're hiring?
2. Summary -- 2 sentences proving you've done this work
3. First 3 bullets of most recent role -- verbs and outcomes match?
4. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}
{confirmed_note}

Every unqualified skill must come from the boundary above and appear in the base resume.
Only the explicitly listed adjacent or exposure skills may be added with their required
truthful qualification. Do not turn an inferred or job-description-only skill into a
production-use claim.

## TAILORING RULES:

INTENSITY: {intensity['label']} ({round(target_aggressiveness * 100)}%).
SECTIONS IN SCOPE: {', '.join(intensity['sections'])}.
{intensity['instruction']}

TITLE: Match the target role. Keep seniority (Senior/Lead/Staff). Drop company suffixes and team names.

SUMMARY: Rewrite according to the selected intensity. Lead with supported skills that matter most. Never imply experience with an unconfirmed exposure skill.

SKILLS: Reorder each category so the job's must-haves appear first.

EXPERIENCE: Reframe the number of bullets appropriate to the selected intensity. Same real work, different angle. At high intensity, reword every relevant bullet.

PROJECTS: Keep every preserved project with its exact name. Reorder by relevance.
The exact project names are: {projects_json}

BULLETS: Strong verb + what you built + quantified impact. Vary verbs (Built, Designed, Implemented, Reduced, Automated, Deployed, Operated, Optimized). Most relevant first. Max 4 per section.

## VOICE:
- Write like a real engineer. Short, direct.
- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Copy each experience header and subtitle exactly from this immutable JSON: {experience_json}
- Copy every education/certification line exactly from this immutable JSON: {education_json}
- Preserve every non-empty string already present in the output skeleton exactly.
- Fill every experience and project bullets array with at least one factual bullet.
- Include every immutable metric verbatim in the summary or a relevant bullet.
- Never emit placeholders such as "Tech", "Dates", "TBD", "N/A", or "placeholder"
- Do NOT change real numbers ({metrics_str})
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}
- Target 1-2 ATS-readable pages; never delete preserved facts just to shorten it.
{creative_override}

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{output_skeleton_json}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    transferable = boundary.get("transferable_familiarity", [])
    transferable_str = ", ".join(transferable) if transferable else "N/A"
    target_learning = boundary.get("target_learning_skills", [])
    target_learning_str = ", ".join(target_learning) if target_learning else "N/A"
    confirmed_experience = boundary.get("confirmed_experience_skills", [])
    confirmed_experience_str = ", ".join(confirmed_experience) if confirmed_experience else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch LIES, not style changes.

You must answer with EXACTLY this format:
VERDICT: PASS or FAIL
SEVERITY: NONE, ADVISORY, or HARD
ISSUES: (list any problems, or "none")

Use HARD only for a clear unsupported material claim: invented work, employer,
title, project, certification, metric, duration, or a tool presented as real work.
Use ADVISORY only when the concern could reasonably be a rephrasing or extraction
ambiguity. A PASS must use SEVERITY: NONE. When uncertain between HARD and
ADVISORY, use HARD so an application cannot silently ship a possible fabrication.

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Change the title to match the target role
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## ALREADY VERIFIED DETERMINISTICALLY (do not judge or fail these):
- Technical skills. The verified skills are: {skills_str}
- The following adjacent skills may appear directly in the closest Technical Skills category
  or in an existing project/experience bullet only with explicit foundation,
  comparison, or transferable-context wording: {transferable_str}. Their qualified
  presence is not a claim of production use and must not be failed.
- The following unrelated JD terms may appear only in the explicitly labeled
  "Targeted Technical Exposure" section: {target_learning_str}. Their presence
  there is not past experience. Fail if any appears in a summary, employer or
  completed-project bullet, achievement, proficiency, metric, or duration claim.
- The user explicitly confirmed real prior use of these skills: {confirmed_experience_str}.
  They may appear in a relevant existing employer or project bullet. Still fail any
  invented outcome, metric, duration, employer, certification, or new project.
- Companies, roles, dates, locations, project names, education, certifications,
  metrics, contact URLs, placeholders, and required sections.

## WHAT IS FABRICATION (FAIL for this):
Inventing work or achievements in an experience/project bullet that have no basis
in any original bullet. Judge semantic work claims only.

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Summarizing an existing duration such as "11+ years of experience building software"
  as "11+ years of software engineering experience" when the number is unchanged
  and the original work clearly describes software engineering
- Moving an existing fact into a Career Highlights section
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## ZERO-STRETCH RULE:
Any skill, tool, framework, project, employer, credential, date, location, or metric
that is not supported by the original resume is a failure, except for the explicitly
listed adjacent items above when clearly qualified as working familiarity or contextual
foundation, the Targeted Technical Exposure items when they remain in that section, or
user-confirmed real experience skills when used without invented surrounding facts.
Do not fail for style or reordering."""


# ── JSON Extraction ───────────────────────────────────────────────────────

_GENERIC_DURATION_WORDS = {
    "a", "an", "and", "application", "applications", "building", "development",
    "developer", "engineering", "experience", "in", "more", "of", "over",
    "production", "professional", "software", "than", "year", "years", "yr", "yrs",
}


def _is_grounded_duration_summary(claim: str, original_text: str) -> bool:
    """Accept only an unchanged duration restated as generic software experience."""
    duration = re.search(r"(?i)\b(\d+(?:\.\d+)?\s*\+?)\s*(?:years?|yrs?)\b", claim)
    if not duration:
        return False
    number = re.sub(r"\s+", "", duration.group(1))
    plus = r"\s*\+" if number.endswith("+") else ""
    original_duration = re.search(
        rf"(?i)\b{re.escape(number.rstrip('+'))}{plus}\s*(?:years?|yrs?)\b",
        original_text,
    )
    if not original_duration:
        return False
    claim_words = set(re.findall(r"[a-z]+", claim.casefold()))
    if claim_words - _GENERIC_DURATION_WORDS:
        return False
    start = max(0, original_duration.start() - 180)
    end = min(len(original_text), original_duration.end() + 240)
    context = original_text[start:end].casefold()
    return bool(re.search(
        r"\b(?:software|engineer|engineering|developer|development|"
        r"application|applications|programming|system|systems)\b",
        context,
    ))


def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def _append_verified_skills(tailored_text: str, skills: list[str]) -> str:
    """Deterministically restore job-relevant skills the LLM's skills section dropped.

    Only ever called with terms already verified present in the master resume
    (or confirmed/transferable skills) -- a formatting safety net, not new
    factual content, so it never needs its own validation pass.
    """
    return _merge_skills_into_technical_categories(tailored_text, skills)


_LANGUAGE_SKILLS = {
    "c", "c#", "c++", "go", "golang", "java", "javascript", "kotlin",
    "python", "ruby", "rust", "scala", "swift", "typescript",
}
_DATA_SKILLS = {
    "airflow", "apache kafka", "dynamodb", "elasticsearch", "kafka",
    "mongodb", "mysql", "nosql", "postgresql", "redis", "snowflake",
    "spark", "sql", "sqlite", "vector databases",
}
_FRAMEWORK_SKILLS = {
    "angular", "django", "fastapi", "flask", "graphql", "jest", "next.js",
    "node.js", "playwright", "pytest", "react", "react native", "selenium",
    "spring", "svelte", "vue", "vue.js",
}
_PLATFORM_SKILLS = {
    "ansible", "aws", "azure", "datadog", "docker", "gcp", "git",
    "github actions", "gitlab", "grafana", "jenkins", "kubernetes", "linux",
    "observability", "prometheus", "sentry", "terraform",
}


def _merge_skills_into_technical_categories(text: str, skills: list[str]) -> str:
    """Merge inferred terms into the closest existing Technical Skills row."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(
        r"^\s*(?:technical\s+)?skills\s*$", line, re.IGNORECASE,
    )), None)
    if start is None:
        lines.extend(["", "TECHNICAL SKILLS"])
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if re.match(
        r"^\s*(?:professional\s+)?experience\s*$", lines[i], re.IGNORECASE,
    )), len(lines))

    def target_words(skill: str) -> tuple[str, ...]:
        key = skill.casefold()
        if key in _LANGUAGE_SKILLS:
            return ("program", "language")
        if key in _DATA_SKILLS:
            return ("data", "database", "storage", "backend")
        if key in _FRAMEWORK_SKILLS:
            return ("framework", "librar", "frontend", "backend", "web")
        return ("cloud", "devops", "tool", "platform", "system", "infrastructure")

    for skill in skills:
        block = "\n".join(lines[start:end])
        if _contains_term(skill, block):
            continue
        category_rows = [i for i in range(start + 1, end) if ":" in lines[i]]
        preferred = next((
            i for i in category_rows
            if any(word in lines[i].split(":", 1)[0].casefold() for word in target_words(skill))
        ), None)
        key = skill.casefold()
        known_technical = key in (
            _LANGUAGE_SKILLS | _DATA_SKILLS | _FRAMEWORK_SKILLS | _PLATFORM_SKILLS
        )
        if preferred is None and category_rows and not known_technical:
            # Domain-neutral fallback: keep an unfamiliar competency inside
            # the resume's own first skills category. Never label sales,
            # healthcare, finance, or operations terms as "Cloud & DevOps".
            preferred = category_rows[0]
        if preferred is None:
            if key in _LANGUAGE_SKILLS:
                label = "Programming"
            elif key in _DATA_SKILLS:
                label = "Databases & Data"
            elif key in _FRAMEWORK_SKILLS:
                label = "Frameworks & Libraries"
            elif key in _PLATFORM_SKILLS:
                label = "Cloud & DevOps"
            else:
                label = "Core Competencies"
            lines.insert(start + 1, f"{label}: {skill}")
            end += 1
        else:
            lines[preferred] = lines[preferred].rstrip() + ", " + skill
    return "\n".join(lines)


def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    fallback_title = (
        profile.get("experience", {}).get("target_role")
        or profile.get("experience", {}).get("current_title")
        or "Professional"
    )
    lines.append(sanitize_text(data.get("title") or fallback_title))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    # Summary
    lines.append("SUMMARY")
    summary = sanitize_text(data["summary"])
    metrics = [str(item) for item in resume_facts.get("real_metrics", [])]
    years_metric = next((item for item in metrics if "years" in item.casefold()), "")
    years_digits_match = re.search(r"(\d+)\+?", years_metric) if years_metric else None
    if years_digits_match:
        digits = years_digits_match.group(1)
        # Strip any inflating prefix in front of the true figure ("over 11
        # years," "more than 11+ years," etc. all overstate a flat "11+ years"
        # fact and would fail the judge's fabrication check) -- the "+" is
        # optional since the LLM doesn't always reproduce it.
        summary = re.sub(
            rf"\b(?:over|more than|upwards of|exceeding)\s+({re.escape(digits)}\+?\s+years)\b",
            r"\1", summary, flags=re.IGNORECASE,
        )
        if years_metric.casefold() not in summary.casefold():
            years_pattern = re.compile(
                rf"\b(?:more than\s+)?{re.escape(digits)}\+?\s+years"
                r"(?:\s+of\s+(?:(?:software engineering|professional)\s+)?experience)?",
                re.IGNORECASE,
            )
            if years_pattern.search(summary):
                summary = years_pattern.sub(years_metric, summary, count=1)
            else:
                summary = f"{fallback_title} with {years_metric}. {summary}"
    lines.append(summary)
    lines.append("")

    # Preserve verified metrics without tying the general renderer to a
    # particular employer. The LLM may place them in context; any metric it
    # omits is surfaced verbatim as a career highlight.
    generated_text = json.dumps(data, ensure_ascii=False).casefold()
    missing_metrics = [
        str(metric).strip() for metric in metrics
        if str(metric).strip() and str(metric).casefold() not in generated_text
    ]
    if missing_metrics:
        lines.append("CAREER HIGHLIGHTS")
        lines.extend(f"- {sanitize_text(metric)}" for metric in missing_metrics)
        lines.append("")

    # Technical Skills
    lines.append("TECHNICAL SKILLS")
    if isinstance(data["skills"], dict):
        for cat, val in data["skills"].items():
            rendered = ", ".join(str(item) for item in val) if isinstance(val, list) else str(val)
            lines.append(f"{cat}: {sanitize_text(rendered)}")
    # Do not rely on the LLM to remember every approved adjacent skill. Keep
    # missing terms inside Technical Skills with an honest proficiency marker.
    transferable = profile.get("skills_boundary", {}).get(
        "transferable_familiarity", []
    )
    # Inspect only the skills payload. A term mentioned in the summary must
    # not trick the deterministic merger into thinking Technical Skills
    # already contains it.
    rendered_skills = json.dumps(data["skills"], ensure_ascii=False).casefold()
    missing_transferable = [
        sanitize_text(str(skill)) for skill in transferable
        if str(skill).strip() and str(skill).casefold() not in rendered_skills
    ]
    if missing_transferable:
        pass  # merged into the closest existing category after assembly
    boundary = profile.get("skills_boundary", {})
    target_learning = boundary.get(
        "target_learning_skills", []
    )
    missing_learning = [
        sanitize_text(str(skill)) for skill in target_learning
        if str(skill).strip() and str(skill).casefold() not in rendered_skills
    ]
    lines.append("")

    creative_skill_placement = bool(boundary.get("allow_unverified_claims", False))
    if missing_learning and not creative_skill_placement:
        lines.append("TARGETED TECHNICAL EXPOSURE")
        for skill in missing_learning:
            use_case = _EXPOSURE_USE_CASES.get(
                skill.casefold(),
                "the responsibilities and technical use cases described for this target role",
            )
            lines.append(
                f"- {skill}: Active learning and exposure focused on {use_case}; "
                "not presented as prior production experience."
            )
        lines.append("")

    immutable_experience = resume_facts.get("experience_entries", [])

    # Experience: headers, employers, dates, and locations are code-injected facts.
    lines.append("EXPERIENCE")
    for index, entry in enumerate(data.get("experience", [])):
        generated_header = str(entry.get("header", ""))
        # The prompt gives the LLM an ordered immutable skeleton. Always restore
        # the header by that same position: company-name matching is ambiguous
        # for repeated clients and once caused an empty legacy company to turn
        # every employer into the first employer in the resume.
        immutable = immutable_experience[index] if index < len(immutable_experience) else None
        lines.append(sanitize_text(
            immutable.get("header", "") if immutable else generated_header,
        ))
        subtitle = immutable.get("subtitle", "") if immutable else entry.get("subtitle", "")
        if subtitle:
            lines.append(sanitize_text(str(subtitle)))
        bullets = list(entry.get("bullets", []))
        for b in bullets:
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Projects
    lines.append("PROJECTS")
    preserved_projects = resume_facts.get("preserved_projects", [])
    for entry in data.get("projects", []):
        generated_header = str(entry.get("header", ""))
        exact_project = next((
            project for project in preserved_projects
            if project.casefold() in generated_header.casefold()
        ), generated_header)
        lines.append(sanitize_text(exact_project))
        for b in entry.get("bullets", []):
            lines.append(f"- {sanitize_text(b)}")
        lines.append("")

    # Education
    lines.append("EDUCATION")
    education_entries = resume_facts.get("education_entries", [])
    if education_entries:
        lines.extend(sanitize_text(str(entry)) for entry in education_entries)
    else:
        lines.append(sanitize_text(str(data.get("education", ""))))

    skills_to_merge = list(missing_transferable)
    if creative_skill_placement:
        skills_to_merge.extend(missing_learning)
    return _merge_skills_into_technical_categories(
        "\n".join(lines), skills_to_merge,
    )


# ── LLM Judge ────────────────────────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        A verdict with severity, issues, raw output, and excluded sections.
    """
    judge_prompt = _build_judge_prompt(profile)

    judge_text = re.sub(
        r"(?ims)^\s*TECHNICAL\s+SKILLS\s*$.*?"
        r"(?=^\s*(?:PROFESSIONAL\s+|WORK\s+)?EXPERIENCE\s*$|\Z)",
        "",
        tailored_text,
    )
    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"ORIGINAL RESUME:\n{original_text.replace('', '+').replace('', '(').replace('', ')').replace('', '-')}\n\n---\n\n"
            f"TAILORED RESUME:\n{judge_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    response = client.chat(messages, max_tokens=512, temperature=0.1)

    verdict_match = re.search(r"(?im)^\s*VERDICT\s*:\s*(PASS|FAIL)\s*$", response)
    passed = bool(verdict_match and verdict_match.group(1).upper() == "PASS")
    severity_match = re.search(
        r"(?im)^\s*SEVERITY\s*:\s*(NONE|ADVISORY|HARD)\s*$", response,
    )
    severity = severity_match.group(1).upper() if severity_match else (
        "NONE" if passed else "HARD"
    )
    if passed and severity != "NONE":
        # Contradictory judge output is not a trustworthy approval.
        passed = False
        severity = "HARD"
    elif not passed and severity == "NONE":
        severity = "HARD"
    issues = "none"
    if "ISSUES:" in response.upper():
        issues_idx = response.upper().index("ISSUES:")
        issues = response[issues_idx + 7:].strip()
    severity_override = ""
    if not passed and severity == "HARD":
        # Ground the narrow false-positive case where the judge says a quoted
        # technology itself is absent even though that exact term is present
        # in the master resume. This does not pardon invented surrounding work.
        quoted = [
            value.strip() for value in re.findall(r'["“]([^"”]+)["”]', issues)
            if value.strip()
        ]
        grounded_text = original_text + "\n" + "\n".join(
            str(value) for value in profile.get("resume_facts", {}).get("real_metrics", [])
        )
        grounded_lower = re.sub(r"[^\w+]+", " ", grounded_text.casefold()).strip()
        unsupported_wording = re.search(
            r"(?i)(?:not supported|without (?:any )?(?:prior )?verification|"
            r"not (?:present|found)|absent from)",
            issues,
        )
        duration_summary = next(
            (value for value in quoted if _is_grounded_duration_summary(value, original_text)),
            None,
        )
        normalized_original = re.sub(
            r"[^\w+]+", " ", original_text.casefold(),
        ).strip()
        duration_concern = unsupported_wording or re.search(
            r"(?i)(?:rephras|more specific|fabricat|alters? (?:the )?original context)",
            issues,
        )
        all_duration_quotes_grounded = bool(quoted) and all(
            _is_grounded_duration_summary(value, original_text)
            or re.sub(r"[^\w+]+", " ", value.casefold()).strip() in normalized_original
            for value in quoted
        )
        if duration_concern and duration_summary and all_duration_quotes_grounded:
            passed = True
            severity = "NONE"
            severity_override = (
                "Unchanged duration was restated as generic software experience."
            )
        elif (
            unsupported_wording and quoted
            and all(
                re.sub(r"[^\w+]+", " ", value.casefold()).strip() in grounded_lower
                for value in quoted
            )
        ):
            severity = "ADVISORY"
            severity_override = (
                "Quoted term is present verbatim in the master resume or preserved facts."
            )

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "severity": severity,
        "issues": issues,
        "raw": response,
        "severity_override": severity_override,
        "excluded_sections": ["TECHNICAL SKILLS", "TARGETED TECHNICAL EXPOSURE"],
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
    require_judge: bool = False, allow_transferable_skills: bool | None = None,
    target_aggressiveness: float | None = None,
    confirmed_experience_skills: list[str] | None = None,
    allow_unverified_claims: bool = False,
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", "lenient", or "creative".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped
                          creative -- new skills and responsibility claims are warnings,
                          while malformed output and identity/history structure remain checked
        allow_transferable_skills: Override for whether adjacent-skill
                          familiarity is auto-inferred (at a fixed 50%
                          family-similarity). Defaults to the profile's
                          skills_boundary.transferable_enabled checkbox when
                          not explicitly passed. Independent of any skills the
                          user manually listed in
                          skills_boundary.transferable_familiarity, which are
                          always honored regardless of this setting.
        target_aggressiveness: How hard the ATS-quality gate pushes for a
                          higher score, from 0.0 to 1.0. 1.0 = reach 90 (or
                          the highest truthfully achievable score, if lower).
                          0.0 = only require matching the master resume's
                          score, never regressing. Values between interpolate.
                          Defaults to the profile's
                          skills_boundary.target_aggressiveness, or 0.5
                          if the profile doesn't set one.

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    profile = copy.deepcopy(profile)
    profile_skills = profile.get("skills_boundary", {})
    if target_aggressiveness is None:
        saved_aggressiveness = profile_skills.get("target_aggressiveness")
        target_aggressiveness = saved_aggressiveness if saved_aggressiveness is not None else 0.5
    target_aggressiveness = max(0.0, min(1.0, target_aggressiveness))
    if allow_transferable_skills is None:
        allow_transferable_skills = bool(
            profile_skills.get(
                "skill_expansion_enabled",
                profile_skills.get("transferable_enabled", True),
            )
        )
    plan = build_tailoring_plan(
        resume_text, job.get("full_description") or "", profile,
        target_aggressiveness, company_name=str(job.get("site") or ""),
        skill_expansion_enabled=bool(allow_transferable_skills),
    )
    minimum_similarity = similarity_threshold(target_aggressiveness)
    # Adjacent skills are inferred from the master resume and JD. The removed
    # manual lists no longer affect automatic applications.
    transferable: list[dict] = []
    if allow_transferable_skills:
        transferable = find_transferable_skills(
            resume_text,
            job.get("full_description") or "",
            minimum_similarity=minimum_similarity,
        )
        auto_familiarity = [item["skill"] for item in transferable]
        profile.setdefault("skills_boundary", {})["transferable_familiarity"] = auto_familiarity
    requested_experience = {
        str(skill).casefold() for skill in (confirmed_experience_skills or [])
        if str(skill).strip()
    }
    unsupported_lookup = {
        str(skill).casefold(): str(skill) for skill in plan["unsupported_skills"]
    }
    confirmed_experience = [
        unsupported_lookup[key] for key in requested_experience if key in unsupported_lookup
    ]
    target_learning_skills = [
        skill for skill in plan["planned_exposure_skills"]
        if skill.casefold() not in {item.casefold() for item in confirmed_experience}
    ]
    profile.setdefault("skills_boundary", {})["target_learning_skills"] = target_learning_skills
    profile.setdefault("skills_boundary", {})["confirmed_experience_skills"] = confirmed_experience
    report: dict = {
        "attempts": 0, "validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
        "transferable_skills": transferable,
        "target_learning_skills": target_learning_skills,
        "confirmed_experience_skills": confirmed_experience,
        "tailoring_plan": plan,
        "introduced_skills": list(dict.fromkeys(
            [item["skill"] for item in transferable]
            + target_learning_skills + confirmed_experience
        )),
        "transferable_enabled": bool(allow_transferable_skills),
        "minimum_transferable_similarity": round(minimum_similarity * 100),
        "unverified_claims_allowed": bool(allow_unverified_claims),
    }
    avoid_notes: list[str] = []
    tailored = ""
    client = get_client()
    tailor_prompt_base = _build_tailor_prompt(
        profile, resume_text, target_aggressiveness,
        allow_unverified_claims=allow_unverified_claims,
    )
    boundary = profile.get("skills_boundary", {})
    confirmed_skills: list[str] = []
    confirmed_skills.extend(boundary.get("transferable_familiarity", []))
    confirmed_skills.extend(boundary.get("target_learning_skills", []))
    confirmed_skills.extend(boundary.get("confirmed_experience_skills", []))
    evidence_text = resume_text + (
        "\nCONFIRMED SKILLS\n" + ", ".join(confirmed_skills)
        if confirmed_skills else ""
    )
    company_name = str(job.get("site") or "")
    master_ats = analyze_ats_match(
        resume_text, job.get("full_description") or "", company_name=company_name,
    )
    supported_ats = analyze_ats_match(
        evidence_text, job.get("full_description") or "", company_name=company_name,
    )
    # Every JD term the master resume genuinely supports -- not just the
    # curated _SKILLS subset. Keyword-match is 55% of the ATS score, so a
    # generic term like "apis" or "infrastructure" losing its exact-phrase
    # match during rewording is just as damaging as losing a named skill.
    verified_keywords = list(supported_ats["matched_keywords"])
    report["master_ats_match"] = master_ats
    report["supported_ats_match"] = supported_ats
    best_candidate: dict = {"text": None, "report": None, "score": -1.0}

    def use_master_fallback(reason: str) -> tuple[str, dict]:
        fallback_text = resume_text
        transferable_names = [item["skill"] for item in transferable]
        if transferable_names:
            fallback_text = _merge_skills_into_technical_categories(
                fallback_text, transferable_names,
            )
        if target_learning_skills and "TARGETED TECHNICAL EXPOSURE" not in fallback_text:
            learning_lines = ["TARGETED TECHNICAL EXPOSURE"]
            for skill in target_learning_skills:
                use_case = _EXPOSURE_USE_CASES.get(
                    skill.casefold(),
                    "the responsibilities and technical use cases described for this target role",
                )
                learning_lines.append(
                    f"- {skill}: Active learning and exposure focused on {use_case}; "
                    "not presented as prior production experience."
                )
            learning_row = "\n".join(learning_lines)
            experience_heading = re.search(
                r"(?im)^\s*(?:PROFESSIONAL\s+)?EXPERIENCE\s*$", fallback_text,
            )
            if experience_heading:
                fallback_text = (
                    fallback_text[:experience_heading.start()].rstrip()
                    + "\n" + learning_row + "\n\n"
                    + fallback_text[experience_heading.start():].lstrip()
                )
            else:
                separator = (
                    "\n" if re.search(r"(?im)^\s*TECHNICAL SKILLS\s*$", fallback_text)
                    else "\n\nTECHNICAL SKILLS\n"
                )
                fallback_text = fallback_text.rstrip() + separator + learning_row
        fallback_ats = analyze_ats_match(
            fallback_text,
            job.get("full_description") or "",
            master_resume_text=evidence_text,
            company_name=company_name,
        )
        report["status"] = "fallback_master"
        report["fallback_to_master"] = True
        report["fallback_reason"] = reason
        report["ats_match"] = fallback_ats
        quality = report.setdefault("ats_quality", {})
        quality.update({
            "passed": True,
            # Reuse the target_aggressiveness-derived value already computed
            # by the loop if one exists (e.g. an early JSON-parse failure
            # means no attempt ever reached that computation).
            "target_score": quality.get("target_score", supported_ats["target_score"]),
            "target_aggressiveness": quality.get("target_aggressiveness", target_aggressiveness),
            "factual_ceiling_score": supported_ats["factual_ceiling_score"],
            "used_master_fallback": True,
        })
        return fallback_text, report

    def use_best_effort_or_master(reason: str) -> tuple[str, dict]:
        """Prefer the best factually-valid tailored attempt over a blind master copy.

        Only called after the ATS-quality gate (a soft scoring heuristic) is
        what exhausted retries -- every candidate here already passed
        validate_tailored_resume's factual/structural checks. A candidate is
        judge-reviewed here since it may not have reached the judge step on
        its own attempt.
        """
        if best_candidate["text"] is None:
            return use_master_fallback(reason)

        candidate_report = best_candidate["report"]
        if validation_mode != "lenient":
            judge = judge_tailored_resume(
                resume_text, best_candidate["text"], job.get("title", ""), profile,
            )
            candidate_report["judge"] = judge
            if not judge["passed"] and allow_unverified_claims:
                candidate_report["judge_advisory"] = judge["issues"]
                candidate_report["unverified_claim_warnings"] = [judge["issues"]]
            elif not judge["passed"] and (
                judge.get("severity") != "ADVISORY"
                or validation_mode == "strict"
                or require_judge
            ):
                return use_master_fallback(reason)
            if not judge["passed"] and not allow_unverified_claims:
                candidate_report["judge_advisory"] = judge["issues"]
        candidate_report["status"] = "approved"
        candidate_report["used_best_effort_attempt"] = True
        candidate_report["best_effort_reason"] = reason
        return best_candidate["text"], candidate_report

    if verified_keywords:
        tailor_prompt_base += (
            "\n\n## VERIFIED ATS TERMS\n"
            "These job-description terms already appear in the original resume. "
            "Every one is mandatory in the output. Preserve each term naturally and "
            "verbatim at least once; do not keyword-stuff: "
            + ", ".join(verified_keywords)
            + "."
        )

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
        ]

        # Immutable experience/project/education content makes the output
        # longer than the previous one-page draft. Avoid truncating the final
        # project or education entries mid-JSON.
        raw = client.chat(messages, max_tokens=3500, temperature=0.2)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError:
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        cleaned_skills, removed_skills = remove_unsupported_skills(
            data.get("skills"), profile, original_text=resume_text,
        )
        if isinstance(data.get("skills"), dict) and not allow_unverified_claims:
            data["skills"] = cleaned_skills
        if removed_skills and not allow_unverified_claims:
            report.setdefault("removed_unsupported_skills", []).extend(
                skill for skill in removed_skills
                if skill not in report.get("removed_unsupported_skills", [])
            )

        # Layer 1: Validate JSON fields
        validation = validate_json_fields(
            data, profile, mode=validation_mode, original_text=resume_text,
        )
        report["validator"] = validation

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            return use_master_fallback("Generated versions failed structured factual validation.")

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        # Deterministic safety net: restore any job-relevant skill the master
        # resume genuinely supports but the LLM's freeform skills section
        # dropped. Only ever adds terms already verified in evidence_text --
        # no new factual content, just not relying on a retry to remember it.
        preliminary_ats = analyze_ats_match(
            tailored, job.get("full_description") or "",
            master_resume_text=evidence_text, company_name=company_name,
        )
        dropped_supported_skills = [
            term for term in preliminary_ats["injectable_keywords"]
            if term in supported_ats["skills"]
        ]
        if dropped_supported_skills:
            tailored = _append_verified_skills(tailored, dropped_supported_skills)

        # Layer 2: deterministic whole-document facts/structure validation.
        full_validation = validate_tailored_resume(
            tailored, profile, original_text=resume_text, mode=validation_mode,
        )
        report["full_validator"] = full_validation
        report["ats_match"] = analyze_ats_match(
            tailored,
            job.get("full_description") or "",
            master_resume_text=evidence_text,
            company_name=company_name,
        )
        ats = report["ats_match"]

        if not full_validation["passed"]:
            avoid_notes.extend(full_validation["errors"])
            if attempt < max_retries:
                continue
            return use_master_fallback("Generated versions failed factual validation.")

        # This attempt is factually safe (passed full_validation). Track it as
        # the best effort seen so far, regardless of whether it clears the ATS
        # quality bar below -- so a real, safely-tailored resume is available
        # if no attempt hits the target within max_retries.
        if ats["overall_score"] > best_candidate["score"]:
            best_candidate = {
                "text": tailored,
                "report": copy.deepcopy(report),
                "score": ats["overall_score"],
            }

        quality_issues: list[str] = []
        missing_supported_skills = [
            term for term in ats["injectable_keywords"]
            if term in supported_ats["skills"]
        ]
        if missing_supported_skills:
            quality_issues.append(
                "Restore these job-relevant skills already supported by the original resume: "
                + ", ".join(missing_supported_skills)
                + "."
            )
        # Target score scales with target_aggressiveness between "must at
        # least match master" (0.0) and "reach 90, or the highest truthfully
        # achievable score if lower" (1.0). Never asks for more than the
        # factual ceiling allows -- that would mean fabricating.
        ceiling = supported_ats["factual_ceiling_score"]
        reach_for = min(90.0, ceiling)
        target = master_ats["overall_score"] + (
            (reach_for - master_ats["overall_score"]) * target_aggressiveness
        )
        target = max(target, master_ats["overall_score"])
        target = min(target, ceiling)
        if ats["overall_score"] < target:
            quality_issues.append(
                f"Reach the target ATS score of {target:.1f} (current "
                f"{ats['overall_score']:.1f}; master resume scores "
                f"{master_ats['overall_score']:.1f}, truthful ceiling is {ceiling:.1f})."
            )
        report["ats_quality"] = {
            "passed": not quality_issues,
            "target_score": round(target, 1),
            "target_aggressiveness": target_aggressiveness,
            "factual_ceiling_score": ceiling,
            "issues": quality_issues,
        }
        if quality_issues:
            avoid_notes.extend(quality_issues)
            if attempt < max_retries:
                continue
            return use_best_effort_or_master(
                "Generated versions did not preserve the master's factual ATS strength."
            )

        # Layer 3: LLM judge (catches subtle fabrication) — skipped in lenient mode
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "approved"
            return tailored, report

        judge = judge_tailored_resume(resume_text, tailored, job.get("title", ""), profile)
        report["judge"] = judge

        if not judge["passed"]:
            if allow_unverified_claims:
                report["judge_advisory"] = judge["issues"]
                report["unverified_claim_warnings"] = [judge["issues"]]
                report["status"] = "approved"
                return tailored, report
            # Only explicitly advisory ambiguity gets the benefit of the doubt.
            # Clear fabrication and malformed/legacy FAIL responses fail closed.
            if (
                judge.get("severity") == "ADVISORY"
                and validation_mode != "strict"
                and not require_judge
            ):
                report["judge_advisory"] = judge["issues"]
                report["status"] = "approved"
                return tailored, report
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                continue
            return use_master_fallback("Generated versions failed semantic factual review.")

        # Both passed
        report["status"] = "approved"
        return tailored, report

    return use_master_fallback("The AI did not return a valid resume after all retries.")


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal",
                  target_urls: list[str] | None = None) -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", "lenient", or "creative".

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    skills_boundary = profile.get("skills_boundary", {})
    allow_unverified_claims = bool(
        skills_boundary.get("allow_unverified_claims", True)
    )
    effective_validation_mode = (
        "creative" if allow_unverified_claims else validation_mode
    )

    if target_urls:
        selected = list(dict.fromkeys(target_urls))[:max(1, limit)]
        placeholders = ",".join("?" for _ in selected)
        jobs = [
            dict(row) for row in conn.execute(
                f"""SELECT * FROM jobs
                    WHERE url IN ({placeholders})
                      AND full_description IS NOT NULL
                      AND trim(full_description) != ''
                      AND applied_at IS NULL""",
                selected,
            ).fetchall()
        ]
    else:
        jobs = get_jobs_by_stage(
            conn=conn, stage="pending_tailor", min_score=min_score, limit=limit,
        )

    if not jobs:
        log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    if config.application_resume_mode(profile) == "master":
        if not config.RESUME_PDF_PATH.is_file():
            raise FileNotFoundError("Master resume PDF is unavailable.")
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "UPDATE jobs SET tailored_resume_path=?, tailored_at=? WHERE url=?",
            ((str(RESUME_PATH), now, job["url"]) for job in jobs),
        )
        conn.commit()
        log.info("Master resume mode prepared %d jobs without rewriting.", len(jobs))
        return {
            "approved": len(jobs), "failed": 0, "errors": 0,
            "elapsed": 0.0, "master_resume": len(jobs),
        }

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {
        "approved": 0, "fallback_master": 0,
        "failed_validation": 0, "failed_judge": 0, "error": 0,
    }

    for job in jobs:
        completed += 1
        try:
            from selfapply.scoring.document_tailor import (
                _source_path, create_tailored_artifacts, tailor_document,
            )

            source_path = _source_path(RESUME_PATH)
            tailored, report, patch_set, manifest = tailor_document(
                resume_text, job, profile,
                target_aggressiveness=profile.get("skills_boundary", {}).get(
                    "target_aggressiveness", 0.5,
                ),
                source_path=source_path,
            )

            # Build safe filename prefix
            prefix = artifact_prefix(job)

            artifacts = create_tailored_artifacts(
                source_path, tailored, patch_set, manifest, TAILORED_DIR / prefix,
            )
            txt_path = Path(artifacts["text_path"])
            tailored = txt_path.read_text(encoding="utf-8")
            report["ats_match"] = analyze_ats_match(
                tailored, job.get("full_description") or "",
                master_resume_text=resume_text,
                company_name=str(job.get("site") or ""),
            )
            report["changed_block_count"] = artifacts.get(
                "actual_edit_count", len(patch_set["edits"]),
            )
            actual_ids = {edit["block_id"] for edit in patch_set["edits"]}
            report["changes"] = [
                change for change in report.get("changes", [])
                if change.get("block_id") in actual_ids
            ]

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['site']}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Save validation report
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            # Prefer source-format rendering. The HTML renderer remains the
            # compatibility fallback for PDF/TXT sources or systems without
            # LibreOffice.
            pdf_path = str(artifacts["pdf_path"]) if artifacts.get("pdf_path") else None
            if pdf_path is None:
                try:
                    from selfapply.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                    if artifacts.get("fidelity_warning"):
                        report["format_fidelity"] = "best-effort-fallback"
                        report["format_fidelity_warning"] = artifacts["fidelity_warning"]
                except Exception:
                    log.debug("PDF generation failed for %s", txt_path, exc_info=True)
            report["artifacts"] = {
                "txt": str(txt_path),
                "docx": str(artifacts["docx_path"]) if artifacts.get("docx_path") else None,
                "pdf": pdf_path,
                "manifest": str(artifacts["manifest_path"]),
                "patches": str(artifacts["patch_path"]),
            }
            report["preservation_checks"] = artifacts.get("preservation_checks", {})
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

            result = {
                "url": job["url"],
                "path": str(txt_path),
                "pdf_path": pdf_path,
                "title": job["title"],
                "site": job["site"],
                "status": report["status"],
                "attempts": report["attempts"],
                "fallback_to_master": bool(report.get("fallback_to_master")),
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        # Persist every completed resume instead of holding the whole batch in
        # memory. The UI can now report live tailoring progress, and a stopped
        # worker does not discard resumes it already finished.
        now = datetime.now(timezone.utc).isoformat()
        if result["status"] == "approved":
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (result["path"], now, result["url"]),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                (result["url"],),
            )
        conn.commit()

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d approved, %d master fallbacks, "
        "%d failed_validation, %d failed_judge, %d errors",
        elapsed,
        stats.get("approved", 0),
        stats.get("fallback_master", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("approved", 0),
        "fallback_master": stats.get("fallback_master", 0),
        "failed": stats.get("failed_validation", 0) + stats.get("failed_judge", 0),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
    }
