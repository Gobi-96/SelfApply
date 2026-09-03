"""Local Connection Center and guarded Self Apply controls."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import platform
import plistlib
import secrets
import subprocess
import threading
import time
import webbrowser
import base64
import copy
import io
import shutil
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

from selfapply import config, database

log = logging.getLogger(__name__)

LABEL = "io.selfapply.worker"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
CONTROL_TOKEN_PATH = config.APP_DIR / "control-token"
WORKER_STATE_PATH = config.APP_DIR / "worker-service.json"

BASE_PORTALS = {
    "Dice": "https://www.dice.com/dashboard/login",
    "Monster": "https://www.monster.com/profile/landing?r=detail",
    "ZipRecruiter": "https://www.ziprecruiter.com/login",
    "Indeed": "https://secure.indeed.com/account/login",
    "LinkedIn": "https://www.linkedin.com/login",
    "Glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
}

_ATS_TASKS: dict[str, dict] = {}
_ATS_TASKS_LOCK = threading.Lock()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    config.ensure_dirs()
    config.write_secret_file(path, json.dumps(value, indent=2))


def _timestamp(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return 0.0


def _env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = config.ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env(values: dict[str, str]) -> None:
    lines = ["# SelfApply configuration", ""]
    lines.extend(f"{key}={value}" for key, value in sorted(values.items()) if value)
    config.write_secret_file(config.ENV_PATH, "\n".join(lines) + "\n")


def _activate_saved_ai_settings() -> None:
    """Load the UI-saved provider into this long-running server process."""
    from selfapply import llm

    values = _env_values()
    keys = (
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_URL",
        "LLM_API_KEY", "LLM_MODEL",
    )
    before = tuple(os.environ.get(key) for key in keys)
    for key in keys:
        if values.get(key):
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)
    if before != tuple(os.environ.get(key) for key in keys):
        llm.reset_client()


def settings_snapshot() -> dict:
    profile = _read_json(config.PROFILE_PATH)
    safe_profile = copy.deepcopy(profile)
    personal = safe_profile.setdefault("personal", {})
    has_password = bool(personal.pop("password", ""))
    try:
        searches = yaml.safe_load(config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        searches = {}
    env = _env_values()
    if env.get("GEMINI_API_KEY"):
        provider = "gemini"
    elif env.get("OPENAI_API_KEY"):
        provider = "openai"
    elif env.get("ANTHROPIC_API_KEY"):
        provider = "anthropic"
    elif env.get("LLM_URL"):
        provider = "local"
    else:
        provider = "none"
    direct_sites = [site["name"] for site in config.load_sites_config().get("sites", [])]
    sources = searches.get("sources", {})
    snapshot = {
        "profile": safe_profile,
        "has_job_site_password": has_password,
        "searches": searches,
        "ai": {
            "provider": provider,
            "model": env.get("LLM_MODEL", ""),
            "local_url": env.get("LLM_URL", ""),
            "has_gemini_key": bool(env.get("GEMINI_API_KEY")),
            "has_openai_key": bool(env.get("OPENAI_API_KEY")),
            "has_anthropic_key": bool(env.get("ANTHROPIC_API_KEY")),
            "has_capsolver_key": bool(env.get("CAPSOLVER_API_KEY")),
            "has_extension_token": bool(env.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN")),
        },
        "resume": {
            "pdf": config.RESUME_PDF_PATH.exists(),
            "text": config.RESUME_PATH.exists(),
            "word": config.RESUME_DOCX_PATH.exists(),
        },
        "discovery": {
            "jobspy": bool(sources.get("jobspy", True)),
            "workday": bool(sources.get("workday", True)),
            "direct": bool(sources.get("direct", True)),
            "available_direct_sites": direct_sites,
            "selected_direct_sites": searches.get("direct_sites", direct_sites),
        },
        "browser_agent": browser_agent_status(),
    }
    snapshot["readiness"] = setup_readiness(snapshot)
    return snapshot


def setup_readiness(snapshot: dict | None = None, *, agent: str | None = None,
                    browser: str | None = None) -> dict:
    """Return the persisted onboarding gaps that make applying unsafe."""
    if snapshot is None:
        snapshot = settings_snapshot()
        return snapshot["readiness"]
    profile = snapshot.get("profile", {})
    personal = profile.get("personal", {})
    auth = profile.get("work_authorization", {})
    disclosures = profile.get("employment_disclosures", {})
    searches = snapshot.get("searches", {})
    discovery = snapshot.get("discovery", {})
    ai = snapshot.get("ai", {})
    missing: list[dict[str, str]] = []

    def require(value: object, label: str, tab: str, field: str) -> None:
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append({"label": label, "tab": tab, "field": field})

    for field, label in (
        ("full_name", "Full name"), ("email", "Email"), ("phone", "Phone"),
        ("address", "Street address"), ("city", "City"),
        ("province_state", "State / province"), ("country", "Country"),
        ("postal_code", "Postal / ZIP code"),
    ):
        require(personal.get(field), label, "account", f"p_{field}")
    if "legally_authorized_to_work" not in auth:
        missing.append({"label": "Work authorization", "tab": "account", "field": "authorized"})
    if "require_sponsorship" not in auth:
        missing.append({"label": "Sponsorship requirement", "tab": "account", "field": "sponsorship"})
    # `No` to both questions gives the browser agent no truthful path through
    # a normal employment-eligibility screen: the candidate says they cannot
    # currently work and also says the employer must not sponsor them.  Treat
    # this as an onboarding conflict instead of silently rejecting jobs one by
    # one.  The UI deliberately asks the user to resolve it; we never infer an
    # immigration answer from a visa/permit label.
    if (auth.get("legally_authorized_to_work") is False
            and auth.get("require_sponsorship") is False):
        missing.append({
            "label": "Review contradictory work-authorization answers",
            "tab": "account",
            "field": "authorized",
        })
    require(profile.get("experience", {}).get("target_role"), "Target role", "account", "p_target_role")
    require((searches.get("defaults") or {}).get("location"), "Target location", "find-jobs", "s_country")
    if not searches.get("queries"):
        missing.append({"label": "At least one target job title", "tab": "find-jobs", "field": "s_titles"})
    enabled_source = any(discovery.get(key) for key in ("jobspy", "workday", "direct"))
    selected_board = bool(searches.get("sites") or discovery.get("selected_direct_sites") or discovery.get("workday"))
    if not enabled_source or not selected_board:
        missing.append({"label": "At least one discovery source", "tab": "find-jobs", "field": "sourceChecks"})
    if not snapshot.get("resume", {}).get("text"):
        missing.append({"label": "Resume upload", "tab": "document-vault", "field": "resumeFile"})
    provider = ai.get("provider", "none")
    if provider == "none":
        missing.append({"label": "AI provider", "tab": "api", "field": "a_provider"})
    else:
        require(ai.get("model"), "AI model", "api", "a_model_select")
        has_key = {
            "openai": ai.get("has_openai_key"), "gemini": ai.get("has_gemini_key"),
            "anthropic": ai.get("has_anthropic_key"), "local": ai.get("local_url"),
        }.get(provider)
        if not has_key:
            missing.append({"label": "Provider API key or endpoint", "tab": "api", "field": "a_api_key"})
    agents = snapshot.get("browser_agent", {})
    if agent:
        if not agents.get(agent, {}).get("authenticated"):
            missing.append({"label": f"{agent.title()} browser connection", "tab": "api", "field": "agentStates"})
    elif not any(item.get("authenticated") for item in agents.values()):
        missing.append({"label": "Codex or Claude browser connection", "tab": "api", "field": "agentStates"})
    if browser == "existing" and not ai.get("has_extension_token"):
        missing.append({"label": "Chrome extension token", "tab": "api", "field": "a_extension"})
    return {"complete": not missing, "missing": missing}


def browser_agent_status() -> dict:
    result: dict[str, dict] = {}
    for name, command in (
        ("codex", ["codex", "login", "status"]),
        ("claude", ["claude", "auth", "status"]),
    ):
        installed = bool(shutil.which(name))
        authenticated = False
        detail = "Not installed"
        if installed:
            try:
                checked = subprocess.run(
                    command, capture_output=True, text=True, timeout=5, check=False,
                )
                raw_detail = (checked.stdout or checked.stderr).strip()[:1000]
                authenticated = checked.returncode == 0 and any(
                    token in raw_detail.casefold()
                    for token in ("logged in", "loggedin", "authenticated", "active")
                )
                detail = "Connected account" if authenticated else "Installed; login required"
            except (OSError, subprocess.TimeoutExpired):
                detail = "Installed; authentication status unavailable"
        result[name] = {"installed": installed, "authenticated": authenticated, "detail": detail}
    return result


_PROFILE_TEXT_FIELDS = {
    "personal": (
        "full_name", "preferred_name", "email", "phone", "city",
        "province_state", "country", "postal_code", "address",
        "linkedin_url", "github_url", "portfolio_url", "website_url",
    ),
    "work_authorization": ("work_permit_type",),
    "compensation": (
        "salary_expectation", "salary_currency", "salary_range_min",
        "salary_range_max",
    ),
    "experience": (
        "years_of_experience_total", "education_level", "current_title",
        "target_role",
    ),
    "eeo_voluntary": (
        "gender", "race_ethnicity", "sexual_orientation", "veteran_status",
        "disability_status",
    ),
    "availability": ("earliest_start_date",),
}


def save_profile_settings(payload: dict) -> dict:
    from selfapply.wizard.init import (
        _validate_email, _validate_money, _validate_optional_url,
        _validate_postal_code,
    )

    existing = _read_json(config.PROFILE_PATH)
    for section, fields in _PROFILE_TEXT_FIELDS.items():
        incoming = payload.get(section, {})
        if not isinstance(incoming, dict):
            raise ValueError(f"Invalid {section} settings.")
        target = existing.setdefault(section, {})
        for field in fields:
            if field in incoming:
                target[field] = str(incoming[field]).strip()
    personal = existing.setdefault("personal", {})
    if not personal.get("full_name"):
        raise ValueError("Full name is required.")
    personal["email"] = _validate_email(personal.get("email", ""))
    personal["postal_code"] = _validate_postal_code(personal.get("postal_code", ""))
    for key in ("linkedin_url", "github_url", "portfolio_url", "website_url"):
        personal[key] = _validate_optional_url(personal.get(key, ""))
    new_password = str(payload.get("job_site_password", ""))
    if payload.get("clear_job_site_password"):
        personal.pop("password", None)
    elif new_password:
        personal["password"] = new_password
    auth = existing.setdefault("work_authorization", {})
    incoming_auth = payload.get("work_authorization", {})
    # This endpoint also accepts small preference-only patches from the ATS
    # tab. Do not erase existing authorization answers when those keys are
    # intentionally absent from a partial update.
    for key in ("legally_authorized_to_work", "require_sponsorship"):
        if key in incoming_auth:
            auth[key] = bool(incoming_auth[key])
    incoming_disclosures = payload.get("employment_disclosures", {})
    if not isinstance(incoming_disclosures, dict):
        raise ValueError("Invalid employment disclosure settings.")
    if "restrictive_covenant" in incoming_disclosures:
        value = incoming_disclosures["restrictive_covenant"]
        if value not in (True, False):
            raise ValueError("Choose Yes or No for the non-compete question.")
        existing.setdefault("employment_disclosures", {})["restrictive_covenant"] = value
    comp = existing.setdefault("compensation", {})
    for key in ("salary_expectation", "salary_range_min", "salary_range_max"):
        comp[key] = _validate_money(comp.get(key, ""))
    if comp.get("salary_range_min") and comp.get("salary_range_max"):
        if float(comp["salary_range_min"]) > float(comp["salary_range_max"]):
            raise ValueError("Minimum salary cannot exceed maximum salary.")
    for section, fields in {
        "skills_boundary": (
            "programming_languages", "frameworks", "tools",
        ),
        "resume_facts": (
            "preserved_companies", "preserved_projects", "real_metrics",
        ),
    }.items():
        incoming = payload.get(section, {})
        target = existing.setdefault(section, {})
        for field in fields:
            value = incoming.get(field, target.get(field, []))
            if isinstance(value, str):
                value = [item.strip() for item in value.split(",") if item.strip()]
            target[field] = value if isinstance(value, list) else []
    skills = existing.setdefault("skills_boundary", {})
    incoming_skills_boundary = payload.get("skills_boundary", {})
    if "transferable_enabled" in incoming_skills_boundary:
        skills["transferable_enabled"] = bool(incoming_skills_boundary["transferable_enabled"])
    if "skill_expansion_enabled" in incoming_skills_boundary:
        skills["skill_expansion_enabled"] = bool(
            incoming_skills_boundary["skill_expansion_enabled"]
        )
        # Retire the two older manual skill-expansion inputs when the new
        # single-control UI is saved.
        skills.pop("confirmed_additional_skills", None)
        skills.pop("confirmed_skills_attested", None)
        skills.pop("transferable_familiarity", None)
    if "allow_unverified_claims" in incoming_skills_boundary:
        skills["allow_unverified_claims"] = bool(
            incoming_skills_boundary["allow_unverified_claims"]
        )
    if "target_aggressiveness" in incoming_skills_boundary:
        raw_aggressiveness = incoming_skills_boundary["target_aggressiveness"]
        try:
            skills["target_aggressiveness"] = max(0.0, min(1.0, float(raw_aggressiveness)))
        except (TypeError, ValueError):
            pass
    facts = existing.setdefault("resume_facts", {})
    if "preserved_school" in payload.get("resume_facts", {}):
        facts["preserved_school"] = str(payload["resume_facts"]["preserved_school"]).strip()
    incoming_facts = payload.get("resume_facts", {})
    if "experience_entries" in incoming_facts:
        value = incoming_facts["experience_entries"]
        if isinstance(value, str):
            value = [{"header": line.strip()} for line in value.splitlines() if line.strip()]
        facts["experience_entries"] = value if isinstance(value, list) else []
    if "education_entries" in incoming_facts:
        value = incoming_facts["education_entries"]
        if isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        facts["education_entries"] = value if isinstance(value, list) else []
    config.write_secret_file(
        config.PROFILE_PATH, json.dumps(existing, indent=2, ensure_ascii=False),
    )
    return {"ok": True, "message": "Account and profile settings saved."}


def save_search_settings(payload: dict) -> dict:
    try:
        distance = int(payload.get("distance", 0))
        hours_old = int(payload.get("hours_old", 72))
        results = int(payload.get("results_per_site", 50))
    except (TypeError, ValueError) as exc:
        raise ValueError("Distance, age, and results must be whole numbers.") from exc
    if distance < 0 or not 1 <= hours_old <= 8760 or not 1 <= results <= 500:
        raise ValueError("Use distance ≥ 0, hours 1–8760, and results 1–500.")
    country = str(payload.get("country", "")).strip()
    state = str(payload.get("state", "")).strip()
    city = str(payload.get("city", "")).strip()
    selected_location = ", ".join(part for part in (city, state, country) if part)
    location = selected_location or str(payload.get("location", "Remote")).strip() or "Remote"
    sites = [
        site for site in payload.get("sites", [])
        if site in {"indeed", "linkedin", "glassdoor", "zip_recruiter", "google"}
    ]
    available_direct = {
        site["name"] for site in config.load_sites_config().get("sites", [])
    }
    selected_direct = [
        name for name in payload.get("direct_sites", []) if name in available_direct
    ]
    titles = [line.strip() for line in str(payload.get("target_titles", "")).splitlines() if line.strip()]
    if not titles:
        raise ValueError("Add at least one target job title.")
    try:
        old = yaml.safe_load(config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        old = {}
    old_location = old.get("location", {}) if isinstance(old.get("location"), dict) else {}
    accepted_locations = [
        line.strip() for line in str(payload.get("accept_patterns", "")).splitlines()
        if line.strip()
    ]
    if not accepted_locations:
        accepted_locations = list(old_location.get("accept_patterns", []) or [])
    if not accepted_locations and location.casefold() != "remote":
        accepted_locations = [location]
    content = {
        "defaults": {
            "location": location, "distance": distance, "hours_old": hours_old,
            "results_per_site": results,
        },
        "sites": sites,
        "sources": {
            "jobspy": bool(payload.get("source_jobspy", True)),
            "workday": bool(payload.get("source_workday", True)),
            "direct": bool(payload.get("source_direct", True)),
        },
        "direct_sites": selected_direct,
        "locations": [
            {"location": location, "remote": bool(payload.get("remote_only"))},
        ],
        "location": {
            "primary": str(payload.get("primary_location", location)).strip(),
            "country": country,
            "state": state,
            "city": city,
            "accept_patterns": accepted_locations,
            "aliases": old_location.get("aliases", {}),
        },
        "queries": [
            {"query": title, "tier": min(index + 1, 3)}
            for index, title in enumerate(titles)
        ],
    }
    config.SEARCH_CONFIG_PATH.write_text(
        yaml.safe_dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    config.secure_file(config.SEARCH_CONFIG_PATH)
    return {"ok": True, "message": "Job preferences saved."}


def save_ai_settings(payload: dict) -> dict:
    from selfapply.llm import normalize_local_url

    provider = str(payload.get("provider", "none"))
    key = str(payload.get("api_key", "")).strip()
    if provider == "auto":
        provider = detect_ai_provider(key)
        if not provider:
            raise ValueError(
                "This key prefix does not identify its provider. Choose OpenAI, "
                "Claude, or Gemini, then save again."
            )
    if provider not in {"none", "gemini", "openai", "anthropic", "local"}:
        raise ValueError("Unknown AI provider.")
    values = _env_values()
    model = str(payload.get("model", "")).strip()
    if not model and provider != "none":
        model = default_model_for_provider(provider)
    if provider == "gemini":
        if key:
            values["GEMINI_API_KEY"] = key
        if not values.get("GEMINI_API_KEY"):
            raise ValueError("Enter a Gemini API key.")
        values.pop("OPENAI_API_KEY", None); values.pop("ANTHROPIC_API_KEY", None); values.pop("LLM_URL", None)
    elif provider == "openai":
        if key:
            values["OPENAI_API_KEY"] = key
        if not values.get("OPENAI_API_KEY"):
            raise ValueError("Enter an OpenAI API key.")
        values.pop("GEMINI_API_KEY", None); values.pop("ANTHROPIC_API_KEY", None); values.pop("LLM_URL", None)
    elif provider == "anthropic":
        if key:
            values["ANTHROPIC_API_KEY"] = key
        if not values.get("ANTHROPIC_API_KEY"):
            raise ValueError("Enter an Anthropic API key.")
        values.pop("GEMINI_API_KEY", None); values.pop("OPENAI_API_KEY", None); values.pop("LLM_URL", None)
    elif provider == "local":
        local_url = str(payload.get("local_url", "")).strip()
        if not local_url.startswith(("http://", "https://")):
            raise ValueError("Enter a complete local model URL.")
        values["LLM_URL"] = normalize_local_url(local_url)
        values.pop("GEMINI_API_KEY", None); values.pop("OPENAI_API_KEY", None); values.pop("ANTHROPIC_API_KEY", None)
    else:
        values.pop("GEMINI_API_KEY", None); values.pop("OPENAI_API_KEY", None); values.pop("ANTHROPIC_API_KEY", None)
        values.pop("LLM_URL", None); values.pop("LLM_MODEL", None)
    if provider != "none":
        if not model:
            raise ValueError("Choose or enter a model.")
        values["LLM_MODEL"] = model
    for field, env_key in (
        ("capsolver_key", "CAPSOLVER_API_KEY"),
        ("extension_token", "PLAYWRIGHT_MCP_EXTENSION_TOKEN"),
    ):
        incoming = str(payload.get(field, "")).strip()
        if incoming:
            values[env_key] = incoming
        if payload.get(f"clear_{field}"):
            values.pop(env_key, None)
    _write_env(values)
    for key in (
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_URL",
        "LLM_MODEL", "CAPSOLVER_API_KEY", "PLAYWRIGHT_MCP_EXTENSION_TOKEN",
    ):
        if key in values:
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)
    from selfapply.llm import reset_client
    reset_client()
    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "message": f"{provider.title()} settings saved securely.",
    }


def detect_ai_provider(api_key: str) -> str | None:
    """Conservatively identify providers whose documented key prefix is distinct.

    A key cannot reveal a model, and unknown prefixes are deliberately not
    guessed. Validation still happens through the provider's real API test.
    """
    value = str(api_key or "").strip()
    if value.startswith("sk-ant-"):
        return "anthropic"
    if value.startswith("AIza"):
        return "gemini"
    if value.startswith(("sk-proj-", "sk-")):
        return "openai"
    return None


def default_model_for_provider(provider: str) -> str:
    return {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-haiku-4-5",
        "gemini": "gemini-2.5-flash",
        "local": "local-model",
    }.get(provider, "")


def reset_local_data(payload: dict) -> dict:
    """Move local user state into a private timestamped backup, then reinitialize.

    The control token and earlier backups stay in place so the current browser
    remains authorized and a reset is recoverable. Nothing is uploaded.
    """
    if str(payload.get("confirmation", "")) != "RESET":
        raise ValueError("Type RESET exactly to confirm the local data reset.")
    if service_running():
        stop_worker()
    if service_running():
        raise RuntimeError("Stop the application worker before resetting data.")

    database.init_db()
    status = database_status()
    database.close_connection()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = config.APP_DIR / "backups" / f"reset-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    if platform.system() != "Windows":
        backup_dir.chmod(0o700)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": "User-requested SelfApply reset",
        "counts": status.get("counts", {}),
        "moved": [],
        "excluded": ["control-token", "backups"],
    }
    for child in list(config.APP_DIR.iterdir()):
        if child.name in {"backups", "control-token"}:
            continue
        shutil.move(str(child), str(backup_dir / child.name))
        manifest["moved"].append(child.name)
    config.write_secret_file(
        backup_dir / "reset-manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False),
    )
    config.ensure_dirs()
    database.init_db()
    _activate_saved_ai_settings()
    return {
        "ok": True,
        "backup": str(backup_dir),
        "message": "Local data reset. Your previous status was saved in a private local backup.",
    }


def delete_saved_secret(kind: str) -> dict:
    values = _env_values()
    provider_keys = ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY")
    mapping = {
        "provider": provider_keys,
        "capsolver": ("CAPSOLVER_API_KEY",),
        "extension": ("PLAYWRIGHT_MCP_EXTENSION_TOKEN",),
    }
    if kind not in mapping:
        raise ValueError("Unknown saved secret.")
    for key in mapping[kind]:
        values.pop(key, None)
        os.environ.pop(key, None)
    _write_env(values)
    return {"ok": True, "message": f"Saved {kind} secret deleted."}


def test_ai_provider() -> dict:
    from selfapply.llm import LLMClient, normalize_local_url

    env = _env_values()
    model = env.get("LLM_MODEL", "")
    if env.get("GEMINI_API_KEY"):
        base, key = "https://generativelanguage.googleapis.com/v1beta/openai", env["GEMINI_API_KEY"]
    elif env.get("OPENAI_API_KEY"):
        base, key = "https://api.openai.com/v1", env["OPENAI_API_KEY"]
    elif env.get("ANTHROPIC_API_KEY"):
        base, key = "https://api.anthropic.com/v1", env["ANTHROPIC_API_KEY"]
    elif env.get("LLM_URL"):
        base, key = normalize_local_url(env["LLM_URL"]), env.get("LLM_API_KEY", "")
    else:
        raise RuntimeError("Configure and save an AI provider first.")
    client = LLMClient(base, model, key)
    try:
        response = client.ask("Reply with exactly: SelfApply OK", max_tokens=20)
    finally:
        client.close()
    if "SelfApply OK" not in response:
        raise RuntimeError("Provider responded, but the model test returned unexpected text.")
    return {"ok": True, "message": f"AI provider test passed with {model}."}


def list_ai_models() -> dict:
    import httpx
    from selfapply.llm import normalize_local_url

    env = _env_values()
    selected_model = env.get("LLM_MODEL", "")
    try:
        if env.get("OPENAI_API_KEY"):
            response = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {env['OPENAI_API_KEY']}"}, timeout=20,
            )
            response.raise_for_status()
            models = sorted(
                item["id"] for item in response.json().get("data", [])
                if item.get("id", "").startswith(("gpt-", "o1", "o3", "o4"))
            )
        elif env.get("GEMINI_API_KEY"):
            response = httpx.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": env["GEMINI_API_KEY"]}, timeout=20,
            )
            response.raise_for_status()
            models = sorted(
                item.get("name", "").removeprefix("models/")
                for item in response.json().get("models", [])
                if "generateContent" in item.get("supportedGenerationMethods", [])
            )
        elif env.get("ANTHROPIC_API_KEY"):
            models = ["claude-sonnet-4-5", "claude-haiku-4-5"]
        elif env.get("LLM_URL"):
            base = normalize_local_url(env["LLM_URL"])
            root = base.removesuffix("/v1")
            models = []
            last_error = ""
            for model_url in (f"{base}/models", f"{root}/api/tags"):
                try:
                    response = httpx.get(model_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("error"):
                        last_error = str(data["error"]); continue
                    candidates = [
                        item.get("id") or item.get("name")
                        for item in data.get("data", data.get("models", []))
                        if item.get("id") or item.get("name")
                    ]
                    models = sorted(
                        name for name in candidates
                        if not any(token in name.casefold() for token in ("embed", "rerank"))
                    )
                    if models:
                        break
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = str(exc)
            if not models:
                raise RuntimeError(
                    "The local server returned no chat models. "
                    f"Check that its OpenAI-compatible API is running. {last_error}".strip()
                )
            if selected_model in {"", "local-model"}:
                selected_model = next(
                    (name for name in models if "coder" in name.casefold() and "7b" in name.casefold()),
                    next((name for name in models if "coder" in name.casefold()), models[0]),
                )
                updated = _env_values()
                updated["LLM_URL"] = base
                updated["LLM_MODEL"] = selected_model
                _write_env(updated)
                os.environ["LLM_URL"] = base
                os.environ["LLM_MODEL"] = selected_model
        else:
            raise RuntimeError("Configure and save an AI provider first.")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not load provider models: {exc}") from exc
    return {
        "ok": True, "models": models, "selected_model": selected_model,
        "message": f"Loaded {len(models)} chat models."
                   + (f" Selected {selected_model}." if selected_model else ""),
    }


def save_resume(payload: dict) -> dict:
    name = str(payload.get("name", ""))
    encoded = str(payload.get("data", ""))
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("Resume upload is not valid base64 data.") from exc
    if not content or len(content) > 10 * 1024 * 1024:
        raise ValueError("Resume must be between 1 byte and 10 MB.")
    suffix = Path(name).suffix.casefold()
    extracted_text = ""
    if suffix == ".pdf" and content.startswith(b"%PDF-"):
        path = config.RESUME_PDF_PATH
        try:
            from pypdf import PdfReader
            from selfapply.scoring.pdf import normalize_resume_text
            extracted_text = "\n\n".join(
                page.extract_text(extraction_mode="layout") or ""
                for page in PdfReader(io.BytesIO(content)).pages
            )
            extracted_text = normalize_resume_text(extracted_text)
        except Exception as exc:
            raise ValueError(f"PDF text extraction failed: {exc}") from exc
    elif suffix == ".docx" and content.startswith(b"PK"):
        path = config.RESUME_DOCX_PATH
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
            extracted_text = "\n".join(
                "".join(node.itertext()).strip()
                for node in root.iter()
                if node.tag.endswith("}p") and "".join(node.itertext()).strip()
            )
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("Word file is not a readable DOCX document.") from exc
    elif suffix == ".txt":
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Text resume must use UTF-8.") from exc
        path = config.RESUME_PATH
    else:
        raise ValueError("Upload a valid PDF or UTF-8 TXT resume.")
    path.write_bytes(content); config.secure_file(path)
    if extracted_text:
        config.write_secret_file(config.RESUME_PATH, extracted_text + "\n")
    elif suffix != ".txt":
        raise ValueError("The document contained no extractable resume text.")
    return {
        "ok": True,
        "message": f"Resume saved as {path.name}; plain text is ready for AI.",
    }


def control_token() -> str:
    config.ensure_dirs()
    if CONTROL_TOKEN_PATH.exists():
        token = CONTROL_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    config.write_secret_file(CONTROL_TOKEN_PATH, token + "\n")
    return token


def portal_targets() -> dict[str, str]:
    from selfapply.apply.launcher import queued_portal_targets

    result = dict(BASE_PORTALS)
    workday_urls = sorted(
        url for url in queued_portal_targets().values()
        if "myworkdayjobs.com" in urlparse(url).netloc.casefold()
    )
    if workday_urls:
        # Show one generic Workday card.  Use a current queued tenant only as
        # the connection-test destination; never pin the UI to an old employer.
        result["Workday"] = workday_urls[0]
    return result


def validate_start_payload(payload: dict) -> dict:
    if payload.get("confirmed") is not True:
        raise ValueError("Check the confirmation box before starting.")
    try:
        max_spend = float(payload["max_spend"])
        target = int(payload.get("target_applied", 500))
        workers = int(payload.get("workers", 1))
        max_actions = int(payload.get("max_actions", 150))
        max_minutes = int(payload.get("max_minutes", 15))
        queue_threshold = int(payload.get("queue_threshold", 50))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Enter valid numeric control values.") from exc
    if not 0 < max_spend <= 10_000:
        raise ValueError("Spending cap must be greater than $0 and at most $10,000.")
    if target < 1 or not 1 <= workers <= 4:
        raise ValueError("Target must be positive and workers must be between 1 and 4.")
    if not 10 <= max_actions <= 300 or not 1 <= max_minutes <= 60:
        raise ValueError("Per-job limits must be 10–300 actions and 1–60 minutes.")
    if not 1 <= queue_threshold <= 1000:
        raise ValueError("Queue threshold must be between 1 and 1000.")
    agent = str(payload.get("agent", "codex"))
    browser = str(payload.get("browser", "managed"))
    if agent not in {"codex", "claude"}:
        raise ValueError("Choose Codex or Claude as the browser agent.")
    if browser not in {"managed", "existing"}:
        raise ValueError("Choose managed or existing Chrome.")
    if browser == "existing" and not _env_values().get("PLAYWRIGHT_MCP_EXTENSION_TOKEN"):
        raise ValueError("Save the Chrome extension token before using existing Chrome.")
    return {
        "max_spend": max_spend, "target_applied": target, "workers": workers,
        "max_actions": max_actions, "max_minutes": max_minutes,
        "queue_threshold": queue_threshold,
        "agent": agent, "browser": browser,
    }


def build_program_arguments(values: dict) -> list[str]:
    executable = _selfapply_executable()
    return [
        str(executable), "run-worker", "--max-spend", str(values["max_spend"]),
        "--agent", values["agent"], "--browser", values["browser"], "--target-applied",
        str(values["target_applied"]), "--queue-threshold",
        str(values["queue_threshold"]), "--workers", str(values["workers"]),
        "--validation", "normal", "--skip-smart-extract", "--max-actions",
        str(values["max_actions"]), "--max-minutes", str(values["max_minutes"]),
    ]


def build_retry_program_arguments(values: dict, url: str) -> list[str]:
    """Build a one-shot, single-job apply command for an explicit UI retry."""
    executable = _selfapply_executable()
    return [
        str(executable), "apply", "--limit", "1", "--url", url,
        "--agent", values["agent"], "--browser", values["browser"],
        "--workers", "1", "--max-actions", str(values["max_actions"]),
        "--max-minutes", str(values["max_minutes"]),
    ]


def _selfapply_executable() -> Path:
    """Return the current virtual environment's console script on any OS."""
    override = os.environ.get("SELFAPPLY_EXECUTABLE")
    if override:
        return Path(override)
    repo = Path(__file__).resolve().parents[2]
    candidates = (
        repo / ".venv" / "Scripts" / "selfapply.exe",
        repo / ".venv" / "Scripts" / "selfapply",
    ) if platform.system() == "Windows" else (repo / ".venv" / "bin" / "selfapply",)
    return next((path for path in candidates if path.exists()), Path("selfapply"))


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    if platform.system() != "Darwin":
        raise RuntimeError(
            "This operating system does not provide the selected service command."
        )
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, timeout=15,
    )


