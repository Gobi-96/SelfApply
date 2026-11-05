"""Local Connection Center and guarded Self Apply controls."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import platform
import plistlib
import re
import secrets
import subprocess
import threading
import time
import webbrowser
import base64
import copy
import io
import importlib.util
import shutil
import sys
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

from selfapply import config, database, profiles

log = logging.getLogger(__name__)

LABEL = "io.selfapply.worker"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
CONTROL_TOKEN_PATH = config.ROOT_DIR / "control-token"
WORKER_STATE_PATH = config.APP_DIR / "worker-service.json"
EXTENSION_TEST_PATH = config.APP_DIR / "chrome-extension-test.json"
BROWSER_AGENT_LINKS_PATH = config.APP_DIR / "browser-agent-links.json"

BASE_PORTALS = {
    "Dice": "https://www.dice.com/dashboard/login",
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
            "extension_verified": extension_connection_verified(env),
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
        "browser_runtime": browser_runtime_status(),
    }
    snapshot["login_connections"] = login_connection_status(searches, snapshot["discovery"])
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
    logins = snapshot.get("login_connections")
    if logins is not None:
        if not logins.get("google_verified"):
            missing.append({
                "label": "Google sign-in", "tab": "connected-accounts", "field": "google",
            })
    return {"complete": not missing, "missing": missing}


def browser_agent_status() -> dict:
    links = _read_json(BROWSER_AGENT_LINKS_PATH)
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
        linked = bool(links.get(name))
        result[name] = {
            "installed": installed,
            "authenticated": authenticated and linked,
            "cli_authenticated": authenticated,
            "linked": linked,
            "detail": (
                detail if authenticated and linked else
                "Signed in to CLI; link to SelfApply required" if authenticated else detail
            ),
        }
    return result


def browser_runtime_status() -> dict:
    """Check every local dependency needed by the Playwright apply runtime."""
    python_package = importlib.util.find_spec("playwright.sync_api") is not None
    node_runner = bool(shutil.which("npx"))
    chrome = False
    chrome_detail = "Chrome or Chromium is not installed"
    try:
        chrome_detail = str(config.get_chrome_path())
        chrome = True
    except (FileNotFoundError, OSError):
        pass

    chromium = False
    chromium_detail = "Playwright Chromium is not installed"
    if python_package:
        try:
            checked = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            locations = re.findall(r"Install location:\s+(.+)", checked.stdout)
            installed = next((Path(value) for value in locations if Path(value).exists()), None)
            if checked.returncode == 0 and installed:
                chromium = True
                chromium_detail = str(installed)
        except (OSError, subprocess.TimeoutExpired):
            pass

    checks = {
        "python_package": python_package,
        "node_runner": node_runner,
        "chrome": chrome,
        "chromium": chromium,
    }
    missing = [
        label for key, label in (
            ("python_package", "Playwright Python package"),
            ("node_runner", "Node.js / npx"),
            ("chrome", "Chrome or Chromium"),
            ("chromium", "Playwright Chromium"),
        ) if not checks[key]
    ]
    return {
        **checks,
        "ready": not missing,
        "missing": missing,
        "detail": "Browser runtime ready" if not missing else "Missing: " + ", ".join(missing),
        "chrome_detail": chrome_detail,
        "chromium_detail": chromium_detail,
    }


def install_playwright_browser() -> dict:
    """Install Playwright's packaged Chromium without requiring a terminal."""
    if importlib.util.find_spec("playwright.sync_api") is None:
        raise RuntimeError("Playwright is not installed. Reinstall SelfApply to restore its dependencies.")
    checked = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True, text=True, timeout=600, check=False,
    )
    if checked.returncode:
        detail = (checked.stderr or checked.stdout or "Installation failed").strip()
        raise RuntimeError(detail[-800:])
    status = browser_runtime_status()
    if not status["chromium"]:
        raise RuntimeError("Playwright finished but Chromium could not be verified.")
    return {"ok": True, "message": "Playwright Chromium installed and verified.", "runtime": status}


def test_playwright_browser() -> dict:
    """Launch a real Playwright browser and verify page automation works."""
    status = browser_runtime_status()
    if not status["ready"]:
        raise RuntimeError(status["detail"])
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("<title>SelfApply browser check</title><p>ready</p>")
                if page.title() != "SelfApply browser check":
                    raise RuntimeError("The test page did not respond correctly.")
            finally:
                browser.close()
    except Exception as exc:
        raise RuntimeError(f"Browser launch failed: {exc}") from exc
    return {
        "ok": True,
        "message": "Browser runtime launched and passed the automation test.",
        "runtime": status,
    }


def ensure_browser_runtime() -> dict:
    """Repair packaged browser assets when possible, otherwise fail clearly."""
    status = browser_runtime_status()
    if status["ready"]:
        return status
    missing = set(status.get("missing", []))
    if missing == {"Playwright Chromium"} and status.get("python_package"):
        return install_playwright_browser()["runtime"]
    raise RuntimeError(
        "SelfApply installation is missing required browser components: "
        + ", ".join(status.get("missing", []))
        + ". Reinstall SelfApply and restart the app."
    )


def _set_browser_agent_link(name: str, linked: bool) -> None:
    links = _read_json(BROWSER_AGENT_LINKS_PATH)
    if linked:
        links[name] = {"linked_at": datetime.now(timezone.utc).isoformat()}
    else:
        links.pop(name, None)
    _write_json(BROWSER_AGENT_LINKS_PATH, links)


def extension_connection_verified(env: dict[str, str] | None = None) -> bool:
    """Return whether the currently saved extension token passed a live test."""
    values = env if env is not None else _env_values()
    token = values.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN", "")
    if not token:
        return False
    state = _read_json(EXTENSION_TEST_PATH)
    digest = hashlib.sha256(token.encode()).hexdigest()
    return secrets.compare_digest(str(state.get("token_hash", "")), digest)


