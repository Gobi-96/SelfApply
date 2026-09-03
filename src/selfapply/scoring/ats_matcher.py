"""Transparent resume-to-job-description ATS match estimation.

The weighted score and master-resume keyword-gap distinction are adapted from
Resume Matcher (https://github.com/srbhr/resume-matcher), Apache-2.0 licensed.
This SelfApply implementation operates on plain text and was substantially
modified for the existing factual resume-tailoring pipeline.
"""

from __future__ import annotations

from collections import Counter
import re


WEIGHTS = {"keyword_match": 0.55, "skills_coverage": 0.25, "section_completeness": 0.20}

_STOP_WORDS = {
    "about", "ability", "above", "across", "after", "again", "against", "all",
    "also", "among", "and", "any", "are", "because", "been", "before", "being",
    "below", "both", "but", "can", "company", "could", "did", "does", "doing",
    "during", "each", "etc", "every", "excellent", "experience", "few", "for",
    "from", "further", "good", "great", "had", "has", "have", "having", "here",
    "how", "include", "includes", "including", "into", "its", "job", "just",
    "knowledge", "like", "looking", "more", "most", "must", "need", "not", "only",
    "other", "our", "ours", "over", "position", "preferred", "qualifications",
    "required", "requirements", "responsibilities", "role", "same", "seeking",
    "shall", "should", "skills", "some", "strong", "such", "team", "than", "that",
    "the", "their", "them", "then", "there", "these", "they", "this", "those",
    "through", "too", "under", "until", "very", "via", "was", "well", "were",
    "what", "when", "where", "which", "while", "who", "will", "with", "within",
    "work", "working", "would", "year", "years", "you", "your",
    "applicant", "applicants", "application", "applications", "benefit", "benefits",
    "business", "candidate", "candidates", "city", "compensation", "core", "develop",
    "developer", "developers", "development", "employee", "employees", "employer",
    "employment", "engineer", "engineering", "equal", "equity", "francisco", "hybrid",
    "growing", "help", "high", "impact", "leading", "location", "native", "office",
    "onsite", "operations", "opportunities",
    "opportunity", "options", "pay", "product", "remote", "retail", "salary", "san",
    "ship", "software", "state", "states", "united", "visible",
    # Generic corporate/benefits boilerplate that shows up in almost every job
    "paid", "flexible", "legal", "decisions", "recruitment", "off", "range",
    "base", "principal", "world", "market", "build", "end", "contribute",
    "making", "performance", "days", "competitive", "health", "insurance",
    "dental", "vision", "retirement", "401k", "vacation", "holiday", "holidays",
    "wellness", "diversity", "inclusion", "belonging", "culture", "values",
    "mission", "vision", "global", "national", "regional", "headquarters",
    "founded", "employer", "veteran", "disability", "orientation", "identity",
    "race", "gender", "religion", "color", "age", "protected", "characteristic",
    "characteristics", "law", "laws", "consideration", "regardless", "status",
    "provide", "provided", "providing", "based", "may", "vary", "depending",
    "listed", "posting", "posted", "apply", "submit", "review", "process",
}

_SKILLS = (
    "accessibility", "agile", "airflow", "angular", "ansible", "apache kafka", "aws",
    "azure", "bash", "c#", "c++", "ci/cd", "cloud computing", "computer vision",
    "css", "cybersecurity", "data engineering", "data structures", "datadog", "django",
    "docker", "dynamodb", "elasticsearch", "embedded systems", "fastapi", "figma",
    "flask", "gcp", "generative ai", "genai", "git", "github actions", "gitlab", "go", "golang", "grafana",
    "graphql", "grpc", "html", "java", "javascript", "jenkins", "jest", "jira",
    "iam", "identity and access management", "kafka", "kotlin", "kubernetes", "langchain", "large language models", "linux", "llm", "machine learning", "mongodb",
    "mysql", "next.js", "node.js", "nosql", "numpy", "observability", "ollama",
    "oauth", "openai", "opencv", "openid connect", "pandas", "playwright", "postman", "postgresql", "prometheus", "pytest", "rag",
    "python", "pytorch", "react", "react native", "redis", "rest", "rest api", "ruby",
    "rust", "salesforce", "scikit-learn", "selenium", "sentry", "snowflake", "soap",
    "spark", "spring", "sql", "sqlite", "svelte", "swift", "system design", "terraform",
    "typescript", "vector databases", "vue", "vue.js", "web services", "workday", "xml",
)

_SECTIONS = {
    "summary": ("summary", "objective", "profile"),
    "experience": ("experience", "work history", "employment"),
    "education": ("education", "academic", "degree"),
    "skills": ("skills", "technologies", "competencies", "technical"),
}


