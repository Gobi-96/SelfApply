"""SelfApply configuration: paths, platform detection, user data."""

import os
import platform
import shutil
import json
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("SELFAPPLY_DIR", Path.home() / ".selfapply"))

# Core paths
DB_PATH = APP_DIR / "selfapply.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
RESUME_DOCX_PATH = APP_DIR / "resume.docx"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
ENV_PATH = APP_DIR / ".env"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
APPLIED_RESUME_DIR = APP_DIR / "applied_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"

def profile_missing_fields(profile: dict | None = None) -> list[str]:
    """Return human-readable required fields missing for real applications."""
    if profile is None:
        try:
            profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ["readable profile.json"]
    required = {
        "personal.full_name": "full name",
        "personal.email": "email",
        "personal.phone": "phone",
        "personal.city": "city",
        "personal.country": "country",
        "work_authorization.legally_authorized_to_work": "work authorization",
        "work_authorization.require_sponsorship": "sponsorship answer",
        "compensation.salary_expectation": "salary expectation",
        "experience.target_role": "target role",
    }
    missing = []
    for dotted, label in required.items():
        section, field = dotted.split(".", 1)
        values = profile.get(section)
        if not isinstance(values, dict) or field not in values or values[field] in (None, ""):
            missing.append(label)
    return missing


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [
        APP_DIR, TAILORED_DIR, APPLIED_RESUME_DIR, COVER_LETTER_DIR,
        LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)
        if platform.system() != "Windows":
            try:
                d.chmod(0o700)
            except OSError:
                pass
    for path in (PROFILE_PATH, ENV_PATH, DB_PATH):
        secure_file(path)


def secure_file(path: Path) -> None:
    """Best-effort chmod 0600 on a file containing private user data."""
    if platform.system() == "Windows" or not path.exists():
        return
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_secret_file(path: Path, content: str) -> None:
    """Write a private file and restrict it to the current user."""
    path.write_text(content, encoding="utf-8")
    secure_file(path)


def load_profile() -> dict:
    """Load user profile from ~/.selfapply/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `selfapply init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.selfapply/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 900,
    "apply_max_actions": 150,
    "viewport": "1280x900",
    # Keep a short interruptible hand-off between independent jobs without
    # making the dashboard look paused after a completed application.
    "apply_min_delay_seconds": 2,
    "apply_max_delay_seconds": 5,
    "apply_domain_cooldown_seconds": 180,
    "apply_retry_backoff_seconds": 300,
    "apply_max_per_employer_per_window": 3,
    "apply_employer_window_seconds": 604800,
    "worker_queue_threshold": 50,
    "worker_discover_interval_seconds": 1800,
    "worker_replenish_poll_seconds": 300,
}


def load_env():
    """Load environment variables from ~/.selfapply/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "report"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Codex or Claude Code CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in (
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_URL",
    ))
    if not has_llm:
        return 1

    has_browser_agent = any(shutil.which(name) for name in ("codex", "claude"))
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_browser_agent and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in (
        "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_URL",
    )):
        missing.append("LLM API key — run [bold]selfapply init[/bold] or set GEMINI_API_KEY")
    if required >= 3:
        if not any(shutil.which(name) for name in ("codex", "claude")):
            missing.append("Codex or Claude Code CLI — a browser agent is required")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
