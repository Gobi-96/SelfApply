"""Conservative, explainable mappings for adjacent technical familiarity.

These mappings never imply production experience. They only allow a missing JD
skill to be described as transferable familiarity when a closely related skill
is evidenced in the master resume.
"""

from __future__ import annotations

from selfapply.scoring.ats_matcher import _SKILLS, _contains, extract_job_keywords


_FAMILIES: tuple[tuple[float, tuple[str, ...]], ...] = (
    (0.70, ("javascript", "typescript")),
    (0.65, ("go", "golang")),
    (0.60, ("react", "vue", "vue.js", "svelte", "angular")),
    (0.60, ("postgresql", "mysql", "sqlite", "sql")),
    (0.60, ("aws", "azure", "gcp", "cloud computing")),
    (0.55, ("fastapi", "flask", "django")),
    (0.55, ("ci/cd", "github actions", "gitlab", "jenkins")),
    (0.55, ("kafka", "apache kafka", "spark", "airflow")),
    (0.55, ("rest", "rest api", "web services", "soap", "graphql", "grpc", "postman")),
    (0.55, ("docker", "kubernetes")),
    (0.55, ("prometheus", "grafana", "datadog", "observability")),
    (0.50, ("terraform", "ansible", "cloud computing")),
    (0.50, ("playwright", "selenium")),
    (0.50, ("sql", "sqlite", "postgresql", "mysql", "mongodb", "dynamodb", "nosql", "elasticsearch")),
    (0.50, ("c", "c++", "go", "golang", "rust")),
    (0.50, ("openai", "langchain", "ollama", "llm", "large language models", "rag", "generative ai", "genai", "vector databases")),
    (0.50, ("iam", "identity and access management", "oauth", "openid connect", "cybersecurity")),
)


def similarity_threshold(target_aggressiveness: float) -> float:
    """Map the ATS slider to the minimum accepted adjacent-skill similarity.

    Low settings remain conservative. At 80% and above the full user-approved
    50%+ adjacency catalogue is available, while unrelated technologies still
    have no mapping and can never be injected.
    """
    value = max(0.0, min(1.0, float(target_aggressiveness)))
    if value >= 0.50:
        return 0.50
    if value >= 0.30:
        return 0.60
    return 0.70


def find_transferable_skills(
    master_resume: str,
    job_description: str,
    *,
    minimum_similarity: float = 0.50,
) -> list[dict]:
    """Return missing JD skills with an evidenced adjacent skill at 50%+."""
    jd_skills = extract_job_keywords(job_description)["skills"]
    evidenced = {skill for skill in _SKILLS if _contains(skill, master_resume)}
    results: list[dict] = []
    for target in jd_skills:
        if target in evidenced:
            continue
        best: tuple[float, str] | None = None
        for similarity, family in _FAMILIES:
            if target not in family or similarity < minimum_similarity:
                continue
            sources = [skill for skill in family if skill in evidenced and skill != target]
            if sources and (best is None or similarity > best[0]):
                best = (similarity, sorted(sources, key=len, reverse=True)[0])
        if best:
            results.append({
                "skill": target,
                "supported_by": best[1],
                "similarity": round(best[0] * 100),
                "usage": "Familiarity only; no production-experience claim.",
            })
    return results