def _contains(term: str, text: str) -> bool:
    escaped = re.escape(term.strip())
    if not escaped:
        return False
    return bool(re.search(rf"(?<![\w+#]){escaped}(?![\w+#])", text, re.IGNORECASE))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\n.!?;]+", text) if part.strip()]


_COMPETENCY_LEAD = re.compile(
    r"(?i)^\s*(?:you\s+(?:will|must)\s+|we\s+(?:need|require)\s+|"
    r"(?:manage|own|drive|prepare|perform|lead|develop|create|coordinate|"
    r"maintain|support|deliver|provide|use|build)\s+)"
)
_COMPETENCY_SIGNAL = re.compile(
    r"(?i)\b(?:required|must|proficien|responsible for|manage|own|drive|prepare|"
    r"perform|lead|develop|create|coordinate|maintain|support|deliver|provide)\b"
)


def _extract_list_competencies(job_description: str) -> list[str]:
    """Extract domain-neutral competency phrases from requirement/action lists.

    Technical terms have a curated vocabulary, but job descriptions in sales,
    accounting, healthcare, marketing, and operations commonly express their
    core competencies as comma-separated noun phrases. Preserve those phrases
    rather than reducing every non-software role to a zero-keyword score.
    """
    found: list[str] = []
    for sentence in _sentences(job_description):
        if not _COMPETENCY_SIGNAL.search(sentence):
            continue
        body = _COMPETENCY_LEAD.sub("", sentence.strip())
        body = re.sub(
            r"(?i)\s+(?:are|is)\s+(?:strongly\s+)?(?:required|preferred).*$",
            "", body,
        )
        # "using" frequently introduces another competency (for example,
        # month-end close using advanced Excel), so retain both list items.
        body = re.sub(r"(?i)\s+using\s+", ", ", body)
        for part in re.split(r",|\s+\band\b\s+", body):
            phrase = _COMPETENCY_LEAD.sub("", part).strip(" :-()")
            phrase = re.sub(r"(?i)^(?:a|an|the)\s+", "", phrase)
            phrase = re.sub(r"(?i)\s+(?:are|is)\s+required$", "", phrase)
            words = re.findall(r"[a-z][a-z0-9+#/-]*", phrase.casefold())
            if not 1 <= len(words) <= 5:
                continue
            if all(word in _STOP_WORDS for word in words):
                continue
            normalized = " ".join(words)
            if normalized and normalized not in found:
                found.append(normalized)
    return found


def extract_job_keywords(
    job_description: str, *, limit: int = 12, company_name: str = "",
) -> dict[str, list[str]]:
    """Extract technical skills and significant JD terms without an LLM call."""
    if not job_description.strip():
        return {"required_skills": [], "preferred_skills": [], "skills": [], "keywords": []}

    # The employer's own name (e.g. "Thomson", "Reuters") is not a skill a
    # resume could ever be expected to contain -- exclude it so it never
    # inflates the "missing" list or drags down the achievable score.
    company_tokens = {
        token for token in re.findall(r"[a-z][a-z0-9+#-]{2,}", company_name.casefold())
    }

    sentences = _sentences(job_description)
    found_skills = [skill for skill in _SKILLS if _contains(skill, job_description)]
    # Prefer the most specific phrase: "React Native" should not also count as
    # separate "React" and "native" requirements.
    skills: list[str] = []
    for skill in sorted(found_skills, key=lambda value: (-len(value), value)):
        if not any(_contains(skill, selected) for selected in skills):
            skills.append(skill)
    required: list[str] = []
    preferred: list[str] = []
    for skill in skills:
        contexts = [sentence.casefold() for sentence in sentences if _contains(skill, sentence)]
        if any(marker in sentence for sentence in contexts for marker in ("required", "must", "minimum", "proficient")):
            required.append(skill)
        elif any(marker in sentence for sentence in contexts for marker in ("preferred", "nice to have", "bonus")):
            preferred.append(skill)

    covered_tokens = {
        token for skill in skills
        for token in re.findall(r"[a-z][a-z0-9+#-]{2,}", skill.casefold())
    }
    tokens = re.findall(r"[a-z][a-z0-9+#-]{2,}", job_description.casefold())
    counts = Counter(
        token for token in tokens
        if token not in _STOP_WORDS and token not in covered_tokens
        and token not in company_tokens and not token.isdigit()
    )
    first_seen = {token: tokens.index(token) for token in counts}
    repeated_keywords = sorted(
        (token for token in counts if counts[token] >= 2),
        key=lambda token: (-counts[token], first_seen[token], token),
    )
    list_competencies = _extract_list_competencies(job_description)
    keywords = list(dict.fromkeys(list_competencies + repeated_keywords))[:limit]
    return {
        "required_skills": required,
        "preferred_skills": preferred,
        "skills": skills,
        "keywords": keywords,
    }


