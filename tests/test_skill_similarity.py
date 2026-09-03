from selfapply.scoring.skill_similarity import (
    find_transferable_skills,
    similarity_threshold,
)


def test_adjacent_frontend_framework_is_transferable_familiarity():
    result = find_transferable_skills(
        "Built production applications with Vue.js and TypeScript.",
        "React and JavaScript are required. React powers our frontend applications.",
    )
    by_skill = {item["skill"]: item for item in result}
    assert by_skill["react"]["similarity"] == 60
    assert by_skill["react"]["supported_by"] == "vue.js"


def test_unrelated_database_is_not_inferred_from_container_tool():
    result = find_transferable_skills(
        "Built Docker containers and Python services.",
        "Kubernetes and PostgreSQL are required for Kubernetes production clusters.",
    )
    by_skill = {item["skill"]: item for item in result}
    assert by_skill["kubernetes"]["supported_by"] == "docker"
    assert "postgresql" not in by_skill


def test_sql_can_support_relational_database_familiarity():
    result = find_transferable_skills(
        "Designed SQL schemas and queries.",
        "PostgreSQL is required. Maintain PostgreSQL databases and schemas.",
    )
    assert result[0]["skill"] == "postgresql"
    assert result[0]["similarity"] == 60


def test_high_slider_enables_full_fifty_percent_catalogue():
    assert similarity_threshold(0.90) == 0.50
    assert similarity_threshold(0.60) == 0.50
    assert similarity_threshold(0.10) == 0.70


def test_container_and_observability_tools_are_adjacent_not_unrelated():
    result = find_transferable_skills(
        "Built Docker services with Datadog observability.",
        "Kubernetes and Grafana are required.",
    )
    by_skill = {item["skill"]: item for item in result}
    assert by_skill["kubernetes"]["similarity"] == 55
    assert by_skill["grafana"]["similarity"] == 55