def service_running() -> bool:
    return (
        _active_service_label() is not None
        or _background_worker_state() is not None
        or bool(_managed_worker_pids())
    )


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _background_worker_state() -> dict | None:
    """Read a live Windows/Linux worker record, removing stale state."""
    if platform.system() == "Darwin":
        return None
    state = _read_json(WORKER_STATE_PATH)
    try:
        pid = int(state.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if pid and _pid_is_running(pid):
        return state
    try:
        WORKER_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _managed_worker_pids() -> list[int]:
    """Find orphaned SelfApply browser-agent and managed-Chrome roots.

    The match is intentionally tied to SelfApply-owned config/profile paths so
    normal Claude, Codex, and Chrome sessions are never included.
    """
    from selfapply.apply.chrome import BASE_CDP_PORT

    if platform.system() == "Windows":
        return []
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="], capture_output=True,
            text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    app_dir = str(config.APP_DIR)
    chrome_marker = f"--user-data-dir={config.CHROME_WORKER_DIR}/worker-"
    claude_marker = f"--mcp-config {app_dir}/.mcp-apply-"
    found: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid_text, command = line.strip().split(None, 1)
            pid = int(pid_text)
        except (ValueError, IndexError):
            continue
        is_managed_chrome = (
            chrome_marker in command and "/MacOS/Google Chrome --" in command
        )
        is_claude_apply = claude_marker in command and "claude " in command
        is_codex_apply = (
            "codex exec " in command
            and "--ephemeral" in command
            and "mcp_servers.playwright" in command
            and (
                any(
                    f"localhost:{BASE_CDP_PORT + worker_id}" in command
                    for worker_id in range(4)
                )
                or str(config.APP_DIR / "tmp") in command
            )
        )
        is_playwright_mcp = (
            "@playwright/mcp" in command
            and any(
                f"--cdp-endpoint=http://localhost:{BASE_CDP_PORT + worker_id}" in command
                or f"--cdp-endpoint=http://127.0.0.1:{BASE_CDP_PORT + worker_id}" in command
                for worker_id in range(4)
            )
        )
        if (is_managed_chrome or is_claude_apply or is_codex_apply
                or is_playwright_mcp):
            found.append(pid)
    return sorted(set(found))


def _stop_orphaned_worker_processes() -> int:
    """Terminate only SelfApply-owned worker process groups."""
    from selfapply.apply.chrome import _kill_process_tree

    pids = _managed_worker_pids()
    for pid in pids:
        _kill_process_tree(pid)
    return len(pids)


def _active_service_label() -> str | None:
    """Return the live worker label, including continuous restart intervals.

    A KeepAlive LaunchAgent can briefly have no running process while launchd
    throttles or restarts it.  It is still authoritative and must remain
    stoppable from the UI during that interval. A completed one-shot agent,
    however, remains loaded as ``state = not running`` and is not active.
    """
    if platform.system() != "Darwin":
        return None
    result = _launchctl("print", f"gui/{os.getuid()}/{LABEL}")
    if result.returncode:
        return None
    output = result.stdout.casefold()
    if "state = running" in output:
        return LABEL
    first_active = next(
        (line for line in output.splitlines() if line.strip().startswith("active count =")),
        "",
    )
    if first_active and not first_active.rstrip().endswith("= 0"):
        return LABEL
    try:
        keep_alive = bool(plistlib.loads(PLIST_PATH.read_bytes()).get("KeepAlive"))
    except (OSError, plistlib.InvalidFileException):
        keep_alive = False
    return LABEL if keep_alive else None


def _plist_for_label(label: str) -> Path:
    return PLIST_PATH


def _worker_configuration(path: Path) -> dict:
    """Read safe display metadata from the loaded worker's plist."""
    try:
        arguments = plistlib.loads(path.read_bytes()).get("ProgramArguments", [])
    except (OSError, plistlib.InvalidFileException):
        arguments = []

    def value_after(flag: str, default: str) -> str:
        try:
            return str(arguments[arguments.index(flag) + 1])
        except (ValueError, IndexError):
            return default

    try:
        workers = max(1, int(value_after("--workers", "1")))
    except ValueError:
        workers = 1
    return {
        "agent": value_after("--agent", "unknown"),
        "browser": value_after("--browser", "unknown"),
        "workers": workers,
        "mode": "single job" if "apply" in arguments[1:2] else "self apply",
    }


def _arguments_configuration(arguments: list[str]) -> dict:
    def value_after(flag: str, default: str) -> str:
        try:
            return str(arguments[arguments.index(flag) + 1])
        except (ValueError, IndexError):
            return default

    try:
        workers = max(1, int(value_after("--workers", "1")))
    except ValueError:
        workers = 1
    return {
        "agent": value_after("--agent", "unknown"),
        "browser": value_after("--browser", "unknown"),
        "workers": workers,
        "mode": "single job" if "apply" in arguments[1:2] else "self apply",
    }


def _install_worker_service(arguments: list[str], *, continuous: bool) -> None:
    """Start one persistent UI-managed worker on macOS, Windows, or Linux."""
    if platform.system() != "Darwin":
        config.ensure_dirs()
        repo = Path(__file__).resolve().parents[2]
        output = (config.LOG_DIR / "worker-service.log").open("a", encoding="utf-8")
        errors = (config.LOG_DIR / "worker-service-error.log").open("a", encoding="utf-8")
        kwargs: dict = {
            "cwd": str(repo), "env": {**os.environ, "SELFAPPLY_DIR": str(config.APP_DIR)},
            "stdin": subprocess.DEVNULL, "stdout": output, "stderr": errors,
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(arguments, **kwargs)
        finally:
            output.close(); errors.close()
        _write_json(WORKER_STATE_PATH, {
            "pid": process.pid,
            "arguments": arguments,
            "continuous": continuous,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        return
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    config.ensure_dirs()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[2]
    plist = {
        "Label": LABEL,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PATH": f"{repo}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False} if continuous else False,
        "ThrottleInterval": 60,
        # The worker owns a visible Chrome window and must run in the logged-in
        # Aqua session. Marking it Background can make macOS terminate the
        # responsible Python process when Chrome requests GUI privacy services.
        "ProcessType": "Interactive",
        "StandardOutPath": str(config.LOG_DIR / "worker-service.log"),
        "StandardErrorPath": str(config.LOG_DIR / "worker-service-error.log"),
    }
    PLIST_PATH.write_bytes(plistlib.dumps(plist))
    PLIST_PATH.chmod(0o600)
    checked = subprocess.run(
        ["plutil", "-lint", str(PLIST_PATH)], capture_output=True, text=True,
    )
    if checked.returncode:
        raise RuntimeError(f"Worker definition is invalid: {checked.stderr.strip()}")
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Unable to start the worker.")


def start_worker(payload: dict) -> dict:
    values = validate_start_payload(payload)
    if service_running():
        raise RuntimeError("The SelfApply worker is already running.")
    # Re-check the exact runtime choices because the dashboard may select an
    # unauthenticated agent or existing Chrome without its bridge token.
    snapshot = settings_snapshot()
    readiness = setup_readiness(
        snapshot, agent=values["agent"], browser=values["browser"],
    )
    if not readiness["complete"]:
        labels = ", ".join(item["label"] for item in readiness["missing"])
        raise ValueError(f"Setup incomplete. Complete these required fields: {labels}.")
    _install_worker_service(build_program_arguments(values), continuous=True)
    return {"ok": True, "message": "Self Apply started with an enforced spending cap."}


def stop_worker() -> dict:
    if platform.system() == "Darwin":
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    else:
        state = _background_worker_state()
        if state:
            from selfapply.apply.chrome import _kill_process_tree
            _kill_process_tree(int(state["pid"]))
        WORKER_STATE_PATH.unlink(missing_ok=True)
    cleaned = _stop_orphaned_worker_processes()
    if service_running():
        raise RuntimeError("The worker is still stopping. Try again in a few seconds.")
    conn = database.get_connection()
    released = conn.execute(
        """UPDATE jobs SET apply_status=NULL, agent_id=NULL
           WHERE apply_status='in_progress' AND applied_at IS NULL""",
    ).rowcount
    conn.commit()
    suffix = f" Released {released} job back to Ready." if released else ""
    cleanup = (
        f" Closed {cleaned} orphaned worker process{'es' if cleaned != 1 else ''}."
        if cleaned else ""
    )
    return {"ok": True, "message": f"Application worker stopped.{cleanup}{suffix}"}


def database_status() -> dict:
    database.init_db()
    running = service_running()
    if not running:
        # A force-killed one-shot worker cannot execute its normal finally
        # block. Release only locks whose recorded worker PID is no longer
        # alive so the UI never shows a ghost running job.
        from selfapply.apply.launcher import release_orphaned_locks
        release_orphaned_locks()
    conn = database.get_connection()
    counts = conn.execute(
        """SELECT COUNT(*) total,
        SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) applied,
        SUM(CASE WHEN applied_at IS NULL AND apply_error IS NOT NULL
                  AND COALESCE(apply_status, '') != 'dismissed'
                  AND lower(apply_error) NOT LIKE 'not_eligible_%'
                 THEN 1 ELSE 0 END) failed,
        SUM(CASE WHEN applied_at IS NULL
                  AND lower(COALESCE(apply_error, '')) LIKE 'not_eligible_%'
                 THEN 1 ELSE 0 END) ineligible,
        SUM(CASE WHEN apply_status='in_progress' THEN 1 ELSE 0 END) in_progress,
        SUM(CASE WHEN tailored_resume_path IS NOT NULL AND applied_at IS NULL
                 AND application_url IS NOT NULL AND apply_status IS NULL
                 THEN 1 ELSE 0 END) ready
        FROM jobs"""
    ).fetchone()
    current = conn.execute(
        "SELECT title, site, url FROM jobs WHERE apply_status='in_progress' LIMIT 1"
    ).fetchone()
    current_job = dict(current) if current else None
    active_label = _active_service_label() if running else None
    active_plist = _plist_for_label(active_label) if active_label else PLIST_PATH
    process_state = _background_worker_state() if running and not active_label else None
    worker = (
        _worker_configuration(active_plist) if active_label
        else _arguments_configuration(process_state.get("arguments", [])) if process_state
        else None
    )
    pending_arguments = (
        process_state.get("arguments", []) if process_state
        else plistlib.loads(active_plist.read_bytes()).get("ProgramArguments", [])
        if active_label and active_plist.exists() else []
    )
    if running and current_job is None and pending_arguments:
        # A one-shot worker is live before its database claim completes. Surface
        # the exact requested job immediately instead of showing "no worker".
        try:
            arguments = pending_arguments
            target_index = arguments.index("--url") + 1
            target_url = arguments[target_index]
            pending = conn.execute(
                "SELECT title, site, url FROM jobs WHERE url=? LIMIT 1",
                (target_url,),
            ).fetchone()
            if pending is None:
                pending = conn.execute(
                    "SELECT title, site, url FROM jobs WHERE application_url=? LIMIT 1",
                    (target_url,),
                ).fetchone()
            if pending:
                current_job = dict(pending)
                current_job["starting"] = True
        except (ValueError, IndexError, OSError, plistlib.InvalidFileException):
            pass
    counts_dict = {key: int(counts[key] or 0) for key in counts.keys()}
    try:
        from selfapply.worker_supervisor import queue_snapshot
        _, counts_dict["ready"] = queue_snapshot(
            config.DEFAULTS["min_score"], config.DEFAULTS["max_apply_attempts"],
        )
    except Exception as exc:
        log.debug("Unable to calculate the worker-ready count: %s", exc)
    return {
        "running": running,
        "counts": counts_dict,
        "current_job": current_job,
        "worker": worker,
    }


def daily_activity_stats(days: int = 7) -> dict:
    """Return verified application counts and recent successes for Overview."""
    days = 7
    conn = database.get_connection()
    rows = conn.execute("""
        SELECT date(applied_at) AS day, COUNT(*) AS applied
        FROM jobs
        WHERE applied_at IS NOT NULL AND date(applied_at) >= date('now', ?)
        GROUP BY day
    """, (f"-{max(1, days) - 1} days",)).fetchall()
    by_day = {row["day"]: dict(row) for row in rows}
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    employer_column = (
        "destination_employer" if "destination_employer" in columns
        else "NULL AS destination_employer"
    )
    recent_rows = conn.execute(f"""
        SELECT title, site, location, applied_at, application_url, url,
               {employer_column}
        FROM jobs
        WHERE applied_at IS NOT NULL
        ORDER BY applied_at DESC
        LIMIT 12
    """).fetchall()
    today = datetime.now(timezone.utc).date()
    ordered_days = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    return {
        "days": [
            {
                "date": day,
                "label": datetime.strptime(day, "%Y-%m-%d").strftime("%-m/%-d"),
                "applied": int((by_day.get(day) or {}).get("applied") or 0),
            }
            for day in ordered_days
        ],
        "recent_applied": [
            {
                "title": row["title"] or "Untitled role",
                "employer": _overview_employer(
                    row["destination_employer"], row["site"],
                ),
                "location": row["location"] or "Location not listed",
                "applied_at": row["applied_at"],
                "job_url": (
                    row["application_url"]
                    if str(row["application_url"] or "").startswith(("http://", "https://"))
                    else row["url"]
                ),
            }
            for row in recent_rows
        ],
    }


def _overview_employer(employer: str | None, site: str | None) -> str:
    """Hide discovery-site names when the actual employer was not captured."""
    value = str(employer or "").strip()
    if value:
        return value
    source = str(site or "").strip()
    job_boards = {
        "dice", "glassdoor", "indeed", "linkedin", "monster",
        "talent.com", "ziprecruiter", "zip_recruiter",
    }
    return "" if source.casefold() in job_boards else source


def _failure_info(error: str | None) -> tuple[str, str, bool]:
    raw = (error or "").strip()
    key = raw.casefold()
    code = key.removeprefix("failed:").split()[0].rstrip(":,.;")
    exact = {
        "no_result_line": ("Worker output missing", "The browser agent stopped without a valid result; inspect the worker log.", True),
        "missing_profile_answer": ("Profile answer missing", "A required answer is missing from the candidate profile.", True),
        "embedded_form_unavailable": ("Embedded form unavailable", "The employer's embedded application form could not be opened.", True),
        "stuck": ("Form stalled", "The form made no progress after its correction attempts.", True),
        "page_error": ("Employer page error", "The employer page returned an error while applying.", True),
        "not_eligible_work_auth": ("Work authorization", "The role's work-authorization requirements did not match the profile.", False),
        "not_eligible_location": ("Location mismatch", "The role did not match the configured location rules.", False),
        "not_eligible_salary": ("Compensation mismatch", "The role's compensation did not meet the configured minimum.", False),
        "resume_upload_failed": ("Resume upload", "The resume could not be attached after direct recovery was attempted.", True),
    }
    if code in exact:
        return exact[code]
    rules = (
        (("captcha", "challenge"), "Security check", "A CAPTCHA or anti-bot check required human attention.", True),
        (("resume_upload", "file_upload"), "Resume upload", "The resume could not be attached to the employer form.", True),
        (("login", "session_expired"), "Login or session", "The employer portal login was unavailable or expired.", True),
        (("action_limit", "time_limit"), "Form timeout", "The form did not finish within the configured action or time limit.", True),
        (("browser", "transport", "orphaned", "terminal host"), "Browser interrupted", "The browser connection or worker was interrupted.", True),
        (("not_eligible_location",), "Location mismatch", "The role did not match the configured location rules.", False),
        (("not_eligible_work_auth", "sponsor"), "Work authorization", "The role's work-authorization requirements did not match the profile.", False),
        (("expired",), "Job expired", "The employer removed or closed the posting.", False),
        (("not_a_job", "wrong_application", "redirect"), "Wrong application page", "The link was not the requested job application, so submission was stopped safely.", False),
        (("duplicate",), "Duplicate protected", "SelfApply stopped to avoid submitting the same application twice.", False),
    )
    for needles, category, explanation, retryable in rules:
        if any(needle in key for needle in needles):
            return category, explanation, retryable
    return "Other form issue", "The application stopped before a verified submission.", True


def application_history(start: str = "", end: str = "", status: str = "all") -> dict:
    if status not in {
        "all", "applied", "failed", "ineligible", "queued", "running", "dismissed",
    }:
        raise ValueError("Unknown application status filter.")
    for value in (start, end):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("Dates must use YYYY-MM-DD.") from exc
    conn = database.get_connection()
    date_expr = "COALESCE(applied_at, last_attempted_at, discovered_at)"
    where = ["(applied_at IS NOT NULL OR apply_error IS NOT NULL OR last_attempted_at IS NOT NULL)"]
    params: list[str] = []
    if start:
        where.append(f"date({date_expr}) >= date(?)"); params.append(start)
    if end:
        where.append(f"date({date_expr}) <= date(?)"); params.append(end)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    employer_column = "destination_employer" if "destination_employer" in columns else "NULL AS destination_employer"
    rows = conn.execute(f"""
        SELECT url, title, site, location, application_url, fit_score,
               applied_at, apply_status, apply_error, apply_attempts,
               last_attempted_at, apply_duration_ms, apply_cost_usd, verification_confidence,
               tailored_resume_path, applied_resume_path,
               {employer_column}
        FROM jobs WHERE {' AND '.join(where)}
        ORDER BY {date_expr} DESC LIMIT 500
    """, params).fetchall()
    items = []
    reasons: dict[str, int] = {}
    counts = {
        "applied": 0, "failed": 0, "ineligible": 0,
        "queued": 0, "running": 0, "dismissed": 0, "retryable": 0,
    }
    for row in rows:
        item = dict(row)
        if item["applied_at"]:
            state = "applied"; counts["applied"] += 1
            category, explanation, retryable = "Applied", "Submission was verified as completed.", False
        elif item["apply_status"] == "in_progress":
            state = "running"; counts["running"] += 1
            category, explanation, retryable = "Running now", "The worker is currently processing this job.", False
        elif item["apply_status"] == "dismissed":
            state = "dismissed"; counts["dismissed"] += 1
            old_category, old_explanation, _ = _failure_info(item["apply_error"])
            category = "Removed from queue"
            explanation = (
                f"Automatic retry is disabled. Previous result: {old_category} — {old_explanation}"
                if item["apply_error"] else
                "Automatic retry is disabled for this job."
            )
            retryable = False
        elif (item["apply_error"] or "").casefold().startswith("not_eligible_"):
            state = "ineligible"; counts["ineligible"] += 1
            category, explanation, retryable = _failure_info(item["apply_error"])
        elif item["apply_error"]:
            state = "failed"; counts["failed"] += 1
            category, explanation, retryable = _failure_info(item["apply_error"])
            if retryable and item.get("tailored_resume_path") and (
                item.get("application_url") or item.get("url")
            ):
                counts["retryable"] += 1
            reasons[category] = reasons.get(category, 0) + 1
        else:
            state = "queued"; counts["queued"] += 1
            category, explanation, retryable = (
                "Ready again",
                "The job was automatically returned to the ready pool after an earlier attempt.",
                False,
            )
        item.update({
            "status": state, "reason_category": category,
            "reason_explanation": explanation, "retryable": retryable,
            "date": item["applied_at"] or item["last_attempted_at"],
            "job_url": (
                item["application_url"] or item["url"]
                if urlparse(item["application_url"] or item["url"] or "").scheme in {"http", "https"}
                else ""
            ),
            "employer": item.get("destination_employer") or item.get("site") or "Unknown employer",
            "can_run_now": bool(
                state in {"failed", "queued"}
                and item.get("tailored_resume_path")
                and (item.get("application_url") or item.get("url"))
                and (state == "queued" or retryable)
            ),
        })
        resume_path_value = item.get("applied_resume_path") or item.get("tailored_resume_path")
        item["resume_url"] = None
        if resume_path_value:
            resume_path = Path(resume_path_value).expanduser().resolve()
            roots = {
                "applied": config.APPLIED_RESUME_DIR.resolve(),
                "tailored": config.TAILORED_DIR.resolve(),
            }
            kind = "applied" if item.get("applied_resume_path") else "tailored"
            if (resume_path.suffix.casefold() == ".txt"):
                resume_path = resume_path.with_suffix(".pdf")
            if resume_path.is_file() and resume_path.is_relative_to(roots[kind]):
                item["resume_url"] = (
                    f"/api/applications/resume?kind={kind}&name={quote(resume_path.name)}"
                )
        items.append(item)
    chart = [
        {"reason": name, "count": count}
        for name, count in sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    # "All" is the active application workspace, not the archive. Explicit
    # eligibility mismatches remain available in their own Not eligible tab.
    active_items = [item for item in items if item["status"] != "ineligible"]
    filtered = active_items if status == "all" else [item for item in items if item["status"] == status]
    return {
        "rows": filtered, "counts": counts, "failure_reasons": chart,
        "total": len(filtered), "all_total": len(active_items), "active_filter": status,
    }


def dismiss_application(url: str) -> dict:
    """Remove one non-running, non-applied job from automatic selection."""
    if not url:
        raise ValueError("Missing job identifier.")
    conn = database.get_connection()
    row = conn.execute(
        "SELECT title, applied_at, apply_status FROM jobs WHERE url=?", (url,),
    ).fetchone()
    if not row:
        raise ValueError("Job was not found.")
    if row["applied_at"] or row["apply_status"] == "applied":
        raise ValueError("Applied jobs are protected and cannot be removed.")
    if row["apply_status"] == "in_progress":
        raise ValueError("Cancel the running worker before removing this job.")
    conn.execute(
        "UPDATE jobs SET apply_status='dismissed', agent_id=NULL WHERE url=?", (url,),
    )
    conn.commit()
    return {
        "ok": True,
        "message": f"{row['title'] or 'Job'} removed from automatic retry.",
    }


def restore_application(url: str) -> dict:
    """Restore a dismissed job to its previous failed or ready state."""
    if not url:
        raise ValueError("Missing job identifier.")
    conn = database.get_connection()
    row = conn.execute(
        "SELECT title, apply_status, apply_error FROM jobs WHERE url=?", (url,),
    ).fetchone()
    if not row:
        raise ValueError("Job was not found.")
    if row["apply_status"] != "dismissed":
        raise ValueError("Only a removed job can be restored.")
    restored_status = "failed" if row["apply_error"] else None
    conn.execute(
        "UPDATE jobs SET apply_status=? WHERE url=?", (restored_status, url),
    )
    conn.commit()
    return {
        "ok": True,
        "message": f"{row['title'] or 'Job'} restored.",
    }


def ats_jobs(limit: int = 250) -> dict:
    """List stored jobs that have enough description data for ATS matching."""
    conn = database.get_connection()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    employer = "destination_employer" if "destination_employer" in columns else "NULL"
    rows = conn.execute(f"""
        SELECT url, title, site, fit_score, tailored_resume_path,
               {employer} AS destination_employer
        FROM jobs
        WHERE full_description IS NOT NULL AND trim(full_description) != ''
        ORDER BY fit_score DESC, discovered_at DESC
        LIMIT ?
    """, (max(1, min(limit, 500)),)).fetchall()
    return {"jobs": [
        {
            "url": row["url"], "title": row["title"] or "Untitled job",
            "employer": row["destination_employer"] or row["site"] or "Unknown employer",
            "fit_score": row["fit_score"], "has_tailored": bool(row["tailored_resume_path"]),
        }
        for row in rows
    ]}


def _ats_job(url: str) -> dict:
    conn = database.get_connection()
    row = conn.execute(
        """SELECT url, title, site, location, full_description,
                  tailored_resume_path, fit_score
           FROM jobs WHERE url=?""",
        (url,),
    ).fetchone()
    if not row:
        raise ValueError("Choose a stored job.")
    if not (row["full_description"] or "").strip():
        raise ValueError("This job does not have a usable job description.")
    return dict(row)


def _read_tailored_resume(path_value: str | None) -> str:
    if not path_value:
        raise ValueError("This job has not been tailored yet.")
    path = Path(path_value).expanduser().resolve()
    allowed = config.TAILORED_DIR.resolve()
    if not path.is_relative_to(allowed) or path.suffix.casefold() != ".txt" or not path.is_file():
        raise ValueError("The tailored resume file is unavailable or outside SelfApply storage.")
    return path.read_text(encoding="utf-8")


def document_vault_files() -> dict:
    """List generated tailored-resume documents for the Document Vault page."""
    items = []
    if config.TAILORED_DIR.exists():
        for path in sorted(
            config.TAILORED_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True,
        ):
            if path.name.endswith(("_JOB.txt", "_REPORT.txt")):
                continue
            pdf_path = path.with_suffix(".pdf")
            items.append({
                "name": path.stem,
                "created": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc,
                ).isoformat(),
                "txt": f"/api/ats/download?name={path.name}",
                "pdf": f"/api/ats/download?name={pdf_path.name}" if pdf_path.exists() else None,
            })
    return {"documents": items[:200]}


def _fetch_job_description(job_link: str) -> dict:
    """Fetch a public job page and extract its title and description."""
    import httpx
    from bs4 import BeautifulSoup

    parsed = urlparse(job_link)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a complete public http:// or https:// job link.")
    if parsed.username or parsed.password or parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only public employer job links can be fetched.")
    try:
        response = httpx.get(
            job_link,
            follow_redirects=True,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 SelfApply ATS Matcher/1.0"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError(
            "The employer page could not be imported. Paste the full JD in the box instead."
        ) from exc
    if len(response.content) > 3 * 1024 * 1024:
        raise ValueError("That job page is too large to import. Paste its job description instead.")
    soup = BeautifulSoup(response.text, "html.parser")
    title = ""
    description = ""

    def find_job_posting(value):
        if isinstance(value, dict):
            if value.get("@type") == "JobPosting":
                return value
            for child in value.values():
                found = find_job_posting(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_job_posting(child)
                if found:
                    return found
        return None

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            posting = find_job_posting(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            posting = None
        if posting and posting.get("description"):
            title = str(posting.get("title") or "").strip()
            description = BeautifulSoup(str(posting["description"]), "html.parser").get_text("\n", strip=True)
            break
    if len(description) < 100:
        for selector in (
            "#job-description", "#job_description", ".job-description",
            "[data-testid*=description]", "[class*=jobDescription]", "main", "article",
        ):
            node = soup.select_one(selector)
            text = node.get_text("\n", strip=True) if node else ""
            if len(text) >= 100:
                description = text
                break
    if not title:
        heading = soup.select_one("h1")
        title = (heading.get_text(" ", strip=True) if heading else "") or (
            soup.title.get_text(" ", strip=True) if soup.title else "Imported job"
        )
    if len(description) < 100:
        raise ValueError(
            "The job description could not be read from this link. Paste the full JD in the box instead."
        )
    return {
        "url": str(response.url), "title": title[:180],
        "site": parsed.hostname, "location": "", "full_description": description,
        "tailored_resume_path": None, "fit_score": None,
    }


def _resolve_ats_job(payload: dict) -> tuple[dict, bool]:
    """Resolve a stored job, public link, or pasted job description."""
    stored_url = str(payload.get("url", "")).strip()
    job_link = str(payload.get("job_link", "")).strip()
    custom_jd = str(payload.get("job_description", "")).strip()
    is_stored = bool(stored_url)
    if stored_url:
        job = _ats_job(stored_url)
    elif job_link and not custom_jd:
        job = _fetch_job_description(job_link)
    elif custom_jd:
        digest = hashlib.sha256(custom_jd.encode("utf-8")).hexdigest()[:16]
        job = {
            "url": job_link or f"manual://ats/{digest}",
            "title": str(payload.get("title", "")).strip() or "Pasted job description",
            "site": str(payload.get("company", "")).strip() or (
                urlparse(job_link).hostname if job_link else "Manual ATS match"
            ),
            "location": "", "full_description": custom_jd,
            "tailored_resume_path": None, "fit_score": None,
        }
    else:
        raise ValueError("Select a stored job, paste a job link, or paste the full job description.")
    if custom_jd:
        job["full_description"] = custom_jd
    if str(payload.get("title", "")).strip():
        job["title"] = str(payload["title"]).strip()
    if str(payload.get("company", "")).strip():
        job["site"] = str(payload["company"]).strip()
    return job, is_stored


def analyze_job_ats(payload: dict) -> dict:
    """Analyze the master or tailored resume against one stored or pasted JD."""
    from selfapply.scoring.ats_matcher import analyze_ats_match

    source = str(payload.get("resume_source", "tailored"))
    if source not in {"master", "tailored"}:
        raise ValueError("Choose the master or tailored resume.")
    job, _ = _resolve_ats_job(payload)
    job_description = str(job.get("full_description") or "")
    if len(job_description) < 100:
        raise ValueError("Paste or select a complete job description (at least 100 characters).")
    master = config.RESUME_PATH.read_text(encoding="utf-8")
    resume = master if source == "master" else _read_tailored_resume(job.get("tailored_resume_path"))
    result = analyze_ats_match(
        resume, job_description, master_resume_text=master,
        company_name=str(job.get("site") or ""),
    )
    return {
        "ok": True, "title": job.get("title") or "Job description",
        "resume_source": source, "result": result,
    }


def tailor_and_score_job(payload: dict, on_progress=None) -> dict:
    """Tailor one stored job, persist approved artifacts, and return its ATS score."""
    progress = on_progress or (lambda percent, stage: None)
    _activate_saved_ai_settings()
    if service_running():
        raise RuntimeError("Stop the apply worker before replacing a tailored resume.")
    progress(8, "Reading job description")
    job, is_stored = _resolve_ats_job(payload)
    from selfapply.scoring.pdf import convert_to_pdf
    from selfapply.scoring.tailor import artifact_prefix, tailor_resume

    profile = config.load_profile()
    master = config.RESUME_PATH.read_text(encoding="utf-8")
    progress(18, "Generating tailored resume with the configured AI")
    aggressiveness = payload.get("target_aggressiveness")
    try:
        aggressiveness = max(0.0, min(1.0, float(aggressiveness))) if aggressiveness not in (None, "") else None
    except (TypeError, ValueError):
        aggressiveness = None
    confirmed_experience = payload.get("confirmed_experience_skills") or []
    if not isinstance(confirmed_experience, list):
        raise ValueError("Confirmed experience skills must be a list.")
    tailored, report = tailor_resume(
        master, job, profile, validation_mode="creative", require_judge=False,
        target_aggressiveness=aggressiveness,
        confirmed_experience_skills=confirmed_experience,
        allow_unverified_claims=True,
    )
    report["introduced_skills"] = [
        skill for skill in report.get("introduced_skills", [])
        if str(skill).casefold() in tailored.casefold()
    ]
    progress(72, "Checking resume structure and ATS alignment")
    if report.get("status") not in {"approved", "fallback_master"}:
        return {
            "ok": False, "message": "Tailoring did not pass factual validation.",
            "report": report,
        }
    progress(82, "Saving generated resume")
    config.TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    prefix = artifact_prefix(job)
    text_path = config.TAILORED_DIR / f"{prefix}.txt"
    report_path = config.TAILORED_DIR / f"{prefix}_REPORT.json"
    text_path.write_text(tailored, encoding="utf-8")
    progress(90, "Creating and verifying downloadable PDF")
    pdf_path = None
    try:
        pdf_path = str(convert_to_pdf(text_path))
    except Exception as exc:
        report["pdf_warning"] = str(exc)
        log.warning("Resume text created but PDF conversion failed: %s", exc)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if is_stored:
        conn = database.get_connection()
        conn.execute(
            """UPDATE jobs SET tailored_resume_path=?, tailored_at=?,
                      tailor_attempts=COALESCE(tailor_attempts,0)+1
               WHERE url=?""",
            (str(text_path), datetime.now(timezone.utc).isoformat(), job["url"]),
        )
        conn.commit()
    return {
        "ok": True,
        "message": (
            "The generated draft could not preserve the required resume structure, "
            "so a master-based version was created."
            if report.get("fallback_to_master")
            else (
                "Tailored resume created and ATS-scored. PDF creation failed, but the TXT "
                "download is ready."
                if not pdf_path
                else "Tailored resume created and ATS-scored. Review highlighted warnings before use."
            )
        ),
        "title": job.get("title"), "text_path": str(text_path),
        "pdf_path": pdf_path, "report": report,
        "master_result": report.get("master_ats_match"),
        "result": report.get("ats_match"),
        "used_master_fallback": bool(report.get("fallback_to_master")),
        "downloads": {
            "txt": f"/api/ats/download?name={text_path.name}",
            "pdf": f"/api/ats/download?name={Path(pdf_path).name}" if pdf_path else None,
        },
    }


def start_ats_tailor_task(payload: dict) -> dict:
    """Run one ATS tailoring request asynchronously with visible progress."""
    task_id = secrets.token_urlsafe(12)
    started = time.time()
    with _ATS_TASKS_LOCK:
        _ATS_TASKS[task_id] = {
            "id": task_id, "status": "running", "percent": 2,
            "stage": "Starting", "started_at": started,
        }

    def update(percent: int, stage: str) -> None:
        with _ATS_TASKS_LOCK:
            task = _ATS_TASKS.get(task_id)
            if task:
                task.update(percent=percent, stage=stage)

    def run() -> None:
        try:
            result = tailor_and_score_job(payload, update)
            with _ATS_TASKS_LOCK:
                task = _ATS_TASKS[task_id]
                task.update(
                    status="completed" if result.get("ok") else "failed",
                    percent=100, stage="Complete" if result.get("ok") else "Validation failed",
                    result=result, finished_at=time.time(),
                )
        except Exception as exc:
            with _ATS_TASKS_LOCK:
                _ATS_TASKS[task_id].update(
                    status="failed", percent=100, stage="Could not create resume",
                    error=str(exc) or exc.__class__.__name__, finished_at=time.time(),
                )

    threading.Thread(target=run, name=f"ats-tailor-{task_id}", daemon=True).start()
    return {"ok": True, "task_id": task_id, "message": "Resume tailoring started."}


def ats_tailor_task(task_id: str) -> dict:
    with _ATS_TASKS_LOCK:
        task = copy.deepcopy(_ATS_TASKS.get(task_id))
    if not task:
        raise ValueError("ATS tailoring task was not found.")
    task["elapsed_seconds"] = round(
        (task.get("finished_at") or time.time()) - task["started_at"], 1,
    )
    return task


def queue_failed_retry(url: str) -> dict:
    if not url:
        raise ValueError("Missing job identifier.")
    conn = database.get_connection()
    row = conn.execute(
        """SELECT title, applied_at, apply_status, apply_error,
                  tailored_resume_path, application_url
           FROM jobs WHERE url = ?""", (url,),
    ).fetchone()
    if not row:
        raise ValueError("Job was not found.")
    if row["applied_at"] or row["apply_status"] == "applied":
        raise ValueError("This job was already applied and cannot be queued again.")
    if not row["apply_error"]:
        raise ValueError("Only failed applications can be queued for retry.")
    category, _, retryable = _failure_info(row["apply_error"])
    if not retryable:
        raise ValueError(f"Retry is disabled because this is a permanent issue: {category}.")
    if not row["tailored_resume_path"] or not row["application_url"]:
        raise ValueError("This job needs a tailored resume and application link before retrying.")
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, apply_error = NULL, apply_attempts = 0, agent_id = NULL WHERE url = ?",
        (url,),
    )
    conn.commit()
    return {"ok": True, "message": f"{row['title'] or 'Job'} queued for retry. It will run when Self Apply starts."}


def start_application_retry(payload: dict) -> dict:
    """Reset one retryable job and launch the authoritative worker for only it."""
    url = str(payload.get("url", "")).strip()
    if not url:
        raise ValueError("Missing job identifier.")
    values = validate_start_payload(payload)
    if service_running():
        raise RuntimeError("A worker is already running. Stop it before starting this job.")
    readiness = setup_readiness(
        settings_snapshot(), agent=values["agent"], browser=values["browser"],
    )
    if not readiness["complete"]:
        labels = ", ".join(item["label"] for item in readiness["missing"])
        raise ValueError(f"Setup incomplete. Complete these required fields: {labels}.")

    conn = database.get_connection()
    row = conn.execute(
        """SELECT title, applied_at, apply_status, apply_error, apply_attempts,
                  agent_id, tailored_resume_path
           FROM jobs WHERE url = ?""",
        (url,),
    ).fetchone()
    if not row:
        raise ValueError("Job was not found.")
    if row["applied_at"] or row["apply_status"] == "applied":
        raise ValueError("This job was already applied and cannot be retried.")
    if row["apply_status"] == "in_progress":
        raise ValueError("This job is already running.")
    if row["apply_status"] == "dismissed":
        raise ValueError("Restore this removed job before retrying it.")
    if not row["tailored_resume_path"]:
        raise ValueError("This job needs a tailored resume before it can run.")
    if row["apply_error"]:
        category, _, retryable = _failure_info(row["apply_error"])
        if not retryable:
            raise ValueError(f"Retry is disabled because this is a permanent issue: {category}.")

    previous = (
        row["apply_status"], row["apply_error"], row["apply_attempts"], row["agent_id"],
    )
    conn.execute(
        """UPDATE jobs SET apply_status=NULL, apply_error=NULL,
                  apply_attempts=0, agent_id=NULL
           WHERE url=? AND applied_at IS NULL""",
        (url,),
    )
    conn.commit()
    try:
        _install_worker_service(
            build_retry_program_arguments(values, url), continuous=False,
        )
    except Exception:
        conn.execute(
            """UPDATE jobs SET apply_status=?, apply_error=?,
                      apply_attempts=?, agent_id=? WHERE url=?""",
            (*previous, url),
        )
        conn.commit()
        raise
    return {
        "ok": True,
        "message": f"Retry started for {row['title'] or 'this job'}. Only this job will run.",
    }


def queue_failed_retries(urls: object) -> dict:
    if not isinstance(urls, list):
        raise ValueError("Select one or more failed jobs.")
    selected = list(dict.fromkeys(str(url) for url in urls if str(url).strip()))
    if not selected or len(selected) > 200:
        raise ValueError("Select between 1 and 200 failed jobs.")
    conn = database.get_connection()
    queued = 0
    skipped = 0
    for url in selected:
        row = conn.execute(
            """SELECT applied_at, apply_status, apply_error,
                      tailored_resume_path, application_url
               FROM jobs WHERE url = ?""", (url,),
        ).fetchone()
        if not row or row["applied_at"] or row["apply_status"] == "applied" or not row["apply_error"]:
            skipped += 1; continue
        _, _, retryable = _failure_info(row["apply_error"])
        if not retryable or not row["tailored_resume_path"] or not row["application_url"]:
            skipped += 1; continue
        cursor = conn.execute(
            "UPDATE jobs SET apply_status = NULL, apply_error = NULL, apply_attempts = 0, agent_id = NULL WHERE url = ? AND applied_at IS NULL",
            (url,),
        )
        queued += cursor.rowcount
    conn.commit()
    return {
        "ok": True,
        "message": f"Queued {queued} job{'s' if queued != 1 else ''} for retry"
                   + (f"; skipped {skipped} protected or changed job{'s' if skipped != 1 else ''}." if skipped else ".")
                   + " Start Self Apply when ready.",
        "queued": queued, "skipped": skipped,
    }


def retry_all_applications(payload: dict) -> dict:
    """Queue every recoverable failure and ensure one authoritative worker runs."""
    values = validate_start_payload(payload)
    readiness = setup_readiness(
        settings_snapshot(), agent=values["agent"], browser=values["browser"],
    )
    if not readiness["complete"]:
        labels = ", ".join(item["label"] for item in readiness["missing"])
        raise ValueError(f"Setup incomplete. Complete these required fields: {labels}.")

    conn = database.get_connection()
    candidates = conn.execute(
        """SELECT url FROM jobs
           WHERE applied_at IS NULL AND apply_error IS NOT NULL
             AND COALESCE(apply_status, '') != 'dismissed'
           ORDER BY COALESCE(last_attempted_at, discovered_at) DESC
           LIMIT 200""",
    ).fetchall()
    result = queue_failed_retries([row["url"] for row in candidates])
    if not result["queued"]:
        return {
            **result,
            "message": "No recoverable failed applications are waiting to retry.",
        }
    if service_running():
        return {
            **result,
            "message": f"Added {result['queued']} application"
                       f"{'s' if result['queued'] != 1 else ''} to the current run.",
        }
    _install_worker_service(build_program_arguments(values), continuous=True)
    return {
        **result,
        "started": True,
        "message": f"Retrying {result['queued']} application"
                   f"{'s' if result['queued'] != 1 else ''}. SelfApply will process them safely.",
    }


@dataclass
class ActionState:
    running: bool = False
    kind: str = ""
    message: str = ""


class Controller:
    def __init__(self) -> None:
        self.action = ActionState()
        self.lock = threading.Lock()
        self.manual_done = threading.Event()
        self.cancel_event = threading.Event()

    def connections(self) -> dict:
        google = _read_json(config.APP_DIR / "google_session.json")
        saved = _read_json(config.APP_DIR / "connected_portals.json")
        targets = portal_targets()
        rows = []
        for name, url in targets.items():
            host = urlparse(url).netloc.casefold()
            candidates = [
                value for value in saved.values()
                if isinstance(value, dict)
                and urlparse(str(value.get("target_url") or value.get("url") or "")).netloc.casefold() == host
            ]
            named = saved.get(f"login-test:{name}") or saved.get(host) or saved.get(name)
            if isinstance(named, dict):
                candidates.append(named)
            entry = max(candidates, key=lambda item: _timestamp(item.get("checked_at")), default={})
            status = entry.get("status", "not_tested") if isinstance(entry, dict) else str(entry)
            display_host = "workday.com" if name == "Workday" else host
            rows.append({
                "name": name, "host": display_host, "url": url, "status": status,
            })
        if google.get("account_email") and not google.get("email"):
            google["email"] = google["account_email"]
        if google.get("email") and "verified" not in google:
            google["verified"] = True
        return {"google": google, "portals": rows, "action": vars(self.action)}

    def _run_action(self, kind: str, task) -> None:
        try:
            message = task()
        except Exception as exc:  # surfaced verbatim in the local UI
            message = f"Failed: {exc}"
        with self.lock:
            self.action = ActionState(False, kind, str(message))

    def begin(self, kind: str, task) -> dict:
        with self.lock:
            if self.action.running:
                raise RuntimeError(f"Another connection action is running: {self.action.kind}")
            self.cancel_event.clear()
            self.action = ActionState(True, kind, "Starting…")
        threading.Thread(target=self._run_action, args=(kind, task), daemon=True).start()
        return {"ok": True, "message": f"{kind} started; follow the opened Chrome window."}

    def connect_google(self, browser_mode: str = "managed") -> dict:
        if browser_mode not in {"managed", "existing"}:
            raise ValueError("Choose managed or existing Chrome.")

        def task() -> str:
            from selfapply.apply import launcher
            from selfapply.cli import _managed_connect_browser
            if browser_mode == "existing":
                with self.lock:
                    self.action.message = "Checking your existing Chrome Google session…"
                ok, email = launcher.check_google_with_browser_agent(
                    browser_mode="existing", cancel_event=self.cancel_event,
                )
                if self.cancel_event.is_set():
                    return "Google connection stopped."
                if not ok or not email:
                    raise RuntimeError(
                        "Google is not signed in on the connected Chrome tab. "
                        "Sign in there once, then choose Connect again."
                    )
                _write_json(config.APP_DIR / "google_session.json", {
                    "verified": True, "email": email,
                    "browser_mode": "existing",
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                })
                return f"Google verified as {email} in your existing Chrome."
            with _managed_connect_browser() as port:
                ok, email = launcher.check_google_session(port)
                if not ok:
                    with self.lock:
                        self.action.message = "Waiting for Google sign-in in Chrome…"
                    ok, email = launcher.wait_for_google_signin(
                        port, cancel_event=self.cancel_event, timeout=600,
                    )
                    if self.cancel_event.is_set():
                        return "Google sign-in stopped."
                if not ok or not email:
                    raise RuntimeError("Google sign-in was not verified within 10 minutes.")
                _write_json(config.APP_DIR / "google_session.json", {
                    "verified": True, "email": email,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                })
                return f"Google verified as {email}."
        return self.begin("Google sign-in", task)

    def test_portal(self, name: str, browser_mode: str = "managed") -> dict:
        targets = portal_targets()
        if name not in targets:
            raise ValueError("Unknown portal.")
        if browser_mode not in {"managed", "existing"}:
            raise ValueError("Choose managed or existing Chrome.")

        def task() -> str:
            from selfapply.apply import launcher
            from selfapply.cli import _managed_connect_browser
            google = _read_json(config.APP_DIR / "google_session.json")
            email = google.get("email") or google.get("account_email")
            if not email or not (google.get("verified") or google.get("verified_at")):
                raise RuntimeError("Connect Google first.")
            def connect(port: int = 0) -> str:
                with self.lock:
                    self.action.message = f"Connecting {name} with Google…"
                return launcher.connect_portal_with_google(
                    targets[name], email, port, cancel_event=self.cancel_event,
                    browser_mode=browser_mode,
                )
            if browser_mode == "existing":
                status = connect()
            else:
                with _managed_connect_browser() as port:
                    status = connect(port)
            if status == "failed:cancelled" or self.cancel_event.is_set():
                return f"{name} connection stopped."
            host = urlparse(targets[name]).netloc.casefold()
            saved = _read_json(config.APP_DIR / "connected_portals.json")
            saved[host] = {"name": name, "url": targets[name], "status": status,
                           "checked_at": datetime.now(timezone.utc).isoformat()}
            _write_json(config.APP_DIR / "connected_portals.json", saved)
            return f"{name}: {status.replace('_', ' ')}"
        return self.begin(f"Connect {name}", task)

    def connect_all(self, browser_mode: str = "managed") -> dict:
        if browser_mode not in {"managed", "existing"}:
            raise ValueError("Choose managed or existing Chrome.")
        rows = self.connections()["portals"]
        names = [
            row["name"] for row in rows
            if row["status"] not in {"connected", "manual_connected"}
        ]
        if not names:
            return {"ok": True, "message": "All portals are already connected."}

        def task() -> str:
            google = _read_json(config.APP_DIR / "google_session.json")
            email = google.get("email") or google.get("account_email")
            if not email or not (google.get("verified") or google.get("verified_at")):
                raise RuntimeError("Connect Google first.")
            connected = 0
            for index, name in enumerate(names, start=1):
                if self.cancel_event.is_set():
                    return f"Connect all stopped after {index - 1}/{len(names)} portals."
                with self.lock:
                    self.action.message = f"Connecting {name} ({index}/{len(names)})…"
                targets = portal_targets()
                from selfapply.apply import launcher
                from selfapply.cli import _managed_connect_browser
                try:
                    if browser_mode == "existing":
                        status = launcher.connect_portal_with_google(
                            targets[name], str(email), 0,
                            cancel_event=self.cancel_event,
                            browser_mode="existing",
                        )
                    else:
                        with _managed_connect_browser() as port:
                            status = launcher.connect_portal_with_google(
                                targets[name], str(email), port,
                                cancel_event=self.cancel_event,
                                browser_mode="managed",
                            )
                except Exception as exc:
                    status = f"failed:{type(exc).__name__.casefold()}"
                host = urlparse(targets[name]).netloc.casefold()
                saved = _read_json(config.APP_DIR / "connected_portals.json")
                saved[host] = {
                    "name": name, "url": targets[name], "status": status,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                _write_json(config.APP_DIR / "connected_portals.json", saved)
                connected += status == "connected"
                if status == "failed:cancelled" or self.cancel_event.is_set():
                    return f"Connect all stopped after {index - 1}/{len(names)} portals."
            return f"Connect all finished: {connected}/{len(names)} connected."
        return self.begin("Connect all with Google", task)

    def manual_login(self, name: str) -> dict:
        targets = portal_targets()
        if name not in targets:
            raise ValueError("Unknown portal.")
        self.manual_done.clear()

        def task() -> str:
            from selfapply.cli import _managed_connect_browser
            from playwright.sync_api import sync_playwright

            with _managed_connect_browser() as port:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}", timeout=15_000,
                    )
                    page = browser.contexts[0].new_page()
                    page.goto(
                        targets[name], wait_until="domcontentloaded", timeout=45_000,
                    )
                    page.bring_to_front()
                    if not self.manual_done.wait(600):
                        return f"{name}: manual login timed out; no status was saved."
            host = urlparse(targets[name]).netloc.casefold()
            saved = _read_json(config.APP_DIR / "connected_portals.json")
            saved[host] = {
                "name": name, "url": targets[name], "status": "manual_connected",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_json(config.APP_DIR / "connected_portals.json", saved)
            return f"{name}: manual login marked complete."
        return self.begin(f"Manual login — {name}", task)

    def finish_manual_login(self) -> dict:
        if not self.action.running or not self.action.kind.startswith("Manual login"):
            raise RuntimeError("No manual portal login is waiting.")
        self.manual_done.set()
        return {"ok": True, "message": "Finishing manual login…"}

    def cancel_connection_action(self) -> dict:
        if not self.action.running:
            return {"ok": True, "message": "No connection action is running."}
        self.cancel_event.set()
        self.manual_done.set()
        return {"ok": True, "message": "Stopping the current connection action…"}

    def unlink(self, name: str) -> dict:
        if self.action.running:
            raise RuntimeError("Stop the current connection action before unlinking.")
        if name == "Google":
            (config.APP_DIR / "google_session.json").unlink(missing_ok=True)
            return {"ok": True, "message": "Google was unlinked from SelfApply."}
        targets = portal_targets()
        if name not in targets:
            raise ValueError("Unknown portal.")
        target_host = urlparse(targets[name]).netloc.casefold()
        saved = _read_json(config.APP_DIR / "connected_portals.json")
        for key, entry in list(saved.items()):
            entry_url = str(entry.get("target_url") or entry.get("url") or "") if isinstance(entry, dict) else ""
            entry_host = urlparse(entry_url).netloc.casefold()
            same_workday = name == "Workday" and "myworkdayjobs.com" in entry_host
            if key in {name, target_host, f"login-test:{name}"} or entry_host == target_host or same_workday:
                saved.pop(key, None)
        _write_json(config.APP_DIR / "connected_portals.json", saved)
        return {"ok": True, "message": f"{name} was unlinked from SelfApply."}

    def connect_browser_agent(self, name: str) -> dict:
        if name not in {"codex", "claude"} or not shutil.which(name):
            raise ValueError(f"{name.title()} CLI is not installed.")

        def task() -> str:
            command = [name, "login"] if name == "codex" else [name, "auth", "login"]
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 600
            while process.poll() is None and time.monotonic() < deadline:
                if self.cancel_event.wait(0.25):
                    process.terminate()
                    return f"{name.title()} account connection cancelled."
            if process.poll() is None:
                process.terminate()
                return f"{name.title()} account connection timed out."
            output = process.stdout.read() if process.stdout else ""
            if process.returncode:
                raise RuntimeError(output.strip()[-500:] or "CLI login failed.")
            if not browser_agent_status().get(name, {}).get("authenticated"):
                raise RuntimeError(
                    f"{name.title()} login window closed, but the account was not linked. "
                    "Choose Connect and finish the browser authorization before closing it."
                )
            return f"{name.title()} account connected."
        return self.begin(f"Connect {name.title()} account", task)

    def test_browser_agent(self, name: str) -> dict:
        if name not in {"codex", "claude"}:
            raise ValueError("Choose Codex or Claude.")
        status = browser_agent_status().get(name, {})
        if not status.get("installed"):
            raise RuntimeError(f"{name.title()} CLI is not installed.")
        if not status.get("authenticated"):
            raise RuntimeError(f"{name.title()} is installed but not connected.")
        return {"ok": True, "message": f"{name.title()} connection test passed."}

    def unlink_browser_agent(self, name: str) -> dict:
        if name not in {"codex", "claude"} or not shutil.which(name):
            raise ValueError(f"{name.title()} CLI is not installed.")
        command = [name, "logout"] if name == "codex" else [name, "auth", "logout"]
        checked = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False,
        )
        if checked.returncode:
            raise RuntimeError(f"Could not unlink {name.title()} account.")
        return {"ok": True, "message": f"{name.title()} account unlinked."}


HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script>document.documentElement.dataset.theme=localStorage.getItem('selfapply-theme')||'light'</script>
<title>SelfApply Control Center</title></head><body class="booting"><div class="app-loader" role="status" aria-live="polite"><b>SelfApply</b><small>Preparing your workspace…</small></div><div class="shell"><button id="mobileMenu" class="mobile-menu" type="button" onclick="toggleSidebar()" aria-label="Open navigation" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button><div id="sidebarScrim" class="sidebar-scrim" onclick="toggleSidebar(false)"></div><aside class="sidebar"><div class="brand"><span class="brand-name">Self<b>Apply</b></span></div><div class="local-pill"><span class="local-status-dot"></span><span><b>Local control center</b><small>Data stays on this device</small></span></div><nav aria-label="Main navigation">
<span class="nav-label">Workspace</span>
<button class="active" data-tab="overview" onclick="showTab('overview')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11.5 12 4l9 7.5M5.5 10v9.5h13V10M9.5 19.5v-6h5v6"/></svg></span><span>Overview</span></button>
<button data-tab="review-apply" onclick="showTab('review-apply')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 4 15 8-15 8V4Zm0 8h8"/></svg></span><span>Self Apply</span></button>
<button data-tab="applications" onclick="showTab('applications')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 4h8M9 3h6v3H9V3ZM6 5H4.5v16h15V5H18M8 11l2.2 2.2L16 8.5M8 17h8"/></svg></span><span>Applications</span></button>
<button data-tab="ats" onclick="showTab('ats')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h9l4 4v14H6V3Zm9 0v5h4M9 12h7M9 16h7"/></svg></span><span>Resume tools</span></button>
<span class="nav-label">Setup</span>
<button data-tab="account" onclick="showTab('account')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 9a7 7 0 0 1 14 0"/></svg></span><span>Profile</span></button>
<button data-tab="find-jobs" onclick="showTab('find-jobs')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4M11 8v6M8 11h6"/></svg></span><span>Job preferences</span></button>
<button data-tab="connected-accounts" onclick="showTab('connected-accounts')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 8 7 6a3 3 0 0 0-4 4l3 3a3 3 0 0 0 4 0l2-2M15 16l2 2a3 3 0 0 0 4-4l-3-3a3 3 0 0 0-4 0l-2 2M8.5 15.5l7-7"/></svg></span><span>Connections</span></button>
<button data-tab="settings" onclick="showTab('settings')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19 13.5v-3l-2-.7-.7-1.7.9-1.9-2.1-2.1-1.9.9-1.7-.7L10.5 2h-3l-.7 2-1.7.7-1.9-.9-2.1 2.1.9 1.9-.7 1.7-2 .7v3l2 .7.7 1.7-.9 1.9 2.1 2.1 1.9-.9 1.7.7.7 2h3l.7-2 1.7-.7 1.9.9 2.1-2.1-.9-1.9.7-1.7 2-.4Z" transform="translate(2.5 0) scale(.78 1)"/></svg></span><span>Settings</span></button>
</nav><div class="sidebar-tools"><button class="sidebar-theme" type="button" onclick="toggleTheme()"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 9 9c-5 2-11-4-9-9Z"/></svg><span>Switch theme</span></button><p class="sidebar-note">Private by design. Stored only on this device.</p></div></aside><main><div id="notice" class="notice">Loading…</div>
<section id="onboarding" class="tab onboarding-page"><div class="onboarding-hero"><span class="eyebrow">Welcome to SelfApply</span><h1>Set up your job search</h1><p>Complete four essentials before applications can be submitted. Your profile, résumé, and keys stay on this computer.</p><div class="onboarding-progress"><i id="onboardingProgress"></i></div><b id="onboardingProgressText">Checking setup…</b></div><div id="onboardingSteps" class="onboarding-steps"></div><div id="onboardingFinish" class="onboarding-finish" hidden><b>You’re ready.</b><span>Review your limits before starting Self Apply.</span><button type="button" onclick="showTab('review-apply')">Continue to Self Apply</button></div></section>
<section id="overview" class="tab active page"><div class="page-head overview-head"><div><span class="eyebrow">Your job search</span><h1>Overview</h1><p class="muted" id="overviewUpdated">Loading…</p></div><button class="ghost" onclick="loadOverview()">Refresh</button></div>
<div id="overviewStats" class="stat-cards"></div>
<div class="overview-grid">
<section class="card activity-card"><div class="section-heading"><div><span class="eyebrow">Momentum</span><h2>Applied over the last 7 days</h2></div><span class="activity-legend"><i class="dot dot-green"></i>Submitted</span></div><div id="dailyChart" class="daily-chart"></div></section>
<section id="profileCard" class="card profile-card"></section>
</div>
<section class="card recent-success"><div class="section-heading"><div><span class="eyebrow">Recent success</span><h2>Applications sent</h2><p class="muted compact">Your latest submitted roles.</p></div><button class="ghost" onclick="showTab('applications')">View all</button></div><div id="recentApplied" class="recent-applied-viewport" aria-live="polite"></div></section>
</section>
<section id="connected-accounts" class="tab card settings"><div class="page-title-row"><div><h2>Connections</h2><p class="muted">Connect once, then SelfApply reuses those sessions for supported job portals.</p></div><button class="secondary" onclick="showTab('api')">AI &amp; browser settings</button></div><label class="connection-browser">Chrome session<select id="connectionBrowser" onchange="localStorage.setItem('selfapply-connection-browser',this.value)"><option value="existing">My existing Chrome</option><option value="managed">SelfApply managed Chrome</option></select><small>Existing Chrome reuses your current Google login through the configured extension.</small></label><div id="google"></div><div class="actions"><button id="connectAll" onclick="post('/api/connections/all',{browser:val('connectionBrowser')})">Connect remaining with Google</button><button id="cancelConnect" class="danger" style="display:none" onclick="post('/api/connections/cancel',{})">Stop connecting</button><button id="manualDone" class="secondary" style="display:none" onclick="post('/api/connections/manual/done',{})">I finished manual login</button></div><details class="portal-list"><summary>Job portal connections <span class="muted">View and manage</span></summary><div id="portals"></div></details></section>
<section id="review-apply" class="tab card settings"><h2>Self Apply</h2><p class="muted">Review the essentials, then start applying.</p><div id="readiness" class="readiness incomplete"><b>Setup incomplete</b><div id="readinessText">Checking setup…</div></div><div id="stats" class="stats"></div><div id="current" class="muted"></div><div id="workerPlan" class="worker-plan"></div><div class="form">
<label>Browser agent<select id="applyAgent"><option value="codex">Codex</option><option value="claude">Claude</option></select></label><label>Chrome mode<select id="browserMode"><option value="managed">Managed persistent Chrome</option><option value="existing">My existing Chrome tab</option></select></label>
<label>Maximum spend (USD)<input id="spend" type="number" min="0.01" step="0.01" value="10"></label><label>Total applied target<input id="target" type="number" min="1" value="500"></label>
<details class="advanced wide"><summary>Advanced worker limits</summary><div class="form-section-grid"><label>Workers<select id="workers"><option>1</option><option>2</option><option>3</option><option selected>4</option></select></label><label>Ready queue threshold<input id="queue" type="number" min="1" value="50"></label><label>Actions per job<input id="actions" type="number" min="100" max="150" step="50" value="150"></label><label>Minutes per job<input id="minutes" type="number" min="10" max="15" step="5" value="15"></label></div></details>
<label class="confirm"><input id="confirm" type="checkbox"> I authorize SelfApply to submit eligible applications and understand the spending cap.</label></div><div class="actions"><button type="button" id="startSelfApply" onclick="startWorker()">Start Self Apply</button><button type="button" id="stopSelfApply" class="danger" style="display:none" onclick="stopWorker()">Stop Self Apply</button></div></section>
<section id="applications" class="tab card settings">
<div class="history-head"><div><h2>Applications</h2><p class="muted">See what was submitted and what needs your attention.</p></div><details class="history-filter-panel"><summary>Filters</summary><div class="history-filters">
<label>From<input id="historyStart" type="date"></label><label>To<input id="historyEnd" type="date"></label>
<label>Status<select id="historyStatus" onchange="historyStatusChanged()"><option value="all">All applications</option><option value="running">Running now</option><option value="applied">Applied</option><option value="failed">Needs attention</option><option value="ineligible">Not eligible</option><option value="queued">Waiting to apply</option><option value="dismissed">Hidden</option></select></label>
<label>Sort<select id="historySort" onchange="resetHistoryList()"><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="score">Highest fit score</option><option value="title">Job title A–Z</option><option value="status">Status</option></select></label>
<button onclick="loadApplications()">Apply filters</button></div></details></div>
<div id="historyWorkerState" class="worker-strip" hidden></div><div id="historyStats" class="stats history-stats"></div>
<div id="retryAllPanel" class="history-primary-action" hidden><span><b>Retry failed applications</b><small>Uses the worker count selected on the Self Apply page and never retries applied or ineligible jobs.</small></span><button id="retryAllButton" onclick="retryAllApplications()">Retry all failed applications</button></div>
<div class="history-layout"><aside id="failurePanel" hidden><h3>What needs attention?</h3><p class="muted compact">Choose a reason to narrow the list.</p><div id="failureChart" class="failure-chart"></div></aside><div><h3>Recent activity</h3><div id="applicationRows" class="application-rows"></div></div></div>
</section>
<section id="ats" class="tab card settings"><div class="page-title-row"><div><h2>Resume tools</h2><p class="muted">Check your match or create a tailored résumé for one job.</p></div><button class="secondary" onclick="showTab('document-vault')">Resume files</button></div><div class="form ats-form"><label class="wide">Choose a saved job<select id="atsJob"><option value="">Loading jobs…</option></select></label><details class="advanced wide"><summary>Use a different job description or link</summary><div class="form-section-grid"><label class="wide">Employer job link<input id="atsJobLink" type="url" placeholder="https://company.com/jobs/software-engineer"></label><label>Job title<input id="atsJobTitle" placeholder="Role from the job posting"></label><label>Employer name<input id="atsCompany" placeholder="Example Company"></label><label class="wide">Job description<textarea id="atsJobDescription" class="ats-jd" placeholder="Paste the complete responsibilities and requirements here."></textarea></label><label>Resume to analyze<select id="atsResumeSource"><option value="master">Master resume</option><option value="tailored">Existing tailored resume</option></select></label><label>Scoring method<input value="Keyword, skill, and section match" disabled></label></div></details></div><div class="actions"><button id="analyzeAtsButton" onclick="analyzeAts()">Check match</button><button id="tailorAtsButton" class="secondary" onclick="tailorAndScore()">Create tailored résumé</button></div><div id="atsResult" class="ats-result"><div class="empty">Choose a saved job to begin.</div></div></section>
<section id="account" class="tab card settings"><h2>Profile</h2><p class="muted">The information used to complete applications and tailor résumés.</p><div class="form">
<h3 class="wide">Personal information</h3><label>Full name <span class="req">*</span><input id="p_full_name" required placeholder="e.g. Jordan Lee"></label><label>Preferred name<input id="p_preferred_name" placeholder="e.g. Jordan"></label><label>Email <span class="req">*</span><input id="p_email" type="email" required placeholder="e.g. jordan@example.com"></label><label>Phone <span class="req">*</span><input id="p_phone" type="tel" required placeholder="e.g. +1 510 555 0123"></label><label>Street address <span class="req">*</span><input id="p_address" required placeholder="e.g. 123 Main Street"></label><label>Country <span class="req">*</span><select id="p_country" required onchange="locationCountryChanged()"></select><input id="p_country_other" class="other-field" placeholder="Type country"></label><label>State / province <span class="req">*</span><select id="p_province_state" required onchange="locationStateChanged()"></select><input id="p_province_state_other" class="other-field" placeholder="Type state or province"></label><label>City <span class="req">*</span><select id="p_city" required></select><input id="p_city_other" class="other-field" placeholder="Type city"></label><label>Postal / ZIP code <span class="req">*</span><input id="p_postal_code" required placeholder="e.g. postal or ZIP code"></label><label>LinkedIn URL<input id="p_linkedin_url" type="url" placeholder="e.g. https://linkedin.com/in/name"></label><label>GitHub URL<input id="p_github_url" type="url" placeholder="e.g. https://github.com/name"></label><label>Portfolio URL<input id="p_portfolio_url" type="url" placeholder="e.g. https://portfolio.example"></label><label>Website URL<input id="p_website_url" type="url" placeholder="e.g. https://example.com"></label><label>Dedicated job-site password<div class="secret-wrap"><input id="jobPassword" type="password" placeholder="Leave blank to keep saved password"><button type="button" class="eye" onclick="toggleSecret('jobPassword',this)" aria-label="Show password">👁</button></div><div id="passwordState" class="secret-note"></div></label>
<h3 class="wide">Work authorization &amp; employment disclosures</h3><label>Authorized to work<select id="authorized"><option value="true">Yes</option><option value="false">No</option></select></label><label>Needs sponsorship now or in future<select id="sponsorship"><option value="false">No</option><option value="true">Yes</option></select></label><label>Permit / immigration status<input id="p_work_permit_type" placeholder="F1 OPT, Citizen, PR…"></label><label>Subject to a non-compete or similar agreement<select id="restrictiveCovenant"><option value="">Not answered</option><option value="false">No</option><option value="true">Yes</option></select><small>Optional until an employer asks; unknown answers stop only that application.</small></label>
<h3 class="wide">Compensation & experience</h3><label>Expected annual salary<input id="p_salary_expectation" inputmode="numeric" placeholder="e.g. 150000"></label><label>Currency<select id="p_salary_currency"><option>USD</option><option>CAD</option><option>EUR</option><option>GBP</option><option>INR</option></select></label><label>Minimum salary<input id="p_salary_range_min" inputmode="numeric" placeholder="e.g. 130000"></label><label>Maximum salary<input id="p_salary_range_max" inputmode="numeric" placeholder="e.g. 175000"></label><label>Total years of experience<input id="p_years_of_experience_total" type="number" min="0" placeholder="e.g. 8"></label><label>Highest education<select id="p_education_level"><option>Bachelor's</option><option>Master's</option><option>PhD</option><option>Associate</option><option>High school</option><option>Self-taught</option><option>Other</option></select></label><label>Current title<input id="p_current_title" placeholder="e.g. your current role"></label><label>Target role <span class="req">*</span><input id="p_target_role" required placeholder="e.g. your target role"></label><label>Earliest start date<select id="p_earliest_start_date"><option>Immediately</option><option>Within 2 weeks</option><option>Within 1 month</option><option>Negotiable</option></select></label>
<h3 class="wide">Voluntary EEO answers</h3><label>Gender<select id="p_gender"><option>Decline to self-identify</option><option>Man</option><option>Woman</option><option>Non-binary</option></select></label><label>Race / ethnicity<select id="p_race_ethnicity"><option>Decline to self-identify</option><option>Asian (Asian Indian)</option><option>Asian</option><option>Black or African American</option><option>Hispanic or Latino</option><option>White</option><option>Two or more races</option></select></label><label>Sexual orientation<select id="p_sexual_orientation"><option>Decline to self-identify</option><option>Heterosexual</option><option>Gay or Lesbian</option><option>Bisexual</option></select></label><label>Veteran status<select id="p_veteran_status"><option>Decline to self-identify</option><option>I am not a protected veteran</option><option>I identify as a protected veteran</option></select></label><label>Disability status<select id="p_disability_status"><option>Decline to self-identify</option><option>No, I do not have a disability</option><option>Yes, I have a disability</option></select></label>
<h3 class="wide">Skills and preserved resume facts</h3><label class="wide">Programming languages (comma-separated)<textarea id="p_programming_languages" placeholder="e.g. Python, Java, Rust, SQL"></textarea></label><label class="wide">Frameworks and libraries (comma-separated)<textarea id="p_frameworks" placeholder="e.g. React, Django, Spring, TensorFlow"></textarea></label><label class="wide">Tools and platforms (comma-separated)<textarea id="p_tools" placeholder="e.g. AWS, Docker, Tableau, Salesforce"></textarea></label><div class="wide confirmed-skills"><label class="confirm"><input id="p_skill_expansion_enabled" type="checkbox" onchange="saveSkillTailoringPreferences()"> Automatically add relevant JD skills while tailoring resumes for applications.</label><label class="confirm"><input id="p_allow_unverified_claims" type="checkbox" checked onchange="saveSkillTailoringPreferences()"> Never block résumé creation for unsupported or newly introduced claims.</label><span class="warn compact">Creative mode may add skills or responsibilities that are not in the master résumé. They are shown as warnings, not blockers. Review the résumé before representing those claims to an employer.</span><label>Skill-tailoring strength <b id="atsAggressivenessValue">50%</b><input id="atsAggressiveness" type="range" min="0" max="100" step="5" value="50" oninput="atsAggressivenessValue.textContent=atsAggressiveness.value+'%'" onchange="saveSkillTailoringPreferences()"></label><span class="muted compact">50% makes moderate changes. Higher values rewrite more of the summary, technical skills, projects, and relevant experience. Changes save automatically and are used by both ATS Matcher and automatic applications.</span></div><label class="wide">Companies that must remain (comma-separated)<textarea id="p_preserved_companies" placeholder="e.g. Example Corp, Acme Labs"></textarea></label><label class="wide">Projects that must remain (comma-separated)<textarea id="p_preserved_projects" placeholder="e.g. Inventory Platform, Customer Analytics Portal"></textarea></label><label class="wide">School names to preserve (comma-separated)<input id="p_preserved_school" placeholder="e.g. Example University, City College"></label><label class="wide">Real metrics that must remain (comma-separated)<textarea id="p_real_metrics" placeholder="e.g. 99.9% uptime, 50k users, reduced latency by 35%"></textarea></label><label class="wide">Exact work-history headings (one per line)<textarea id="p_experience_entries" placeholder="e.g. Product Manager, Example Company — Jan 2022-Present"></textarea><span class="muted compact">Keep employer, title, and dates together exactly as they appear on the master resume.</span></label><label class="wide">Exact education entries (one per line)<textarea id="p_education_entries" placeholder="e.g. BA Economics, Example University, 2020"></textarea></label></div><div class="actions"><button onclick="saveProfile()">Save account & profile</button></div></section>
<section id="find-jobs" class="tab card settings">
<h2>Job preferences</h2><p class="muted">Choose the roles and locations you want.</p>
<div class="form">
<input id="s_location" type="hidden">
<label>Country <span class="req">*</span><select id="s_country" required onchange="jobCountryChanged()"></select><input id="s_country_other" class="other-field" placeholder="Type country" oninput="syncJobLocation()"></label>
<label>State / province <span class="req">*</span><select id="s_state" required onchange="jobStateChanged()"></select><input id="s_state_other" class="other-field" placeholder="Type state or province" oninput="syncJobLocation()"></label>
<label>City <span class="req">*</span><select id="s_city" required onchange="syncJobLocation()"></select><input id="s_city_other" class="other-field" placeholder="Type city" oninput="syncJobLocation()"></label>
<label>Search radius<select id="s_distance"><option value="25">25 miles</option><option value="50">50 miles</option><option value="100">100 miles</option><option value="250">250 miles</option><option value="0">Any distance</option></select></label>
<label>Maximum job age<select id="s_hours_old"><option value="24">Past 24 hours</option><option value="72">Past 3 days</option><option value="168">Past week</option><option value="720">Past month</option></select></label>
<label>Results per site<select id="s_results"><option>25</option><option>50</option><option>100</option><option>200</option></select></label>
<label>Location mode<select id="s_remote"><option value="false">On-site, hybrid, and local</option><option value="true">Remote only</option></select></label>
<h3 class="wide">Discovery sources <span class="req">*</span></h3>
<div id="sourceChecks" class="source-grid wide"><label><input id="sourceJobspy" type="checkbox"> Major job boards</label><label><input id="sourceWorkday" type="checkbox"> Workday employers</label><label><input id="sourceDirect" type="checkbox"> Direct and remote boards</label></div>
<details class="wide source-details"><summary>Major job boards <span id="majorCount" class="badge"></span></summary><div class="detail-tools"><button type="button" class="ghost" onclick="toggleChecks('#siteChecks input',true);updateBoardCounts()">Select all</button><button type="button" class="ghost" onclick="toggleChecks('#siteChecks input',false);updateBoardCounts()">Clear</button></div><div id="siteChecks" class="check-grid"><label><input type="checkbox" value="indeed" onchange="updateBoardCounts()"> Indeed</label><label><input type="checkbox" value="linkedin" onchange="updateBoardCounts()"> LinkedIn</label><label><input type="checkbox" value="glassdoor" onchange="updateBoardCounts()"> Glassdoor</label><label><input type="checkbox" value="zip_recruiter" onchange="updateBoardCounts()"> ZipRecruiter</label><label><input type="checkbox" value="google" onchange="updateBoardCounts()"> Google Jobs</label></div></details>
<details class="wide source-details"><summary>Direct and remote boards <span id="directCount" class="badge"></span></summary><div class="detail-tools"><button type="button" class="ghost" onclick="toggleChecks('#directChecks input',true);updateBoardCounts()">Select all</button><button type="button" class="ghost" onclick="toggleChecks('#directChecks input',false);updateBoardCounts()">Clear</button></div><div id="directChecks" class="check-grid"></div></details>
<label class="wide">Target job titles — one per line <span class="req">*</span><textarea id="s_titles" required placeholder="e.g.&#10;Product Manager&#10;Data Analyst&#10;Registered Nurse"></textarea></label>
</div><div class="actions"><button onclick="saveJobs()">Save job preferences</button></div></section>
<section id="document-vault" class="tab card settings"><h2>Document vault</h2><p class="muted">Your master resume, plus every tailored resume SelfApply has generated for a specific job.</p><div class="form"><h3 class="wide">Master resume <span class="req">*</span></h3><label class="wide">Upload PDF, Word DOCX, or plain-text resume<input id="resumeFile" type="file" accept=".pdf,.docx,.txt"><div id="resumeState" class="secret-note"></div><div class="muted">PDF and DOCX files are automatically converted to plain text for AI scoring and tailoring.</div></label></div><div class="actions"><button class="secondary" onclick="uploadResume()">Upload and convert resume</button></div><h3>Tailored resumes</h3><div id="documentList" class="doc-list"><div class="empty">Loading…</div></div></section>
<section id="api" class="tab card settings"><h2>API & AI</h2><p class="muted">The content provider scores jobs and tailors resumes. Saved secrets stay masked; the eye button shows only what you are currently typing.</p><div class="form"><label>AI content provider <span class="req">*</span><select id="a_provider" onchange="providerChanged()"><option value="auto">Detect from API key</option><option value="openai">OpenAI</option><option value="anthropic">Claude / Anthropic</option><option value="gemini">Google Gemini</option><option value="local">Other: local / Ollama / compatible</option><option value="none">Disable AI</option></select></label><label>Model <span class="req">*</span><select id="a_model_select" onchange="modelChanged()"></select></label><label id="customModelWrap" style="display:none">Custom model name<input id="a_model_custom" placeholder="e.g. gpt-5-mini"></label><label id="apiKeyWrap">Provider API key <span class="req">*</span><div class="secret-wrap"><input id="a_api_key" type="password" oninput="detectProviderKey()" placeholder="Enter a new key or keep the saved key"><button type="button" class="eye" onclick="toggleSecret('a_api_key',this)" aria-label="Show API key">👁</button></div><div id="apiKeyState" class="secret-note"></div><button type="button" id="deleteApiKey" class="danger mini" onclick="deleteSecret('provider')">Delete saved API key</button></label><label id="localUrlWrap" style="display:none">Compatible API endpoint<input id="a_local_url" type="url" placeholder="e.g. http://localhost:11434/v1"></label><label>CapSolver key (optional)<div class="secret-wrap"><input id="a_capsolver" type="password" placeholder="Enter a new key or keep the saved key"><button type="button" class="eye" onclick="toggleSecret('a_capsolver',this)" aria-label="Show CapSolver key">👁</button></div><div id="capsolverState" class="secret-note"></div><button type="button" id="deleteCapsolver" class="danger mini" onclick="deleteSecret('capsolver')">Delete saved key</button></label></div><div class="actions"><button onclick="saveAI()">Save API & AI settings</button><button class="secondary" onclick="post('/api/settings/ai/test',{})">Test saved provider</button></div><h3>Browser automation account <span class="req">*</span></h3><p class="muted">Test or unlink each connection here. This is separate from the content API.</p><div id="agentStates" class="agent-grid"></div><h3>Use my existing Chrome tab</h3><ol class="muted"><li>Install the Playwright MCP Bridge extension.</li><li>Open the extension and copy its PLAYWRIGHT_MCP_EXTENSION_TOKEN.</li><li>Paste and save it below, keep Chrome open, then choose “My existing Chrome tab” on Self Apply.</li></ol><div class="actions"><a class="link-button secondary" href="https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm" target="_blank" rel="noopener">Install Chrome extension</a></div><div class="form"><label class="wide">Chrome extension token<div class="secret-wrap"><input id="a_extension" type="password" placeholder="Enter a new token or keep the saved token"><button type="button" class="eye" onclick="toggleSecret('a_extension',this)" aria-label="Show extension token">👁</button></div><div id="extensionState" class="secret-note"></div><button type="button" id="deleteExtension" class="danger mini" onclick="deleteSecret('extension')">Delete saved token</button></label></div><div class="actions"><button onclick="saveAI()">Save Chrome connection</button></div></section>
<section id="settings" class="tab card settings"><div class="page-title-row"><div><h2>Settings</h2><p class="muted">Appearance, privacy, and local data controls.</p></div></div><div class="settings-grid"><section class="preference-card"><div><span class="eyebrow">Appearance</span><h3>Theme</h3><p class="muted">Light is the default. Your choice is saved only in this browser.</p></div><div class="theme-options"><button id="themeLight" class="theme-option" type="button" role="radio" onclick="setTheme('light')"><span class="theme-preview"><i></i><i></i><i></i></span><b>Light</b><small>Bright and clear</small></button><button id="themeDark" class="theme-option" type="button" role="radio" onclick="setTheme('dark')"><span class="theme-preview preview-dark"><i></i><i></i><i></i></span><b>Dark</b><small>Lower-light workspace</small></button></div></section><section class="preference-card danger-zone"><div><span class="eyebrow">Local data</span><h3>Reset SelfApply</h3><p class="muted">Stops the worker and starts fresh. Your current profile, application history, résumés, settings, and logs are moved into a private timestamped backup first.</p></div><button type="button" class="danger" onclick="resetSelfApply()">Back up and reset local data</button><small id="resetState" class="muted">API keys are included in the private local backup. Nothing is uploaded.</small></section></div></section></main></div>
<script>
const token='__TOKEN__',notice=document.getElementById('notice');
const connected=s=>s==='connected'||s==='manual_connected';
async function get(p){return(await fetch(p)).json()}
async function post(p,b){try{let r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json','X-SelfApply-Token':token},body:JSON.stringify(b)}),j=await r.json();if(r.status===403&&j.error==='Invalid control token.'){notice.textContent='This page expired. Reloading the current SelfApply controls…';setTimeout(()=>location.reload(),600);return {ok:false,error:j.error}}await refresh();notice.textContent=j.message||j.error||'Done';return j}catch(error){notice.textContent=`Request failed: ${error.message||error}`;return {ok:false,error:String(error.message||error)}}}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){
 let [s,c]=await Promise.all([get('/api/status'),get('/api/connections')]),g=c.google||{},busy=!!c.action.running;
 workerRunning=!!s.running;
 notice.classList.toggle('busy',busy||s.running);
 let googleBusy=busy&&c.action.kind==='Google sign-in';
 document.getElementById('google').innerHTML=`<div class=row><span><b>Google</b><br><small>${esc(g.email||'Not verified')}</small></span><span class=portal-actions><span class="badge ${g.verified?'connected':''}">${g.verified?'Connected':googleBusy?'Connecting…':'Not connected'}</span>${g.verified?`<button class=ghost onclick="unlinkAccount('Google')" ${busy?'disabled':''}>Unlink</button>`:`<button onclick="post('/api/connections/google',{browser:val('connectionBrowser')})" ${busy?'disabled':''}>${googleBusy?'Connecting…':'Connect'}</button>`}</span></div>`;
 document.getElementById('portals').innerHTML=c.portals.map(p=>{let ok=connected(p.status),portalBusy=busy&&(c.action.kind===`Connect ${p.name}`||c.action.message?.startsWith(`Connecting ${p.name} `));return `<div class=row><span><b>${esc(p.name)}</b><br><small>${esc(p.host)}</small></span><span class=portal-actions><span class="badge ${ok?'connected':p.status.startsWith('failed')?'failed':''}">${ok?'Connected':portalBusy?'Connecting…':esc(p.status.replaceAll('_',' '))}</span>${ok?`<button class=ghost onclick='unlinkAccount(${JSON.stringify(p.name)})' ${busy?'disabled':''}>Unlink</button>`:`<button onclick='googlePortal(${JSON.stringify(p.name)})' ${busy?'disabled':''}>${portalBusy?'Connecting…':'Connect with Google'}</button><button class=secondary onclick='manualPortal(${JSON.stringify(p.name)})' ${busy?'disabled':''}>Manual login</button>`}</span></div>`}).join('');
 let remaining=c.portals.filter(p=>!connected(p.status)).length,x=s.counts;
 connectAll.style.display=remaining&&!busy?'inline-block':'none';connectAll.textContent=`Connect remaining with Google (${remaining})`;
 cancelConnect.style.display=busy?'inline-block':'none';manualDone.style.display=(busy&&c.action.kind.startsWith('Manual login'))?'inline-block':'none';
 document.getElementById('stats').innerHTML=`<div class=stat><b>${x.applied}</b>Applied</div><div class=stat><b>${x.failed}</b>Needs attention</div><div class=stat><b>${x.in_progress}</b>Active</div><div class=stat><b>${x.ready}</b>Ready</div>`;
 let w=s.worker||{},workerLabel=w.agent&&w.agent!=='unknown'?`${w.agent==='claude'?'Claude':'Codex'} · ${w.workers||1} worker${(w.workers||1)===1?'':'s'} · ${w.browser||'browser'}`:'running';document.getElementById('current').textContent=s.current_job?`Current: ${s.current_job.title} @ ${s.current_job.site} · ${workerLabel}`:`Worker: ${s.running?workerLabel:'stopped'}`;
 renderWorkerPlan(s);
 startSelfApply.style.display=s.running?'none':'inline-flex';stopSelfApply.style.display=s.running?'inline-flex':'none';startSelfApply.disabled=s.running;startSelfApply.textContent='Start Self Apply';stopSelfApply.disabled=false;stopSelfApply.textContent='Stop Self Apply';
 let hs=document.getElementById('historyWorkerState');if(hs){hs.hidden=!s.running;if(s.running){let hc='worker-strip worker-live',hh=s.current_job?`<b>${s.current_job.starting?'Starting application':'Applying now'}</b><span>${esc(s.current_job.title)} · ${esc(s.current_job.site)}</span>`:'<b>Starting application</b><span>Preparing the selected job…</span>';if(hs.className!==hc)hs.className=hc;if(hs.innerHTML!==hh)hs.innerHTML=hh}}
 if(busy)notice.textContent=c.action.message||c.action.kind+' is running…';else notice.textContent=c.action.message||'Ready';
 let agentAction=!busy&&c.action.kind?.startsWith('Connect ')&&c.action.kind?.endsWith(' account')?`${c.action.kind}:${c.action.message}`:'';if(agentAction&&agentAction!==lastAgentAction){let guided=document.body.classList.contains('onboarding-mode');lastAgentAction=agentAction;await loadSettings();if(guided)advanceOnboarding()}
 let historyVisible=document.getElementById('applications').classList.contains('active'),refreshHistory=historyVisible&&(s.running||historyWorkerWasRunning);historyWorkerWasRunning=s.running;if(refreshHistory)await loadApplications()
}
const el=id=>document.getElementById(id),val=id=>el(id).value;
let lastSettings={},readinessKnown=false,lastAgentAction='',selectedFailureReason='',historyWorkerWasRunning=false,lastHistorySignature='',historyPageSize=20,workerRunning=false,retryableFailureCount=0;
const WORKSPACE_TABS=new Set(['overview','review-apply','applications','ats']);
const LOCATIONS={
 'United States':{'California':['San Francisco','San Jose','Oakland','Los Angeles','San Diego','Sacramento'],'New York':['New York City','Buffalo','Rochester'],'Texas':['Austin','Dallas','Houston'],'Washington':['Seattle','Bellevue'],'Oregon':['Portland','Salem','Eugene'],'Massachusetts':['Boston','Cambridge'],'Illinois':['Chicago'],'Colorado':['Denver','Boulder'],'Florida':['Miami','Orlando','Tampa'],'Virginia':['Arlington','Richmond'],'North Carolina':['Raleigh','Charlotte'],'Other':[]},
 'Canada':{'Ontario':['Toronto','Ottawa','Waterloo'],'British Columbia':['Vancouver','Victoria'],'Alberta':['Calgary','Edmonton'],'Quebec':['Montreal','Quebec City'],'Other':[]},
 'India':{'Tamil Nadu':['Chennai','Coimbatore'],'Karnataka':['Bengaluru','Mysuru'],'Maharashtra':['Mumbai','Pune'],'Telangana':['Hyderabad'],'Delhi':['New Delhi'],'Other':[]},
 'United Kingdom':{'England':['London','Manchester','Birmingham'],'Scotland':['Edinburgh','Glasgow'],'Wales':['Cardiff'],'Northern Ireland':['Belfast'],'Other':[]},
 'Australia':{'New South Wales':['Sydney'],'Victoria':['Melbourne'],'Queensland':['Brisbane'],'Western Australia':['Perth'],'Other':[]},
 'Germany':{'Berlin':['Berlin'],'Bavaria':['Munich'],'Hesse':['Frankfurt'],'Other':[]},
 'France':{'Île-de-France':['Paris'],'Auvergne-Rhône-Alpes':['Lyon'],'Other':[]},
 'Other':{'Other':[]}
};
function fillSelect(id,options,selected,otherId){let node=el(id),known=options.includes(selected);node.innerHTML=options.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('');if(otherId&&!options.includes('Other'))node.innerHTML+='<option value="Other">Other / type manually</option>';node.value=known?selected:(selected?'Other':options[0]||'Other');if(otherId){let other=el(otherId);other.style.display=node.value==='Other'?'block':'none';other.value=known?'':(selected==='Other'?'':selected||'')}}
function locationCountryChanged(stateValue='',cityValue=''){let country=val('p_country');p_country_other.style.display=country==='Other'?'block':'none';let regions=Object.keys(LOCATIONS[country]||LOCATIONS.Other);fillSelect('p_province_state',regions,stateValue,'p_province_state_other');locationStateChanged(cityValue)}
function locationStateChanged(cityValue=''){let country=val('p_country'),state=val('p_province_state');p_province_state_other.style.display=state==='Other'?'block':'none';let cities=((LOCATIONS[country]||{})[state]||[]);fillSelect('p_city',cities.length?cities:['Other'],cityValue,'p_city_other')}
function locationValue(id){return val(id)==='Other'?val(id+'_other').trim():val(id)}
function initLocation(country,state,city){fillSelect('p_country',Object.keys(LOCATIONS),country,'p_country_other');if(!country){p_country.innerHTML='<option value="">Choose country…</option>'+p_country.innerHTML;p_country.value='';p_country_other.style.display='none';fillSelect('p_province_state',['Other'],state,'p_province_state_other');fillSelect('p_city',['Other'],city,'p_city_other');return}locationCountryChanged(state,city)}
function jobLocationPart(id){return val(id)==='Other'?val(id+'_other').trim():val(id)}
function syncJobLocation(){let parts=[jobLocationPart('s_city'),jobLocationPart('s_state'),jobLocationPart('s_country')].filter(Boolean);put('s_location',parts.join(', '))}
function jobCountryChanged(stateValue='',cityValue=''){let country=val('s_country');s_country_other.style.display=country==='Other'?'block':'none';fillSelect('s_state',Object.keys(LOCATIONS[country]||LOCATIONS.Other),stateValue,'s_state_other');jobStateChanged(cityValue);syncJobLocation()}
function jobStateChanged(cityValue=''){let country=val('s_country'),state=val('s_state');s_state_other.style.display=state==='Other'?'block':'none';let cities=((LOCATIONS[country]||{})[state]||[]);fillSelect('s_city',cities.length?cities:['Other'],cityValue,'s_city_other');syncJobLocation()}
function initJobLocation(country,state,city,legacy=''){if(!country&&legacy){let parts=legacy.split(',').map(x=>x.trim()).filter(Boolean);country=parts.pop()||'';state=parts.pop()||'';city=parts.join(', ')}fillSelect('s_country',Object.keys(LOCATIONS),country,'s_country_other');if(!country){s_country.innerHTML='<option value="">Choose country…</option>'+s_country.innerHTML;s_country.value='';s_country_other.style.display='none';fillSelect('s_state',['Other'],state,'s_state_other');fillSelect('s_city',['Other'],city,'s_city_other')}else jobCountryChanged(state,city);syncJobLocation()}
function enableAnyCurrency(){let old=el('p_salary_currency');if(!old||old.tagName!=='SELECT')return;let input=document.createElement('input');input.id=old.id;input.name=old.name;input.placeholder='e.g. USD, CAD, EUR, JPY';input.setAttribute('list','currencyCodes');let list=document.createElement('datalist');list.id='currencyCodes';list.innerHTML=['USD','CAD','EUR','GBP','INR','AUD','JPY','CHF','CNY','SGD','NZD','BRL','MXN','ZAR'].map(x=>`<option value="${x}">`).join('');old.replaceWith(input);input.after(list)}
function toggleSidebar(force){let open=typeof force==='boolean'?force:!document.body.classList.contains('nav-open');document.body.classList.toggle('nav-open',open);mobileMenu.setAttribute('aria-expanded',String(open))}
function setTheme(theme){let value=theme==='dark'?'dark':'light';document.documentElement.dataset.theme=value;localStorage.setItem('selfapply-theme',value);document.querySelectorAll('.theme-option').forEach(x=>{let active=x.id===`theme${value[0].toUpperCase()+value.slice(1)}`;x.classList.toggle('active',active);x.setAttribute('aria-checked',String(active))})}
function toggleTheme(){setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark')}
function simplifyProfileSections(){let form=document.querySelector('#account>.form');if(!form||form.dataset.grouped)return;form.dataset.grouped='true';let headings=[...form.querySelectorAll(':scope>h3')];headings.forEach((heading,index)=>{let details=document.createElement('details');details.className='profile-section wide';details.open=index===0;let summary=document.createElement('summary');summary.textContent=heading.textContent.trim();let grid=document.createElement('div');grid.className='form-section-grid';let node=heading.nextSibling;while(node&&!headings.includes(node)){let next=node.nextSibling;grid.appendChild(node);node=next}details.append(summary,grid);heading.replaceWith(details)})}
function simplifyJobPreferences(){let form=document.querySelector('#find-jobs>.form');if(!form||form.dataset.grouped)return;form.dataset.grouped='true';let city=el('s_city')?.closest('label');if(city){let accepted=document.createElement('label');accepted.className='wide';accepted.innerHTML='Other accepted onsite or hybrid locations — one per line<textarea id="s_accept_locations" placeholder="e.g.&#10;Toronto&#10;Mississauga&#10;Greater Toronto Area"></textarea><span class="muted compact">Add other cities or regions where you can commute. Remote jobs are evaluated separately.</span>';city.after(accepted)}let fields=['s_distance','s_hours_old','s_results'].map(id=>el(id)?.closest('label')).filter(Boolean),anchor=fields[0];if(!anchor)return;let details=document.createElement('details');details.className='advanced wide';let summary=document.createElement('summary');summary.textContent='Search limits';let grid=document.createElement('div');grid.className='form-section-grid';anchor.before(details);details.append(summary,grid);fields.forEach(x=>grid.appendChild(x))}
function syncAcceptedLocations(){let node=el('s_accept_locations');if(node&&!node.dataset.loaded){node.value=((lastSettings.searches||{}).location?.accept_patterns||[]).join('\n');node.dataset.loaded='true'}}
function simplifyAtsAnalysis(){if(atsResult.querySelector(':scope>.analysis-details'))return;let nodes=[...atsResult.children].filter(x=>x.matches('.ats-breakdown,.ats-group'));if(!nodes.length)return;let details=document.createElement('details');details.className='analysis-details';let summary=document.createElement('summary');summary.textContent='See match details';details.append(summary);nodes[0].before(details);nodes.forEach(x=>details.appendChild(x))}
function showTab(id,push=true){if(!document.getElementById(id)?.classList.contains('tab'))id='overview';if(readinessKnown&&!lastSettings.readiness?.complete&&WORKSPACE_TABS.has(id))id='onboarding';document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.id===id));document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('active',x.dataset.tab===id));if(push&&location.hash!==`#${id}`)history.pushState({tab:id},'',`#${id}`);document.title=`${document.querySelector(`nav button[data-tab="${id}"]`)?.textContent.trim()||'SelfApply'} · SelfApply`;window.scrollTo(0,0);toggleSidebar(false);if(id==='applications')loadApplications();if(id==='ats')loadAtsJobs();if(id==='overview')loadOverview();if(id==='document-vault')loadDocuments()}
function put(id,value){let node=el(id);if(node)node.value=value??''}
function section(obj,key){return(obj&&obj[key])||{}}
async function loadSettings(){let d=await get('/api/settings'),p=d.profile||{},personal=section(p,'personal'),auth=section(p,'work_authorization'),disclosures=section(p,'employment_disclosures'),comp=section(p,'compensation'),exp=section(p,'experience'),eeo=section(p,'eeo_voluntary'),avail=section(p,'availability'),skills=section(p,'skills_boundary'),facts=section(p,'resume_facts');
 lastSettings=d;readinessKnown=true;for(let key of ['full_name','preferred_name','email','phone','address','postal_code','linkedin_url','github_url','portfolio_url','website_url'])put('p_'+key,personal[key]);initLocation(personal.country||'',personal.province_state||'',personal.city||'');syncAcceptedLocations();
 put('authorized',String(auth.legally_authorized_to_work??true));put('sponsorship',String(auth.require_sponsorship??false));put('p_work_permit_type',auth.work_permit_type);put('restrictiveCovenant','restrictive_covenant' in disclosures?String(disclosures.restrictive_covenant):'');
 for(let key of ['salary_expectation','salary_currency','salary_range_min','salary_range_max'])put('p_'+key,comp[key]);
 for(let key of ['years_of_experience_total','education_level','current_title','target_role'])put('p_'+key,exp[key]);put('p_earliest_start_date',avail.earliest_start_date);
 for(let key of ['gender','race_ethnicity','sexual_orientation','veteran_status','disability_status'])put('p_'+key,eeo[key]);
 for(let key of ['programming_languages','frameworks','tools'])put('p_'+key,(skills[key]||[]).join(', '));p_skill_expansion_enabled.checked=skills.skill_expansion_enabled??skills.transferable_enabled??true;p_allow_unverified_claims.checked=skills.allow_unverified_claims??true;let savedAggressiveness=Math.round((skills.target_aggressiveness??.5)*100);atsAggressiveness.value=savedAggressiveness;atsAggressivenessValue.textContent=savedAggressiveness+'%';for(let key of ['preserved_companies','preserved_projects','real_metrics'])put('p_'+key,(facts[key]||[]).join(', '));put('p_preserved_school',facts.preserved_school);put('p_experience_entries',(facts.experience_entries||[]).map(x=>typeof x==='object'?(x.header||''):x).filter(Boolean).join('\n'));put('p_education_entries',(facts.education_entries||[]).join('\n'));
 passwordState.textContent=d.has_job_site_password?'Saved password configured':'';
 let s=d.searches||{},defs=s.defaults||{},loc=s.location||{},disc=d.discovery||{};initJobLocation(loc.country||'',loc.state||'',loc.city||'',defs.location||'');put('s_distance',String(defs.distance??0));put('s_hours_old',String(defs.hours_old??72));put('s_results',String(defs.results_per_site??50));put('s_remote',String(!!((s.locations||[])[0]||{}).remote));let sites=new Set(s.sites||[]);document.querySelectorAll('#siteChecks input').forEach(x=>x.checked=sites.has(x.value));sourceJobspy.checked=disc.jobspy;sourceWorkday.checked=disc.workday;sourceDirect.checked=disc.direct;let direct=new Set(disc.selected_direct_sites||[]);directChecks.innerHTML=(disc.available_direct_sites||[]).map(name=>`<label><input type="checkbox" value="${esc(name)}" ${direct.has(name)?'checked':''} onchange="updateBoardCounts()"> ${esc(name)}</label>`).join('');put('s_titles',(s.queries||[]).map(x=>x.query).join('\n'));resumeState.textContent=`PDF: ${d.resume.pdf?'configured':'missing'} · Word: ${d.resume.word?'configured':'missing'} · Text: ${d.resume.text?'configured':'missing'}`;updateBoardCounts();
 if(!loc.country&&personal.country)initJobLocation(personal.country,personal.province_state||'',personal.city||'',defs.location||'');
 let a=d.ai||{};put('a_provider',a.provider||'none');put('a_local_url',a.local_url);providerChanged(a.model);let hasProviderKey=a.has_openai_key||a.has_gemini_key||a.has_anthropic_key;apiKeyState.textContent=hasProviderKey?'Saved API key configured (hidden)':'No saved API key';deleteApiKey.style.display=hasProviderKey?'inline-block':'none';capsolverState.textContent=a.has_capsolver_key?'Saved key configured (hidden)':'Not configured';deleteCapsolver.style.display=a.has_capsolver_key?'inline-block':'none';extensionState.textContent=a.has_extension_token?'Saved token configured (hidden)':'Not configured';deleteExtension.style.display=a.has_extension_token?'inline-block':'none';let agents=d.browser_agent||{};agentStates.innerHTML=['codex','claude'].map(name=>{let x=agents[name]||{},ok=x.authenticated,label=name==='codex'?'Codex':'Claude';return `<div class=agent-card><b>${label}</b><small>${esc(x.detail||'Not installed')}</small><div class=portal-actions><span class="badge ${ok?'connected':''}">${ok?'Connected':x.installed?'Needs login':'Not installed'}</span>${ok?`<button class=secondary onclick="testAgent('${name}')">Test</button><button class=danger onclick="unlinkAgent('${name}')">Unlink</button>`:x.installed?`<button onclick="post('/api/settings/browser-agent/connect',{name:'${name}'})">Connect</button>`:''}</div></div>`}).join('');renderReadiness(d);renderOnboarding(d);let requested=location.hash.slice(1)||'overview';if(!d.readiness?.complete&&WORKSPACE_TABS.has(requested))showTab('onboarding',false);}
function profileText(keys){let out={};for(let key of keys)out[key]=val('p_'+key);return out}
async function saveProfile(){let guided=document.body.classList.contains('onboarding-mode'),personal=profileText(['full_name','preferred_name','email','phone','address','postal_code','linkedin_url','github_url','portfolio_url','website_url']);personal.country=locationValue('p_country');personal.province_state=locationValue('p_province_state');personal.city=locationValue('p_city');let disclosureValue=val('restrictiveCovenant');let facts=profileText(['preserved_companies','preserved_projects','preserved_school','real_metrics','experience_entries','education_entries']);let payload={personal,work_authorization:{...profileText(['work_permit_type']),legally_authorized_to_work:val('authorized')==='true',require_sponsorship:val('sponsorship')==='true'},employment_disclosures:disclosureValue===''?{}:{restrictive_covenant:disclosureValue==='true'},compensation:profileText(['salary_expectation','salary_currency','salary_range_min','salary_range_max']),experience:profileText(['years_of_experience_total','education_level','current_title','target_role']),availability:profileText(['earliest_start_date']),eeo_voluntary:profileText(['gender','race_ethnicity','sexual_orientation','veteran_status','disability_status']),skills_boundary:{...profileText(['programming_languages','frameworks','tools']),skill_expansion_enabled:p_skill_expansion_enabled.checked,allow_unverified_claims:p_allow_unverified_claims.checked,target_aggressiveness:(atsAggressiveness.value/100)},resume_facts:facts,job_site_password:jobPassword.value};let result=await post('/api/settings/profile',payload);jobPassword.value='';await loadSettings();if(guided&&result.ok)advanceOnboarding()}
async function saveSkillTailoringPreferences(){await post('/api/settings/profile',{skills_boundary:{skill_expansion_enabled:p_skill_expansion_enabled.checked,allow_unverified_claims:p_allow_unverified_claims.checked,target_aggressiveness:(atsAggressiveness.value/100)}})}
function toggleChecks(selector,state){document.querySelectorAll(selector).forEach(x=>x.checked=state)}
async function saveJobs(){let guided=document.body.classList.contains('onboarding-mode');syncJobLocation();let result=await post('/api/settings/search',{location:val('s_location'),country:jobLocationPart('s_country'),state:jobLocationPart('s_state'),city:jobLocationPart('s_city'),primary_location:val('s_location'),accept_patterns:val('s_accept_locations'),distance:+val('s_distance'),hours_old:+val('s_hours_old'),results_per_site:+val('s_results'),remote_only:val('s_remote')==='true',source_jobspy:sourceJobspy.checked,source_workday:sourceWorkday.checked,source_direct:sourceDirect.checked,sites:[...document.querySelectorAll('#siteChecks input:checked')].map(x=>x.value),direct_sites:[...document.querySelectorAll('#directChecks input:checked')].map(x=>x.value),target_titles:val('s_titles')});await loadSettings();if(guided&&result.ok)advanceOnboarding()}
async function uploadResume(){let f=resumeFile.files[0];if(!f){notice.textContent='Select a PDF, DOCX, or TXT resume first.';return}let guided=document.body.classList.contains('onboarding-mode'),reader=new FileReader();reader.onload=async()=>{let result=await post('/api/settings/resume',{name:f.name,data:String(reader.result).split(',',2)[1]});resumeFile.value='';await loadSettings();if(guided&&result.ok)advanceOnboarding()};reader.readAsDataURL(f)}
const MODELS={auto:[],openai:['gpt-4o-mini','gpt-4o','gpt-5-mini'],anthropic:['claude-sonnet-4-5','claude-haiku-4-5'],gemini:['gemini-2.0-flash','gemini-2.5-flash'],local:['local-model'],none:[]};
function providerChanged(selected){let p=val('a_provider'),options=MODELS[p]||[];a_model_select.innerHTML=options.map(x=>`<option value="${x}">${x}</option>`).join('')+'<option value="custom">Custom…</option>';if(selected&&options.includes(selected))put('a_model_select',selected);else if(selected){put('a_model_select','custom');put('a_model_custom',selected)}apiKeyWrap.style.display=['auto','openai','gemini','anthropic'].includes(p)?'block':'none';localUrlWrap.style.display=p==='local'?'block':'none';modelChanged()}function modelChanged(){customModelWrap.style.display=val('a_model_select')==='custom'?'block':'none'}
function detectProviderKey(){let key=val('a_api_key').trim(),provider=key.startsWith('sk-ant-')?'anthropic':key.startsWith('AIza')?'gemini':key.startsWith('sk-')?'openai':'';if(provider&&val('a_provider')!==provider){put('a_provider',provider);providerChanged();apiKeyState.textContent=`Detected ${provider==='anthropic'?'Claude / Anthropic':provider[0].toUpperCase()+provider.slice(1)}. Save, then test the connection.`}}
async function refreshModels(){let r=await fetch('/api/settings/ai/models',{method:'POST',headers:{'Content-Type':'application/json','X-SelfApply-Token':token},body:'{}'}),j=await r.json();notice.textContent=j.message||j.error;if(j.models){a_model_select.innerHTML=j.models.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join('')+'<option value="custom">Custom…</option>';if(j.selected_model&&j.models.includes(j.selected_model))put('a_model_select',j.selected_model)}}
async function saveAI(){let guided=document.body.classList.contains('onboarding-mode'),model=val('a_model_select')==='custom'?val('a_model_custom'):val('a_model_select');let result=await post('/api/settings/ai',{provider:val('a_provider'),model,api_key:val('a_api_key'),local_url:val('a_local_url'),capsolver_key:val('a_capsolver'),extension_token:val('a_extension')});a_api_key.value='';a_capsolver.value='';a_extension.value='';await loadSettings();if(guided&&result.ok)advanceOnboarding()}
function renderOnboarding(d){let missing=(d.readiness?.missing||[]),groups=[['account','Profile','Tell us how to complete application forms.'],['find-jobs','Job preferences','Choose target roles, locations, and sources.'],['document-vault','Résumé','Upload the master résumé used for matching.'],['api','AI & browser','Connect a content provider and browser agent.']],complete=groups.filter(([tab])=>!missing.some(x=>x.tab===tab)).length;onboardingProgress.style.width=`${complete/groups.length*100}%`;onboardingProgressText.textContent=`${complete} of ${groups.length} essentials complete`;onboardingSteps.innerHTML=groups.map(([tab,title,copy],index)=>{let items=missing.filter(x=>x.tab===tab),done=!items.length;return `<article class="onboarding-step ${done?'done':''}"><span class="step-number">${done?'✓':index+1}</span><div><b>${esc(title)}</b><p>${esc(copy)}</p>${items.length?`<small>${items.length} required item${items.length===1?'':'s'} remaining</small>`:'<small>Complete</small>'}</div><button type="button" class="${done?'ghost':''}" onclick="showTab('${tab}')">${done?'Review':'Complete step'}</button></article>`}).join('');onboardingFinish.hidden=!d.readiness?.complete;document.body.classList.toggle('onboarding-mode',!d.readiness?.complete)}
function advanceOnboarding(){let next=(lastSettings.readiness?.missing||[])[0];if(next)showTab(next.tab);else showTab('review-apply')}
async function resetSelfApply(){let confirmation=window.prompt('This stops Self Apply and moves all local data into a private backup. Type RESET to continue.');if(confirmation===null)return;let result=await post('/api/settings/reset',{confirmation});if(result.ok){resetState.textContent=`Backup saved locally: ${result.backup}`;await loadSettings();await loadOverview();showTab('onboarding')}}
function toggleSecret(id,button){let node=el(id);node.type=node.type==='password'?'text':'password';button.textContent=node.type==='password'?'👁':'🙈'}
async function deleteSecret(kind){if(!window.confirm(`Delete the saved ${kind} secret?`))return;await post('/api/settings/secret/delete',{kind});await loadSettings()}
function testAgent(name){post('/api/settings/browser-agent/test',{name})}async function unlinkAgent(name){if(!window.confirm(`Unlink ${name==='codex'?'Codex':'Claude'} from SelfApply?`))return;await post('/api/settings/browser-agent/unlink',{name});await loadSettings()}
function updateBoardCounts(){let major=document.querySelectorAll('#siteChecks input'),direct=document.querySelectorAll('#directChecks input');majorCount.textContent=`${[...major].filter(x=>x.checked).length}/${major.length} selected`;directCount.textContent=`${[...direct].filter(x=>x.checked).length}/${direct.length} selected`}
function historyDate(value){if(!value)return 'Date unavailable';let date=new Date(value);return Number.isNaN(date.valueOf())?value:date.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function duration(ms){if(!ms)return '—';let seconds=Math.round(ms/1000);return seconds<60?`${seconds}s`:`${Math.floor(seconds/60)}m ${seconds%60}s`}
function renderWorkerPlan(s){let ready=Number(s?.counts?.ready||0),threshold=Number(queue.value||50),count=Number(workers.value||1),searching=ready<threshold;workerPlan.innerHTML=`<div class="plan-step ${s?.running?'active':''}"><b>1. Apply ready jobs</b><span>${ready} ready now · ${count} worker${count===1?'':'s'}</span></div><div class="plan-step"><b>2. Prepare saved jobs</b><span>Runs quietly in the background</span></div><div class="plan-step ${searching?'next':''}"><b>3. Find new jobs</b><span>${searching?'Ready supply is below '+threshold+' — discovery will run':'Starts when ready jobs fall below '+threshold}</span></div>`}
function resetHistoryList(){historyPageSize=20;lastHistorySignature='';loadApplications()}
function setHistoryStatus(status){selectedFailureReason='';put('historyStatus',status);resetHistoryList()}
function historyStatusChanged(){if(val('historyStatus')!=='failed')selectedFailureReason='';resetHistoryList()}
function filterFailureReason(reason){selectedFailureReason=selectedFailureReason===reason?'':reason;if(selectedFailureReason)put('historyStatus','failed');resetHistoryList()}
let atsJobsLoaded=false;
async function loadAtsJobs(force=false){if(atsJobsLoaded&&!force)return;let d=await get('/api/ats/jobs'),jobs=d.jobs||[],current=atsJob.value;atsJob.innerHTML='<option value="">Choose a stored job…</option>'+jobs.map(x=>`<option value="${esc(x.url)}">${esc(x.title)} · ${esc(x.employer)} · fit ${x.fit_score??'—'}${x.has_tailored?' · tailored':''}</option>`).join('');if(current&&jobs.some(x=>x.url===current))atsJob.value=current;atsJobsLoaded=true}
function atsChips(title,items,kind=''){return `<div class="ats-group"><b>${title} <span>${items.length}</span></b><div class="ats-chips">${items.length?items.slice(0,24).map(x=>`<span class="ats-chip ${kind}">${esc(x)}</span>`).join(''):'<small class="muted">None</small>'}</div></div>`}
function atsPayload(){return {url:atsJob.value,job_link:val('atsJobLink'),title:val('atsJobTitle'),company:val('atsCompany'),job_description:val('atsJobDescription'),resume_source:atsResumeSource.value,target_aggressiveness:(atsAggressiveness.value/100)}}
function atsScoreCard(label,r){let score=Number(r?.overall_score||0),tone=score>=75?'score-good':score>=55?'score-mid':'score-low';return `<div class="ats-score-card"><div class="ats-ring ${tone}"><b>${score.toFixed(0)}</b><span>/100</span></div><div><h3>${esc(label)}</h3><p>${esc(r?.disclaimer||'Estimated ATS match.')}</p></div></div>`}
function atsFailure(data){let report=data.report||{},details=[...(report.validator?.errors||[]),...(report.full_validator?.errors||[])];if(report.judge?.issues&&report.judge.issues!=='none')details.push(report.judge.issues);let message=data.error||data.message||'The tailored resume could not be created.';atsResult.innerHTML=`<div class="ats-error"><b>Resume was not created</b><p>${esc(message)}</p>${details.length?`<ul>${details.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}<small>Check API & AI → Test saved provider, correct the issue, and try again.</small></div>`}
function renderAtsProgress(task){let pct=Math.max(2,Math.min(100,Number(task.percent||2))),elapsed=Number(task.elapsed_seconds||0).toFixed(0);atsResult.innerHTML=`<div class="ats-progress"><div class="progress-head"><b>${esc(task.stage||'Working…')}</b><span>${elapsed}s elapsed</span></div><div class="progress-track"><i style="width:${pct}%"></i></div><small>${pct}% · Keep this page open. Tailoring continues in the background.</small></div>`}
function renderAtsResult(data){let r=data.result||data.report?.ats_match;if(!r){atsFailure(data);return}let s=r.sub_scores||{},report=data.report||{},transfer=(report.transferable_skills||[]).map(x=>`${x.skill} · ${x.similarity}% similar to ${x.supported_by}`),learning=report.target_learning_skills||[],confirmed=report.confirmed_experience_skills||[],introduced=report.introduced_skills||[],unverified=(report.unverified_claim_warnings||[]),introducedWarning=report.unverified_claims_allowed?`<div class="ats-warning"><b>Creative tailoring is enabled</b><span>${esc(unverified.join(' ')||'This draft may include skills or responsibilities not supported by the master resume.')} Review the generated resume before using it.</span></div>`:'',fallback=data.used_master_fallback?`<div class="ats-warning"><b>Safe master-based version used</b><span>Unsafe AI rewrites were rejected. The download preserves your history and uses only transparent skill placement.</span></div>`:'',comparison=data.master_result?`<div class="ats-comparison">${atsScoreCard('Master resume',data.master_result)}${atsScoreCard(data.used_master_fallback?'Selected safe version':'Tailored resume',r)}</div>`:atsScoreCard(data.title||'ATS match',r),downloads=data.downloads?`<div class="ats-downloads"><b>Resume ready for manual application</b><div class="actions">${data.downloads.pdf?`<a class="link-button" href="${esc(data.downloads.pdf)}" download>Download PDF</a>`:''}${data.downloads.txt?`<a class="link-button secondary" href="${esc(data.downloads.txt)}" download>Download TXT</a>`:''}</div></div>`:'';atsResult.innerHTML=`${introducedWarning}${fallback}${comparison}<div class="ats-breakdown"><div><b>${s.keyword_match??0}%</b><span>Selected keyword match</span></div><div><b>${s.skills_coverage??0}%</b><span>Selected skills coverage</span></div><div><b>${r.factual_ceiling_score??r.overall_score??0}</b><span>Evidence-aware score ceiling</span></div></div>${downloads}${confirmed.length?atsChips('User-confirmed real experience',confirmed,'matched'):''}${transfer.length?atsChips('Related technologies added',transfer,'injectable'):''}${learning.length?atsChips('New JD technologies added',learning,'unsupported'):''}${atsChips('Matched terms',r.matched_keywords||[],'matched')}${atsChips('Safe to add from master resume',r.injectable_keywords||[],'injectable')}${atsChips('Missing and unsupported',r.unsupported_keywords||[],'unsupported')}<div class="ats-group"><b>Recommendations</b><ul>${(r.recommendations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`}
async function pollAtsTask(id){for(;;){let task=await get('/api/ats/task?id='+encodeURIComponent(id));if(task.error){atsFailure(task);return}renderAtsProgress(task);if(task.status==='completed'){renderAtsResult(task.result||{});return}if(task.status==='failed'){atsFailure(task.result||{error:task.error||'Tailoring failed.'});return}await new Promise(resolve=>setTimeout(resolve,1000))}}
async function analyzeAts(){analyzeAtsButton.disabled=true;atsResult.innerHTML='<div class="empty">Reading the job description and analyzing your resume…</div>';try{renderAtsResult(await post('/api/ats/analyze',atsPayload()))}finally{analyzeAtsButton.disabled=false}}
async function tailorAndScore(){let p=atsPayload();if(!p.url&&!p.job_link&&!p.job_description.trim()){notice.textContent='Select a discovered job, paste a job link, or paste the full JD.';return}analyzeAtsButton.disabled=true;tailorAtsButton.disabled=true;renderAtsProgress({stage:'Starting whole-resume tailoring',percent:2,elapsed_seconds:0});try{await post('/api/settings/profile',{skills_boundary:{target_aggressiveness:p.target_aggressiveness}});let started=await post('/api/ats/tailor',p);if(!started.task_id){atsFailure(started);return}await pollAtsTask(started.task_id);atsJobsLoaded=false;await loadAtsJobs(true)}catch(error){atsFailure({error:error.message||String(error)})}finally{analyzeAtsButton.disabled=false;tailorAtsButton.disabled=false}}
function historyActions(x){let u=encodeURIComponent(x.url),t=encodeURIComponent(x.title||'this job'),open=`<a class="link-button secondary" href="${esc(x.job_url)}" target="_blank" rel="noopener">${x.status==='applied'?'View job':'Apply myself'}</a>`,resume=x.resume_url?`<a class="link-button secondary" href="${esc(x.resume_url)}" target="_blank" rel="noopener">View résumé</a>`:'';if(x.status==='running')return `<button class="danger" onclick="cancelHistoryWorker()">Stop</button>${open}`;if(x.status==='dismissed')return `<button class="secondary" onclick="restoreApplication(decodeURIComponent('${u}'))">Show again</button>${open}`;if(x.status==='applied')return `${resume}${open}`;let retry='';if(x.can_run_now&&x.status==='failed')retry=workerRunning?`<button onclick="queueApplication(decodeURIComponent('${u}'))">Add next</button>`:`<button onclick="retryApplication(decodeURIComponent('${u}'),decodeURIComponent('${t}'))">Retry</button>`;else if(x.can_run_now&&x.status==='queued'&&!workerRunning)retry=`<button onclick="retryApplication(decodeURIComponent('${u}'),decodeURIComponent('${t}'))">Apply now</button>`;return `${retry}${open}`}
function historyStatusLabel(status){return status==='applied'?'Applied':status==='ineligible'?'Not eligible':status==='failed'?'Needs attention':status==='running'?'Applying now':status==='dismissed'?'Hidden':'Waiting to apply'}
function sortHistoryRows(rows){let mode=val('historySort');return [...rows].sort((a,b)=>{if(mode==='oldest')return String(a.date||'').localeCompare(String(b.date||''));if(mode==='score')return (b.fit_score??-1)-(a.fit_score??-1);if(mode==='title')return String(a.title||'').localeCompare(String(b.title||''));if(mode==='status')return historyStatusLabel(a.status).localeCompare(historyStatusLabel(b.status));return String(b.date||'').localeCompare(String(a.date||''))})}
async function loadApplications(){
 if(!applicationRows.children.length)applicationRows.innerHTML='<div class="empty">Loading application history…</div>';
 let active=val('historyStatus'),q=new URLSearchParams({start:val('historyStart'),end:val('historyEnd'),status:active}),d=await get('/api/applications?'+q),counts=d.counts||{};
 failurePanel.hidden=active!=='failed';
 let reasons=d.failure_reasons||[],rows=d.rows||[];if(selectedFailureReason)rows=rows.filter(x=>x.reason_category===selectedFailureReason);rows=sortHistoryRows(rows);let totalRows=rows.length;rows=rows.slice(0,historyPageSize);
 let signature=JSON.stringify({active,sort:val('historySort'),start:val('historyStart'),end:val('historyEnd'),selectedFailureReason,counts,reasons,rows});if(signature===lastHistorySignature)return;lastHistorySignature=signature;
 retryableFailureCount=counts.retryable||0;historyStats.innerHTML=`<button class="stat filter-stat ${active==='all'?'active':''}" onclick="setHistoryStatus('all')"><b>${d.all_total||0}</b>All</button><button class="stat filter-stat ${active==='applied'?'active':''}" onclick="setHistoryStatus('applied')"><b>${counts.applied||0}</b>Applied</button><button class="stat filter-stat ${active==='failed'?'active':''}" onclick="setHistoryStatus('failed')"><b>${counts.failed||0}</b>Needs attention<small>${retryableFailureCount?retryableFailureCount+' can retry':'Manual review'}</small></button><button class="stat filter-stat ${active==='ineligible'?'active':''}" onclick="setHistoryStatus('ineligible')"><b>${counts.ineligible||0}</b>Not eligible</button>${counts.running?`<button class="stat filter-stat ${active==='running'?'active':''}" onclick="setHistoryStatus('running')"><b>${counts.running}</b>Applying now</button>`:''}`;
 retryAllPanel.hidden=active!=='failed'||!retryableFailureCount;retryAllButton.textContent=`Retry ${retryableFailureCount} failed application${retryableFailureCount===1?'':'s'}`;
 let max=Math.max(1,...reasons.map(x=>x.count));failureChart.innerHTML=reasons.length?reasons.map(x=>`<button class="chart-row ${selectedFailureReason===x.reason?'active':''}" onclick="filterFailureReason(decodeURIComponent('${encodeURIComponent(x.reason)}'))"><span>${esc(x.reason)}</span><div class="chart-track"><i style="width:${Math.max(7,x.count/max*100)}%"></i></div><b>${x.count}</b></button>`).join(''):'<div class="empty">No failures in this date range.</div>';
 applicationRows.innerHTML=rows.length?rows.map(x=>{let label=historyStatusLabel(x.status),badge=x.status==='applied'?'connected':x.status==='ineligible'?'ineligible-badge':x.status==='failed'?'failed':x.status==='running'?'running-badge':'';let reason=x.reason_category&&x.status!=='applied'&&x.status!=='queued'?`<span class="reason-inline">${esc(x.reason_category)}</span>`:'';return `<article class="application-card compact-card"><div class="application-main"><div class="application-copy"><a href="${esc(x.job_url)}" target="_blank" rel="noopener">${esc(x.title||'Untitled job')}</a><small>${esc(x.employer)} · ${esc(x.location||'Location not listed')} · ${esc(historyDate(x.date))}</small>${reason}</div><span class="badge ${badge}">${label}</span><div class="compact-actions">${historyActions(x)}</div></div><details class="application-detail"><summary>Why this status?</summary><div class="process-line"><span><b>Fit score</b>${x.fit_score??'—'}</span><span><b>Attempts</b>${x.apply_attempts??0}</span><span><b>Time</b>${duration(x.apply_duration_ms)}</span><span><b>Status</b>${label}</span></div><p>${esc(x.reason_explanation)}</p>${x.apply_error?`<p class="technical"><b>Technical detail:</b> ${esc(x.apply_error)}</p>`:''}<button class="ghost mini" onclick="dismissApplication(decodeURIComponent('${encodeURIComponent(x.url)}'),decodeURIComponent('${encodeURIComponent(x.title||'this job')}'))">Hide from this list</button></details></article>`}).join('')+(totalRows>rows.length?`<button class="show-more" onclick="historyPageSize+=20;lastHistorySignature='';loadApplications()">Show more <span>${rows.length} of ${totalRows}</span></button>`:''):'<div class="empty">No applications match these filters.</div>'
}
function retryWorkerPayload(url){return {url,max_spend:+spend.value,target_applied:+target.value,workers:+workers.value,queue_threshold:+queue.value,max_actions:+actions.value,max_minutes:+minutes.value,agent:val('applyAgent'),browser:val('browserMode'),confirmed:true}}
async function retryApplication(url,title){if(!window.confirm(`Apply to ${title} now? SelfApply will run only this job.`))return;let result=await post('/api/applications/retry-now',retryWorkerPayload(url));if(result.ok)await loadApplications()}
async function queueApplication(url){let result=await post('/api/applications/retry',{url});if(result.ok)await loadApplications()}
async function retryAllApplications(){let count=+workers.value;if(!retryableFailureCount)return;if(!window.confirm(`Retry ${retryableFailureCount} failed application${retryableFailureCount===1?'':'s'} using ${count} worker${count===1?'':'s'}? Applied and ineligible jobs will be skipped.`))return;retryAllButton.disabled=true;retryAllButton.textContent=workerRunning?'Adding retries…':'Starting workers…';let result=await post('/api/applications/retry-all',retryWorkerPayload(''));retryAllButton.disabled=false;if(result.ok)await loadApplications()}
async function cancelHistoryWorker(){if(!window.confirm('Stop the current application? You can try it again later.'))return;let result=await post('/api/worker/stop',{});if(result.ok)await loadApplications()}
async function dismissApplication(url,title){if(!window.confirm(`Hide ${title} from this list?`))return;let result=await post('/api/applications/dismiss',{url});if(result.ok)await loadApplications()}
async function restoreApplication(url){let result=await post('/api/applications/restore',{url});if(result.ok)await loadApplications()}
function renderReadiness(d){document.querySelectorAll('.invalid').forEach(x=>x.classList.remove('invalid'));document.querySelectorAll('nav button').forEach(x=>x.classList.remove('incomplete'));let missing=[...((d.readiness||{}).missing||[])],agent=val('applyAgent'),browser=val('browserMode'),agents=d.browser_agent||{},a=d.ai||{};if(!agents[agent]?.authenticated&&!missing.some(x=>x.field==='agentStates'))missing.push({label:`${agent==='codex'?'Codex':'Claude'} browser connection`,tab:'api',field:'agentStates'});if(browser==='existing'&&!a.has_extension_token&&!missing.some(x=>x.field==='a_extension'))missing.push({label:'Chrome extension token',tab:'api',field:'a_extension'});for(let item of missing){let field=el(item.field);field?.classList.add('invalid');field?.closest('details')?.setAttribute('open','');if(item.label==='Review contradictory work-authorization answers')el('sponsorship')?.classList.add('invalid');let navTab=item.tab==='api'?'connected-accounts':item.tab==='document-vault'?'ats':item.tab;document.querySelector(`nav button[data-tab="${navTab}"]`)?.classList.add('incomplete')}let complete=missing.length===0,heading=readiness.querySelector('b');readiness.className=`readiness ${complete?'complete':'incomplete'}`;heading.textContent=complete?'Setup complete':'Setup incomplete';if(complete)readinessText.textContent='Ready to start Self Apply.';else{let groups={account:0,'find-jobs':0,'document-vault':0,api:0},names={account:'Profile','find-jobs':'Job preferences','document-vault':'Resume tools',api:'Connections'};for(let item of missing)groups[item.tab]=(groups[item.tab]||0)+1;readinessText.innerHTML=Object.entries(groups).filter(([,count])=>count).map(([tab,count])=>`<button class="missing-link" onclick="showTab('${tab}')">${esc(names[tab])}: ${count} missing</button>`).join('')}startSelfApply.disabled=!complete;return complete}
function googlePortal(n){post('/api/connections/portal',{name:n,browser:val('connectionBrowser')})}function manualPortal(n){post('/api/connections/manual',{name:n})}function unlinkAccount(n){post('/api/connections/unlink',{name:n})}async function startWorker(){if(!renderReadiness(lastSettings)){notice.textContent='Complete the red required fields before starting Self Apply.';return}let authorization=el('confirm');if(!authorization.checked){notice.textContent='Check the authorization box before starting Self Apply.';authorization.focus();return}let label=startSelfApply.textContent;startSelfApply.disabled=true;startSelfApply.textContent='Starting…';notice.textContent='Starting Self Apply…';let result=await post('/api/worker/start',{max_spend:+spend.value,target_applied:+target.value,workers:+workers.value,queue_threshold:+queue.value,max_actions:+actions.value,max_minutes:+minutes.value,agent:val('applyAgent'),browser:val('browserMode'),confirmed:true});if(!result.ok){startSelfApply.disabled=false;startSelfApply.textContent=label}}
async function stopWorker(){if(!window.confirm('Stop Self Apply after releasing its current job safely?'))return;stopSelfApply.disabled=true;stopSelfApply.textContent='Stopping…';let result=await post('/api/worker/stop',{});if(!result.ok){stopSelfApply.disabled=false;stopSelfApply.textContent='Stop Self Apply'}}
function docDate(value){if(!value)return '';let date=new Date(value);return Number.isNaN(date.valueOf())?'':date.toLocaleDateString([], {year:'numeric',month:'short',day:'numeric'})}
async function loadDocuments(){documentList.innerHTML='<div class="empty">Loading…</div>';let d=await get('/api/documents'),docs=d.documents||[];documentList.innerHTML=docs.length?docs.map(x=>`<article class="doc-card"><div><b>${esc(x.name)}</b><small>${esc(docDate(x.created))}</small></div><div class="actions">${x.pdf?`<a class="link-button" href="${esc(x.pdf)}" download>Download PDF</a>`:''}<a class="link-button secondary" href="${esc(x.txt)}" download>Download TXT</a></div></article>`).join(''):'<div class="empty">No tailored resumes yet. Create one from Job matches.</div>'}
function statCard(label,value){return `<div class="stat-card"><span>${esc(label)}</span><b>${value}</b></div>`}
function recentAppliedCard(job){let url=String(job.job_url||''),safe=/^https?:\/\//i.test(url)?url:'#',date=historyDate(job.applied_at),employer=String(job.employer||'').trim();return `<a class="recent-application" href="${esc(safe)}" ${safe==='#'?'':'target="_blank" rel="noopener"'}><span class="success-mark" aria-hidden="true">✓</span><span class="recent-copy"><b>${esc(job.title||'Untitled role')}</b>${employer?`<small>${esc(employer)}</small>`:''}<em>${esc(date||job.location||'Applied')}</em></span><span class="recent-arrow" aria-hidden="true">↗</span></a>`}
function renderRecentApplied(jobs){if(!jobs.length){recentApplied.innerHTML='<div class="empty overview-empty">No submitted applications yet. Ready roles will appear here after Self Apply sends them.</div>';return}let cards=jobs.map(recentAppliedCard).join(''),scroll=jobs.length>3;recentApplied.innerHTML=`<div class="recent-applied-track ${scroll?'is-scrolling':''}"><div class="recent-applied-set">${cards}</div>${scroll?`<div class="recent-applied-set" aria-hidden="true">${cards}</div>`:''}</div>`;recentApplied.querySelectorAll('.recent-applied-set[aria-hidden] a').forEach(link=>link.tabIndex=-1)}
async function loadOverview(){
 let s=await get('/api/status'),x=s.counts||{},needsAttention=Number(x.failed||0);
 overviewStats.innerHTML=[
  statCard('Applied',x.applied||0),
  statCard('Ready to apply',x.ready||0),
  statCard('Needs attention',needsAttention),
  statCard('Not eligible',x.ineligible||0),
 ].join('');
 overviewUpdated.textContent=`Updated ${new Date().toLocaleString([], {hour:'numeric',minute:'2-digit',month:'short',day:'numeric'})} · ${s.running?'worker running':'worker idle'}`;
 let daily=await get('/api/status/daily'),days=daily.days||[],max=Math.max(1,...days.map(d=>d.applied));
 dailyChart.innerHTML=days.map(d=>`<div class="day-col" title="${esc(d.label)}: ${d.applied} applied"><b class="day-value">${d.applied}</b><div class="day-bars"><i class="bar bar-green" style="height:${d.applied/max*100}%"></i></div><small>${esc(d.label)}</small></div>`).join('');
 renderRecentApplied(daily.recent_applied||[]);
 let p=(lastSettings.profile||{}),personal=p.personal||{},exp=p.experience||{},skills=p.skills_boundary||{},facts=p.resume_facts||{},allSkills=[...(skills.programming_languages||[]),...(skills.frameworks||[]),...(skills.tools||[])];
 profileCard.innerHTML=personal.full_name?`<span class="eyebrow">Candidate profile</span><h3>${esc(personal.full_name)}</h3><p class="profile-role">${esc(exp.current_title||exp.target_role||'Open to opportunities')}</p><p class="muted compact">${esc([personal.city,personal.province_state,personal.country].filter(Boolean).join(', '))}</p><div class="chip-row">${allSkills.slice(0,8).map(sk=>`<span class="chip">${esc(sk)}</span>`).join('')}</div><div class="profile-meta"><span><b>${esc(exp.years_of_experience_total||'—')}</b>Years</span><span><b>${(facts.preserved_companies||[]).length}</b>Companies</span><span><b>${(facts.preserved_projects||[]).length}</b>Projects</span></div><button class="ghost profile-edit" onclick="showTab('account')">Edit profile</button>`:'<span class="eyebrow">Candidate profile</span><h3>Complete your profile</h3><p class="muted">Add your experience and skills to improve job matching.</p><button onclick="showTab(\'account\')">Complete profile</button>';
}
connectionBrowser.value=localStorage.getItem('selfapply-connection-browser')||'existing';applyAgent.onchange=()=>renderReadiness(lastSettings);browserMode.onchange=()=>renderReadiness(lastSettings);workers.onchange=()=>refresh();queue.oninput=()=>refresh();window.addEventListener('popstate',()=>showTab(location.hash.slice(1)||'overview',false));enableAnyCurrency();simplifyProfileSections();simplifyJobPreferences();new MutationObserver(simplifyAtsAnalysis).observe(atsResult,{childList:true});setTheme(document.documentElement.dataset.theme);loadSettings().then(()=>{showTab(location.hash.slice(1)||'overview',false);loadOverview();document.body.classList.remove('booting')}).catch(error=>{notice.textContent=`Could not load local settings: ${error.message||error}`;document.body.classList.remove('booting')});refresh();setInterval(refresh,3000)
</script></body></html>'''
_UI_COPY_REPLACEMENTS = {
    '<section id="find-jobs" class="tab card settings"><h2>Find jobs</h2><p class="muted">Choose where to search without keeping every board visible.</p>':
        '<section id="find-jobs" class="tab card settings"><h2>Job preferences</h2><p class="muted">Choose the roles and locations you want.</p>',
    '<div class="history-head"><div><h2>Application history</h2><p class="muted">Compact status, clear actions, and one worker for one selected retry.</p>':
        '<div class="history-head"><div><h2>Applications</h2><p class="muted">Track results and retry jobs that need attention.</p>',
    '<section id="account" class="tab card settings"><h2>Account & profile</h2><p class="muted">These values power form auto-fill and resume tailoring. Changes save to profile.json.</p>':
        '<section id="account" class="tab card settings"><h2>Profile</h2><p class="muted">The information used to complete applications and tailor résumés.</p>',
    '<section id="document-vault" class="tab card settings"><h2>Document vault</h2><p class="muted">Your master resume, plus every tailored resume SelfApply has generated for a specific job.</p>':
        '<section id="document-vault" class="tab card settings"><div class="page-title-row"><div><h2>Resume files</h2><p class="muted">Your master résumé and generated versions.</p></div><button class="secondary" onclick="showTab(\'ats\')">Back to resume tools</button></div>',
    '<section id="api" class="tab card settings"><h2>API & AI</h2><p class="muted">The content provider scores jobs and tailors resumes. Saved secrets stay masked; the eye button shows only what you are currently typing.</p>':
        '<section id="api" class="tab card settings"><div class="page-title-row"><div><h2>AI & browser settings</h2><p class="muted">Configure the services SelfApply uses.</p></div><button class="secondary" onclick="showTab(\'connected-accounts\')">Back to connections</button></div>',
}
for _old_copy, _new_copy in _UI_COPY_REPLACEMENTS.items():
    HTML = HTML.replace(_old_copy, _new_copy)

HTML = HTML.replace(
    "The content provider scores jobs and tailors resumes. Secrets are write-only",
    "The content provider scores jobs and tailors resumes. It needs an API key, "
    "except for a local server. Secrets are write-only",
).replace(
    "Test saved provider</button>",
    "Test saved provider</button><button class=\"ghost\" "
    "onclick=\"refreshModels()\">Load available models</button>",
).replace(
    'id="agentStates" class="row"', 'id="agentStates" class="agent-grid"',
)
# Keep the production visual layer separate from the local API/controller.
_CONTROL_CENTER_CSS = (
    Path(__file__).with_name("ui") / "control_center.css"
).read_text(encoding="utf-8")
HTML = HTML.replace(
    "</head>", f'<style id="selfapply-production-ui">{_CONTROL_CENTER_CSS}</style></head>', 1,
)


def make_handler(controller: Controller, token: str):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: dict, status: int = 200) -> None:
            body = json.dumps(value).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body)

        def _download(self, name: str) -> None:
            if not name or Path(name).name != name or Path(name).suffix.casefold() not in {".txt", ".pdf"}:
                self._json({"error": "Invalid download."}, 400); return
            path = (config.TAILORED_DIR / name).resolve()
            if not path.is_relative_to(config.TAILORED_DIR.resolve()) or not path.is_file():
                self._json({"error": "Download not found."}, 404); return
            body = path.read_bytes()
            content_type = "application/pdf" if path.suffix.casefold() == ".pdf" else "text/plain; charset=utf-8"
            self.send_response(200); self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(body)

        def _application_resume(self, name: str, kind: str) -> None:
            roots = {
                "applied": config.APPLIED_RESUME_DIR,
                "tailored": config.TAILORED_DIR,
            }
            root = roots.get(kind)
            if (root is None or not name or Path(name).name != name
                    or Path(name).suffix.casefold() != ".pdf"):
                self._json({"error": "Invalid resume."}, 400); return
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                self._json({"error": "Resume not found."}, 404); return
            body = path.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "private, no-store")
            self.end_headers(); self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = HTML.replace("__TOKEN__", token).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body); return
            if parsed.path == "/api/status": self._json(database_status()); return
            if parsed.path == "/api/status/daily": self._json(daily_activity_stats()); return
            if parsed.path == "/api/documents": self._json(document_vault_files()); return
            if parsed.path == "/api/connections": self._json(controller.connections()); return
            if parsed.path == "/api/settings": self._json(settings_snapshot()); return
            if parsed.path == "/api/ats/jobs": self._json(ats_jobs()); return
            if parsed.path == "/api/ats/task":
                try:
                    self._json(ats_tailor_task(parse_qs(parsed.query).get("id", [""])[0]))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/ats/download":
                self._download(parse_qs(parsed.query).get("name", [""])[0]); return
            if parsed.path == "/api/applications":
                query = parse_qs(parsed.query)
                try:
                    result = application_history(
                        query.get("start", [""])[0], query.get("end", [""])[0],
                        query.get("status", ["all"])[0],
                    )
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400); return
                self._json(result); return
            if parsed.path == "/api/applications/resume":
                query = parse_qs(parsed.query)
                self._application_resume(
                    query.get("name", [""])[0], query.get("kind", [""])[0],
                ); return
            self._json({"error": "Not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not secrets.compare_digest(self.headers.get("X-SelfApply-Token", ""), token):
                self._json({"error": "Invalid control token."}, 403); return
            try:
                request_limit = 14 * 1024 * 1024 if self.path == "/api/settings/resume" else 64 * 1024
                declared = int(self.headers.get("Content-Length", "0"))
                if declared > request_limit:
                    self._json({"error": "Request is too large."}, 413); return
                length = min(declared, request_limit)
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/worker/start": result = start_worker(payload)
                elif self.path == "/api/worker/stop": result = stop_worker()
                elif self.path == "/api/connections/google": result = controller.connect_google(str(payload.get("browser", "managed")))
                elif self.path == "/api/connections/portal": result = controller.test_portal(str(payload.get("name", "")), str(payload.get("browser", "managed")))
                elif self.path == "/api/connections/all": result = controller.connect_all(str(payload.get("browser", "managed")))
                elif self.path == "/api/connections/manual": result = controller.manual_login(str(payload.get("name", "")))
                elif self.path == "/api/connections/manual/done": result = controller.finish_manual_login()
                elif self.path == "/api/connections/cancel": result = controller.cancel_connection_action()
                elif self.path == "/api/connections/unlink": result = controller.unlink(str(payload.get("name", "")))
                elif self.path == "/api/settings/profile": result = save_profile_settings(payload)
                elif self.path == "/api/settings/search": result = save_search_settings(payload)
                elif self.path == "/api/settings/ai": result = save_ai_settings(payload)
                elif self.path == "/api/settings/ai/test": result = test_ai_provider()
                elif self.path == "/api/settings/ai/models": result = list_ai_models()
                elif self.path == "/api/settings/secret/delete": result = delete_saved_secret(str(payload.get("kind", "")))
                elif self.path == "/api/settings/reset": result = reset_local_data(payload)
                elif self.path == "/api/settings/resume": result = save_resume(payload)
                elif self.path == "/api/settings/browser-agent/connect": result = controller.connect_browser_agent(str(payload.get("name", "")))
                elif self.path == "/api/settings/browser-agent/test": result = controller.test_browser_agent(str(payload.get("name", "")))
                elif self.path == "/api/settings/browser-agent/unlink": result = controller.unlink_browser_agent(str(payload.get("name", "")))
                elif self.path == "/api/applications/retry": result = queue_failed_retry(str(payload.get("url", "")))
                elif self.path == "/api/applications/retry-selected": result = queue_failed_retries(payload.get("urls"))
                elif self.path == "/api/applications/retry-now": result = start_application_retry(payload)
                elif self.path == "/api/applications/retry-all": result = retry_all_applications(payload)
                elif self.path == "/api/applications/dismiss": result = dismiss_application(str(payload.get("url", "")))
                elif self.path == "/api/applications/restore": result = restore_application(str(payload.get("url", "")))
                elif self.path == "/api/ats/analyze": result = analyze_job_ats(payload)
                elif self.path == "/api/ats/tailor": result = start_ats_tailor_task(payload)
                else: self._json({"error": "Not found"}, 404); return
                self._json(result)
            except (
                ValueError, RuntimeError, json.JSONDecodeError,
                OSError, subprocess.TimeoutExpired,
            ) as exc:
                self._json({"error": str(exc)}, 400)

        def log_message(self, format: str, *args) -> None:
            return
    return Handler


def serve(port: int = 8766, open_browser: bool = True) -> None:
    # launcher installs process signal handlers at import time, so load it once
    # on the main thread before request/action threads use its helpers.
    from selfapply.apply import launcher as _launcher  # noqa: F401

    config.ensure_dirs(); database.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Controller(), control_token()))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"SelfApply Control Center: {url}")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