def _section_completeness(resume_text: str) -> tuple[float, list[str]]:
    found = []
    for name, headings in _SECTIONS.items():
        if any(re.search(rf"(?im)^\s*{re.escape(heading)}\b", resume_text) for heading in headings):
            found.append(name)
    return (len(found) / len(_SECTIONS)) * 100, found


def analyze_ats_match(
    resume_text: str,
    job_description: str,
    *,
    master_resume_text: str | None = None,
    company_name: str = "",
) -> dict:
    """Return an explainable estimated ATS match score and safe keyword gaps."""
    extracted = extract_job_keywords(job_description, company_name=company_name)
    language_warning = ""
    alpha_words = re.findall(r"[^\W\d_]+", f"{resume_text} {job_description}", re.UNICODE)
    ascii_words = sum(word.isascii() for word in alpha_words)
    if alpha_words and ascii_words / len(alpha_words) < 0.75:
        language_warning = (
            "This v1 ATS estimate is optimized for English resumes and job descriptions; "
            "treat this score as unavailable for other languages."
        )
    skills = list(dict.fromkeys(extracted["skills"]))
    keywords = list(dict.fromkeys(skills + extracted["keywords"]))
    matched = [term for term in keywords if _contains(term, resume_text)]
    missing = [term for term in keywords if term not in matched]
    keyword_score = (len(matched) / len(keywords) * 100) if keywords else 0.0

    # The curated vocabulary improves precision for technical roles, but it
    # must not cap candidates in healthcare, finance, education, operations,
    # design, or another field. When no curated term applies, the JD's own
    # significant terms become the competency-coverage set.
    competency_terms = skills or list(extracted["keywords"])
    matched_skills = [
        skill for skill in competency_terms if _contains(skill, resume_text)
    ]
    skills_score = (
        len(matched_skills) / len(competency_terms) * 100
        if competency_terms else 0.0
    )
    section_score, sections_found = _section_completeness(resume_text)
    overall = (
        keyword_score * WEIGHTS["keyword_match"]
        + skills_score * WEIGHTS["skills_coverage"]
        + section_score * WEIGHTS["section_completeness"]
    )

    master = master_resume_text if master_resume_text is not None else resume_text
    injectable = [term for term in missing if _contains(term, master)]
    unsupported = [term for term in missing if term not in injectable]
    master_matched = [term for term in keywords if _contains(term, master)]
    master_matched_skills = [
        skill for skill in competency_terms if _contains(skill, master)
    ]
    ceiling_keyword = (len(master_matched) / len(keywords) * 100) if keywords else 0.0
    ceiling_skills = (
        len(master_matched_skills) / len(competency_terms) * 100
        if competency_terms else 0.0
    )
    factual_ceiling = (
        ceiling_keyword * WEIGHTS["keyword_match"]
        + ceiling_skills * WEIGHTS["skills_coverage"]
        + 100 * WEIGHTS["section_completeness"]
    )
    retained_supported = (
        len([term for term in master_matched if _contains(term, resume_text)])
        / len(master_matched) * 100
        if master_matched else 100.0
    )
    recommendations: list[str] = []
    if injectable:
        recommendations.append(
            "Naturally add master-resume-supported terms: " + ", ".join(injectable[:8]) + "."
        )
    if unsupported:
        recommendations.append(
            "Do not add unless factually true: " + ", ".join(unsupported[:8]) + "."
        )
    if factual_ceiling < 90:
        recommendations.append(
            f"A truthful 90 is not attainable from the master resume for this JD; "
            f"the estimated factual ceiling is {factual_ceiling:.1f}."
        )
    if section_score < 100:
        absent = [name.title() for name in _SECTIONS if name not in sections_found]
        recommendations.append("Add or clearly label sections: " + ", ".join(absent) + ".")
    if not recommendations:
        recommendations.append("Strong structural and keyword alignment; review clarity and measurable impact.")

    return {
        "overall_score": round(overall, 1),
        "factual_ceiling_score": round(factual_ceiling, 1),
        "target_score": round(90.0 if factual_ceiling >= 90 else factual_ceiling, 1),
        "supported_term_retention": round(retained_supported, 1),
        "sub_scores": {
            "keyword_match": round(keyword_score, 1),
            "skills_coverage": round(skills_score, 1),
            "section_completeness": round(section_score, 1),
        },
        "matched_keywords": matched,
        "missing_keywords": missing,
        "injectable_keywords": injectable,
        "unsupported_keywords": unsupported,
        "required_skills": extracted["required_skills"],
        "preferred_skills": extracted["preferred_skills"],
        "skills": skills,
        "competency_terms": competency_terms,
        "recommendations": recommendations,
        "disclaimer": "Estimated match only; employer ATS systems use private rules and may score differently.",
        "language_warning": language_warning,
    }