def test_chrome_extension() -> dict:
    """Use the bridge once to prove that the saved token reaches Chrome."""
    if service_running():
        raise RuntimeError("Stop Self Apply before testing the Chrome extension.")
    env = _env_values()
    token = env.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN", "")
    if not token:
        raise ValueError("Save the Chrome extension token first.")
    if not browser_agent_status().get("codex", {}).get("authenticated"):
        raise RuntimeError("Connect Codex before testing the Chrome extension.")
    from selfapply.apply.launcher import _run_one_shot_browser_agent

    output = _run_one_shot_browser_agent(
        """Use the Playwright browser tool to inspect the currently connected Chrome
tab. Do not navigate, click, type, submit, open a tab, or read page content beyond
the visible page title and URL. If the browser tool successfully returns a page,
reply exactly RESULT:EXTENSION_CONNECTED. Otherwise reply exactly
RESULT:FAILED:extension_unreachable.""",
        timeout=60, browser_mode="existing",
    )
    if "RESULT:EXTENSION_CONNECTED" not in output:
        raise RuntimeError(
            "The extension token is saved, but SelfApply could not reach Chrome. "
            "Open Chrome, enable the extension, copy its current token, and try again."
        )
    _write_json(EXTENSION_TEST_PATH, {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "message": "Chrome extension connection verified."}


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
    previous_resume_mode = config.application_resume_mode(existing)
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
    incoming_resume_preferences = payload.get("resume_preferences", {})
    if not isinstance(incoming_resume_preferences, dict):
        raise ValueError("Invalid resume preference settings.")
    if "application_mode" in incoming_resume_preferences:
        application_mode = str(
            incoming_resume_preferences["application_mode"]
        ).strip().casefold()
        if application_mode not in {"master", "tailored"}:
            raise ValueError("Choose master or tailored resume mode.")
        existing.setdefault("resume_preferences", {})[
            "application_mode"
        ] = application_mode
    config.write_secret_file(
        config.PROFILE_PATH, json.dumps(existing, indent=2, ensure_ascii=False),
    )
    current_resume_mode = config.application_resume_mode(existing)
    if previous_resume_mode == "master" and current_resume_mode == "tailored":
        conn = database.get_connection()
        master_paths = {
            str(config.RESUME_PATH), str(config.RESUME_PDF_PATH),
            str(config.RESUME_DOCX_PATH),
        }
        placeholders = ",".join("?" for _ in master_paths)
        conn.execute(
            f"UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL "
            f"WHERE applied_at IS NULL AND tailored_resume_path IN ({placeholders})",
            tuple(sorted(master_paths)),
        )
        conn.commit()
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
    old_resume_titles = {
        str(title).strip().casefold()
        for title in old.get("resume_suggested_titles", [])
        if str(title).strip()
    }
    old_location = old.get("location", {}) if isinstance(old.get("location"), dict) else {}
    accepted_locations = [
        line.strip() for line in str(payload.get("accept_patterns", "")).splitlines()
        if line.strip()
    ]
    if not accepted_locations:
        accepted_locations = list(old_location.get("accept_patterns", []) or [])
    if not accepted_locations and location.casefold() != "remote":
        accepted_locations = [location]
    location_mode = str(payload.get("location_mode", "")).strip().casefold()
    if location_mode not in {"local_and_remote", "local", "remote"}:
        # Backward compatibility for older clients that sent one boolean.
        location_mode = "remote" if payload.get("remote_only") else "local"
    search_locations = []
    if location_mode in {"local_and_remote", "local"}:
        search_locations.append({"location": location, "remote": False})
    if location_mode in {"local_and_remote", "remote"}:
        search_locations.append({"location": "Remote", "remote": True})
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
        "location_mode": location_mode,
        "locations": search_locations,
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
        # Keep only resume suggestions that the user retained. Newly typed or
        # edited titles are therefore treated as user-owned on the next upload.
        "resume_suggested_titles": [
            title for title in titles if title.casefold() in old_resume_titles
        ],
    }
    config.SEARCH_CONFIG_PATH.write_text(
        yaml.safe_dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    config.secure_file(config.SEARCH_CONFIG_PATH)
    return {"ok": True, "message": "Job preferences saved."}


_ROLE_TITLE_WORDS = re.compile(
    r"(?i)\b(?:engineer|architect|developer|manager|analyst|scientist|designer|"
    r"administrator|consultant|specialist|director|lead|technician|coordinator|"
    r"product owner|project manager|accountant|nurse|teacher|recruiter|attorney|"
    r"counsel|sales|marketing|operations|supervisor|executive|officer|researcher)\b"
)


def _clean_resume_role(value: str) -> str:
    """Return a plausible role title, or an empty string for company/prose text."""
    cleaned = re.sub(r"(?i)^\s*(?:job\s+)?role\s*:\s*", "", value)
    cleaned = re.sub(r"(?i)\s*responsibilities\s*:.*$", "", cleaned)
    cleaned = re.sub(r"(?i)\s*(?:present|current)\s*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" |,-–—:;.")
    cleaned = re.sub(r"(?i)\b(sr|jr)\.(?=[A-Za-z])", r"\1. ", cleaned)
    if (
        not cleaned or len(cleaned) > 100 or len(cleaned.split()) > 12
        or not _ROLE_TITLE_WORDS.search(cleaned)
        or re.search(r"@|https?://|\b(?:19|20)\d{2}\b", cleaned)
    ):
        return ""
    return cleaned


def _resume_job_titles(
    current_title: str, entry_headers: list[dict[str, str]], date_range: re.Pattern,
) -> list[str]:
    """Derive conservative target titles from resume headline/work history."""
    candidates = [current_title]
    for entry in entry_headers:
        header = str(entry.get("header", ""))
        role_matches = list(re.finditer(r"(?i)role\s*:\s*([^|]+)", header))
        candidates.extend(match.group(1) for match in role_matches)
        for segment in header.split("|"):
            # A combined "Client … dates Role: …" segment is not itself a
            # title; the explicit role captured above is the useful portion.
            if re.search(r"(?i)role\s*:", segment):
                continue
            candidates.append(date_range.sub(" ", segment))
    titles: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        title = _clean_resume_role(candidate)
        key = title.casefold()
        if title and key not in seen:
            seen.add(key)
            titles.append(title)
    return titles[:10]


def _merge_resume_job_titles(suggested_titles: list[str]) -> list[str]:
    """Refresh resume-derived search titles without removing user-entered ones."""
    if not suggested_titles:
        return []
    try:
        search = yaml.safe_load(config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        search = {}
    if not isinstance(search, dict):
        search = {}
    previous_auto = {
        str(title).strip().casefold()
        for title in search.get("resume_suggested_titles", [])
        if str(title).strip()
    }
    existing = [
        str(item.get("query", "")).strip()
        for item in search.get("queries", [])
        if isinstance(item, dict) and str(item.get("query", "")).strip()
    ]
    manual = [title for title in existing if title.casefold() not in previous_auto]
    merged: list[str] = []
    seen: set[str] = set()
    for title in manual + suggested_titles:
        key = title.casefold()
        if key not in seen:
            seen.add(key)
            merged.append(title)
    search["queries"] = [
        {"query": title, "tier": min(index + 1, 3)}
        for index, title in enumerate(merged)
    ]
    manual_keys = {title.casefold() for title in manual}
    imported = [title for title in suggested_titles if title.casefold() not in manual_keys]
    search["resume_suggested_titles"] = imported
    config.ensure_dirs()
    config.SEARCH_CONFIG_PATH.write_text(
        yaml.safe_dump(search, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    config.secure_file(config.SEARCH_CONFIG_PATH)
    return imported


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
            if field == "extension_token":
                EXTENSION_TEST_PATH.unlink(missing_ok=True)
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


def logout_browser_agents() -> list[str]:
    """Disconnect supported browser-agent CLIs during an explicit full reset."""
    # Remove SelfApply authorization first. Even if an external CLI refuses
    # its global logout, it must not remain linked inside the reset app.
    BROWSER_AGENT_LINKS_PATH.unlink(missing_ok=True)
    disconnected: list[str] = []
    for name, command in (
        ("Codex", ["codex", "logout"]),
        ("Claude", ["claude", "auth", "logout"]),
    ):
        if not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            disconnected.append(name)
    return disconnected


def _active_profile_metadata() -> tuple[str, str]:
    registry = _read_json(config.ROOT_DIR / "profiles.json")
    active_id = str(registry.get("active_profile_id") or config.APP_DIR.name)
    entry = next(
        (item for item in registry.get("profiles", [])
         if isinstance(item, dict) and str(item.get("id")) == active_id),
        {},
    )
    return active_id, str(entry.get("name") or "Current profile")


def reset_local_data(payload: dict) -> dict:
    """Reset only the active profile, optionally keeping a local backup."""
    if str(payload.get("confirmation", "")) != "RESET":
        raise ValueError("Type RESET exactly to confirm the local data reset.")
    if service_running():
        stop_worker()
    if service_running():
        raise RuntimeError("Stop the application worker before resetting data.")

    database.init_db()
    status = database_status()
    database.close_connection()
    keep_backup = payload.get("backup", True) is not False
    backup_dir = None
    if keep_backup:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        managed_profile = (
            config.APP_DIR.parent.name == "profiles"
            and config.APP_DIR.parent.parent == config.ROOT_DIR
        )
        backup_root = (
            config.ROOT_DIR / "backups" / config.APP_DIR.name
            if managed_profile else config.APP_DIR / "backups"
        )
        backup_dir = backup_root / f"reset-{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        if platform.system() != "Windows":
            backup_dir.chmod(0o700)
    profile_id, profile_name = _active_profile_metadata()
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": "User-requested SelfApply reset",
        "profile_id": profile_id,
        "profile_name": profile_name,
        "counts": status.get("counts", {}),
        "moved": [],
        "backup_created": keep_backup,
    }
    for child in list(config.APP_DIR.iterdir()):
        if child.name in {"backups", "control-token"}:
            continue
        if keep_backup and backup_dir is not None:
            shutil.move(str(child), str(backup_dir / child.name))
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
        manifest["moved"].append(child.name)
    if backup_dir is not None:
        config.write_secret_file(
            backup_dir / "reset-manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )
    config.ensure_dirs()
    database.init_db()
    _activate_saved_ai_settings()
    return {
        "ok": True,
        "backup": str(backup_dir) if backup_dir else None,
        "message": (
            "This profile was reset and its backup was saved locally."
            if backup_dir else "This profile was permanently reset."
        ),
    }


def _profile_backup_root(profile_id: str | None = None) -> Path:
    """Return the private backup directory for one profile."""
    managed_profile = (
        config.APP_DIR.parent.name == "profiles"
        and config.APP_DIR.parent.parent == config.ROOT_DIR
    )
    return (
        config.ROOT_DIR / "backups" / (profile_id or config.APP_DIR.name)
        if managed_profile else config.APP_DIR / "backups"
    )


def _backup_catalog() -> list[tuple[str, str, Path, dict]]:
    """Discover backups shared by every local profile."""
    registry = _read_json(config.ROOT_DIR / "profiles.json")
    profile_names = {
        str(item.get("id")): str(item.get("name") or "Local profile")
        for item in registry.get("profiles", []) if isinstance(item, dict)
    }
    managed_profile = (
        config.APP_DIR.parent.name == "profiles"
        and config.APP_DIR.parent.parent == config.ROOT_DIR
    )
    owner_roots: list[tuple[str, Path]] = []
    if managed_profile:
        shared_root = config.ROOT_DIR / "backups"
        if shared_root.is_dir():
            owner_roots.extend(
                (path.name, path) for path in shared_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
    else:
        owner_roots.append(("local", _profile_backup_root()))

    catalog: list[tuple[str, str, Path, dict]] = []
    for owner_id, owner_root in owner_roots:
        if not owner_root.is_dir():
            continue
        for path in owner_root.iterdir():
            manifest_path = path / "reset-manifest.json"
            if not path.is_dir() or path.is_symlink() or not manifest_path.is_file():
                continue
            manifest = _read_json(manifest_path)
            if not manifest:
                continue
            source_id = str(manifest.get("profile_id") or owner_id)
            source_name = str(
                manifest.get("profile_name")
                or profile_names.get(source_id)
                or ("Current profile" if source_id == "local" else f"Profile {source_id}")
            )
            catalog.append((f"{owner_id}/{path.name}", source_name, path, manifest))
    return catalog


def available_profile_backups() -> dict:
    """List valid backups shared by all local profiles."""
    backups: list[dict] = []
    for backup_id, source_name, _path, manifest in _backup_catalog():
        counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
        backups.append({
            "id": backup_id,
            "source_profile": source_name,
            "created_at": str(manifest.get("created_at") or ""),
            "reason": str(manifest.get("reason") or "SelfApply backup"),
            "counts": counts,
        })
    backups.sort(key=lambda item: item["created_at"], reverse=True)
    return {"ok": True, "backups": backups}


_STALE_CHROME_LINKS = {
    "SingletonCookie", "SingletonLock", "SingletonSocket", "RunningChromeVersion",
}


def _validate_backup_links(source: Path) -> None:
    """Permit contained runtime links but reject links escaping the backup."""
    source = source.resolve()
    for path in source.rglob("*"):
        if not path.is_symlink() or path.name in _STALE_CHROME_LINKS:
            continue
        try:
            target = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"The backup contains a broken link: {path.name}.") from exc
        if not target.is_relative_to(source):
            raise ValueError("The backup contains an unsafe external link.")


def _restore_copy_ignore(_directory: str, names: list[str]) -> set[str]:
    """Do not restore browser process locks from a previous Chrome process."""
    return set(names).intersection(_STALE_CHROME_LINKS)


def restore_profile_backup(payload: dict) -> dict:
    """Restore a known local backup into the active profile directory."""
    if str(payload.get("confirmation", "")) != "RESTORE":
        raise ValueError("Type RESTORE exactly to confirm the backup restoration.")
    backup_id = str(payload.get("backup_id", "")).strip()
    parts = backup_id.split("/")
    if len(parts) != 2 or any(not part or Path(part).name != part for part in parts):
        raise ValueError("Choose a valid SelfApply backup.")
    match = next((item for item in _backup_catalog() if item[0] == backup_id), None)
    if match is None:
        raise ValueError("That backup is no longer available.")
    _catalog_id, source_name, source, manifest = match
    source = source.resolve()
    _validate_backup_links(source)

    _require_idle_profile_change()
    database.close_connection()
    config.APP_DIR.mkdir(parents=True, exist_ok=True)

    # Preserve the current state automatically, so an accidental restore is
    # recoverable. This snapshot appears in the same restore list.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_root = _profile_backup_root()
    recovery = backup_root / f"pre-restore-{stamp}"
    recovery.mkdir(parents=True, exist_ok=False)
    if platform.system() != "Windows":
        recovery.chmod(0o700)
    moved: list[str] = []
    for child in list(config.APP_DIR.iterdir()):
        if child.name in {"backups", "control-token"}:
            continue
        shutil.move(str(child), str(recovery / child.name))
        moved.append(child.name)
    profile_id, profile_name = _active_profile_metadata()
    recovery_manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": "Automatic snapshot before restoring a backup",
        "profile_id": profile_id,
        "profile_name": profile_name,
        "counts": {},
        "moved": moved,
        "backup_created": True,
    }
    config.write_secret_file(
        recovery / "reset-manifest.json",
        json.dumps(recovery_manifest, indent=2, ensure_ascii=False),
    )

    for child in source.iterdir():
        if child.name == "reset-manifest.json":
            continue
        destination = config.APP_DIR / child.name
        if child.is_dir():
            shutil.copytree(
                child, destination, symlinks=True, ignore=_restore_copy_ignore,
            )
        else:
            shutil.copy2(child, destination)
    active_profile_id, _active_profile_name = _active_profile_metadata()
    try:
        profiles.update_profile(
            active_profile_id, name=source_name, root=config.ROOT_DIR,
        )
    except ValueError:
        # Legacy single-profile installs have no registry entry to rename.
        pass
    config.ensure_dirs()
    database.init_db()
    _activate_saved_ai_settings()
    return {
        "ok": True,
        "restart": True,
        "restored": backup_id,
        "recovery_backup": recovery.name,
        "message": f"Backup from {source_name} restored. Reloading your profile…",
    }


def profile_list() -> dict:
    return profiles.list_profiles(config.ROOT_DIR)


def _require_idle_profile_change() -> None:
    if service_running():
        stop_worker()
    if service_running():
        raise RuntimeError("Stop the application worker before changing profiles.")


def create_local_profile(payload: dict) -> dict:
    _require_idle_profile_change()
    entry = profiles.create_profile(str(payload.get("name", "New profile")), config.ROOT_DIR)
    return {"ok": True, "profile": entry, "restart": True,
            "message": f"{entry['name']} was created. Opening setup…"}


def switch_local_profile(payload: dict) -> dict:
    _require_idle_profile_change()
    entry = profiles.switch_profile(str(payload.get("id", "")), config.ROOT_DIR)
    return {"ok": True, "profile": entry, "restart": True,
            "message": f"Switching to {entry['name']}…"}


def update_local_profile(payload: dict) -> dict:
    entry = profiles.update_profile(
        str(payload.get("id", "")), name=str(payload.get("name", "")),
        color=str(payload.get("color", "")), root=config.ROOT_DIR,
    )
    return {"ok": True, "profile": entry, "message": "Profile updated."}


def delete_local_profile(payload: dict) -> dict:
    profile_id = str(payload.get("id", ""))
    listing = profiles.list_profiles(config.ROOT_DIR)
    entry = next((item for item in listing["profiles"] if item["id"] == profile_id), None)
    if not entry:
        raise ValueError("That profile no longer exists.")
    if str(payload.get("confirmation", "")).strip() != entry["name"]:
        raise ValueError("Type the profile name exactly to confirm deletion.")
    _require_idle_profile_change()
    backup_path = None
    data_dir = config.ROOT_DIR / "profiles" / profile_id
    if payload.get("backup") is True and data_dir.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = config.ROOT_DIR / "backups" / profile_id / f"deleted-{stamp}"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(data_dir, backup_path)
        config.write_secret_file(
            backup_path / "reset-manifest.json",
            json.dumps({
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reason": "Backup created before deleting a profile",
                "profile_id": profile_id,
                "profile_name": entry["name"],
                "counts": {"applied": profiles.application_count(data_dir)},
                "backup_created": True,
            }, indent=2, ensure_ascii=False),
        )
    was_active = profile_id == listing["active_profile_id"]
    profiles.delete_profile(profile_id, config.ROOT_DIR)
    return {"ok": True, "restart": was_active, "backup": str(backup_path) if backup_path else None,
            "message": f"{entry['name']} was deleted."}


def delete_saved_secret(kind: str) -> dict:
    values = _env_values()
    provider_keys = (
        "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY",
        "LLM_URL", "LLM_MODEL",
    )
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
    if kind == "extension":
        EXTENSION_TEST_PATH.unlink(missing_ok=True)
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


def probe_ai_provider(payload: dict) -> dict:
    """Detect and test unsaved AI credentials without persisting the secret."""
    import httpx
    from selfapply.llm import LLMClient, normalize_local_url

    key = str(payload.get("api_key", "")).strip()
    local_url = str(payload.get("local_url", "")).strip()
    if key and local_url:
        raise ValueError("Enter either an API key or a local model URL, not both.")
    if not key and not local_url:
        result = test_ai_provider()
        env = _env_values()
        provider = (
            "gemini" if env.get("GEMINI_API_KEY") else
            "openai" if env.get("OPENAI_API_KEY") else
            "anthropic" if env.get("ANTHROPIC_API_KEY") else "local"
        )
        return {
            **result, "provider": provider, "model": env.get("LLM_MODEL", ""),
            "saved": True,
        }

    provider = detect_ai_provider(key) if key else "local"
    if key and not provider:
        raise ValueError(
            "The API-key format was not recognized. Use an OpenAI, Anthropic, "
            "or Google Gemini key, or enter a compatible local URL instead."
        )
    model = default_model_for_provider(provider or "")
    if provider == "gemini":
        base = "https://generativelanguage.googleapis.com/v1beta/openai"
    elif provider == "openai":
        base = "https://api.openai.com/v1"
    elif provider == "anthropic":
        base = "https://api.anthropic.com/v1"
    else:
        if not local_url.startswith(("http://", "https://")):
            raise ValueError("Enter a complete local model URL beginning with http:// or https://.")
        base = normalize_local_url(local_url)
        root = base.removesuffix("/v1")
        models: list[str] = []
        last_error = ""
        for model_url in (f"{base}/models", f"{root}/api/tags"):
            try:
                response = httpx.get(model_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                candidates = [
                    item.get("id") or item.get("name")
                    for item in data.get("data", data.get("models", []))
                    if isinstance(item, dict) and (item.get("id") or item.get("name"))
                ]
                models = [
                    name for name in candidates
                    if not any(token in name.casefold() for token in ("embed", "rerank"))
                ]
                if models:
                    break
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                last_error = str(exc)
        if not models:
            raise RuntimeError(
                "No chat model was found at that URL. Start its OpenAI-compatible "
                f"API and try again. {last_error}".strip()
            )
        model = next((name for name in models if "coder" in name.casefold()), models[0])

    client = LLMClient(base, model, key if provider != "local" else "")
    try:
        response = client.ask("Reply with exactly: SelfApply OK", max_tokens=20)
    finally:
        client.close()
    if "SelfApply OK" not in response:
        raise RuntimeError("The provider responded, but its model test returned unexpected text.")
    label = {
        "openai": "OpenAI", "anthropic": "Claude / Anthropic",
        "gemini": "Google Gemini", "local": "Local AI",
    }[provider or "local"]
    return {
        "ok": True, "provider": provider, "model": model, "saved": False,
        "message": f"Connected to {label} using {model}. Ready to save.",
    }


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


def extract_profile_from_resume(text: str) -> dict:
    """Extract reusable profile fields without requiring an AI provider."""
    from selfapply.scoring.pdf import parse_entries, parse_resume, parse_skills

    parsed = parse_resume(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = "\n".join(lines[:16])
    personal: dict[str, str] = {}
    experience: dict[str, str] = {}
    skills: dict[str, list[str]] = {
        "programming_languages": [], "frameworks": [], "tools": [],
    }
    facts: dict[str, object] = {}

    name = str(parsed.get("name", "")).strip()
    if name and "@" not in name and not re.search(r"https?://|\d{5,}", name):
        personal["full_name"] = name
        personal["preferred_name"] = name.split()[0]
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", header)
    if email:
        personal["email"] = email.group(0)
    for candidate in re.findall(r"(?:\+?\d[\d().\s-]{7,}\d)", header):
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 15:
            personal["phone"] = re.sub(r"\s+", " ", candidate).strip()
            break
    urls = [
        ("https://" + url if url.casefold().startswith("www.") else url).rstrip(".,;|)")
        for url in re.findall(r"(?:https?://|www\.)[^\s|]+", text, re.I)
    ]
    for url in urls:
        lowered = url.casefold()
        if "linkedin.com/" in lowered:
            personal["linkedin_url"] = url
        elif "github.com/" in lowered:
            personal["github_url"] = url
        elif "portfolio_url" not in personal:
            personal["portfolio_url"] = url
        elif "website_url" not in personal:
            personal["website_url"] = url
    postal = re.search(r"\b\d{5}(?:-\d{4})?\b|\b[A-Z]\d[A-Z][ -]?\d[A-Z]\d\b", header, re.I)
    if postal:
        personal["postal_code"] = postal.group(0)
    street = next((
        line for line in lines[:16]
        if re.search(r"\b\d{1,6}\s+.+\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|drive|dr|lane|ln|terrace|way)\b", line, re.I)
    ), "")
    if street:
        personal["address"] = street
    location = str(parsed.get("location", "")).strip()
    if location and "@" not in location and not re.search(r"https?://", location):
        parts = [part.strip() for part in location.split(",") if part.strip()]
        if len(parts) >= 3:
            personal.update(city=parts[0], province_state=parts[-2], country=parts[-1])
        elif len(parts) == 2:
            personal.update(city=parts[0], province_state=parts[1])

    title = str(parsed.get("title", "")).strip()
    if title and "@" not in title and not re.search(r"https?://|\d{5,}", title):
        experience["current_title"] = title
        experience["target_role"] = title
    years = re.search(r"\b(\d{1,2})\s*\+?\s+years?(?:\s+of)?\s+experience\b", text, re.I)
    if years:
        experience["years_of_experience_total"] = years.group(1)

    sections = parsed.get("sections", {})

    def section_text(*prefixes: str) -> str:
        for key, value in sections.items():
            normalized = str(key).strip().rstrip(":").casefold()
            if any(normalized.startswith(prefix.casefold()) for prefix in prefixes):
                return str(value)
        return ""

    education_text = section_text("education")
    for level, pattern in (
        ("PhD", r"\b(?:ph\.?d|doctorate)\b"),
        ("Master's", r"\b(?:master(?:'s)?|m\.?s\.?|m\.?a\.?|mba)\b"),
        ("Bachelor's", r"\b(?:bachelor(?:'s)?|b\.?s\.?|b\.?a\.?)\b"),
        ("Associate", r"\bassociate(?:'s)?\b"),
    ):
        if re.search(pattern, education_text, re.I):
            experience["education_level"] = level
            break

    skill_text = section_text("technical skills", "skills", "core competencies")
    skill_pairs = parse_skills(skill_text)
    if not skill_pairs:
        skill_lines = [line.strip() for line in skill_text.splitlines() if line.strip()]
        # Many exported resumes render category and values on alternating lines.
        for index in range(0, len(skill_lines) - 1, 2):
            category, values = skill_lines[index], skill_lines[index + 1]
            if len(category) <= 60:
                skill_pairs.append((category, values))
    for category, values in skill_pairs:
        items = [item.strip() for item in re.split(r"[,;|]", values) if item.strip()]
        category_key = category.casefold()
        if any(word in category_key for word in ("language", "programming")):
            skills["programming_languages"].extend(items)
        elif any(word in category_key for word in ("framework", "librar")):
            skills["frameworks"].extend(items)
        else:
            skills["tools"].extend(items)
    known_languages = (
        "Python", "JavaScript", "TypeScript", "Java", "Go", "Golang", "SQL",
        "C++", "C#", "C", "Rust", "Ruby", "PHP", "Kotlin", "Swift", "Scala",
        "R", "MATLAB", "Bash", "PowerShell", "Dart", "Objective-C", "COBOL",
    )
    known_frameworks = (
        "React", "React Native", "Vue", "Vue.js", "Svelte", "Angular", "Next.js",
        "Node.js", "Django", "Flask", "FastAPI", "Spring", "Spring Boot", ".NET",
        "Rails", "Laravel", "TensorFlow", "PyTorch", "Spark", "PySpark", "Hadoop",
    )
    searchable = text.casefold()
    for term in known_languages:
        if re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", searchable):
            skills["programming_languages"].append(term)
    for term in known_frameworks:
        if re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", searchable):
            skills["frameworks"].append(term)
    language_keys = {item.casefold() for item in skills["programming_languages"]}
    framework_keys = {item.casefold() for item in skills["frameworks"]}
    skills["tools"] = [
        item for item in skills["tools"]
        if item.casefold() not in language_keys | framework_keys
    ]
    if skill_text and not any(skills.values()):
        skills["tools"] = [
            item.strip() for item in re.split(r"[,;|\n]", skill_text)
            if 1 < len(item.strip()) < 80
        ]
    skills = {key: list(dict.fromkeys(values)) for key, values in skills.items() if values}

    work_text = section_text("experience", "professional experience", "work history")
    work_lines = [line.strip() for line in work_text.splitlines() if line.strip()]
    date_range = re.compile(
        r"(?i)(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?|\b(?:19|20)\d{2}\b).{0,28}(?:present|current|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
        r"(?:19|20)\d{2})"
    )
    entry_headers: list[dict[str, str]] = []
    companies: list[str] = []
    for index, line in enumerate(work_lines):
        match = date_range.search(line)
        if not match:
            continue
        before_date = line[:match.start()].strip(" |-–—,")
        header = line
        if len(before_date) < 3 and index:
            header = f"{work_lines[index - 1]} | {line}"
            before_date = work_lines[index - 1].strip(" |-–—,")
        next_line = work_lines[index + 1] if index + 1 < len(work_lines) else ""
        next_title = re.sub(r"(?i)\s*responsibilities\s*:.*$", "", next_line).strip()
        after_date = line[match.end():].strip(" |-–—,")
        embedded_title = re.sub(r"(?i)^role\s*:\s*", "", after_date).strip()
        subtitle = embedded_title or (
            next_title if next_title and len(next_title) <= 100
            and not date_range.search(next_title) else ""
        )
        if next_title and len(next_title) <= 100 and not date_range.search(next_title):
            header = f"{header} | {next_title}"
        if embedded_title:
            header = line
        company_source = before_date
        if company_source.casefold().startswith("client:"):
            company = company_source.split(",", 1)[0].strip()
        else:
            company = re.split(r"\s+[–—-]\s+", company_source, maxsplit=1)[0].strip()
        fact = {"header": header, "company": company}
        if subtitle:
            fact["subtitle"] = subtitle
        entry_headers.append(fact)
        if before_date:
            if company and len(company) <= 100:
                companies.append(company)
    if entry_headers:
        facts["experience_entries"] = entry_headers
    if companies:
        facts["preserved_companies"] = list(dict.fromkeys(companies))
    education_entries = [line.strip() for line in education_text.splitlines() if line.strip()]
    if education_entries:
        facts["education_entries"] = education_entries
        schools: list[str] = []
        for line in education_entries:
            school_match = re.search(
                r"(?i)\b(?:at|from)\s+(.+?)(?=\s*[-–—]\s*"
                r"(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
                r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
                r"nov(?:ember)?|dec(?:ember)?)\s+)?(?:19|20)\d{2}|$)",
                line,
            )
            if school_match:
                schools.append(school_match.group(1).strip(" .,-"))
        if schools:
            facts["preserved_school"] = ", ".join(dict.fromkeys(schools))
    project_text = section_text("projects", "selected projects")
    if project_text:
        projects = [
            str(entry.get("title", "")).strip()
            for entry in parse_entries(project_text)
            if str(entry.get("title", "")).strip()
            and len(str(entry.get("title", "")).strip()) <= 120
            and not str(entry.get("title", "")).strip().endswith(".")
        ]
        if projects:
            facts["preserved_projects"] = list(dict.fromkeys(projects[:20]))
    metrics = re.findall(
        r"(?i)(?:\$\s?\d[\d,.]*(?:k|m|million|billion)?|\b\d+(?:\.\d+)?%|"
        r"\b\d+(?:\.\d+)?\s*(?:k|m|million|billion)\+?\s+(?:users|customers|"
        r"transactions|records|requests|devices|employees|members|patients)\b)",
        work_text,
    )
    if metrics:
        facts["real_metrics"] = list(dict.fromkeys(metric.strip() for metric in metrics))
    return {
        "personal": personal, "experience": experience,
        "skills_boundary": skills, "resume_facts": facts,
        "suggested_job_titles": _resume_job_titles(title, entry_headers, date_range),
    }


def _merge_resume_profile(
    suggestions: dict, *, replace_existing: bool = False,
) -> list[str]:
    """Merge resume-derived values, optionally refreshing prior resume data."""
    profile = _read_json(config.PROFILE_PATH)
    imported: list[str] = []
    if replace_existing:
        skills = profile.setdefault("skills_boundary", {})
        for field in ("programming_languages", "frameworks", "tools"):
            skills.pop(field, None)
        facts = profile.setdefault("resume_facts", {})
        for field in (
            "experience_entries", "education_entries", "preserved_companies",
            "preserved_projects", "preserved_school", "real_metrics",
        ):
            facts.pop(field, None)
    for section, values in suggestions.items():
        if not isinstance(values, dict):
            continue
        target = profile.setdefault(section, {})
        for field, value in values.items():
            may_replace = replace_existing and not (
                section == "experience" and field == "target_role"
                and target.get(field)
            )
            if value and (may_replace or not target.get(field)):
                target[field] = value
                imported.append(f"{section}.{field}")
    if imported:
        config.write_secret_file(
            config.PROFILE_PATH, json.dumps(profile, indent=2, ensure_ascii=False),
        )
    return imported


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
    profile_link_text = ""
    if suffix == ".pdf" and content.startswith(b"%PDF-"):
        path = config.RESUME_PDF_PATH
        try:
            from pypdf import PdfReader
            from selfapply.scoring.pdf import normalize_resume_text
            reader = PdfReader(io.BytesIO(content))
            extracted_text = "\n\n".join(
                page.extract_text(extraction_mode="layout") or ""
                for page in reader.pages
            )
            extracted_text = normalize_resume_text(extracted_text)
            links: list[str] = []
            for page in reader.pages:
                for annotation_ref in page.get("/Annots", []):
                    try:
                        annotation = annotation_ref.get_object()
                        uri = str((annotation.get("/A") or {}).get("/URI") or "").strip()
                    except (AttributeError, TypeError):
                        uri = ""
                    if uri.startswith(("http://", "https://")):
                        links.append(uri)
            if links:
                profile_link_text = "\n".join(dict.fromkeys(links))
        except Exception as exc:
            raise ValueError(f"PDF text extraction failed: {exc}") from exc
    elif suffix == ".docx" and content.startswith(b"PK"):
        path = config.RESUME_DOCX_PATH
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
                try:
                    relationships = ElementTree.fromstring(
                        archive.read("word/_rels/document.xml.rels")
                    )
                    links = [
                        str(node.get("Target") or "").strip()
                        for node in relationships
                        if str(node.get("TargetMode") or "").casefold() == "external"
                    ]
                    if links:
                        profile_link_text = "\n".join(dict.fromkeys(links))
                except KeyError:
                    pass
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
        raise ValueError("Upload a valid PDF, DOCX, or UTF-8 TXT resume.")
    # Exactly one uploaded document is authoritative. Keeping an older PDF
    # beside a newer DOCX caused links and layout to be inferred from the wrong
    # candidate resume.
    for candidate in (path.parent / "resume.pdf", path.parent / "resume.docx"):
        if candidate != path:
            candidate.unlink(missing_ok=True)
    path.write_bytes(content); config.secure_file(path)
    if extracted_text:
        config.write_secret_file(config.RESUME_PATH, extracted_text + "\n")
    elif suffix != ".txt":
        raise ValueError("The document contained no extractable resume text.")
    resume_text = extracted_text if extracted_text else content.decode("utf-8")
    from selfapply.scoring.pdf import save_master_layout
    save_master_layout(
        content, suffix, resume_text,
        output_path=config.RESUME_PATH.with_name("resume-layout.json"),
    )
    from selfapply.scoring.document_tailor import save_resume_manifest
    save_resume_manifest(
        path, resume_text,
        output_path=config.RESUME_PATH.with_name("resume-manifest.json"),
    )
    config.write_secret_file(
        config.RESUME_PATH.with_name("resume-source.json"),
        json.dumps({"name": Path(name).name, "format": suffix}, indent=2),
    )
    profile_source = resume_text + ("\n" + profile_link_text if profile_link_text else "")
    suggestions = extract_profile_from_resume(profile_source)
    imported = _merge_resume_profile(
        suggestions,
        replace_existing=payload.get("replace_profile") is True,
    )
    imported_titles = _merge_resume_job_titles(suggestions.get("suggested_job_titles", []))
    return {
        "ok": True,
        "profile_fields_imported": imported,
        "job_titles_imported": imported_titles,
        "message": (
            f"Resume saved and {len(imported)} profile field"
            f"{'s were' if len(imported) != 1 else ' was'} updated for review."
            + (f" Added {len(imported_titles)} target job title suggestions."
               if imported_titles else "")
        ),
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


def login_connection_status(searches: dict | None = None,
                            discovery: dict | None = None) -> dict:
    """Return Google status and optional sign-in state for enabled job sites."""
    if searches is None:
        try:
            searches = yaml.safe_load(
                config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")
            ) or {}
        except (OSError, yaml.YAMLError):
            searches = {}
    if discovery is None:
        sources = searches.get("sources", {})
        discovery = {
            "workday": bool(sources.get("workday", True)),
            "selected_direct_sites": searches.get("direct_sites", []),
        }

    selected_sites = {str(site).casefold() for site in searches.get("sites", [])}
    selected_direct = {
        str(site).casefold() for site in discovery.get("selected_direct_sites", [])
    }
    source_names = {
        "indeed": "Indeed", "linkedin": "LinkedIn", "glassdoor": "Glassdoor",
        "zip_recruiter": "ZipRecruiter", "dice": "Dice",
    }
    required = {
        label for key, label in source_names.items()
        if key in selected_sites or key in selected_direct
    }
    if discovery.get("workday"):
        required.add("Workday")

    google = _read_json(config.APP_DIR / "google_session.json")
    if google.get("account_email") and not google.get("email"):
        google["email"] = google["account_email"]
    if google.get("email") and "verified" not in google:
        google["verified"] = True
    if google.get("verified"):
        from selfapply.apply.chrome import has_google_auth_session
        if not has_google_auth_session():
            google["verified"] = False
            google["stale"] = True
    saved = _read_json(config.APP_DIR / "connected_portals.json")
    rows = []
    for name, url in portal_targets().items():
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
        rows.append({
            "name": name,
            "host": "workday.com" if name == "Workday" else host,
            "url": url,
            "status": status,
            "enabled": name in required,
        })
    enabled_rows = [row for row in rows if row["enabled"]]
    enabled_connected = sum(
        row["status"] in {"connected", "manual_connected"} for row in enabled_rows
    )
    return {
        "google": google,
        "google_verified": bool(google.get("verified")),
        "portals": rows,
        "enabled_total": len(enabled_rows),
        "enabled_connected": enabled_connected,
        "enabled_remaining": len(enabled_rows) - enabled_connected,
    }


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
    if browser != "managed":
        raise ValueError("SelfApply uses managed Chrome. Reload the page and try again.")
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


def build_test_program_arguments(values: dict) -> list[str]:
    """Build a prepare-if-needed, one-job test that cannot submit."""
    executable = _selfapply_executable()
    return [
        str(executable), "run-worker", "--dry-run", "--test-limit", "1",
        "--max-spend", str(values["max_spend"]),
        "--queue-threshold", str(values["queue_threshold"]),
        "--agent", values["agent"], "--browser", values["browser"],
        "--workers", "1", "--max-actions", str(values["max_actions"]),
        "--max-minutes", str(values["max_minutes"]), "--validation", "normal",
        "--skip-smart-extract",
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
    result = _launchctl("print", f"{_launchd_gui_domain()}/{LABEL}")
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
        keep_alive = plistlib.loads(PLIST_PATH.read_bytes()).get("KeepAlive", False)
    except (OSError, plistlib.InvalidFileException):
        keep_alive = False
    if keep_alive is True:
        return LABEL
    if isinstance(keep_alive, dict):
        exit_match = re.search(r"last exit code\s*=\s*(-?\d+)", output)
        last_exit = int(exit_match.group(1)) if exit_match else None
        if keep_alive.get("SuccessfulExit") is False and last_exit not in {None, 0}:
            return LABEL
        if keep_alive.get("SuccessfulExit") is True and last_exit == 0:
            return LABEL
        # Other launchd KeepAlive predicates are active independently of the
        # process exit code and should remain controllable during throttling.
        if any(key != "SuccessfulExit" and bool(value) for key, value in keep_alive.items()):
            return LABEL
    return None


def _plist_for_label(label: str) -> Path:
    return PLIST_PATH


def _launchd_gui_domain() -> str:
    """Return the per-user launchd domain, including in cross-platform tests."""
    getuid = getattr(os, "getuid", None)
    return f"gui/{getuid() if getuid else 0}"


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
    mode = (
        "test run" if "--dry-run" in arguments
        else "single job" if "apply" in arguments[1:2]
        else "self apply"
    )
    return {
        "agent": value_after("--agent", "unknown"),
        "browser": value_after("--browser", "unknown"),
        "workers": workers,
        "mode": mode,
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
    mode = (
        "test run" if "--dry-run" in arguments
        else "single job" if "apply" in arguments[1:2]
        else "self apply"
    )
    return {
        "agent": value_after("--agent", "unknown"),
        "browser": value_after("--browser", "unknown"),
        "workers": workers,
        "mode": mode,
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
    _launchctl("bootout", f"{_launchd_gui_domain()}/{LABEL}")
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
    result = _launchctl("bootstrap", _launchd_gui_domain(), str(PLIST_PATH))
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
    ensure_browser_runtime()
    _install_worker_service(build_program_arguments(values), continuous=True)
    return {"ok": True, "message": "Self Apply started with an enforced spending cap."}


def start_test_run(payload: dict) -> dict:
    """Fill and validate one ready application without final submission."""
    values = validate_start_payload(payload)
    if service_running():
        raise RuntimeError("A SelfApply worker or test run is already running.")
    snapshot = settings_snapshot()
    readiness = setup_readiness(
        snapshot, agent=values["agent"], browser=values["browser"],
    )
    if not readiness["complete"]:
        labels = ", ".join(item["label"] for item in readiness["missing"])
        raise ValueError(f"Setup incomplete. Complete these required fields: {labels}.")
    ensure_browser_runtime()
    _install_worker_service(build_test_program_arguments(values), continuous=False)
    return {
        "ok": True,
        "message": "Test run started. SelfApply will prepare a job if needed, fill and validate one form, then stop with final submission blocked.",
    }


def stop_worker() -> dict:
    if platform.system() == "Darwin":
        _launchctl("bootout", f"{_launchd_gui_domain()}/{LABEL}")
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


def _recent_scoring_progress() -> tuple[int, int] | None:
    """Read the latest active batch counter for workers started before this UI build."""
    path = config.LOG_DIR / "worker-service-error.log"
    try:
        if time.time() - path.stat().st_mtime > 180:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")[-131_072:]
    except OSError:
        return None
    matches = re.findall(r"\[(\d+)/(\d+)\]\s+(?:score=|ineligible=)", text)
    if not matches:
        return None
    current, total = (int(value) for value in matches[-1])
    return (current, total) if total > 0 and current <= total else None


def _recent_tailoring_progress() -> tuple[int, int] | None:
    path = config.LOG_DIR / "worker-service-error.log"
    try:
        if time.time() - path.stat().st_mtime > 180:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")[-131_072:]
    except OSError:
        return None
    matches = re.findall(
        r"(?:^|\n)(\d+)/(\d+)\s+\[(?:APPROVED|FALLBACK_MASTER|FAILED_[A-Z_]+|ERROR)\]",
        text,
    )
    if not matches:
        return None
    current, total = (int(value) for value in matches[-1])
    return (current, total) if total > 0 and current <= total else None


def _preparation_status(
    conn, *, running: bool, current_job: dict | None, ready: int,
    test_mode: bool = False,
) -> dict:
    """Describe the background pipeline in language suitable for a new user."""
    stats = database.get_stats(conn)
    if not running:
        return {
            "stage": "stopped", "label": "Not running", "detail": (
                f"{ready} job{'s' if ready != 1 else ''} ready to apply."
                if ready else "Start Self Apply to find and prepare jobs."
            ), "current": 0, "total": 0, "percent": 0,
        }
    if current_job:
        return {
            "stage": "applying",
            "label": "Testing application" if test_mode else "Applying now",
            "detail": (
                f"Filling {current_job.get('title') or 'a prepared job'}; final submission is blocked."
                if test_mode
                else f"Submitting {current_job.get('title') or 'a prepared job'}."
            ),
            "current": 1, "total": max(1, int(ready) + 1), "percent": 100,
        }

    unscored = int(stats.get("unscored") or 0)
    if unscored:
        recent = _recent_scoring_progress()
        if recent:
            current, total = recent
            detail = f"Scoring {current} of {total} discovered jobs."
        else:
            total = max(1, int(stats.get("with_description") or unscored))
            current = max(0, total - unscored)
            detail = f"Scoring saved jobs · {unscored} remaining."
        return {
            "stage": "scoring", "label": "Checking job matches", "detail": detail,
            "current": current, "total": total,
            "percent": min(100, round(current * 100 / max(1, total))),
        }

    untailored = conn.execute(
        """SELECT COUNT(*) FROM jobs
           WHERE fit_score >= ? AND full_description IS NOT NULL
             AND tailored_resume_path IS NULL
             AND COALESCE(tailor_attempts, 0) < 5
             AND applied_at IS NULL""",
        (config.DEFAULTS["min_score"],),
    ).fetchone()[0]
    if untailored:
        recent = _recent_tailoring_progress()
        if recent:
            current, total = recent
            detail = (
                f"Preparing resume {current} of {total} in this batch · "
                f"{untailored} match{'es' if untailored != 1 else ''} remaining."
            )
        else:
            current, total = 0, untailored
            detail = f"Creating tailored resumes · {untailored} remaining."
        return {
            "stage": "tailoring", "label": "Preparing resumes",
            "detail": detail,
            "current": current, "total": total,
            "percent": min(100, round(current * 100 / max(1, total))),
        }

    pending_detail = int(stats.get("pending_detail") or 0)
    if pending_detail:
        total = max(1, int(stats.get("total") or pending_detail))
        current = max(0, total - pending_detail)
        return {
            "stage": "enriching", "label": "Reading job details",
            "detail": f"Reading {pending_detail} saved job posting{'s' if pending_detail != 1 else ''}.",
            "current": current, "total": total,
            "percent": min(100, round(current * 100 / max(1, total))),
        }

    if ready:
        return {
            "stage": "ready", "label": "Jobs ready",
            "detail": f"{ready} prepared job{'s are' if ready != 1 else ' is'} ready for the application workers.",
            "current": ready, "total": ready, "percent": 100,
        }
    return {
        "stage": "discovering", "label": "Finding new jobs",
        "detail": "Searching your enabled job sources for matching roles…",
        "current": 0, "total": 0, "percent": 8,
    }


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
    preparation = _preparation_status(
        conn, running=running, current_job=current_job,
        ready=counts_dict.get("ready", 0),
        test_mode=bool(worker and worker.get("mode") == "test run"),
    )
    return {
        "running": running,
        "counts": counts_dict,
        "current_job": current_job,
        "worker": worker,
        "preparation": preparation,
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
                "label": (
                    f"{datetime.strptime(day, '%Y-%m-%d').month}/"
                    f"{datetime.strptime(day, '%Y-%m-%d').day}"
                ),
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
        "dice", "glassdoor", "indeed", "linkedin",
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
        item["resume_docx_url"] = None
        item["resume_compare_name"] = None
        tailored_path_value = item.get("tailored_resume_path")
        if tailored_path_value:
            tailored_path = Path(tailored_path_value).expanduser().resolve()
            if tailored_path.is_relative_to(config.TAILORED_DIR.resolve()):
                report_path = tailored_path.with_suffix("").with_name(
                    tailored_path.stem + "_REPORT.json"
                )
                if report_path.is_file():
                    item["resume_compare_name"] = tailored_path.stem
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
                docx_path = resume_path.with_suffix(".docx")
                if docx_path.is_file():
                    item["resume_docx_url"] = (
                        f"/api/applications/resume?kind={kind}&name={quote(docx_path.name)}"
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
    source = _read_json(config.RESUME_SOURCE_PATH)
    source_suffix = str(source.get("format") or "").casefold()
    master_path = {
        ".pdf": config.RESUME_PDF_PATH,
        ".docx": config.RESUME_DOCX_PATH,
        ".txt": config.RESUME_PATH,
    }.get(source_suffix)
    if master_path is None or not master_path.is_file():
        master_path = next(
            (path for path in (
                config.RESUME_DOCX_PATH, config.RESUME_PDF_PATH, config.RESUME_PATH,
            ) if path.is_file()),
            None,
        )
    master = None
    if master_path:
        suffix = master_path.suffix.casefold().lstrip(".")
        original_name = Path(str(source.get("name") or master_path.name)).name
        master = {
            "name": original_name,
            "format": "Word" if suffix == "docx" else suffix.upper(),
            "view": f"/api/documents/master?format={suffix}&view=1",
            "download": f"/api/documents/master?format={suffix}",
            "updated": datetime.fromtimestamp(
                master_path.stat().st_mtime, tz=timezone.utc,
            ).isoformat(),
        }
    title_by_path: dict[str, str] = {}
    try:
        rows = database.get_connection().execute(
            "SELECT title, tailored_resume_path FROM jobs WHERE tailored_resume_path IS NOT NULL"
        ).fetchall()
        title_by_path = {
            str(Path(row["tailored_resume_path"]).resolve()): str(row["title"] or "")
            for row in rows if row["tailored_resume_path"]
        }
    except Exception:
        log.debug("Unable to map tailored resume titles", exc_info=True)
    items = []
    if config.TAILORED_DIR.exists():
        for path in sorted(
            config.TAILORED_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True,
        ):
            if path.name.endswith(("_JOB.txt", "_REPORT.txt")):
                continue
            pdf_path = path.with_suffix(".pdf")
            docx_path = path.with_suffix(".docx")
            report_path = path.with_name(path.stem + "_REPORT.json")
            items.append({
                "name": title_by_path.get(str(path.resolve())) or path.stem.replace("_", " "),
                "file_name": path.stem,
                "created": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc,
                ).isoformat(),
                "pdf": f"/api/ats/download?name={pdf_path.name}" if pdf_path.exists() else None,
                "pdf_view": f"/api/ats/download?name={pdf_path.name}&view=1" if pdf_path.exists() else None,
                "docx": f"/api/ats/download?name={docx_path.name}" if docx_path.exists() else None,
                "docx_view": f"/api/ats/download?name={docx_path.name}&view=1" if docx_path.exists() else None,
                "compare_name": path.stem if report_path.is_file() else None,
            })
    return {"master": master, "documents": items[:200]}


def resume_change_report(name: str) -> dict:
    """Return the bounded master-to-tailored edits for one generated resume."""
    if not name or Path(name).name != name or Path(name).stem != name:
        raise ValueError("Invalid resume comparison.")
    report_path = (config.TAILORED_DIR / f"{name}_REPORT.json").resolve()
    manifest_path = (config.TAILORED_DIR / f"{name}_MANIFEST.json").resolve()
    if not report_path.is_relative_to(config.TAILORED_DIR.resolve()) or not report_path.is_file():
        raise ValueError("Resume comparison is unavailable.")
    report = _read_json(report_path)
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    blocks = {
        str(block.get("id")): block
        for block in manifest.get("blocks", []) if isinstance(block, dict)
    }
    changes = []
    for change in report.get("changes", []):
        if not isinstance(change, dict):
            continue
        before = str(change.get("before") or "").strip()
        after = str(change.get("after") or "").strip()
        if before == after or not (before or after):
            continue
        block = blocks.get(str(change.get("block_id") or ""), {})
        changes.append({
            "section": str(change.get("section") or "Resume"),
            "before": before,
            "after": after,
            "page": block.get("page") if isinstance(block.get("page"), int) else None,
            "rect": block.get("rect") if isinstance(block.get("rect"), list) else None,
        })
    return {
        "name": name,
        "changed_block_count": len(changes),
        "changes": changes,
        "format_fidelity": report.get("format_fidelity"),
        "warning": report.get("format_fidelity_warning"),
    }


def _resume_pdf_path(name: str, kind: str) -> Path:
    if kind == "master":
        path = config.RESUME_PDF_PATH.resolve()
        if not path.is_file():
            raise ValueError("The master resume PDF preview is unavailable.")
        return path
    roots = {
        "applied": config.APPLIED_RESUME_DIR,
        "tailored": config.TAILORED_DIR,
    }
    root = roots.get(kind)
    if (root is None or not name or Path(name).name != name
            or Path(name).suffix.casefold() != ".pdf"):
        raise ValueError("Invalid resume preview.")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Resume preview is unavailable.")
    return path


def resume_preview_info(name: str, kind: str) -> dict:
    """Describe PDF pages rendered by SelfApply's in-app document viewer."""
    path = _resume_pdf_path(name, kind)
    try:
        import fitz  # type: ignore

        document = fitz.open(path)
        page_count = document.page_count
        sizes = [
            {"width": float(page.rect.width), "height": float(page.rect.height)}
            for page in document
        ]
        document.close()
    except (ImportError, RuntimeError, ValueError) as exc:
        raise ValueError(f"The resume PDF could not be opened: {exc}") from exc
    return {
        "name": name,
        "page_count": page_count,
        "sizes": sizes,
        "pages": [
            f"/api/resumes/page?kind={quote(kind)}&name={quote(name)}&page={index}"
            for index in range(page_count)
        ],
    }


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
    from selfapply.scoring.tailor import artifact_prefix
    from selfapply.scoring.document_tailor import (
        _source_path, create_tailored_artifacts, tailor_document,
    )

    profile = config.load_profile()
    master = config.RESUME_PATH.read_text(encoding="utf-8")
    progress(18, "Analyzing the master resume structure")
    aggressiveness = payload.get("target_aggressiveness")
    try:
        aggressiveness = max(0.0, min(1.0, float(aggressiveness))) if aggressiveness not in (None, "") else None
    except (TypeError, ValueError):
        aggressiveness = None
    source_path = _source_path(config.RESUME_PATH)
    progress(30, "Matching job requirements to resume blocks")
    tailored, report, patch_set, manifest = tailor_document(
        master, job, profile, target_aggressiveness=aggressiveness,
        source_path=source_path,
    )
    progress(70, "Applying anchored edits while preserving formatting")
    config.TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    prefix = artifact_prefix(job)
    prefix_path = config.TAILORED_DIR / prefix
    report_path = config.TAILORED_DIR / f"{prefix}_REPORT.json"
    artifacts = create_tailored_artifacts(
        source_path, tailored, patch_set, manifest, prefix_path,
    )
    text_path = Path(artifacts["text_path"])
    tailored = text_path.read_text(encoding="utf-8")
    from selfapply.scoring.ats_matcher import analyze_ats_match
    report["ats_match"] = analyze_ats_match(
        tailored, job.get("full_description") or "", master_resume_text=master,
        company_name=str(job.get("site") or ""),
    )
    report["changed_block_count"] = artifacts.get("actual_edit_count", len(patch_set["edits"]))
    actual_ids = {edit["block_id"] for edit in patch_set["edits"]}
    report["changes"] = [
        change for change in report.get("changes", []) if change.get("block_id") in actual_ids
    ]
    pdf_path = Path(artifacts["pdf_path"]) if artifacts.get("pdf_path") else None
    docx_path = Path(artifacts["docx_path"]) if artifacts.get("docx_path") else None
    if artifacts.get("fidelity_warning"):
        report["format_fidelity"] = "best-effort-fallback"
        report["format_fidelity_warning"] = artifacts["fidelity_warning"]
    progress(88, "Rendering and validating the application PDF")
    if pdf_path is None:
        try:
            pdf_path = convert_to_pdf(text_path)
        except Exception as exc:
            report["pdf_warning"] = str(exc)
            log.warning("Resume created but PDF conversion failed: %s", exc)
    report["artifacts"] = {
        "txt": str(text_path), "docx": str(docx_path) if docx_path else None,
        "pdf": str(pdf_path) if pdf_path else None,
        "manifest": str(artifacts["manifest_path"]), "patches": str(artifacts["patch_path"]),
    }
    report["preservation_checks"] = artifacts.get("preservation_checks", {})
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
            "Format-preserving resume created. The editable PDF fallback could not be rendered, "
            "but the source-format and TXT downloads are ready."
            if not pdf_path else
            "Format-preserving resume created, ATS-scored, and verified for download."
        ),
        "title": job.get("title"), "text_path": str(text_path),
        # API task results must stay JSON-serializable.  Returning a Path here
        # caused the completed task poll to close its HTTP response, which the
        # browser surfaced only as the misleading `Failed to fetch` message.
        "pdf_path": str(pdf_path) if pdf_path else None, "report": report,
        "master_result": report.get("master_ats_match"),
        "result": report.get("ats_match"),
        "used_master_fallback": bool(report.get("fallback_to_master")),
        "downloads": {
            "txt": f"/api/ats/download?name={text_path.name}",
            "pdf": f"/api/ats/download?name={Path(pdf_path).name}" if pdf_path else None,
            "docx": f"/api/ats/download?name={docx_path.name}" if docx_path else None,
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
    ensure_browser_runtime()

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
    ensure_browser_runtime()

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
        self.action_thread: threading.Thread | None = None
        self.action_id = 0

    def connections(self) -> dict:
        result = login_connection_status()
        result["action"] = vars(self.action)
        return result

    def _run_action(self, action_id: int, kind: str, task) -> None:
        try:
            message = task()
        except Exception as exc:  # surfaced verbatim in the local UI
            message = f"Failed: {exc}"
        with self.lock:
            # An abandoned browser-agent login may have been replaced by a new
            # attempt. Never let the older thread overwrite the newer status.
            if action_id == self.action_id:
                self.action = ActionState(False, kind, str(message))
                self.action_thread = None

    def begin(self, kind: str, task, *, replace_kinds: tuple[str, ...] = ()) -> dict:
        previous_thread: threading.Thread | None = None
        with self.lock:
            if self.action.running:
                if self.action.kind not in replace_kinds:
                    raise RuntimeError(f"Another connection action is running: {self.action.kind}")
                # Closing an OAuth browser tab does not stop the CLI waiting on
                # its callback. Cancel that stale attempt before starting over.
                self.cancel_event.set()
                previous_thread = self.action_thread
        if previous_thread and previous_thread is not threading.current_thread():
            previous_thread.join(timeout=3)
        with self.lock:
            if self.action.running:
                raise RuntimeError(
                    f"The previous connection is still stopping: {self.action.kind}. Try again in a moment."
                )
            self.cancel_event = threading.Event()
            self.action_id += 1
            action_id = self.action_id
            self.action = ActionState(True, kind, "Starting…")
            thread = threading.Thread(
                target=self._run_action, args=(action_id, kind, task), daemon=True,
            )
            self.action_thread = thread
        thread.start()
        return {"ok": True, "message": f"{kind} started; follow the opened Chrome window."}

    def connect_google(self, browser_mode: str = "managed") -> dict:
        if browser_mode != "managed":
            raise ValueError("SelfApply connections use managed Chrome.")

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
                from selfapply.apply.chrome import mark_shared_session_updated
                mark_shared_session_updated()
                return f"Google verified as {email}."
        return self.begin(
            "Google sign-in",
            task,
            replace_kinds=("Connect Codex account", "Connect Claude account"),
        )

    def test_portal(self, name: str, browser_mode: str = "managed") -> dict:
        targets = portal_targets()
        if name not in targets:
            raise ValueError("Unknown portal.")
        if browser_mode != "managed":
            raise ValueError("SelfApply connections use managed Chrome.")

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
        if browser_mode != "managed":
            raise ValueError("SelfApply connections use managed Chrome.")
        rows = self.connections()["portals"]
        names = [
            row["name"] for row in rows
            if row["enabled"]
            and row["status"] not in {"connected", "manual_connected"}
        ]
        if not names:
            return {"ok": True, "message": "All enabled job sites are connected."}

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
            cancel_event = self.cancel_event
            command = [name, "login"] if name == "codex" else [name, "auth", "login"]
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 600
            while process.poll() is None and time.monotonic() < deadline:
                if cancel_event.wait(0.25):
                    process.terminate()
                    return f"{name.title()} account connection cancelled."
            if process.poll() is None:
                process.terminate()
                return f"{name.title()} account connection timed out."
            output = process.stdout.read() if process.stdout else ""
            if process.returncode:
                raise RuntimeError(output.strip()[-500:] or "CLI login failed.")
            if not browser_agent_status().get(name, {}).get("cli_authenticated"):
                raise RuntimeError(
                    f"{name.title()} login window closed, but the account was not linked. "
                    "Choose Connect and finish the browser authorization before closing it."
                )
            _set_browser_agent_link(name, True)
            return f"{name.title()} account connected."
        return self.begin(
            f"Connect {name.title()} account",
            task,
            replace_kinds=("Connect Codex account", "Connect Claude account"),
        )

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
        _set_browser_agent_link(name, False)
        if checked.returncode:
            raise RuntimeError(
                f"{name.title()} was unlinked from SelfApply, but its CLI logout failed."
            )
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
<button data-tab="connected-accounts" onclick="showTab('connected-accounts');renderPrimaryConnections(lastSettings)"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 8 7 6a3 3 0 0 0-4 4l3 3a3 3 0 0 0 4 0l2-2M15 16l2 2a3 3 0 0 0 4-4l-3-3a3 3 0 0 0-4 0l-2 2M8.5 15.5l7-7"/></svg></span><span>Connections</span></button>
<button data-tab="settings" onclick="showTab('settings')"><span class="nav-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19 13.5v-3l-2-.7-.7-1.7.9-1.9-2.1-2.1-1.9.9-1.7-.7L10.5 2h-3l-.7 2-1.7.7-1.9-.9-2.1 2.1.9 1.9-.7 1.7-2 .7v3l2 .7.7 1.7-.9 1.9 2.1 2.1 1.9-.9 1.7.7.7 2h3l.7-2 1.7-.7 1.9.9 2.1-2.1-.9-1.9.7-1.7 2-.4Z" transform="translate(2.5 0) scale(.78 1)"/></svg></span><span>Settings</span></button>
</nav><div class="sidebar-tools"><div class="profile-switcher"><button id="profileButton" class="profile-button" type="button" onclick="toggleProfileMenu()" aria-haspopup="true" aria-expanded="false"><span id="profileAvatar" class="profile-avatar">?</span><span><b id="profileName">My profile</b><small>Switch profile</small></span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></button><div id="profileMenu" class="profile-menu" hidden><div class="profile-menu-head"><b>Profiles</b><small>Up to 5 local profiles</small></div><div id="profileRows"></div><button id="addProfileButton" class="profile-add" type="button" onclick="createProfile()">+ Add profile</button><button class="profile-manage" type="button" onclick="showTab('settings');toggleProfileMenu(false)">Manage profiles</button></div></div><div class="developer-links"><a href="https://github.com/Gobi-96" target="_blank" rel="noopener">GitHub · Gobi-96</a><span><a href="https://github.com/Gobi-96/SelfApply/issues" target="_blank" rel="noopener">Contribute</a><a href="https://github.com/sponsors/Gobi-96" target="_blank" rel="noopener">Sponsor</a></span></div><p class="sidebar-note">Private by design. Stored only on this device.</p></div></aside><main><div id="notice" class="notice">Loading…</div>
<section id="onboarding" class="tab onboarding-page"><div class="onboarding-hero"><span class="eyebrow">Welcome to SelfApply</span><h1>Set up your job search</h1><p>Complete five essentials before applications can be submitted. Your profile, resume, and keys stay on this computer.</p><div class="onboarding-progress"><i id="onboardingProgress"></i></div><b id="onboardingProgressText">Checking setup…</b></div><div id="onboardingSteps" class="onboarding-steps"></div><div id="onboardingFinish" class="onboarding-finish" hidden><b>You’re ready.</b><span>Review your limits before starting Self Apply.</span><button type="button" onclick="showTab('review-apply')">Continue to Self Apply</button></div></section>
<section id="overview" class="tab active page"><div class="page-head overview-head"><div><span class="eyebrow">Your job search</span><h1>Overview</h1><p class="muted" id="overviewUpdated">Loading…</p></div><button class="ghost" onclick="loadOverview()">Refresh</button></div>
<div id="overviewStats" class="stat-cards"></div>
<div class="overview-grid">
<section class="card activity-card"><div class="section-heading"><div><span class="eyebrow">Momentum</span><h2>Applied over the last 7 days</h2></div><span class="activity-legend"><i class="dot dot-green"></i>Submitted</span></div><div id="dailyChart" class="daily-chart"></div></section>
<section id="profileCard" class="card profile-card"></section>
</div>
<section class="card recent-success"><div class="section-heading"><div><span class="eyebrow">Recent success</span><h2>Applications sent</h2><p class="muted compact">Your latest submitted roles.</p></div><button class="ghost" onclick="showTab('applications')">View all</button></div><div id="recentApplied" class="recent-applied-viewport" aria-live="polite"></div></section>
</section>
<section id="connected-accounts" class="tab settings connections-page">
<div class="page-title-row"><div><span class="eyebrow">Setup</span><h1>Connections</h1><p class="muted">Connect your automation agent and Google account. Job-site sign-ins can be added now or when needed.</p></div><button class="secondary" onclick="showTab('api')">Advanced settings</button></div><input id="connectionBrowser" type="hidden" value="managed">
<section class="card connection-section"><div class="connection-section-head"><span class="connection-step">1</span><div><h2>Required automation</h2><p>Connect an AI provider and one browser agent.</p></div><b id="connectionRequiredSummary" class="connection-count">Checking…</b></div><div id="primaryConnections" class="agent-grid"></div></section>
<section id="googleConnectionCard" class="card connection-section"><div class="connection-section-head"><span class="connection-step">2</span><div><h2>Google account <span class="req">*</span></h2><p>Sign in once in SelfApply's browser. The session is reused when a job site offers Google sign-in.</p></div><span id="googleRequiredBadge" class="badge failed">Action needed</span></div><div id="google"></div></section>
<section class="card connection-section"><div class="connection-section-head"><span class="connection-step">3</span><div><h2>Job-site sign-ins <small class="optional-label">Recommended</small></h2><p>Connect enabled sites now to reduce interruptions. Missing portal accounts do not block SelfApply.</p></div><b id="portalConnectionSummary" class="connection-count">Checking…</b></div><div class="actions portal-batch-actions"><button id="connectAll" onclick="post('/api/connections/all',{browser:val('connectionBrowser')})">Connect enabled sites with Google</button><button id="cancelConnect" class="danger" style="display:none" onclick="post('/api/connections/cancel',{})">Stop connecting</button><button id="manualDone" class="secondary" style="display:none" onclick="post('/api/connections/manual/done',{})">Login completed</button></div><details class="portal-list"><summary><span>Manage individual job sites</span><span class="muted">Open list</span></summary><div id="portals"></div></details></section>
</section>
<section id="review-apply" class="tab card settings"><h2>Self Apply</h2><p id="selfApplyIntro" class="muted">Review the essentials, then start applying.</p><div id="readiness" class="readiness incomplete"><b>Setup incomplete</b><div id="readinessText">Checking setup…</div></div><div id="stats" class="stats"></div><div id="current" class="muted"></div><div id="workerPlan" class="worker-plan"></div><div id="activeRunSummary" class="active-run-summary" hidden></div><div class="form worker-setup-controls">
<label>Browser agent<select id="applyAgent"><option value="codex">Codex</option><option value="claude">Claude</option></select></label><input id="browserMode" type="hidden" value="managed">
<label>Maximum spend (USD)<input id="spend" type="number" min="0.01" step="0.01" value="10"></label><label>Total applied target<input id="target" type="number" min="1" value="500"></label>
<label class="confirm wide master-resume-choice"><input id="useMasterResume" type="checkbox" onchange="saveApplicationResumeMode()"><span><b>Use my master resume for every application</b><small>Skip resume rewriting and upload the same master resume to every job.</small></span></label>
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
<section id="ats" class="tab card settings"><div class="page-title-row"><div><h2>Resume tools</h2><p class="muted">Check your match or create a tailored resume for one job.</p></div><button class="secondary" onclick="showTab('document-vault')">Resume files</button></div><div class="form ats-form"><label class="wide">Choose a saved job<select id="atsJob"><option value="">Loading jobs…</option></select></label><details class="advanced wide"><summary>Use a different job description or link</summary><div class="form-section-grid"><label class="wide">Employer job link<input id="atsJobLink" type="url" placeholder="https://company.com/jobs/software-engineer"></label><label>Job title<input id="atsJobTitle" placeholder="Role from the job posting"></label><label>Employer name<input id="atsCompany" placeholder="Example Company"></label><label class="wide">Job description<textarea id="atsJobDescription" class="ats-jd" placeholder="Paste the complete responsibilities and requirements here."></textarea></label><label>Resume to analyze<select id="atsResumeSource"><option value="master">Master resume</option><option value="tailored">Existing tailored resume</option></select></label><label>Scoring method<input value="Keyword, skill, and section match" disabled></label></div></details></div><div class="actions"><button id="analyzeAtsButton" onclick="analyzeAts()">Check match</button><button id="tailorAtsButton" class="secondary" onclick="tailorAndScore()">Create tailored resume</button></div><div id="atsResult" class="ats-result"><div class="empty">Choose a saved job to begin.</div></div></section>
<section id="account" class="tab card settings"><div class="page-title-row"><div><h2>Profile</h2><p class="muted">We fill what we can from your master resume. Review the imported details and add only what is missing.</p></div><button class="secondary" onclick="showTab('document-vault')">Replace resume</button></div><div id="resumeImportState" class="profile-import-note">Resume details are imported automatically after upload. Work authorization and preferences still require your review.</div><div class="form">
<h3 class="wide">Personal information</h3><label>Full name <span class="req">*</span><input id="p_full_name" required placeholder="e.g. Jordan Lee"></label><label>Preferred name<input id="p_preferred_name" placeholder="e.g. Jordan"></label><label>Email <span class="req">*</span><input id="p_email" type="email" required placeholder="e.g. jordan@example.com"></label><label>Phone <span class="req">*</span><input id="p_phone" type="tel" required placeholder="e.g. +1 510 555 0123"></label><label>Street address <span class="req">*</span><input id="p_address" required placeholder="e.g. 123 Main Street"></label><label>Country <span class="req">*</span><select id="p_country" required onchange="locationCountryChanged()"></select><input id="p_country_other" class="other-field" placeholder="Type country"></label><label>State / province <span class="req">*</span><select id="p_province_state" required onchange="locationStateChanged()"></select><input id="p_province_state_other" class="other-field" placeholder="Type state or province"></label><label>City <span class="req">*</span><select id="p_city" required></select><input id="p_city_other" class="other-field" placeholder="Type city"></label><label>Postal / ZIP code <span class="req">*</span><input id="p_postal_code" required placeholder="e.g. postal or ZIP code"></label><label>LinkedIn URL<input id="p_linkedin_url" type="url" placeholder="e.g. https://linkedin.com/in/name"></label><label>GitHub URL<input id="p_github_url" type="url" placeholder="e.g. https://github.com/name"></label><label>Portfolio URL<input id="p_portfolio_url" type="url" placeholder="e.g. https://portfolio.example"></label><label>Website URL<input id="p_website_url" type="url" placeholder="e.g. https://example.com"></label><label>Dedicated job-site password<div class="secret-wrap"><input id="jobPassword" type="password" placeholder="Leave blank to keep saved password"><button type="button" class="eye" onclick="toggleSecret('jobPassword',this)" aria-label="Show password">👁</button></div><div id="passwordState" class="secret-note"></div></label>
<h3 class="wide">Work authorization &amp; employment disclosures</h3><label>Authorized to work<select id="authorized"><option value="true">Yes</option><option value="false">No</option></select></label><label>Needs sponsorship now or in future<select id="sponsorship"><option value="false">No</option><option value="true">Yes</option></select></label><label>Permit / immigration status<input id="p_work_permit_type" placeholder="F1 OPT, Citizen, PR…"></label><label>Subject to a non-compete or similar agreement<select id="restrictiveCovenant"><option value="">Not answered</option><option value="false">No</option><option value="true">Yes</option></select><small>Optional until an employer asks; unknown answers stop only that application.</small></label>
<h3 class="wide">Compensation & experience</h3><label>Expected annual salary<input id="p_salary_expectation" inputmode="numeric" placeholder="e.g. 150000"></label><label>Currency<select id="p_salary_currency"><option>USD</option><option>CAD</option><option>EUR</option><option>GBP</option><option>INR</option></select></label><label>Minimum salary<input id="p_salary_range_min" inputmode="numeric" placeholder="e.g. 130000"></label><label>Maximum salary<input id="p_salary_range_max" inputmode="numeric" placeholder="e.g. 175000"></label><label>Total years of experience<input id="p_years_of_experience_total" type="number" min="0" placeholder="e.g. 8"></label><label>Highest education<select id="p_education_level"><option>Bachelor's</option><option>Master's</option><option>PhD</option><option>Associate</option><option>High school</option><option>Self-taught</option><option>Other</option></select></label><label>Current title<input id="p_current_title" placeholder="e.g. your current role"></label><label>Target role <span class="req">*</span><input id="p_target_role" required placeholder="e.g. your target role"></label><label>Earliest start date<select id="p_earliest_start_date"><option>Immediately</option><option>Within 2 weeks</option><option>Within 1 month</option><option>Negotiable</option></select></label>
<h3 class="wide">Voluntary EEO answers</h3><label>Gender<select id="p_gender"><option>Decline to self-identify</option><option>Man</option><option>Woman</option><option>Non-binary</option></select></label><label>Race / ethnicity<select id="p_race_ethnicity"><option>Decline to self-identify</option><option>Asian (Asian Indian)</option><option>Asian</option><option>Black or African American</option><option>Hispanic or Latino</option><option>White</option><option>Two or more races</option></select></label><label>Sexual orientation<select id="p_sexual_orientation"><option>Decline to self-identify</option><option>Heterosexual</option><option>Gay or Lesbian</option><option>Bisexual</option></select></label><label>Veteran status<select id="p_veteran_status"><option>Decline to self-identify</option><option>I am not a protected veteran</option><option>I identify as a protected veteran</option></select></label><label>Disability status<select id="p_disability_status"><option>Decline to self-identify</option><option>No, I do not have a disability</option><option>Yes, I have a disability</option></select></label>
<h3 class="wide">Skills and preserved resume facts</h3><label class="wide">Programming languages (comma-separated)<textarea id="p_programming_languages" placeholder="e.g. Python, Java, Rust, SQL"></textarea></label><label class="wide">Frameworks and libraries (comma-separated)<textarea id="p_frameworks" placeholder="e.g. React, Django, Spring, TensorFlow"></textarea></label><label class="wide">Tools and platforms (comma-separated)<textarea id="p_tools" placeholder="e.g. AWS, Docker, Tableau, Salesforce"></textarea></label><div class="wide confirmed-skills"><label class="confirm"><input id="p_skill_expansion_enabled" type="checkbox" onchange="saveSkillTailoringPreferences()"> Automatically add relevant JD skills while tailoring resumes for applications.</label><label class="confirm"><input id="p_allow_unverified_claims" type="checkbox" checked onchange="saveSkillTailoringPreferences()"> Never block resume creation for unsupported or newly introduced claims.</label><span class="warn compact">Creative mode may add skills or responsibilities that are not in the master resume. They are shown as warnings, not blockers. Review the resume before representing those claims to an employer.</span><label>Skill-tailoring strength <b id="atsAggressivenessValue">50%</b><input id="atsAggressiveness" type="range" min="0" max="100" step="5" value="50" oninput="atsAggressivenessValue.textContent=atsAggressiveness.value+'%'" onchange="saveSkillTailoringPreferences()"></label><span class="muted compact">50% makes moderate changes. Higher values rewrite more of the summary, technical skills, projects, and relevant experience. Changes save automatically and are used by both ATS Matcher and automatic applications.</span></div><label class="wide">Companies that must remain (comma-separated)<textarea id="p_preserved_companies" placeholder="e.g. Example Corp, Acme Labs"></textarea></label><label class="wide">Projects that must remain (comma-separated)<textarea id="p_preserved_projects" placeholder="e.g. Inventory Platform, Customer Analytics Portal"></textarea></label><label class="wide">School names to preserve (comma-separated)<input id="p_preserved_school" placeholder="e.g. Example University, City College"></label><label class="wide">Real metrics that must remain (comma-separated)<textarea id="p_real_metrics" placeholder="e.g. 99.9% uptime, 50k users, reduced latency by 35%"></textarea></label><label class="wide">Exact work-history headings (one per line)<textarea id="p_experience_entries" placeholder="e.g. Product Manager, Example Company — Jan 2022-Present"></textarea><span class="muted compact">Keep employer, title, and dates together exactly as they appear on the master resume.</span></label><label class="wide">Exact education entries (one per line)<textarea id="p_education_entries" placeholder="e.g. BA Economics, Example University, 2020"></textarea></label></div><div class="actions"><button onclick="saveProfile()">Save account & profile</button></div></section>
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
<label>Location mode<select id="s_location_mode"><option value="local_and_remote">Remote + on-site/hybrid within my radius</option><option value="local">On-site/hybrid within my radius</option><option value="remote">Remote only</option></select><span class="muted compact">The radius applies to local commuting searches. Remote roles are searched separately.</span></label>
<h3 class="wide">Discovery sources <span class="req">*</span></h3>
<div id="sourceChecks" class="source-grid wide"><label><input id="sourceJobspy" type="checkbox"> Major job boards</label><label><input id="sourceWorkday" type="checkbox"> Workday employers</label><label><input id="sourceDirect" type="checkbox"> Direct and remote boards</label></div>
<details class="wide source-details"><summary>Major job boards <span id="majorCount" class="badge"></span></summary><div class="detail-tools"><button type="button" class="ghost" onclick="toggleChecks('#siteChecks input',true);updateBoardCounts()">Select all</button><button type="button" class="ghost" onclick="toggleChecks('#siteChecks input',false);updateBoardCounts()">Clear</button></div><div id="siteChecks" class="check-grid"><label><input type="checkbox" value="indeed" onchange="updateBoardCounts()"> Indeed</label><label><input type="checkbox" value="linkedin" onchange="updateBoardCounts()"> LinkedIn</label><label><input type="checkbox" value="glassdoor" onchange="updateBoardCounts()"> Glassdoor</label><label><input type="checkbox" value="zip_recruiter" onchange="updateBoardCounts()"> ZipRecruiter</label><label><input type="checkbox" value="google" onchange="updateBoardCounts()"> Google Jobs</label></div></details>
<details class="wide source-details"><summary>Direct and remote boards <span id="directCount" class="badge"></span></summary><div class="detail-tools"><button type="button" class="ghost" onclick="toggleChecks('#directChecks input',true);updateBoardCounts()">Select all</button><button type="button" class="ghost" onclick="toggleChecks('#directChecks input',false);updateBoardCounts()">Clear</button></div><div id="directChecks" class="check-grid"></div></details>
<label class="wide">Target job titles — one per line <span class="req">*</span><textarea id="s_titles" required placeholder="e.g.&#10;Product Manager&#10;Data Analyst&#10;Registered Nurse"></textarea></label>
</div><div class="actions"><button onclick="saveJobs()">Save job preferences</button></div></section>
<section id="document-vault" class="tab card settings"><h2>Document vault</h2><p class="muted">Your master resume, plus every tailored resume SelfApply has generated for a specific job.</p><div class="form"><h3 class="wide">Master resume <span class="req">*</span></h3><label class="wide resume-drop-field">Upload PDF, Word DOCX, or plain-text resume<input id="resumeFile" type="file" accept=".pdf,.docx,.txt" onchange="uploadResume()"><div id="resumeState" class="secret-note">Choose a file to upload. Processing starts automatically.</div><div class="muted">PDF and DOCX files are automatically converted to plain text for AI scoring and tailoring.</div></label></div><h3>Tailored resumes</h3><div id="documentList" class="doc-list"><div class="empty">Loading…</div></div></section>
<section id="api" class="tab card settings"><h2>API & AI</h2><p class="muted">Connect an AI service for scoring and resume tailoring. SelfApply detects the provider and model for you.</p><div class="form ai-simple-form"><label class="wide">API key<div class="secret-wrap"><input id="a_api_key" type="password" oninput="markAIUntested()" placeholder="Paste an OpenAI, Claude, or Gemini API key"><button type="button" class="eye" onclick="toggleSecret('a_api_key',this)" aria-label="Show API key">👁</button></div><div id="apiKeyState" class="secret-note"></div></label><div class="wide choice-divider"><span>or</span></div><label class="wide">Local model URL<input id="a_local_url" type="url" oninput="markAIUntested()" placeholder="e.g. http://localhost:11434"><span class="muted compact">Works with Ollama, LM Studio, and compatible local servers.</span></label><input id="a_provider" type="hidden" value="none"><select id="a_model_select" hidden></select><input id="a_model_custom" type="hidden"><div id="aiDetectionState" class="wide connection-result">Enter an API key or local URL, then test the connection.</div><details class="advanced wide"><summary>Optional CAPTCHA service</summary><div class="form-section-grid"><label class="wide">CapSolver key<div class="secret-wrap"><input id="a_capsolver" type="password" placeholder="Enter a new key or keep the saved key"><button type="button" class="eye" onclick="toggleSecret('a_capsolver',this)" aria-label="Show CapSolver key">👁</button></div><div id="capsolverState" class="secret-note"></div><button type="button" id="deleteCapsolver" class="danger mini" onclick="deleteSecret('capsolver')">Delete saved key</button></label></div></details></div><div class="actions"><button class="secondary" onclick="testAIConnection()">Test connection</button><button id="saveAIConnection" onclick="saveAI()">Save connection</button><button type="button" id="deleteApiKey" class="danger" onclick="deleteSecret('provider')">Remove AI connection</button></div><h3>Browser automation account <span class="req">*</span></h3><p class="muted">Test or unlink each connection here. This is separate from the content API.</p><div id="agentStates" class="agent-grid"></div><h3>Use my existing Chrome tab</h3><ol class="muted"><li>Install the Playwright MCP Bridge extension.</li><li>Open the extension and copy its PLAYWRIGHT_MCP_EXTENSION_TOKEN.</li><li>Paste and save it below, keep Chrome open, then choose “My existing Chrome tab” on Self Apply.</li></ol><div class="actions"><a class="link-button secondary" href="https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm" target="_blank" rel="noopener">Install Chrome extension</a></div><div class="form"><label class="wide">Chrome extension token<div class="secret-wrap"><input id="a_extension" type="password" placeholder="Enter a new token or keep the saved token"><button type="button" class="eye" onclick="toggleSecret('a_extension',this)" aria-label="Show extension token">👁</button></div><div id="extensionState" class="secret-note"></div><button type="button" id="deleteExtension" class="danger mini" onclick="deleteSecret('extension')">Delete saved token</button></label></div><div class="actions"><button onclick="saveAI()">Save Chrome connection</button></div></section>
<section id="settings" class="tab card settings"><div class="page-title-row"><div><h2>Settings</h2><p class="muted">Profiles, appearance, privacy, and local data controls.</p></div></div><section class="preference-card profile-management"><div><span class="eyebrow">Local profiles</span><h3>Manage profiles</h3><p class="muted">Each profile has separate applications, resumes, keys, sign-ins, and browser data.</p></div><div id="settingsProfileRows" class="settings-profile-rows"></div></section><div class="settings-grid"><section class="preference-card"><div><span class="eyebrow">Appearance</span><h3>Theme</h3><p class="muted">Light is the default. Your choice is saved only in this browser.</p></div><div class="theme-options"><button id="themeLight" class="theme-option" type="button" role="radio" onclick="setTheme('light')"><span class="theme-preview"><i></i><i></i><i></i></span><b>Light</b><small>Bright and clear</small></button><button id="themeDark" class="theme-option" type="button" role="radio" onclick="setTheme('dark')"><span class="theme-preview preview-dark"><i></i><i></i><i></i></span><b>Dark</b><small>Lower-light workspace</small></button></div></section><section class="preference-card danger-zone"><div><span class="eyebrow">Current profile data</span><h3>Reset this profile</h3><p class="muted">Stops its worker and removes this profile's application history, resumes, keys, settings, sign-ins, and logs.</p></div><label class="backup-choice"><input id="resetBackup" type="checkbox" checked><span><b>Back up before resetting</b><small>Optional. Saved privately on this device.</small></span></label><button type="button" class="danger" onclick="resetSelfApply()">Reset current profile</button><small id="resetState" class="muted">Uncheck backup to permanently delete the current data.</small></section></div><section class="preference-card restore-card"><div><span class="eyebrow">Recovery</span><h3>Restore from a backup</h3><p class="muted">Restores profile fields, applications, resumes, job preferences, API keys, connections, and saved Google/job-site sessions. Backups remain private on this device.</p></div><div id="backupRows" class="backup-rows"><div class="empty compact">Checking for backups…</div></div></section></section></main></div>
<dialog id="appActionDialog" class="app-action-dialog" aria-labelledby="appActionTitle"><div class="app-dialog-icon" id="appActionIcon" aria-hidden="true">?</div><div class="app-dialog-copy"><span id="appActionEyebrow" class="eyebrow">Confirm action</span><h2 id="appActionTitle">Are you sure?</h2><p id="appActionMessage" class="muted"></p></div><label id="appActionInputWrap" class="app-dialog-field" hidden><span id="appActionInputLabel">Value</span><input id="appActionInput" autocomplete="off"></label><label id="appActionCheckWrap" class="app-dialog-check" hidden><input id="appActionCheck" type="checkbox"><span><b id="appActionCheckLabel">Create a backup</b><small id="appActionCheckHelp"></small></span></label><div class="app-dialog-actions"><button id="appActionCancel" type="button" class="ghost" onclick="finishAppDialog(false)">Cancel</button><button id="appActionConfirm" type="button" onclick="finishAppDialog(true)">Continue</button></div></dialog>
<dialog id="resumeCompareDialog" class="resume-compare-dialog" aria-labelledby="resumeCompareTitle"><div class="compare-head"><div><span class="eyebrow">Tailored resume</span><h2 id="resumeCompareTitle">What changed</h2><p id="resumeCompareHelp" class="muted">Red was replaced from the master. Green is in the tailored version.</p></div><button type="button" class="dialog-close" onclick="resumeCompareDialog.close()" aria-label="Close comparison">×</button></div><div class="compare-toolbar" role="group" aria-label="Resume view"><button id="resumeViewUnified" type="button" class="view-toggle active" onclick="showResumeCompareMode('unified')" aria-pressed="true">Unified</button><button id="resumeViewSplit" type="button" class="view-toggle" onclick="showResumeCompareMode('split')" aria-pressed="false">Split</button><button id="resumeViewPdf" type="button" class="view-toggle view-pdf" onclick="showResumeCompareMode('pdf')" aria-pressed="false"><span aria-hidden="true">👁</span> PDF</button></div><div id="resumeCompareBody" class="resume-compare-body"><div class="empty">Loading changes…</div></div></dialog>
<script>
const token='__TOKEN__',notice=document.getElementById('notice');
const connected=s=>s==='connected'||s==='manual_connected';
let appDialogResolve=null;
function openAppDialog(options={}){let danger=!!options.danger;appActionEyebrow.textContent=options.eyebrow||(danger?'Please confirm':'SelfApply');appActionTitle.textContent=options.title||'Confirm action';appActionMessage.textContent=options.message||'';appActionIcon.textContent=danger?'!':(options.icon||'?');appActionDialog.classList.toggle('danger-dialog',danger);appActionConfirm.className=danger?'danger':'';appActionConfirm.textContent=options.confirmLabel||'Continue';appActionInputWrap.hidden=!options.input;appActionInputLabel.textContent=options.inputLabel||'Value';appActionInput.value=options.inputValue||'';appActionInput.placeholder=options.inputPlaceholder||'';appActionCheckWrap.hidden=!options.checkbox;appActionCheck.checked=options.checked!==false;appActionCheckLabel.textContent=options.checkboxLabel||'Create a backup';appActionCheckHelp.textContent=options.checkboxHelp||'';if(appActionDialog.open)appActionDialog.close();appActionDialog.showModal();setTimeout(()=>options.input?appActionInput.select():appActionConfirm.focus(),0);return new Promise(resolve=>appDialogResolve=resolve)}
function finishAppDialog(confirmed){let resolve=appDialogResolve;appDialogResolve=null;let result=confirmed?{confirmed:true,value:appActionInput.value,checked:appActionCheck.checked}:null;appActionDialog.close();if(resolve)resolve(result)}
appActionDialog.addEventListener('cancel',event=>{event.preventDefault();finishAppDialog(false)});appActionDialog.addEventListener('click',event=>{if(event.target===appActionDialog)finishAppDialog(false)});appActionInput.addEventListener('keydown',event=>{if(event.key==='Enter')finishAppDialog(true)});
async function appConfirm(title,message,options={}){return !!(await openAppDialog({...options,title,message}))}
async function appPrompt(title,message,options={}){let result=await openAppDialog({...options,title,message,input:true});return result?result.value:null}
async function get(p){return(await fetch(p)).json()}
async function post(p,b){try{let r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json','X-SelfApply-Token':token},body:JSON.stringify(b)}),j=await r.json();if(r.status===403&&j.error==='Invalid control token.'){notice.textContent='This page expired. Reloading the current SelfApply controls…';setTimeout(()=>location.reload(),600);return {ok:false,error:j.error}}notice.textContent=j.message||j.error||'Done';if(j.restart){document.body.classList.add('booting');setTimeout(waitForProfileRestart,700);return j}await refresh();return j}catch(error){notice.textContent=`Request failed: ${error.message||error}`;return {ok:false,error:String(error.message||error)}}}
async function waitForProfileRestart(){for(let attempt=0;attempt<30;attempt++){try{let response=await fetch('/api/profiles',{cache:'no-store'});if(response.ok){location.href='/?profile='+Date.now()+'#onboarding';return}}catch(error){}await new Promise(resolve=>setTimeout(resolve,350))}location.reload()}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){
 let [s,c]=await Promise.all([get('/api/status'),get('/api/connections')]),g=c.google||{},busy=!!c.action.running;
 workerRunning=!!s.running;
 notice.classList.toggle('busy',busy||s.running);
 let googleBusy=busy&&c.action.kind==='Google sign-in';
 let agentBusy=busy&&/^Connect (Codex|Claude) account$/.test(c.action.kind||''),agentActionBox=document.getElementById('agentConnectAction');
 if(agentActionBox){agentActionBox.hidden=!agentBusy;agentActionBox.innerHTML=agentBusy?`<span><b>${esc(c.action.kind)}</b><small>${esc(c.action.message||'Waiting for browser authorization…')}</small></span><button class="danger" onclick="post('/api/connections/cancel',{})">Cancel connection</button>`:''}
 googleConnectionCard.classList.toggle('required-card',!g.verified);googleRequiredBadge.className=`badge ${g.verified?'connected':'failed'}`;googleRequiredBadge.textContent=g.verified?'Ready':'Action needed';
 let googleHelp=g.stale?'The previous Google session is no longer authenticated. Reconnect once to refresh it.':g.email||(agentBusy?`${c.action.kind} is still waiting. Connecting Google will cancel it.`:'SelfApply uses this for supported “Continue with Google” sign-ins.');
 document.getElementById('google').innerHTML=`<div class=row><span><b>${g.verified?'Google is connected':g.stale?'Reconnect your Google account':'Connect your Google account'}</b><br><small>${esc(googleHelp)}</small></span><span class=portal-actions>${g.verified?`<button class=ghost onclick="unlinkAccount('Google')" ${busy?'disabled':''}>Disconnect</button>`:`<button onclick="post('/api/connections/google',{browser:'managed'})" ${busy&&!agentBusy?'disabled':''}>${googleBusy?'Opening Google…':agentBusy?'Connect Google instead':g.stale?'Reconnect Google':'Connect Google account'}</button>`}</span></div>`;
 document.getElementById('portals').innerHTML=c.portals.map(p=>{let ok=connected(p.status),portalBusy=busy&&(c.action.kind===`Connect ${p.name}`||c.action.message?.startsWith(`Connecting ${p.name} `)),failed=p.status.startsWith('failed'),state=ok?'Connected':portalBusy?'Connecting…':failed?'Needs attention':p.enabled?'Not connected':'Not enabled';return `<div class=row><span><b>${esc(p.name)}</b><br><small>${esc(p.host)} · ${p.enabled?'Enabled in Job Preferences':'Not enabled in Job Preferences'}</small></span><span class=portal-actions><span class="badge ${ok?'connected':failed?'failed':''}">${esc(state)}</span>${ok?`<button class=ghost onclick='unlinkAccount(${JSON.stringify(p.name)})' ${busy?'disabled':''}>Disconnect</button>`:`<button onclick='googlePortal(${JSON.stringify(p.name)})' ${busy||!g.verified?'disabled':''}>${portalBusy?'Connecting…':'Use Google'}</button><button class=secondary onclick='manualPortal(${JSON.stringify(p.name)})' ${busy?'disabled':''}>Sign in manually</button>`}</span></div>`}).join('');
 let enabledPortals=c.portals.filter(p=>p.enabled),remaining=enabledPortals.filter(p=>!connected(p.status)).length,x=s.counts;
 portalConnectionSummary.textContent=`${enabledPortals.length-remaining} of ${enabledPortals.length} enabled sites connected`;
 connectAll.style.display=remaining&&!busy?'inline-block':'none';connectAll.disabled=!g.verified;connectAll.title=g.verified?'Connect enabled sites using your Google account':'Connect Google above first';connectAll.textContent=g.verified?`Connect ${remaining} enabled site${remaining===1?'':'s'}`:'Connect Google first';
 cancelConnect.style.display=busy?'inline-block':'none';manualDone.style.display=(busy&&c.action.kind.startsWith('Manual login'))?'inline-block':'none';
 document.getElementById('stats').innerHTML=`<div class=stat><b>${x.applied}</b>Applied</div><div class=stat><b>${x.failed}</b>Needs attention</div><div class=stat><b>${x.in_progress}</b>Active</div><div class=stat><b>${x.ready}</b>Ready</div>`;
 let w=s.worker||{},workerCount=Number(w.workers||1),testMode=w.mode==='test run',agentName=w.agent==='claude'?'Claude':'Codex',workerLabel=w.agent&&w.agent!=='unknown'?`${agentName} · ${workerCount} worker${workerCount===1?'':'s'} · ${w.browser||'managed'}`:'running',prep=s.preparation||{};document.getElementById('current').textContent=s.current_job?`${testMode?'Testing now':'Applying now'}: ${s.current_job.title} @ ${s.current_job.site} · ${workerLabel}`:s.running&&prep.detail?`Working now: ${prep.label||'Preparing jobs'} — ${prep.detail} · ${workerLabel}`:`Worker: ${s.running?workerLabel:'stopped'}`;
 if(s.running&&readiness.classList.contains('complete')){readiness.querySelector('b').textContent='Self Apply is running';readinessText.textContent=prep.detail||'Finding and preparing jobs…'}
 renderWorkerPlan(s);
 let selfApplyPage=document.getElementById('review-apply'),runSummary=document.getElementById('activeRunSummary');
 selfApplyPage.classList.toggle('worker-active',s.running);
 selfApplyIntro.textContent=s.running?(testMode?'Testing one application without submitting.':'Your applications are running. Settings are locked until you stop.'):'Review the essentials, then start applying.';
 runSummary.hidden=!s.running;
 if(s.running)runSummary.innerHTML=`<span><small>Run mode</small><b>${testMode?'Safe test':'Self Apply'}</b></span><span><small>Browser agent</small><b>${esc(agentName)}</b></span><span><small>Workers</small><b>${workerCount}</b></span><span><small>Browser</small><b>Managed Chrome</b></span>`;
 startSelfApply.style.display=s.running?'none':'inline-flex';testRunButton.style.display=s.running?'none':'inline-flex';stopSelfApply.style.display=s.running?'inline-flex':'none';startSelfApply.disabled=s.running;testRunButton.disabled=s.running;testRunButton.title=Number(x.ready||0)<1?'SelfApply will first find and prepare a matching job, then test its form.':'Fill one ready application without submitting it.';startSelfApply.textContent='Start Self Apply';testRunButton.textContent='Test one application — no submit';stopSelfApply.disabled=false;stopSelfApply.textContent='Stop Self Apply';
 let hs=document.getElementById('historyWorkerState');if(hs){hs.hidden=!s.running;if(s.running){let hc='worker-strip worker-live',hh=s.current_job?`<b>${s.current_job.starting?'Starting application':'Applying now'}</b><span>${esc(s.current_job.title)} · ${esc(s.current_job.site)}</span>`:'<b>Starting application</b><span>Preparing the selected job…</span>';if(hs.className!==hc)hs.className=hc;if(hs.innerHTML!==hh)hs.innerHTML=hh}}
 if(busy)notice.textContent=c.action.message||c.action.kind+' is running…';else notice.textContent=c.action.message||'Ready';
 let completedConnectionAction=!busy&&c.action.kind?`${c.action.kind}:${c.action.message}`:'';if(completedConnectionAction&&completedConnectionAction!==lastConnectionAction){let guided=document.body.classList.contains('onboarding-mode');lastConnectionAction=completedConnectionAction;await loadSettings();if(guided)advanceOnboarding()}
 let historyVisible=document.getElementById('applications').classList.contains('active'),refreshHistory=historyVisible&&(s.running||historyWorkerWasRunning);historyWorkerWasRunning=s.running;if(refreshHistory)await loadApplications()
}
const el=id=>document.getElementById(id),val=id=>el(id).value;
let lastSettings={},readinessKnown=false,lastConnectionAction='',selectedFailureReason='',historyWorkerWasRunning=false,lastHistorySignature='',historyPageSize=20,workerRunning=false,retryableFailureCount=0;
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
function showOtherField(selectId,otherId){let select=el(selectId),other=el(otherId),show=select?.value==='Other';if(!other)return;other.hidden=!show;other.style.display=show?'block':'none';other.disabled=!show;if(show)setTimeout(()=>other.focus(),0)}
function fillSelect(id,options,selected,otherId){let node=el(id),known=options.includes(selected),previousOther=otherId?val(otherId):'';node.innerHTML=options.map(x=>`<option value="${esc(x)}">${x==='Other'?'Other / type manually':esc(x)}</option>`).join('');if(otherId&&!options.includes('Other'))node.innerHTML+='<option value="Other">Other / type manually</option>';node.value=known?selected:(selected?'Other':options[0]||'Other');if(otherId){let other=el(otherId);other.value=known?'':(selected==='Other'?previousOther:(selected||previousOther));showOtherField(id,otherId)}}
function locationCountryChanged(stateValue='',cityValue=''){let country=val('p_country');showOtherField('p_country','p_country_other');let regions=Object.keys(LOCATIONS[country]||LOCATIONS.Other);fillSelect('p_province_state',regions,stateValue,'p_province_state_other');locationStateChanged(cityValue)}
function locationStateChanged(cityValue=''){let country=val('p_country'),state=val('p_province_state');showOtherField('p_province_state','p_province_state_other');let cities=((LOCATIONS[country]||{})[state]||[]);fillSelect('p_city',cities.length?cities:['Other'],cityValue,'p_city_other')}
function locationValue(id){return val(id)==='Other'?val(id+'_other').trim():val(id)}
function initLocation(country,state,city){p_city.onchange=()=>showOtherField('p_city','p_city_other');fillSelect('p_country',Object.keys(LOCATIONS),country,'p_country_other');if(!country){p_country.innerHTML='<option value="">Choose country…</option>'+p_country.innerHTML;p_country.value='';showOtherField('p_country','p_country_other');fillSelect('p_province_state',['Other'],state,'p_province_state_other');fillSelect('p_city',['Other'],city,'p_city_other');return}locationCountryChanged(state,city)}
function jobLocationPart(id){return val(id)==='Other'?val(id+'_other').trim():val(id)}
function syncJobLocation(){let parts=[jobLocationPart('s_city'),jobLocationPart('s_state'),jobLocationPart('s_country')].filter(Boolean);put('s_location',parts.join(', '))}
function jobCountryChanged(stateValue='',cityValue=''){let country=val('s_country');showOtherField('s_country','s_country_other');fillSelect('s_state',Object.keys(LOCATIONS[country]||LOCATIONS.Other),stateValue,'s_state_other');jobStateChanged(cityValue);syncJobLocation()}
function jobStateChanged(cityValue=''){let country=val('s_country'),state=val('s_state');showOtherField('s_state','s_state_other');let cities=((LOCATIONS[country]||{})[state]||[]);fillSelect('s_city',cities.length?cities:['Other'],cityValue,'s_city_other');syncJobLocation()}
function initJobLocation(country,state,city,legacy=''){if(!country&&legacy){let parts=legacy.split(',').map(x=>x.trim()).filter(Boolean);country=parts.pop()||'';state=parts.pop()||'';city=parts.join(', ')}fillSelect('s_country',Object.keys(LOCATIONS),country,'s_country_other');if(!country){s_country.innerHTML='<option value="">Choose country…</option>'+s_country.innerHTML;s_country.value='';s_country_other.style.display='none';fillSelect('s_state',['Other'],state,'s_state_other');fillSelect('s_city',['Other'],city,'s_city_other')}else jobCountryChanged(state,city);syncJobLocation()}
function enableAnyCurrency(){let old=el('p_salary_currency');if(!old||old.tagName!=='SELECT')return;let input=document.createElement('input');input.id=old.id;input.name=old.name;input.placeholder='e.g. USD, CAD, EUR, JPY';input.setAttribute('list','currencyCodes');let list=document.createElement('datalist');list.id='currencyCodes';list.innerHTML=['USD','CAD','EUR','GBP','INR','AUD','JPY','CHF','CNY','SGD','NZD','BRL','MXN','ZAR'].map(x=>`<option value="${x}">`).join('');old.replaceWith(input);input.after(list)}
function toggleSidebar(force){let open=typeof force==='boolean'?force:!document.body.classList.contains('nav-open');document.body.classList.toggle('nav-open',open);mobileMenu.setAttribute('aria-expanded',String(open))}
function setTheme(theme){let value=theme==='dark'?'dark':'light';document.documentElement.dataset.theme=value;localStorage.setItem('selfapply-theme',value);document.querySelectorAll('.theme-option').forEach(x=>{let active=x.id===`theme${value[0].toUpperCase()+value.slice(1)}`;x.classList.toggle('active',active);x.setAttribute('aria-checked',String(active))})}
function toggleTheme(){setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark')}
let profileListing={profiles:[],active_profile_id:'',limit:5};
function toggleProfileMenu(force){let menu=el('profileMenu'),open=typeof force==='boolean'?force:menu.hidden;menu.hidden=!open;el('profileButton').setAttribute('aria-expanded',String(open))}
async function loadProfiles(){profileListing=await get('/api/profiles');let active=profileListing.profiles.find(x=>x.active)||profileListing.profiles[0];if(active){profileAvatar.textContent=active.initials;profileAvatar.style.background=active.color;profileName.textContent=active.name}profileRows.innerHTML=profileListing.profiles.map(x=>`<button type="button" class="profile-row ${x.active?'active':''}" onclick="switchProfile('${x.id}')"><span class="profile-avatar" style="background:${esc(x.color)}">${esc(x.initials)}</span><span><b>${esc(x.name)}</b><small>${x.application_count} application${x.application_count===1?'':'s'}</small></span>${x.active?'<em>Current</em>':''}</button>`).join('');addProfileButton.disabled=profileListing.profiles.length>=profileListing.limit;addProfileButton.title=addProfileButton.disabled?'Five-profile limit reached':'';renderSettingsProfiles()}
function renderSettingsProfiles(){if(!el('settingsProfileRows'))return;settingsProfileRows.innerHTML=profileListing.profiles.map(x=>`<article class="settings-profile-row"><span class="profile-avatar" style="background:${esc(x.color)}">${esc(x.initials)}</span><span><b>${esc(x.name)}</b><small>${x.application_count} submitted application${x.application_count===1?'':'s'} · ${x.active?'Current profile':'Stored locally'}</small></span><div>${x.active?'<span class="badge connected">Active</span>':`<button class="ghost" onclick="switchProfile('${x.id}')">Switch</button>`}<button class="ghost" onclick="renameProfile('${x.id}')">Rename</button><button class="danger ghost" onclick="deleteProfile('${x.id}')">Delete</button></div></article>`).join('')}
async function createProfile(){if(profileListing.profiles.length>=profileListing.limit)return;toggleProfileMenu(false);let name=await appPrompt('Create a profile','Use a person’s name or a short label such as “Product roles.”',{eyebrow:'New local profile',inputLabel:'Profile name',inputPlaceholder:'e.g. Alex Morgan',confirmLabel:'Create profile'});if(name===null||!name.trim())return;await post('/api/profiles/create',{name:name.trim()})}
async function switchProfile(id){if(id===profileListing.active_profile_id){toggleProfileMenu(false);return}toggleProfileMenu(false);await post('/api/profiles/switch',{id})}
async function renameProfile(id){let current=profileListing.profiles.find(x=>x.id===id),name=await appPrompt('Rename profile','This changes only the profile label shown in SelfApply.',{eyebrow:'Profile settings',inputLabel:'Profile name',inputValue:current?.name||'',confirmLabel:'Save name'});if(name===null||!name.trim())return;let result=await post('/api/profiles/update',{id,name:name.trim(),color:current?.color});if(result.ok)await loadProfiles()}
async function deleteProfile(id){let current=profileListing.profiles.find(x=>x.id===id);if(!current)return;let choice=await openAppDialog({title:`Delete ${current.name}?`,message:`Type “${current.name}” exactly. This removes this profile’s applications, resumes, keys, sign-ins, and browser data.`,eyebrow:'Delete local profile',input:true,inputLabel:'Profile name',inputPlaceholder:current.name,checkbox:true,checked:true,checkboxLabel:'Back up before deleting',checkboxHelp:'Optional. The backup stays private on this device.',confirmLabel:'Delete profile',danger:true});if(!choice)return;let result=await post('/api/profiles/delete',{id,confirmation:choice.value,backup:choice.checked});if(result.ok&&!result.restart)await loadProfiles()}
function simplifyProfileSections(){let form=document.querySelector('#account>.form');if(!form||form.dataset.grouped)return;form.dataset.grouped='true';let headings=[...form.querySelectorAll(':scope>h3')];headings.forEach((heading,index)=>{let details=document.createElement('details');details.className='profile-section wide';details.open=index===0;let summary=document.createElement('summary');summary.textContent=heading.textContent.trim();let grid=document.createElement('div');grid.className='form-section-grid';let node=heading.nextSibling;while(node&&!headings.includes(node)){let next=node.nextSibling;grid.appendChild(node);node=next}details.append(summary,grid);heading.replaceWith(details)})}
function simplifyJobPreferences(){let form=document.querySelector('#find-jobs>.form');if(!form||form.dataset.grouped)return;form.dataset.grouped='true';let city=el('s_city')?.closest('label');if(city){let accepted=document.createElement('label');accepted.className='wide';accepted.innerHTML='Other accepted onsite or hybrid locations — one per line<textarea id="s_accept_locations" placeholder="e.g.&#10;Toronto&#10;Mississauga&#10;Greater Toronto Area"></textarea><span class="muted compact">Add other cities or regions where you can commute. Remote jobs are evaluated separately.</span>';city.after(accepted)}let fields=['s_distance','s_hours_old','s_results'].map(id=>el(id)?.closest('label')).filter(Boolean),anchor=fields[0];if(!anchor)return;let details=document.createElement('details');details.className='advanced wide';let summary=document.createElement('summary');summary.textContent='Search limits';let grid=document.createElement('div');grid.className='form-section-grid';anchor.before(details);details.append(summary,grid);fields.forEach(x=>grid.appendChild(x))}
function syncAcceptedLocations(){let node=el('s_accept_locations');if(node&&!node.dataset.loaded){node.value=((lastSettings.searches||{}).location?.accept_patterns||[]).join('\n');node.dataset.loaded='true'}}
function simplifyAtsAnalysis(){if(atsResult.querySelector(':scope>.analysis-details'))return;let nodes=[...atsResult.children].filter(x=>x.matches('.ats-breakdown,.ats-group'));if(!nodes.length)return;let details=document.createElement('details');details.className='analysis-details';let summary=document.createElement('summary');summary.textContent='See match details';details.append(summary);nodes[0].before(details);nodes.forEach(x=>details.appendChild(x))}
function organizeAutomationSettings(){let page=el('api');if(!page||page.dataset.organized)return;page.dataset.organized='true';let form=page.querySelector(':scope>.ai-simple-form'),actions=form?.nextElementSibling,agentHeading=[...page.querySelectorAll(':scope>h3')].find(x=>x.textContent.includes('Browser automation account')),agentIntro=agentHeading?.nextElementSibling,agents=el('agentStates');let wrap=(id,label,nodes)=>{let first=nodes.find(Boolean);if(!first)return;let panel=document.createElement('section');panel.id=id;panel.className='automation-panel';panel.dataset.label=label;first.before(panel);nodes.filter(Boolean).forEach(x=>panel.appendChild(x))};wrap('automationAI','AI provider',[form,actions]);wrap('automationAgent','Browser agent',[agentHeading,agentIntro,agents])}
function setAutomationView(kind='all'){organizeAutomationSettings();let page=el('api'),titles={ai:['AI provider','Add or update the service used for scoring and resume tailoring.'],agent:['Browser agent','Connect and test Codex or Claude for application form automation.']},panels={ai:el('automationAI'),agent:el('automationAgent')},focused=!!titles[kind];Object.entries(panels).forEach(([name,node])=>{if(node)node.hidden=focused&&name!==kind});let heading=page.querySelector('.page-title-row h2'),copy=page.querySelector('.page-title-row p');if(focused){heading.textContent=titles[kind][0];copy.textContent=titles[kind][1];page.classList.add('automation-focused')}else{heading.textContent='AI & browser settings';copy.textContent='Configure the services SelfApply uses.';page.classList.remove('automation-focused')}}
function focusAutomation(kind){showTab('api');setAutomationView(kind);requestAnimationFrame(()=>el(`automation${kind[0].toUpperCase()+kind.slice(1)}`)?.scrollIntoView({block:'start'}))}
function showTab(id,push=true){if(!document.getElementById(id)?.classList.contains('tab'))id='overview';if(readinessKnown&&!lastSettings.readiness?.complete&&WORKSPACE_TABS.has(id))id='onboarding';if(id==='api')setAutomationView('all');document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.id===id));document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('active',x.dataset.tab===id));if(push&&location.hash!==`#${id}`)history.pushState({tab:id},'',`#${id}`);document.title=`${document.querySelector(`nav button[data-tab="${id}"]`)?.textContent.trim()||'SelfApply'} · SelfApply`;window.scrollTo(0,0);toggleSidebar(false);if(id==='applications')loadApplications();if(id==='ats')loadAtsJobs();if(id==='overview')loadOverview();if(id==='document-vault')loadDocuments();if(id==='connected-accounts')renderPrimaryConnections(lastSettings)}
function put(id,value){let node=el(id);if(node)node.value=value??''}
function section(obj,key){return(obj&&obj[key])||{}}
function renderPrimaryConnections(d){let node=el('primaryConnections');if(!node)return;let a=d.ai||{},agents=d.browser_agent||{},provider=a.provider||'none',providerReady=provider!=='none'&&!!a.model&&(provider==='local'?!!a.local_url:!!(a.has_openai_key||a.has_gemini_key||a.has_anthropic_key)),connectedAgents=['codex','claude'].filter(name=>agents[name]?.authenticated),agentReady=connectedAgents.length>0,providerName={openai:'OpenAI',anthropic:'Claude / Anthropic',gemini:'Google Gemini',local:'Local AI'}[provider]||'Not configured',readyCount=[providerReady,agentReady].filter(Boolean).length;connectionRequiredSummary.textContent=`${readyCount} of 2 ready`;node.innerHTML=`<div class="agent-card ${providerReady?'':'required-card'}"><span><b>AI provider</b><small>${providerReady?esc(providerName+' · '+a.model):'Adds job scoring and resume tailoring'}</small></span><div class="portal-actions"><span class="badge ${providerReady?'connected':'failed'}">${providerReady?'Ready':'Action needed'}</span><button class="${providerReady?'ghost':''}" onclick="focusAutomation('ai')">${providerReady?'Manage':'Connect'}</button></div></div><div class="agent-card ${agentReady?'':'required-card'}"><span><b>Browser agent</b><small>${agentReady?esc(connectedAgents.map(x=>x==='codex'?'Codex':'Claude').join(' and ')):'Connect Codex or Claude to complete forms'}</small></span><div class="portal-actions"><span class="badge ${agentReady?'connected':'failed'}">${agentReady?'Ready':'Action needed'}</span><button class="${agentReady?'ghost':''}" onclick="focusAutomation('agent')">${agentReady?'Manage':'Connect'}</button></div></div>`}
async function loadSettings(){let d=await get('/api/settings'),p=d.profile||{},personal=section(p,'personal'),auth=section(p,'work_authorization'),disclosures=section(p,'employment_disclosures'),comp=section(p,'compensation'),exp=section(p,'experience'),eeo=section(p,'eeo_voluntary'),avail=section(p,'availability'),skills=section(p,'skills_boundary'),facts=section(p,'resume_facts');
 lastSettings=d;readinessKnown=true;for(let key of ['full_name','preferred_name','email','phone','address','postal_code','linkedin_url','github_url','portfolio_url','website_url'])put('p_'+key,personal[key]);initLocation(personal.country||'',personal.province_state||'',personal.city||'');syncAcceptedLocations();
 put('authorized',String(auth.legally_authorized_to_work??true));put('sponsorship',String(auth.require_sponsorship??false));put('p_work_permit_type',auth.work_permit_type);put('restrictiveCovenant','restrictive_covenant' in disclosures?String(disclosures.restrictive_covenant):'');
 for(let key of ['salary_expectation','salary_currency','salary_range_min','salary_range_max'])put('p_'+key,comp[key]);
 for(let key of ['years_of_experience_total','education_level','current_title','target_role'])put('p_'+key,exp[key]);put('p_earliest_start_date',avail.earliest_start_date);
 for(let key of ['gender','race_ethnicity','sexual_orientation','veteran_status','disability_status'])put('p_'+key,eeo[key]);
 for(let key of ['programming_languages','frameworks','tools'])put('p_'+key,(skills[key]||[]).join(', '));p_skill_expansion_enabled.checked=skills.skill_expansion_enabled??skills.transferable_enabled??true;p_allow_unverified_claims.checked=skills.allow_unverified_claims??true;useMasterResume.checked=(p.resume_preferences?.application_mode||'tailored')==='master';let savedAggressiveness=Math.round((skills.target_aggressiveness??.5)*100);atsAggressiveness.value=savedAggressiveness;atsAggressivenessValue.textContent=savedAggressiveness+'%';for(let key of ['preserved_companies','preserved_projects','real_metrics'])put('p_'+key,(facts[key]||[]).join(', '));put('p_preserved_school',facts.preserved_school);put('p_experience_entries',(facts.experience_entries||[]).map(x=>typeof x==='object'?(x.header||''):x).filter(Boolean).join('\n'));put('p_education_entries',(facts.education_entries||[]).join('\n'));
 passwordState.textContent=d.has_job_site_password?'Saved password configured':'';
 let s=d.searches||{},defs=s.defaults||{},loc=s.location||{},disc=d.discovery||{};initJobLocation(loc.country||'',loc.state||'',loc.city||'',defs.location||'');put('s_distance',String(defs.distance??50));put('s_hours_old',String(defs.hours_old??72));put('s_results',String(defs.results_per_site??50));let locations=s.locations||[],hasRemote=locations.some(x=>x?.remote),hasLocal=locations.some(x=>!x?.remote),locationMode=s.location_mode||(hasRemote?(hasLocal?'local_and_remote':'remote'):(locations.length?'local':'local_and_remote'));put('s_location_mode',locationMode);const majorDefaults=['indeed','linkedin','glassdoor','zip_recruiter','google'];let sites=new Set(Object.prototype.hasOwnProperty.call(s,'sites')?(s.sites||[]):majorDefaults);document.querySelectorAll('#siteChecks input').forEach(x=>x.checked=sites.has(x.value));sourceJobspy.checked=disc.jobspy;sourceWorkday.checked=disc.workday;sourceDirect.checked=disc.direct;let direct=new Set(disc.selected_direct_sites||[]);directChecks.innerHTML=(disc.available_direct_sites||[]).map(name=>`<label><input type="checkbox" value="${esc(name)}" ${direct.has(name)?'checked':''} onchange="updateBoardCounts()"> ${esc(name)}</label>`).join('');put('s_titles',(s.queries||[]).map(x=>x.query).join('\n'));if(!resumeUploadRunning){let ready=d.resume.pdf||d.resume.word||d.resume.text;resumeState.className=`secret-note ${ready?'upload-complete':''}`;resumeState.textContent=ready?'Master resume ready. Choose another file to replace it.':'Choose a file to upload. Processing starts automatically.'}updateBoardCounts();
 if(!loc.country&&personal.country)initJobLocation(personal.country,personal.province_state||'',personal.city||'',defs.location||'');
 let a=d.ai||{};put('a_provider',a.provider||'none');put('a_local_url',a.provider==='local'?(a.local_url||''):'');a_model_select.innerHTML=`<option value="${esc(a.model||'')}">${esc(a.model||'')}</option>`;let hasProviderKey=a.has_openai_key||a.has_gemini_key||a.has_anthropic_key,hasAI=a.provider&&a.provider!=='none';apiKeyState.textContent=hasProviderKey?'Saved API key configured (hidden)':'Paste a new key to connect';deleteApiKey.style.display=hasAI?'inline-block':'none';saveAIConnection.disabled=!hasAI;aiDetectionState.textContent=hasAI?`Connected: ${a.provider==='anthropic'?'Claude / Anthropic':a.provider==='gemini'?'Google Gemini':a.provider==='openai'?'OpenAI':'Local AI'} · ${a.model}`:'Enter an API key or local URL, then test the connection.';capsolverState.textContent=a.has_capsolver_key?'Saved key configured (hidden)':'Not configured';deleteCapsolver.style.display=a.has_capsolver_key?'inline-block':'none';extensionState.textContent=a.has_extension_token?'Saved token configured (hidden)':'Not configured';deleteExtension.style.display=a.has_extension_token?'inline-block':'none';let runtime=d.browser_runtime||{};browserRuntime.innerHTML=`<div class="agent-card ${runtime.ready?'':'required-card'}"><span><b>Browser runtime <span class=req>*</span></b><small>${esc(runtime.detail||'Checking browser dependencies…')} Playwright is included with SelfApply; the old Chrome extension is not used.</small></span><div class=portal-actions><span class="badge ${runtime.ready?'connected':'failed'}">${runtime.ready?'Ready':'Required'}</span><button class=secondary onclick="testBrowserRuntime()">Test launch</button>${!runtime.chromium&&runtime.python_package?'<button onclick="installBrowserRuntime()">Install browser</button>':''}${!runtime.python_package?'<span class="muted compact">Reinstall SelfApply to restore Playwright.</span>':''}${!runtime.node_runner?'<a class="link-button secondary" target="_blank" rel="noopener" href="https://nodejs.org/">Install Node.js</a>':''}${!runtime.chrome?'<a class="link-button secondary" target="_blank" rel="noopener" href="https://www.google.com/chrome/">Install Chrome</a>':''}</div></div>`;let agents=d.browser_agent||{};agentStates.innerHTML=['codex','claude'].map(name=>{let x=agents[name]||{},ok=x.authenticated,label=name==='codex'?'Codex':'Claude',state=ok?'Connected':x.cli_authenticated?'Not linked':x.installed?'Needs login':'Not installed';return `<div class=agent-card><b>${label}</b><small>${esc(x.detail||'Not installed')}</small><div class=portal-actions><span class="badge ${ok?'connected':''}">${state}</span>${ok?`<button class=secondary onclick="testAgent('${name}')">Test</button><button class=danger onclick="unlinkAgent('${name}')">Unlink</button>`:x.installed?`<button onclick="post('/api/settings/browser-agent/connect',{name:'${name}'})">Connect</button>`:''}</div></div>`}).join('');syncAgentChoices(agents);renderReadiness(d);renderOnboarding(d);let requested=location.hash.slice(1)||'overview';if(!d.readiness?.complete&&WORKSPACE_TABS.has(requested))showTab('onboarding',false);}
function profileText(keys){let out={};for(let key of keys)out[key]=val('p_'+key);return out}
async function saveProfile(){let guided=document.body.classList.contains('onboarding-mode'),personal=profileText(['full_name','preferred_name','email','phone','address','postal_code','linkedin_url','github_url','portfolio_url','website_url']);personal.country=locationValue('p_country');personal.province_state=locationValue('p_province_state');personal.city=locationValue('p_city');let disclosureValue=val('restrictiveCovenant');let facts=profileText(['preserved_companies','preserved_projects','preserved_school','real_metrics','experience_entries','education_entries']);let payload={personal,work_authorization:{...profileText(['work_permit_type']),legally_authorized_to_work:val('authorized')==='true',require_sponsorship:val('sponsorship')==='true'},employment_disclosures:disclosureValue===''?{}:{restrictive_covenant:disclosureValue==='true'},compensation:profileText(['salary_expectation','salary_currency','salary_range_min','salary_range_max']),experience:profileText(['years_of_experience_total','education_level','current_title','target_role']),availability:profileText(['earliest_start_date']),eeo_voluntary:profileText(['gender','race_ethnicity','sexual_orientation','veteran_status','disability_status']),skills_boundary:{...profileText(['programming_languages','frameworks','tools']),skill_expansion_enabled:p_skill_expansion_enabled.checked,allow_unverified_claims:p_allow_unverified_claims.checked,target_aggressiveness:(atsAggressiveness.value/100)},resume_facts:facts,job_site_password:jobPassword.value};let result=await post('/api/settings/profile',payload);jobPassword.value='';await loadSettings();if(guided&&result.ok)advanceOnboarding()}
async function saveSkillTailoringPreferences(){await post('/api/settings/profile',{skills_boundary:{skill_expansion_enabled:p_skill_expansion_enabled.checked,allow_unverified_claims:p_allow_unverified_claims.checked,target_aggressiveness:(atsAggressiveness.value/100)}})}
async function saveApplicationResumeMode(){let mode=useMasterResume.checked?'master':'tailored';let result=await post('/api/settings/profile',{resume_preferences:{application_mode:mode}});notice.textContent=result.ok?(mode==='master'?'Master resume will be used for every application.':'Per-job resume tailoring is enabled.'):(result.error||'Could not save resume preference.');await loadSettings()}
function toggleChecks(selector,state){document.querySelectorAll(selector).forEach(x=>x.checked=state)}
async function saveJobs(){let guided=document.body.classList.contains('onboarding-mode');syncJobLocation();let result=await post('/api/settings/search',{location:val('s_location'),country:jobLocationPart('s_country'),state:jobLocationPart('s_state'),city:jobLocationPart('s_city'),primary_location:val('s_location'),accept_patterns:val('s_accept_locations'),distance:+val('s_distance'),hours_old:+val('s_hours_old'),results_per_site:+val('s_results'),location_mode:val('s_location_mode'),source_jobspy:sourceJobspy.checked,source_workday:sourceWorkday.checked,source_direct:sourceDirect.checked,sites:[...document.querySelectorAll('#siteChecks input:checked')].map(x=>x.value),direct_sites:[...document.querySelectorAll('#directChecks input:checked')].map(x=>x.value),target_titles:val('s_titles')});await loadSettings();if(guided&&result.ok)advanceOnboarding()}
let resumeUploadRunning=false;
async function uploadResume(){let f=resumeFile.files[0];if(!f||resumeUploadRunning)return;resumeUploadRunning=true;resumeFile.disabled=true;resumeState.className='secret-note upload-working';resumeState.textContent=`Uploading ${f.name}…`;notice.textContent='Reading and converting your resume…';let reader=new FileReader();reader.onerror=()=>finishResumeUpload(null,'SelfApply could not read that file. Choose it again or try another supported format.');reader.onload=async()=>{let encoded=String(reader.result||'').split(',',2)[1];if(!encoded){finishResumeUpload(null,'SelfApply could not read that file.');return}resumeState.textContent='Converting resume and importing profile details…';let result=await post('/api/settings/resume',{name:f.name,data:encoded,replace_profile:true});await loadSettings();if(!result.ok){finishResumeUpload(null,result.error||'Resume upload failed.');return}finishResumeUpload(result);showTab('account');let state=el('resumeImportState');if(state)state.textContent=result.profile_fields_imported?.length?`${result.profile_fields_imported.length} details were updated from your resume. Review the required fields, then save.`:'Resume saved. We could not confidently identify profile fields, so review the required fields below.'};reader.readAsDataURL(f)}
function finishResumeUpload(result,error=''){resumeUploadRunning=false;resumeFile.disabled=false;resumeFile.value='';resumeState.className=`secret-note ${error?'upload-error':'upload-complete'}`;resumeState.textContent=error||`${result?.message||'Resume uploaded and converted.'} You can replace it by choosing another file.`;notice.textContent=error||result?.message||'Resume ready.'}
let testedAI=null;
function markAIUntested(){if(document.activeElement===a_api_key&&a_api_key.value)a_local_url.value='';if(document.activeElement===a_local_url&&a_local_url.value)a_api_key.value='';testedAI=null;saveAIConnection.disabled=true;aiDetectionState.textContent='Connection changed. Test it before saving.'}
function renderTestedAIState(){if(!testedAI)return;let label={openai:'OpenAI',anthropic:'Claude / Anthropic',gemini:'Google Gemini',local:'Local AI'}[testedAI.provider]||testedAI.provider;put('a_provider',testedAI.provider);if(testedAI.local_url)put('a_local_url',testedAI.local_url);a_model_select.innerHTML=`<option value="${esc(testedAI.model)}">${esc(testedAI.model)}</option>`;a_model_select.value=testedAI.model;saveAIConnection.disabled=false;aiDetectionState.className='wide connection-result verified';aiDetectionState.innerHTML=`<b>Connection verified</b><span>${esc(label)} · ${esc(testedAI.model)}</span><small>Test passed. Click Save connection to keep this provider.</small>`}
const loadSettingsBase=loadSettings;
loadSettings=async function(){let pending=testedAI?{...testedAI,local_url:val('a_local_url')}:null;await loadSettingsBase();await loadBackups();if(pending){testedAI=pending;renderTestedAIState()}}
async function testAIConnection(){let localUrl=val('a_local_url'),result=await post('/api/settings/ai/probe',{api_key:val('a_api_key'),local_url:localUrl});if(!result.ok){testedAI=null;saveAIConnection.disabled=true;aiDetectionState.className='wide connection-result';aiDetectionState.textContent=result.error||'Connection failed.';return}testedAI={provider:result.provider,model:result.model,local_url:result.provider==='local'?localUrl:''};renderTestedAIState()}
async function saveAI(){let guided=document.body.classList.contains('onboarding-mode'),key=val('a_api_key').trim(),url=val('a_local_url').trim(),saved=lastSettings.ai||{},isNew=!!key||(!!url&&url!==(saved.local_url||''));if(isNew&&!testedAI){notice.textContent='Test the AI connection before saving it.';return}let provider=testedAI?.provider||saved.provider||'none',model=testedAI?.model||saved.model||'';let result=await post('/api/settings/ai',{provider,model,api_key:key,local_url:url||saved.local_url||'',capsolver_key:val('a_capsolver'),extension_token:val('a_extension')});if(result.ok)testedAI=null;a_api_key.value='';a_capsolver.value='';a_extension.value='';await loadSettings();if(guided&&result.ok)advanceOnboarding()}
function renderOnboarding(d){let missing=(d.readiness?.missing||[]),groups=[['document-vault','Resume','Upload once; SelfApply imports contact details, experience, education, and skills for review.'],['account','Review profile','Confirm imported details and answer work-authorization questions.'],['find-jobs','Job preferences','Choose target roles, locations, and sources.'],['api','AI & browser','Connect a content provider and browser agent.'],['connected-accounts','Google sign-in','Connect Google once for supported job-site sign-ins.']],complete=groups.filter(([tab])=>!missing.some(x=>x.tab===tab)).length;onboardingProgress.style.width=`${complete/groups.length*100}%`;onboardingProgressText.textContent=`${complete} of ${groups.length} essentials complete`;onboardingSteps.innerHTML=groups.map(([tab,title,copy],index)=>{let items=missing.filter(x=>x.tab===tab),done=!items.length,missingText=items.map(x=>x.label).join(', ');return `<article class="onboarding-step ${done?'done':''}"><span class="step-number">${done?'✓':index+1}</span><div><b>${esc(title)}</b><p>${esc(copy)}</p>${items.length?`<small>Missing: ${esc(missingText)}</small>`:'<small>Complete</small>'}</div><button type="button" class="${done?'ghost':''}" onclick="showTab('${tab}')">${done?'Review':'Complete step'}</button></article>`}).join('');onboardingFinish.hidden=!d.readiness?.complete;document.body.classList.toggle('onboarding-mode',!d.readiness?.complete)}
function advanceOnboarding(){let next=(lastSettings.readiness?.missing||[])[0];if(next)showTab(next.tab);else showTab('review-apply')}
function backupDate(value){if(!value)return 'Date unavailable';let date=new Date(value);return Number.isNaN(date.valueOf())?value:date.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
async function loadBackups(){let result=await get('/api/settings/backups'),rows=result.backups||[];backupRows.innerHTML=rows.length?rows.map(item=>{let counts=item.counts||{},applied=Number(counts.applied||0),total=Number(counts.total||0)||Object.entries(counts).filter(([key])=>key!=='total').reduce((sum,[,value])=>sum+(Number(value)||0),0);return `<article class="backup-row"><span><b>${esc(backupDate(item.created_at))}</b><small>From ${esc(item.source_profile||'Local profile')} · ${esc(item.reason)}${total?` · ${applied} applied · ${total} saved jobs`:''}</small></span><button type="button" class="secondary" onclick="restoreBackup('${encodeURIComponent(item.id)}','${encodeURIComponent(backupDate(item.created_at))}')">Restore</button></article>`}).join(''):'<div class="empty compact">No backups yet. Select “Back up before resetting” when you reset a profile.</div>'}
async function restoreBackup(encodedId,encodedDate){let id=decodeURIComponent(encodedId),date=decodeURIComponent(encodedDate),confirmation=await appPrompt('Restore this backup?',`This replaces the current profile with the backup from ${date}. SelfApply will first save the current state as a recovery backup. Type RESTORE to continue.`,{eyebrow:'Restore profile',inputLabel:'Type RESTORE',inputPlaceholder:'RESTORE',confirmLabel:'Restore backup',danger:true});if(confirmation===null)return;await post('/api/settings/backup/restore',{backup_id:id,confirmation})}
async function resetSelfApply(){let backup=el('resetBackup').checked,confirmation=await appPrompt('Reset current profile',backup?'A private local backup will be created before this profile is cleared. Type RESET to continue.':'No backup will be created. Applications, resumes, keys, settings, and sign-ins will be permanently cleared. Type RESET to continue.',{eyebrow:'Local data',inputLabel:'Type RESET',inputPlaceholder:'RESET',confirmLabel:'Reset profile',danger:true});if(confirmation===null)return;let result=await post('/api/settings/reset',{confirmation,backup});if(result.ok){resetState.textContent=result.backup?`Backup saved locally: ${result.backup}`:'Current profile data was permanently removed.';await loadSettings();await loadOverview();showTab('onboarding')}}
function toggleSecret(id,button){let node=el(id);node.type=node.type==='password'?'text':'password';button.textContent=node.type==='password'?'👁':'🙈'}
async function deleteSecret(kind){if(!await appConfirm('Remove saved connection?',`The saved ${kind} secret will be removed from this profile.`,{confirmLabel:'Remove',danger:true}))return;await post('/api/settings/secret/delete',{kind});await loadSettings()}
async function testChromeExtension(){let result=await post('/api/settings/extension/test',{});await loadSettings();if(result.ok)extensionState.textContent='Verified — SelfApply can reach your existing Chrome tab.'}
async function installBrowserRuntime(){notice.textContent='Installing Playwright Chromium…';let result=await post('/api/settings/browser-runtime/install',{});await loadSettings();if(result.ok)notice.textContent=result.message}
async function testBrowserRuntime(){notice.textContent='Launching a Playwright test browser…';let result=await post('/api/settings/browser-runtime/test',{});await loadSettings();notice.textContent=result.message||result.error||'Browser runtime test finished.'}
function testAgent(name){post('/api/settings/browser-agent/test',{name})}async function unlinkAgent(name){let label=name==='codex'?'Codex':'Claude';if(!await appConfirm(`Unlink ${label}?`,`SelfApply will stop using this ${label} connection for the current profile.`,{confirmLabel:'Unlink',danger:true}))return;await post('/api/settings/browser-agent/unlink',{name});await loadSettings()}
function syncAgentChoices(agents){let linked=['codex','claude'].filter(name=>agents[name]?.authenticated),preferred=localStorage.getItem('selfapply-agent')||applyAgent.value;applyAgent.innerHTML=linked.length?linked.map(name=>`<option value="${name}">${name==='codex'?'Codex':'Claude'}</option>`).join(''):'<option value="">Connect Codex or Claude first</option>';if(linked.includes(preferred))applyAgent.value=preferred;applyAgent.disabled=linked.length<2}
function updateBoardCounts(){let major=document.querySelectorAll('#siteChecks input'),direct=document.querySelectorAll('#directChecks input');majorCount.textContent=`${[...major].filter(x=>x.checked).length}/${major.length} selected`;directCount.textContent=`${[...direct].filter(x=>x.checked).length}/${direct.length} selected`}
function historyDate(value){if(!value)return 'Date unavailable';let date=new Date(value);return Number.isNaN(date.valueOf())?value:date.toLocaleString([], {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
function duration(ms){if(!ms)return '—';let seconds=Math.round(ms/1000);return seconds<60?`${seconds}s`:`${Math.floor(seconds/60)}m ${seconds%60}s`}
function renderWorkerPlan(s){let ready=Number(s?.counts?.ready||0),threshold=Number(queue.value||50),count=Number(s?.worker?.workers||workers.value||1),testMode=s?.worker?.mode==='test run',p=s?.preparation||{},stage=p.stage||'stopped',finding=stage==='discovering'||stage==='enriching',preparing=['scoring','tailoring'].includes(stage),applying=['ready','applying'].includes(stage),hasJobs=Number(s?.counts?.total||0)>0,pct=Math.max(0,Math.min(100,Number(p.percent||0))),progress=s?.running?`<div class="worker-progress"><div class="worker-progress-head"><b>${esc(p.label||'Preparing your job search')}</b><span>${esc(p.detail||'Starting…')}</span></div><div class="worker-progress-track"><i style="width:${pct}%"></i></div><small>${stage==='discovering'?'New matches appear here as they are found.':stage==='applying'?(testMode?'The form is being checked; final submission is blocked.':'Application workers are submitting prepared jobs.'):'Self Apply continues automatically; no action is needed.'}</small></div>`:'';let html=`${progress}<div class="plan-step ${finding?'active':hasJobs?'done':''}"><b>1. Find matching jobs</b><span>${finding?'Searching enabled sources now…':hasJobs?`${s.counts.total} saved job${s.counts.total===1?'':'s'} found`:`Starts when ready supply is below ${threshold}`}</span></div><div class="plan-step ${preparing?'active':applying?'done':''}"><b>2. Score and tailor matches</b><span>${preparing?esc(p.detail):applying?'Prepared jobs are ready':'Starts automatically after jobs are found'}</span></div><div class="plan-step ${applying?'active':''}"><b>${testMode?'3. Test one application':'3. Apply automatically'}</b><span>${testMode?'Fills and validates one form · never submits':`${ready} ready now · up to ${count} worker${count===1?'':'s'}${s?.running?' · starts automatically':''}`}</span></div>`;if(workerPlan.innerHTML!==html)workerPlan.innerHTML=html}
function resetHistoryList(){historyPageSize=20;lastHistorySignature='';loadApplications()}
function setHistoryStatus(status){selectedFailureReason='';put('historyStatus',status);resetHistoryList()}
function historyStatusChanged(){if(val('historyStatus')!=='failed')selectedFailureReason='';resetHistoryList()}
function filterFailureReason(reason){selectedFailureReason=selectedFailureReason===reason?'':reason;if(selectedFailureReason)put('historyStatus','failed');resetHistoryList()}
let atsJobsLoaded=false;
async function loadAtsJobs(force=false){if(atsJobsLoaded&&!force)return;let d=await get('/api/ats/jobs'),jobs=d.jobs||[],current=atsJob.value;atsJob.innerHTML='<option value="">Choose a stored job…</option>'+jobs.map(x=>`<option value="${esc(x.url)}">${esc(x.title)} · ${esc(x.employer)} · fit ${x.fit_score??'—'}${x.has_tailored?' · tailored':''}</option>`).join('');if(current&&jobs.some(x=>x.url===current))atsJob.value=current;atsJobsLoaded=true}
function atsChips(title,items,kind=''){return `<div class="ats-group"><b>${title} <span>${items.length}</span></b><div class="ats-chips">${items.length?items.slice(0,24).map(x=>`<span class="ats-chip ${kind}">${esc(x)}</span>`).join(''):'<small class="muted">None</small>'}</div></div>`}
function atsPayload(){return {url:atsJob.value,job_link:val('atsJobLink'),title:val('atsJobTitle'),company:val('atsCompany'),job_description:val('atsJobDescription'),resume_source:atsResumeSource.value,target_aggressiveness:(atsAggressiveness.value/100)}}
function atsScoreCard(label,r){let score=Number(r?.overall_score||0),tone=score>=75?'score-good':score>=55?'score-mid':'score-low';return `<div class="ats-score-card"><div class="ats-ring ${tone}"><b>${score.toFixed(0)}</b><span>/100</span></div><div><h3>${esc(label)}</h3><p>${esc(r?.disclaimer||'Estimated ATS match.')}</p></div></div>`}
function atsFailure(data){let report=data.report||{},details=[...(report.validator?.errors||[]),...(report.full_validator?.errors||[])];if(report.judge?.issues&&report.judge.issues!=='none')details.push(report.judge.issues);let message=data.error||data.message||'The tailored resume could not be created.',network=/failed to fetch|networkerror|load failed/i.test(message),guidance=network?'The local SelfApply service did not return this request. Reload the page and try again; your saved AI connection may still be healthy.':'Check API & AI → Test saved provider, correct the issue, and try again.';atsResult.innerHTML=`<div class="ats-error"><b>Resume was not created</b><p>${esc(message)}</p>${details.length?`<ul>${details.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}<small>${esc(guidance)}</small></div>`}
function renderAtsProgress(task){let pct=Math.max(2,Math.min(100,Number(task.percent||2))),elapsed=Number(task.elapsed_seconds||0).toFixed(0);atsResult.innerHTML=`<div class="ats-progress"><div class="progress-head"><b>${esc(task.stage||'Working…')}</b><span>${elapsed}s elapsed</span></div><div class="progress-track"><i style="width:${pct}%"></i></div><small>${pct}% · Keep this page open. Tailoring continues in the background.</small></div>`}
function renderAtsResult(data){let r=data.result||data.report?.ats_match;if(!r){atsFailure(data);return}let s=r.sub_scores||{},report=data.report||{},unverified=(report.unsupported_claim_warnings||report.unverified_claim_warnings||[]),introducedWarning=unverified.length?`<div class="ats-warning"><b>Added content needs review</b><span>${esc(unverified.join(' '))}</span></div>`:'',fidelity=report.format_fidelity?`<div class="ats-group"><b>Format preservation</b><p>${esc(report.format_fidelity==='exact-structure'?'Original DOCX structure, styles, links, headers and layout were retained.':'Best-effort visual preservation was used for this source format.')}</p>${report.format_fidelity_warning?`<small>${esc(report.format_fidelity_warning)}</small>`:''}</div>`:'',comparison=data.master_result?`<div class="ats-comparison">${atsScoreCard('Master resume',data.master_result)}${atsScoreCard('Tailored resume',r)}</div>`:atsScoreCard(data.title||'ATS match',r),downloads=data.downloads?`<div class="ats-downloads"><b>Resume ready for manual application</b><div class="actions">${data.downloads.docx?`<a class="link-button" href="${esc(data.downloads.docx)}" download>Download DOCX</a>`:''}${data.downloads.pdf?`<a class="link-button" href="${esc(data.downloads.pdf)}" download>Download PDF</a>`:''}</div></div>`:'';atsResult.innerHTML=`${introducedWarning}${comparison}<div class="ats-breakdown"><div><b>${s.keyword_match??0}%</b><span>Keyword match</span></div><div><b>${s.skills_coverage??0}%</b><span>Skills coverage</span></div><div><b>${report.changed_block_count??0}</b><span>Resume blocks changed</span></div></div>${downloads}${fidelity}${atsChips('Matched terms',r.matched_keywords||[],'matched')}${atsChips('Still missing',r.missing_keywords||[],'unsupported')}<div class="ats-group"><b>Recommendations</b><ul>${(r.recommendations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`}
const renderAtsResultCore=renderAtsResult;
renderAtsResult=function(data){renderAtsResultCore(data);let changes=data.report?.changes||[];if(!changes.length)return;atsResult.insertAdjacentHTML('beforeend',`<details class="ats-group"><summary><b>See ${changes.length} targeted changes</b></summary><div class="change-list">${changes.map(x=>`<article><small>${esc(x.section||'Resume')}</small><del>${esc(x.before||'')}</del><ins>${esc(x.after||'')}</ins></article>`).join('')}</div></details>`)};
async function pollAtsTask(id){for(;;){let task=await get('/api/ats/task?id='+encodeURIComponent(id));if(task.error){atsFailure(task);return}renderAtsProgress(task);if(task.status==='completed'){renderAtsResult(task.result||{});return}if(task.status==='failed'){atsFailure(task.result||{error:task.error||'Tailoring failed.'});return}await new Promise(resolve=>setTimeout(resolve,1000))}}
async function analyzeAts(){analyzeAtsButton.disabled=true;atsResult.innerHTML='<div class="empty">Reading the job description and analyzing your resume…</div>';try{renderAtsResult(await post('/api/ats/analyze',atsPayload()))}finally{analyzeAtsButton.disabled=false}}
async function tailorAndScore(){let p=atsPayload();if(!p.url&&!p.job_link&&!p.job_description.trim()){notice.textContent='Select a discovered job, paste a job link, or paste the full JD.';return}analyzeAtsButton.disabled=true;tailorAtsButton.disabled=true;renderAtsProgress({stage:'Mapping editable resume blocks',percent:2,elapsed_seconds:0});try{await post('/api/settings/profile',{skills_boundary:{target_aggressiveness:p.target_aggressiveness}});let started=await post('/api/ats/tailor',p);if(!started.task_id){atsFailure(started);return}await pollAtsTask(started.task_id);atsJobsLoaded=false;await loadAtsJobs(true)}catch(error){atsFailure({error:error.message||String(error)})}finally{analyzeAtsButton.disabled=false;tailorAtsButton.disabled=false}}
function historyActions(x){let u=encodeURIComponent(x.url),t=encodeURIComponent(x.title||'this job'),open=`<a class="link-button secondary" href="${esc(x.job_url)}" target="_blank" rel="noopener">${x.status==='applied'?'View job':'Apply myself'}</a>`,resume=x.resume_url?documentView(x.resume_url,'resume PDF',x.resume_compare_name):'',docx=x.resume_docx_url?`<a class="link-button secondary" href="${esc(x.resume_docx_url)}" download>Download DOCX</a>`:'';if(x.status==='running')return `<button class="danger" onclick="cancelHistoryWorker()">Stop</button>${resume}${open}`;if(x.status==='dismissed')return `<button class="secondary" onclick="restoreApplication(decodeURIComponent('${u}'))">Show again</button>${resume}${open}`;if(x.status==='applied')return `${resume}${docx}${open}`;let retry='';if(x.can_run_now&&x.status==='failed')retry=workerRunning?`<button onclick="queueApplication(decodeURIComponent('${u}'))">Add next</button>`:`<button onclick="retryApplication(decodeURIComponent('${u}'),decodeURIComponent('${t}'))">Retry</button>`;else if(x.can_run_now&&x.status==='queued'&&!workerRunning)retry=`<button onclick="retryApplication(decodeURIComponent('${u}'),decodeURIComponent('${t}'))">Apply now</button>`;return `${retry}${resume}${open}`}
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
async function retryApplication(url,title){if(!await appConfirm(`Apply to ${title}?`,'SelfApply will start a worker for only this selected job.',{eyebrow:'Try application again',confirmLabel:'Apply now'}))return;let result=await post('/api/applications/retry-now',retryWorkerPayload(url));if(result.ok)await loadApplications()}
async function queueApplication(url){let result=await post('/api/applications/retry',{url});if(result.ok)await loadApplications()}
async function retryAllApplications(){let count=+workers.value;if(!retryableFailureCount)return;if(!await appConfirm(`Retry ${retryableFailureCount} failed application${retryableFailureCount===1?'':'s'}?`,`SelfApply will use ${count} worker${count===1?'':'s'}. Applied and ineligible jobs will be skipped.`,{eyebrow:'Bulk retry',confirmLabel:'Start retries'}))return;retryAllButton.disabled=true;retryAllButton.textContent=workerRunning?'Adding retries…':'Starting workers…';let result=await post('/api/applications/retry-all',retryWorkerPayload(''));retryAllButton.disabled=false;if(result.ok)await loadApplications()}
async function cancelHistoryWorker(){if(!await appConfirm('Stop the current application?','The current job will be released safely and can be tried again later.',{confirmLabel:'Stop application',danger:true}))return;let result=await post('/api/worker/stop',{});if(result.ok)await loadApplications()}
async function dismissApplication(url,title){if(!await appConfirm(`Hide ${title}?`,'This removes the job from the current list. You can restore it from the Hidden filter.',{confirmLabel:'Hide application'}))return;let result=await post('/api/applications/dismiss',{url});if(result.ok)await loadApplications()}
async function restoreApplication(url){let result=await post('/api/applications/restore',{url});if(result.ok)await loadApplications()}
function renderReadiness(d){document.querySelectorAll('.invalid').forEach(x=>x.classList.remove('invalid'));document.querySelectorAll('nav button').forEach(x=>x.classList.remove('incomplete'));let missing=[...((d.readiness||{}).missing||[])],agent=val('applyAgent'),browser=val('browserMode'),agents=d.browser_agent||{},a=d.ai||{};if(!agents[agent]?.authenticated&&!missing.some(x=>x.field==='agentStates'))missing.push({label:`${agent==='codex'?'Codex':'Claude'} browser connection`,tab:'api',field:'agentStates'});if(browser==='existing'&&!a.extension_verified&&!missing.some(x=>x.field==='a_extension'))missing.push({label:'Verified Chrome extension',tab:'api',field:'a_extension'});for(let item of missing){let field=el(item.field);field?.classList.add('invalid');field?.closest('details')?.setAttribute('open','');if(item.label==='Review contradictory work-authorization answers')el('sponsorship')?.classList.add('invalid');let navTab=item.tab==='api'?'connected-accounts':item.tab==='document-vault'?'review-apply':item.tab;document.querySelector(`nav button[data-tab="${navTab}"]`)?.classList.add('incomplete')}let complete=missing.length===0,heading=readiness.querySelector('b');readiness.className=`readiness ${complete?'complete':'incomplete'}`;heading.textContent=complete?'Setup complete':'Setup incomplete';if(complete)readinessText.textContent='Ready to start Self Apply.';else{let groups={account:0,'find-jobs':0,'document-vault':0,api:0},names={account:'Profile','find-jobs':'Job preferences','document-vault':'Resume tools',api:'Connections'};for(let item of missing)groups[item.tab]=(groups[item.tab]||0)+1;readinessText.innerHTML=Object.entries(groups).filter(([,count])=>count).map(([tab,count])=>`<button class="missing-link" onclick="showTab('${tab}')">${esc(names[tab])}: ${count} missing</button>`).join('')}startSelfApply.disabled=!complete;return complete}
function googlePortal(n){post('/api/connections/portal',{name:n,browser:val('connectionBrowser')})}function manualPortal(n){post('/api/connections/manual',{name:n})}function unlinkAccount(n){post('/api/connections/unlink',{name:n})}
function workerPayload(){return {max_spend:+spend.value,target_applied:+target.value,workers:+workers.value,queue_threshold:+queue.value,max_actions:+actions.value,max_minutes:+minutes.value,agent:val('applyAgent'),browser:val('browserMode'),confirmed:true}}
async function startTestRun(){if(!renderReadiness(lastSettings)){notice.textContent='Complete the red required fields before starting a test run.';return}let label=testRunButton.textContent;testRunButton.disabled=true;testRunButton.textContent='Starting test…';notice.textContent='Starting a no-submit test run…';let result=await post('/api/worker/test-run',workerPayload());if(!result.ok){testRunButton.disabled=false;testRunButton.textContent=label}}
async function startWorker(){if(!renderReadiness(lastSettings)){notice.textContent='Complete the red required fields before starting Self Apply.';return}let authorization=el('confirm');if(!authorization.checked){notice.textContent='Check the authorization box before starting Self Apply.';authorization.focus();return}let label=startSelfApply.textContent;startSelfApply.disabled=true;startSelfApply.textContent='Starting…';notice.textContent='Starting Self Apply…';let result=await post('/api/worker/start',workerPayload());if(!result.ok){startSelfApply.disabled=false;startSelfApply.textContent=label}}
async function stopWorker(){if(!await appConfirm('Stop Self Apply?','The worker will release its current job safely before stopping.',{confirmLabel:'Stop worker',danger:true}))return;stopSelfApply.disabled=true;stopSelfApply.textContent='Stopping…';let result=await post('/api/worker/stop',{});if(!result.ok){stopSelfApply.disabled=false;stopSelfApply.textContent='Stop Self Apply'}}
function docDate(value){if(!value)return '';let date=new Date(value);return Number.isNaN(date.valueOf())?'':date.toLocaleDateString([], {year:'numeric',month:'short',day:'numeric'})}
function documentView(url,label,compareName=''){if(!url)return '';let attrs=compareName?`href="#" onclick="openResumeComparison(event,'${encodeURIComponent(compareName)}','${encodeURIComponent(url)}')"`:`href="${esc(url)}" target="_blank" rel="noopener"`;return `<a class="icon-button document-view" ${attrs} aria-label="View ${esc(label)}" title="View ${esc(label)}"><span aria-hidden="true">👁</span></a>`}
function documentDownload(url,label){return url?`<a class="link-button" href="${esc(url)}" download>Download ${esc(label)}</a>`:''}
let activeResumeChanges=[],activeResumePdf='',activeResumeMode='unified',activeTailoredPreview=null,activeMasterPreview=null;
function resumeDiffEmpty(){return '<div class="empty">The master resume was preserved because no proposed change could be applied safely.</div>'}
function resumeHighlight(change,size,tone,index){let r=change.rect;if(!Array.isArray(r)||r.length!==4||!size?.width||!size?.height)return '';let left=Math.max(0,r[0]/size.width*100),top=Math.max(0,r[1]/size.height*100),width=Math.min(100-left,Math.max(.8,(r[2]-r[0])/size.width*100)),height=Math.min(100-top,Math.max(.8,(r[3]-r[1])/size.height*100));return `<span class="pdf-highlight ${tone}" style="left:${left}%;top:${top}%;width:${width}%;height:${height}%"><b>${index+1}</b></span>`}
function resumePage(preview,index,tone='',changes=[]){if(!preview?.pages?.[index])return '<div class="pdf-page missing-page">Page unavailable</div>';let size=preview.sizes?.[index],marks=changes.map(x=>resumeHighlight(x.change,size,tone,x.index)).join('');return `<div class="pdf-page"><img src="${esc(preview.pages[index])}" alt="Resume page ${index+1}">${marks}<span class="page-number">${index+1}</span></div>`}
function changeCards(changes){return changes.map(({change:x,index})=>`<article class="resume-diff unified-diff"><small><span>${esc(x.section)}</span><em>Change ${index+1}</em></small><div class="diff-line removed"><b>−</b><span>${esc(x.before)}</span></div><div class="diff-line added"><b>+</b><span>${esc(x.after)}</span></div></article>`).join('')}
function pageChanges(page){return activeResumeChanges.map((change,index)=>({change,index})).filter(x=>x.change.page===page)}
function unifiedResumeDiff(){if(!activeTailoredPreview)return '<div class="empty">The full resume preview is unavailable.</div>';return `<div class="unified-document">${activeTailoredPreview.pages.map((_,page)=>{let changes=pageChanges(page);return `<section class="unified-page"><div>${resumePage(activeTailoredPreview,page,'added',changes)}</div><aside>${changes.length?changeCards(changes):'<div class="unchanged-page">No changes on this page</div>'}</aside></section>`}).join('')}</div>`}
function splitResumeDiff(){if(!activeTailoredPreview||!activeMasterPreview)return '<div class="empty">The full master-to-tailored preview is unavailable.</div>';let count=Math.max(activeMasterPreview.page_count||0,activeTailoredPreview.page_count||0);return `<div class="split-labels"><b>Master resume</b><b>Tailored resume</b></div><div class="split-document">${Array.from({length:count},(_,page)=>{let changes=pageChanges(page);return `<section class="split-pages">${resumePage(activeMasterPreview,page,'removed',changes)}${resumePage(activeTailoredPreview,page,'added',changes)}</section>`}).join('')}</div>`}
function cleanResumePdf(){if(!activeTailoredPreview)return '<div class="empty">The full resume preview is unavailable.</div>';return `<div class="clean-pdf-document">${activeTailoredPreview.pages.map((_,page)=>resumePage(activeTailoredPreview,page)).join('')}</div>`}
function showResumeCompareMode(mode){activeResumeMode=['unified','split','pdf'].includes(mode)?mode:'unified';for(let [name,id] of [['unified','resumeViewUnified'],['split','resumeViewSplit'],['pdf','resumeViewPdf']]){let button=el(id),on=name===activeResumeMode;button.classList.toggle('active',on);button.setAttribute('aria-pressed',String(on))}resumeCompareBody.classList.toggle('pdf-mode',activeResumeMode==='pdf');if(activeResumeMode==='pdf'){resumeCompareHelp.textContent='Complete application-ready tailored PDF.';resumeCompareBody.innerHTML=cleanResumePdf();return}if(activeResumeMode==='split'){resumeCompareHelp.textContent='Complete master and tailored PDFs shown side by side.';resumeCompareBody.innerHTML=splitResumeDiff();return}resumeCompareHelp.textContent='Complete tailored PDF with every changed region highlighted. Red is the master text; green is the replacement.';resumeCompareBody.innerHTML=activeResumeChanges.length?unifiedResumeDiff():`${cleanResumePdf()}${resumeDiffEmpty()}`}
async function openResumeComparison(event,name,url){event?.preventDefault();activeResumePdf=decodeURIComponent(url);activeResumeChanges=[];activeTailoredPreview=null;activeMasterPreview=null;resumeCompareTitle.textContent='Loading resume…';resumeCompareBody.innerHTML='<div class="empty">Rendering complete resume pages…</div>';resumeCompareDialog.showModal();let parsed=new URL(activeResumePdf,location.origin),file=parsed.searchParams.get('name')||'',kind=parsed.searchParams.get('kind')||'tailored';let [data,tailored,master]=await Promise.all([get('/api/resumes/changes?name='+name),get('/api/resumes/preview?kind='+encodeURIComponent(kind)+'&name='+encodeURIComponent(file)),get('/api/resumes/preview?kind=master&name=resume.pdf')]);if(data.error){resumeCompareTitle.textContent='Comparison unavailable';resumeCompareBody.innerHTML=`<div class="empty">${esc(data.error)}</div>`;return}activeResumeChanges=data.changes||[];activeTailoredPreview=tailored.error?null:tailored;activeMasterPreview=master.error?null:master;resumeCompareTitle.textContent=activeResumeChanges.length?`${activeResumeChanges.length} change${activeResumeChanges.length===1?'':'s'}`:'No text changes';showResumeCompareMode('unified')}
async function loadDocuments(){documentList.innerHTML='<div class="empty">Loading…</div>';let d=await get('/api/documents'),master=d.master,docs=d.documents||[];masterDocument.innerHTML=master?`<article class="doc-card master-doc"><div><span class="eyebrow">Uploaded master</span><b>${esc(master.name)}</b><small>${esc(master.format)} · updated ${esc(docDate(master.updated))}</small></div><div class="doc-actions">${documentView(master.view,master.format)}${documentDownload(master.download,master.format)}</div></article>`:'<div class="empty">No master resume uploaded.</div>';documentList.innerHTML=docs.length?docs.map(x=>{let view=x.pdf_view?documentView(x.pdf_view,'PDF',x.compare_name):x.docx_view?documentView(x.docx_view,'Word',x.compare_name):'';return `<article class="doc-card"><div><b>${esc(x.name)}</b><small>${esc(docDate(x.created))} · ${esc(x.file_name)}</small></div><div class="doc-actions">${view}${documentDownload(x.pdf,'PDF')}${documentDownload(x.docx,'Word')}</div></article>`}).join(''):'<div class="empty">No tailored resumes yet. Create one from Job matches.</div>'}
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
connectionBrowser.value=localStorage.getItem('selfapply-connection-browser')||'existing';applyAgent.onchange=()=>{localStorage.setItem('selfapply-agent',applyAgent.value);renderReadiness(lastSettings)};browserMode.onchange=()=>renderReadiness(lastSettings);workers.onchange=()=>refresh();queue.oninput=()=>refresh();window.addEventListener('popstate',()=>showTab(location.hash.slice(1)||'overview',false));document.addEventListener('click',event=>{if(!event.target.closest('.profile-switcher'))toggleProfileMenu(false)});enableAnyCurrency();simplifyProfileSections();simplifyJobPreferences();new MutationObserver(simplifyAtsAnalysis).observe(atsResult,{childList:true});setTheme(document.documentElement.dataset.theme);Promise.all([loadSettings(),loadProfiles()]).then(()=>{showTab(location.hash.slice(1)||'overview',false);loadOverview();document.body.classList.remove('booting')}).catch(error=>{notice.textContent=`Could not load local settings: ${error.message||error}`;document.body.classList.remove('booting')});refresh();setInterval(refresh,3000)
</script></body></html>'''
_UI_COPY_REPLACEMENTS = {
    '<section id="find-jobs" class="tab card settings"><h2>Find jobs</h2><p class="muted">Choose where to search without keeping every board visible.</p>':
        '<section id="find-jobs" class="tab card settings"><h2>Job preferences</h2><p class="muted">Choose the roles and locations you want.</p>',
    '<div class="history-head"><div><h2>Application history</h2><p class="muted">Compact status, clear actions, and one worker for one selected retry.</p>':
        '<div class="history-head"><div><h2>Applications</h2><p class="muted">Track results and retry jobs that need attention.</p>',
    '<section id="account" class="tab card settings"><h2>Account & profile</h2><p class="muted">These values power form auto-fill and resume tailoring. Changes save to profile.json.</p>':
        '<section id="account" class="tab card settings"><h2>Profile</h2><p class="muted">The information used to complete applications and tailor resumes.</p>',
    '<section id="document-vault" class="tab card settings"><h2>Document vault</h2><p class="muted">Your master resume, plus every tailored resume SelfApply has generated for a specific job.</p>':
        '<section id="document-vault" class="tab card settings"><div class="page-title-row"><div><h2>Resume files</h2><p class="muted">Your master resume and generated versions.</p></div><button class="secondary" onclick="showTab(\'ats\')">Back to resume tools</button></div>',
    '<section id="api" class="tab card settings"><h2>API & AI</h2><p class="muted">Connect an AI service for scoring and resume tailoring. SelfApply detects the provider and model for you.</p>':
        '<section id="api" class="tab card settings"><div class="page-title-row"><div><h2>AI & browser settings</h2><p class="muted">Configure the services SelfApply uses.</p></div><button class="secondary" onclick="showTab(\'connected-accounts\')">Back to connections</button></div>',
}
for _old_copy, _new_copy in _UI_COPY_REPLACEMENTS.items():
    HTML = HTML.replace(_old_copy, _new_copy)

HTML = HTML.replace(
    '<div class="actions"><button onclick="saveAI()">Save Chrome connection</button></div></section>',
    '<div class="actions"><button id="saveChromeButton" onclick="saveAI()">Save Chrome connection</button>'
    '<button id="testChromeButton" class="secondary" onclick="testChromeExtension()">Test Chrome extension</button>'
    '</div></section>',
).replace(
    '<h3>Use my existing Chrome tab</h3>',
    '<h3>Use my existing Chrome tab <span class="req">*</span></h3>'
    '<p class="muted compact">Required when “My existing Chrome tab” is selected. '
    'The Codex/Claude connection above does not replace this Chrome bridge.</p>',
).replace(
    '<label class="wide">Chrome extension token',
    '<label class="wide">Chrome extension token <span class="req">*</span>',
).replace(
    '<input id="a_extension" type="password"',
    '<input id="a_extension" type="password" required '
    'oninput="saveChromeButton.disabled=!this.value.trim()"',
).replace(
    "extensionState.textContent=a.has_extension_token?'Saved token configured (hidden)':'Not configured';",
    "extensionState.textContent=a.extension_verified?'Verified — existing Chrome is ready':"
    "a.has_extension_token?'Saved token — test required':'Required for existing Chrome';"
    "extensionState.className='secret-note '+(a.extension_verified?'extension-ok':'extension-missing');"
    "testChromeButton.disabled=!a.has_extension_token;saveChromeButton.disabled=true;",
)

HTML = HTML.replace(
    '<div id="agentStates" class="agent-grid"></div><h3>Use my existing Chrome tab',
    '<div id="browserRuntime" hidden aria-hidden="true"></div>'
    '<div id="agentConnectAction" class="connection-action" hidden></div>'
    '<div id="agentStates" class="agent-grid"></div><h3>Use my existing Chrome tab',
    1,
)

HTML = HTML.replace(
    "connectionBrowser.value=localStorage.getItem('selfapply-connection-browser')||'existing';",
    "connectionBrowser.value='managed';browserMode.value='managed';"
    "localStorage.removeItem('selfapply-browser-mode');"
    "localStorage.removeItem('selfapply-connection-browser');",
).replace(
    "browserMode.onchange=()=>renderReadiness(lastSettings)",
    "browserMode.onchange=()=>renderReadiness(lastSettings)",
)

# First normalize the legacy block so it can be removed deterministically
# below. The low-level CLI remains backwards-compatible.
HTML = HTML.replace(
    '<h3>Use my existing Chrome tab <span class="req">*</span></h3>',
    '<div hidden aria-hidden="true"><h3>Use my existing Chrome tab '
    '<span class="req">*</span></h3>',
    1,
).replace(
    '<button id="testChromeButton" class="secondary" onclick="testChromeExtension()">'
    'Test Chrome extension</button></div></section>',
    '<button id="testChromeButton" class="secondary" onclick="testChromeExtension()">'
    'Test Chrome extension</button></div></div></section>',
    1,
)

# Managed Chrome is the web app's only browser mode. Remove the obsolete
# extension setup panel from the rendered UI. Minimal hidden nodes preserve
# compatibility with the shared settings serializer and older local config.
_legacy_bridge_start = HTML.find(
    '<div hidden aria-hidden="true"><h3>Use my existing Chrome tab'
)
_legacy_bridge_end_marker = (
    '<button id="testChromeButton" class="secondary" '
    'onclick="testChromeExtension()">Test Chrome extension</button></div></div></section>'
)
if _legacy_bridge_start >= 0:
    _legacy_bridge_end = HTML.find(_legacy_bridge_end_marker, _legacy_bridge_start)
    if _legacy_bridge_end >= 0:
        _legacy_bridge_end += len(_legacy_bridge_end_marker)
        _legacy_bridge_compat = (
            '<div hidden aria-hidden="true">'
            '<input id="a_extension" type="hidden">'
            '<span id="extensionState"></span><span id="deleteExtension"></span>'
            '<span id="saveChromeButton"></span><span id="testChromeButton"></span>'
            '</div></section>'
        )
        HTML = (
            HTML[:_legacy_bridge_start]
            + _legacy_bridge_compat
            + HTML[_legacy_bridge_end:]
        )
HTML = HTML.replace(
    "async function testChromeExtension(){let result=await post('/api/settings/extension/test',{});"
    "await loadSettings();if(result.ok)extensionState.textContent='Verified — SelfApply can reach "
    "your existing Chrome tab.'}\n",
    "",
)

HTML = HTML.replace(
    '</button></div><label class="connection-browser">Chrome session',
    '</button></div><h3>Required to start Self Apply <span class="req">*</span></h3>'
    '<p class="muted compact">Connect an AI content provider and at least one browser agent.</p>'
    '<div id="primaryConnections" class="agent-grid"></div>'
    '<label class="connection-browser">Chrome session',
    1,
).replace(
    "Your current profile, application history, resumes, settings, and logs are moved into a private timestamped backup first.",
    "Your profile, applications, resumes, API keys, browser tokens, portal sessions, and AI-agent connections are moved to a private backup or disconnected.",
).replace(
    "API keys are included in the private local backup. Nothing is uploaded.",
    "API keys are backed up locally, then removed from the active app. Codex and Claude are disconnected. Nothing is uploaded.",
)

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
HTML = HTML.replace(
    '<div class="actions"><button type="button" id="startSelfApply" '
    'onclick="startWorker()">Start Self Apply</button><button type="button" '
    'id="stopSelfApply" class="danger" style="display:none" '
    'onclick="stopWorker()">Stop Self Apply</button></div></section>',
    '<p class="test-run-note worker-setup-controls"><b>Want to check it first?</b> Test run fills and '
    'validates one ready application. A browser-level safety guard blocks the '
    'final Submit button.</p><div class="actions worker-actions"><button type="button" '
    'id="testRunButton" class="secondary" onclick="startTestRun()">Test one '
    'application — no submit</button><button type="button" id="startSelfApply" '
    'onclick="startWorker()">Start Self Apply</button><button type="button" '
    'id="stopSelfApply" class="danger" style="display:none" '
    'onclick="stopWorker()">Stop Self Apply</button></div></section>',
    1,
)
HTML = HTML.replace(
    '<h3>Tailored resumes</h3><div id="documentList"',
    '<h3>Your uploaded master</h3><div id="masterDocument" class="doc-list">'
    '<div class="empty">Loading…</div></div><h3>Tailored resumes</h3>'
    '<div id="documentList"',
    1,
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

        def _send_document(self, path: Path, display_name: str, *, inline: bool) -> None:
            body = path.read_bytes()
            content_type = {
                ".pdf": "application/pdf",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }.get(path.suffix.casefold(), "text/plain; charset=utf-8")
            disposition = "inline" if inline else "attachment"
            safe_name = Path(display_name).name.replace('"', "")
            self.send_response(200); self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'{disposition}; filename="{safe_name}"')
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "private, no-store")
            self.end_headers(); self.wfile.write(body)

        def _download(self, name: str, *, inline: bool = False) -> None:
            if not name or Path(name).name != name or Path(name).suffix.casefold() not in {".txt", ".pdf", ".docx"}:
                self._json({"error": "Invalid download."}, 400); return
            path = (config.TAILORED_DIR / name).resolve()
            if not path.is_relative_to(config.TAILORED_DIR.resolve()) or not path.is_file():
                self._json({"error": "Download not found."}, 404); return
            self._send_document(path, path.name, inline=inline)

        def _master_document(self, suffix: str, *, inline: bool = False) -> None:
            paths = {
                "pdf": config.RESUME_PDF_PATH,
                "docx": config.RESUME_DOCX_PATH,
                "txt": config.RESUME_PATH,
            }
            path = paths.get(suffix)
            if path is None or not path.is_file():
                self._json({"error": "Master resume format not found."}, 404); return
            source = _read_json(config.RESUME_SOURCE_PATH)
            original = Path(str(source.get("name") or path.name)).name
            if Path(original).suffix.casefold() != path.suffix.casefold():
                original = path.name
            self._send_document(path, original, inline=inline)

        def _application_resume(self, name: str, kind: str) -> None:
            roots = {
                "applied": config.APPLIED_RESUME_DIR,
                "tailored": config.TAILORED_DIR,
            }
            root = roots.get(kind)
            if (root is None or not name or Path(name).name != name
                    or Path(name).suffix.casefold() not in {".pdf", ".docx"}):
                self._json({"error": "Invalid resume."}, 400); return
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                self._json({"error": "Resume not found."}, 404); return
            body = path.read_bytes()
            content_type = (
                "application/pdf" if path.suffix.casefold() == ".pdf" else
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            self.send_response(200); self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
            self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "private, no-store")
            self.end_headers(); self.wfile.write(body)

        def _resume_preview_page(self, name: str, kind: str, page_value: str) -> None:
            try:
                page_index = int(page_value)
                path = _resume_pdf_path(name, kind)
                import fitz  # type: ignore

                document = fitz.open(path)
                if page_index < 0 or page_index >= document.page_count:
                    document.close()
                    raise ValueError("Resume page was not found.")
                pixmap = document[page_index].get_pixmap(
                    matrix=fitz.Matrix(1.75, 1.75), alpha=False,
                )
                body = pixmap.tobytes("png")
                document.close()
            except (ImportError, RuntimeError, ValueError) as exc:
                self._json({"error": str(exc)}, 404); return
            self.send_response(200); self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers(); self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = HTML.replace("__TOKEN__", token).encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body); return
            if parsed.path == "/api/status": self._json(database_status()); return
            if parsed.path == "/api/profiles": self._json(profile_list()); return
            if parsed.path == "/api/status/daily": self._json(daily_activity_stats()); return
            if parsed.path == "/api/documents": self._json(document_vault_files()); return
            if parsed.path == "/api/resumes/changes":
                try:
                    self._json(resume_change_report(
                        parse_qs(parsed.query).get("name", [""])[0]
                    ))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/resumes/preview":
                query = parse_qs(parsed.query)
                try:
                    self._json(resume_preview_info(
                        query.get("name", [""])[0], query.get("kind", [""])[0],
                    ))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/resumes/page":
                query = parse_qs(parsed.query)
                self._resume_preview_page(
                    query.get("name", [""])[0], query.get("kind", [""])[0],
                    query.get("page", ["0"])[0],
                ); return
            if parsed.path == "/api/connections": self._json(controller.connections()); return
            if parsed.path == "/api/settings": self._json(settings_snapshot()); return
            if parsed.path == "/api/settings/backups": self._json(available_profile_backups()); return
            if parsed.path == "/api/ats/jobs": self._json(ats_jobs()); return
            if parsed.path == "/api/ats/task":
                try:
                    self._json(ats_tailor_task(parse_qs(parsed.query).get("id", [""])[0]))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/ats/download":
                query = parse_qs(parsed.query)
                self._download(
                    query.get("name", [""])[0], inline=query.get("view", [""])[0] == "1",
                ); return
            if parsed.path == "/api/documents/master":
                query = parse_qs(parsed.query)
                self._master_document(
                    query.get("format", [""])[0], inline=query.get("view", [""])[0] == "1",
                ); return
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
                elif self.path == "/api/worker/test-run": result = start_test_run(payload)
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
                elif self.path == "/api/settings/ai/probe": result = probe_ai_provider(payload)
                elif self.path == "/api/settings/ai/test": result = test_ai_provider()
                elif self.path == "/api/settings/ai/models": result = list_ai_models()
                elif self.path == "/api/settings/extension/test": result = test_chrome_extension()
                elif self.path == "/api/settings/browser-runtime/install": result = install_playwright_browser()
                elif self.path == "/api/settings/browser-runtime/test": result = test_playwright_browser()
                elif self.path == "/api/settings/secret/delete": result = delete_saved_secret(str(payload.get("kind", "")))
                elif self.path == "/api/settings/reset": result = reset_local_data(payload)
                elif self.path == "/api/settings/backup/restore": result = restore_profile_backup(payload)
                elif self.path == "/api/profiles/create": result = create_local_profile(payload)
                elif self.path == "/api/profiles/switch": result = switch_local_profile(payload)
                elif self.path == "/api/profiles/update": result = update_local_profile(payload)
                elif self.path == "/api/profiles/delete": result = delete_local_profile(payload)
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
                if result.get("restart") and not os.environ.get("PYTEST_CURRENT_TEST"):
                    threading.Timer(0.25, _restart_control_server).start()
            except (
                ValueError, RuntimeError, json.JSONDecodeError,
                OSError, subprocess.TimeoutExpired,
            ) as exc:
                self._json({"error": str(exc)}, 400)

        def log_message(self, format: str, *args) -> None:
            return
    return Handler


def _restart_control_server() -> None:
    """Re-exec after a profile switch so all imported path constants refresh."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


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
