"""SelfApply first-time setup wizard.

Interactive flow that creates ~/.selfapply/ with:
  - resume.txt (and optionally resume.pdf)
  - profile.json
  - searches.yaml
  - .env (LLM API key)
"""

from __future__ import annotations

import json
import re
import shutil
import io
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from selfapply.config import (
    APP_DIR,
    ENV_PATH,
    PROFILE_PATH,
    RESUME_PATH,
    RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH,
    ensure_dirs,
    write_secret_file,
)

console = Console()

CODEX_INSTALL = "npm install -g @openai/codex"
CLAUDE_INSTALL = "npm install -g @anthropic-ai/claude-code"


def _load_profile_checkpoint() -> dict:
    """Load an existing/partial profile so rerunning init resumes safely."""
    if not PROFILE_PATH.exists():
        return {}
    try:
        value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        console.print("[yellow]Existing profile.json could not be read; starting a fresh profile.[/yellow]")
        return {}
    return value if isinstance(value, dict) else {}


def _save_profile_checkpoint(profile: dict, section: str) -> None:
    """Persist one completed wizard section with secret-file permissions."""
    write_secret_file(PROFILE_PATH, json.dumps(profile, indent=2, ensure_ascii=False))
    console.print(f"[dim]Saved progress: {section}[/dim]")


def _ask_validated(label: str, validator, *, default: str = "") -> str:
    """Prompt until validator returns a normalized value."""
    while True:
        value = Prompt.ask(label, default=default)
        try:
            return validator(value)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")


def _validate_email(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Enter a valid email address, for example name@example.com.")
    return value


def _validate_optional_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a complete URL beginning with https://, or leave it blank.")
    return value


def _validate_postal_code(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{1,10}[A-Za-z0-9]", value):
        raise ValueError("Enter a valid postal/ZIP code (3-12 letters or digits).")
    return value


def _validate_money(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    normalized = value.replace(",", "").replace("$", "").strip()
    try:
        amount = float(normalized)
    except ValueError as exc:
        raise ValueError("Enter a number such as 120000, or leave it blank.") from exc
    if amount <= 0:
        raise ValueError("Salary must be greater than zero, or left blank.")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _validate_salary_range(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = re.split(r"\s*[-–—]\s*", value, maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Enter a range such as 80000-120000, or leave it blank.")
    low = _validate_money(parts[0])
    high = _validate_money(parts[1])
    if float(low) > float(high):
        raise ValueError("The minimum salary cannot be greater than the maximum.")
    return f"{low}-{high}"


def _validate_nonnegative_int(value: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError("Enter a whole number, such as 0 or 25.") from exc
    if parsed < 0:
        raise ValueError("The value cannot be negative.")
    return parsed


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def _setup_resume() -> None:
    """Prompt for resume file and copy into APP_DIR."""
    console.print(Panel("[bold]Step 1: Resume[/bold]\nPoint to your master resume file (.txt or .pdf)."))

    while True:
        path_str = Prompt.ask("Resume file path")
        src = Path(path_str.strip().strip('"').strip("'")).expanduser().resolve()

        if not src.exists():
            console.print(f"[red]File not found:[/red] {src}")
            continue

        suffix = src.suffix.lower()
        if suffix not in (".txt", ".pdf"):
            console.print("[red]Unsupported format.[/red] Provide a .txt or .pdf file.")
            continue

        if suffix == ".txt":
            shutil.copy2(src, RESUME_PATH)
            console.print(f"[green]Copied to {RESUME_PATH}[/green]")
        elif suffix == ".pdf":
            shutil.copy2(src, RESUME_PDF_PATH)
            console.print(f"[green]Copied to {RESUME_PDF_PATH}[/green]")

            try:
                from pypdf import PdfReader
                from selfapply.scoring.pdf import normalize_resume_text
                extracted = "\n\n".join(
                    page.extract_text(extraction_mode="layout") or ""
                    for page in PdfReader(io.BytesIO(src.read_bytes())).pages
                )
                extracted = normalize_resume_text(extracted).strip()
            except Exception as exc:
                console.print(f"[red]Could not extract text from that PDF: {exc}[/red]")
                continue
            if not extracted:
                console.print(
                    "[red]That PDF has no readable text. Export a text-based PDF "
                    "or provide a .txt resume.[/red]"
                )
                continue
            write_secret_file(RESUME_PATH, extracted + "\n")
            console.print(f"[green]Extracted AI-ready text to {RESUME_PATH}[/green]")
        break


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def _setup_profile() -> dict:
    """Walk through profile questions and return a nested profile dict."""
    console.print(
        Panel(
            "[bold]Step 2: Profile[/bold]\nTell SelfApply about yourself. This powers scoring, tailoring, and auto-fill."
        )
    )

    profile = _load_profile_checkpoint()
    if profile:
        console.print(
            "[cyan]Existing saved answers found.[/cyan] Press Enter to keep each default; "
            "completed sections are checkpointed as you go."
        )

    # -- Personal --
    console.print("\n[bold cyan]Personal Information[/bold cyan]")
    existing = profile.get("personal", {})
    full_name = Prompt.ask("Full name", default=existing.get("full_name", ""))
    while not full_name.strip():
        console.print("[red]Full name is required.[/red]")
        full_name = Prompt.ask("Full name")
    existing_password = str(existing.get("password", ""))
    console.print(
        Panel(
            "SelfApply uses one dedicated password when an employer portal has no "
            "Google, LinkedIn, or Indeed sign-in option. It may reuse this password to "
            "create accounts on multiple unrelated employer portals. The value is stored "
            f"locally in [cyan]{PROFILE_PATH}[/cyan] with restricted file permissions. "
            "Use a unique password that you do not use for email, banking, or any other account.",
            title="Before entering a job-site password",
            border_style="yellow",
        )
    )
    if existing_password and Confirm.ask("Keep the saved job-site password?", default=True):
        job_site_password = existing_password
    else:
        job_site_password = Prompt.ask(
            "Dedicated job-site password (optional)",
            password=True,
            default="",
        )
    profile["personal"] = {
        "full_name": full_name.strip(),
        "preferred_name": Prompt.ask(
            "Preferred/nickname (leave blank to use first name)", default=existing.get("preferred_name", "")
        ),
        "email": _ask_validated("Email address", _validate_email, default=existing.get("email", "")),
        "phone": Prompt.ask("Phone number", default=existing.get("phone", "")),
        "city": Prompt.ask("City", default=existing.get("city", "")),
        "province_state": Prompt.ask(
            "Province/State (e.g. Ontario, California)", default=existing.get("province_state", "")
        ),
        "country": Prompt.ask("Country", default=existing.get("country", "")),
        "postal_code": _ask_validated(
            "Postal/ZIP code", _validate_postal_code, default=existing.get("postal_code", "")
        ),
        "address": Prompt.ask(
            "Street address (optional, used for form auto-fill)", default=existing.get("address", "")
        ),
        "linkedin_url": _ask_validated(
            "LinkedIn URL", _validate_optional_url, default=existing.get("linkedin_url", "")
        ),
        "github_url": _ask_validated(
            "GitHub URL (optional)", _validate_optional_url, default=existing.get("github_url", "")
        ),
        "portfolio_url": _ask_validated(
            "Portfolio URL (optional)", _validate_optional_url, default=existing.get("portfolio_url", "")
        ),
        "website_url": _ask_validated(
            "Personal website URL (optional)", _validate_optional_url, default=existing.get("website_url", "")
        ),
        "password": job_site_password,
    }
    _save_profile_checkpoint(profile, "personal information")

    # -- Work Authorization --
    console.print("\n[bold cyan]Work Authorization[/bold cyan]")
    existing = profile.get("work_authorization", {})
    profile["work_authorization"] = {
        "legally_authorized_to_work": Confirm.ask(
            "Are you legally authorized to work in your target country?",
            default=bool(existing.get("legally_authorized_to_work", True)),
        ),
        "require_sponsorship": Confirm.ask(
            "Will you now or in the future need sponsorship?", default=bool(existing.get("require_sponsorship", False))
        ),
        "work_permit_type": Prompt.ask(
            "Work permit type (e.g. Citizen, PR, Open Work Permit — leave blank if N/A)",
            default=existing.get("work_permit_type", ""),
        ),
    }
    _save_profile_checkpoint(profile, "work authorization")

    # -- Compensation --
    console.print("\n[bold cyan]Compensation[/bold cyan]")
    existing = profile.get("compensation", {})
    salary = _ask_validated(
        "Expected annual salary (number)", _validate_money, default=existing.get("salary_expectation", "")
    )
    salary_currency = Prompt.ask(
        "Currency code (e.g. USD, CAD, EUR, JPY)",
        default=existing.get("salary_currency", ""),
    )
    old_range = ""
    if existing.get("salary_range_min") or existing.get("salary_range_max"):
        old_range = f"{existing.get('salary_range_min', '')}-{existing.get('salary_range_max', '')}"
    salary_range = _ask_validated("Acceptable range (e.g. 80000-120000)", _validate_salary_range, default=old_range)
    range_parts = salary_range.split("-") if "-" in salary_range else [salary, salary]
    profile["compensation"] = {
        "salary_expectation": salary,
        "salary_currency": salary_currency,
        "salary_range_min": range_parts[0].strip(),
        "salary_range_max": range_parts[1].strip() if len(range_parts) > 1 else range_parts[0].strip(),
    }
    _save_profile_checkpoint(profile, "compensation")

    # -- Experience --
    console.print("\n[bold cyan]Experience[/bold cyan]")
    existing = profile.get("experience", {})
    current_title = Prompt.ask("Current/most recent job title", default=existing.get("current_title", ""))
    target_role = Prompt.ask(
        "Target role (what you're applying for, e.g. 'Senior Backend Engineer')",
        default=existing.get("target_role", current_title),
    )
    profile["experience"] = {
        "years_of_experience_total": Prompt.ask(
            "Years of professional experience", default=existing.get("years_of_experience_total", "")
        ),
        "education_level": Prompt.ask(
            "Highest education (e.g. Bachelor's, Master's, PhD, Self-taught)",
            default=existing.get("education_level", ""),
        ),
        "current_title": current_title,
        "target_role": target_role,
    }
    _save_profile_checkpoint(profile, "experience")

    # -- Skills Boundary --
    console.print("\n[bold cyan]Skills[/bold cyan] (comma-separated)")
    existing = profile.get("skills_boundary", {})
    langs = Prompt.ask("Programming languages", default=", ".join(existing.get("programming_languages", [])))
    frameworks = Prompt.ask("Frameworks & libraries", default=", ".join(existing.get("frameworks", [])))
    tools = Prompt.ask("Tools & platforms (e.g. Docker, AWS, Git)", default=", ".join(existing.get("tools", [])))
    profile["skills_boundary"] = {
        "programming_languages": [s.strip() for s in langs.split(",") if s.strip()],
        "frameworks": [s.strip() for s in frameworks.split(",") if s.strip()],
        "tools": [s.strip() for s in tools.split(",") if s.strip()],
    }
    _save_profile_checkpoint(profile, "skills")

    # -- Resume Facts (preserved truths for tailoring) --
    console.print("\n[bold cyan]Resume Facts[/bold cyan]")
    console.print("[dim]These are preserved exactly during resume tailoring — the AI will never change them.[/dim]")
    existing = profile.get("resume_facts", {})
    companies = Prompt.ask(
        "Companies to always keep (comma-separated)", default=", ".join(existing.get("preserved_companies", []))
    )
    projects = Prompt.ask(
        "Projects to always keep (comma-separated)", default=", ".join(existing.get("preserved_projects", []))
    )
    school = Prompt.ask("School name(s) to preserve", default=existing.get("preserved_school", ""))
    metrics = Prompt.ask(
        "Real metrics to preserve (e.g. '99.9% uptime, 50k users')", default=", ".join(existing.get("real_metrics", []))
    )
    experience_entries = Prompt.ask(
        "Exact experience headings to preserve (separate entries with |; e.g. Senior Engineer, Acme — Jan 2020-Present)",
        default=" | ".join(
            str(item.get("header", item)) if isinstance(item, dict) else str(item)
            for item in existing.get("experience_entries", [])
        ),
    )
    education_entries = Prompt.ask(
        "Exact education entries to preserve (separate entries with |; e.g. MS Computer Science, State University, 2020)",
        default=" | ".join(str(item) for item in existing.get("education_entries", [])),
    )
    profile["resume_facts"] = {
        "preserved_companies": [s.strip() for s in companies.split(",") if s.strip()],
        "preserved_projects": [s.strip() for s in projects.split(",") if s.strip()],
        "preserved_school": school.strip(),
        "real_metrics": [s.strip() for s in metrics.split(",") if s.strip()],
        "experience_entries": [
            {"header": item.strip()} for item in experience_entries.split("|") if item.strip()
        ],
        "education_entries": [
            item.strip() for item in education_entries.split("|") if item.strip()
        ],
    }
    _save_profile_checkpoint(profile, "resume facts")

    # -- EEO Voluntary (defaults) --
    profile.setdefault(
        "eeo_voluntary",
        {
            "gender": "Decline to self-identify",
            "race_ethnicity": "Decline to self-identify",
            "veteran_status": "Decline to self-identify",
            "disability_status": "Decline to self-identify",
        },
    )

    # -- Availability --
    existing = profile.get("availability", {})
    profile["availability"] = {
        "earliest_start_date": Prompt.ask(
            "Earliest start date", default=existing.get("earliest_start_date", "Immediately")
        ),
    }
    _save_profile_checkpoint(profile, "availability")

    console.print(f"\n[green]Profile saved to {PROFILE_PATH}[/green]")
    return profile


# ---------------------------------------------------------------------------
# Search config
# ---------------------------------------------------------------------------


def _setup_searches() -> None:
    """Generate a searches.yaml from user input."""
    console.print(Panel("[bold]Step 3: Job Search Config[/bold]\nDefine what you're looking for."))

    location = Prompt.ask("Target location (e.g. 'Remote', 'Canada', 'New York, NY')", default="Remote")
    while True:
        distance_str = Prompt.ask("Search radius in miles (0 for remote-only)", default="0")
        try:
            distance = _validate_nonnegative_int(distance_str)
            break
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")

    roles_raw = Prompt.ask("Target job titles (comma-separated, e.g. 'Backend Engineer, Full Stack Developer')")
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()]

    if not roles:
        console.print("[red]Add at least one target job title.[/red]")
        roles_raw = Prompt.ask("Target job titles (comma-separated)")
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
        if not roles:
            raise ValueError("At least one target job title is required.")

    # Build YAML content
    lines = [
        "# SelfApply search configuration",
        "# Edit this file to refine your job search queries.",
        "",
        "defaults:",
        f'  location: "{location}"',
        f"  distance: {distance}",
        "  hours_old: 72",
        "  results_per_site: 50",
        "",
        "locations:",
        f'  - location: "{location}"',
        f"    remote: {str(distance == 0).lower()}",
        "",
        "queries:",
    ]
    for i, role in enumerate(roles):
        lines.append(f'  - query: "{role}"')
        lines.append(f"    tier: {min(i + 1, 3)}")

    SEARCH_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print(f"[green]Search config saved to {SEARCH_CONFIG_PATH}[/green]")


# ---------------------------------------------------------------------------
# AI Features
# ---------------------------------------------------------------------------


def _setup_ai_features() -> None:
    """Ask about AI scoring/tailoring — optional LLM configuration."""
    console.print(
        Panel(
            "[bold]Step 4: AI Features (optional)[/bold]\n"
            "An LLM powers job scoring, resume tailoring, and cover letters.\n"
            "Without this, you can still discover and enrich jobs."
        )
    )

    if not Confirm.ask("Enable AI scoring and resume tailoring?", default=True):
        console.print("[dim]Discovery-only mode. You can configure AI later with [bold]selfapply init[/bold].[/dim]")
        return

    console.print("Supported providers: [bold]Gemini[/bold], OpenAI, Anthropic Claude, local (Ollama/llama.cpp)")
    provider = Prompt.ask(
        "Provider",
        choices=["gemini", "openai", "anthropic", "local"],
        default="gemini",
    )

    env_lines = ["# SelfApply configuration", ""]

    if provider == "gemini":
        api_key = Prompt.ask("Gemini API key (from aistudio.google.com)")
        model = Prompt.ask("Model", default="gemini-2.0-flash")
        env_lines.append(f"GEMINI_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "openai":
        api_key = Prompt.ask("OpenAI API key")
        model = Prompt.ask("Model", default="gpt-4o-mini")
        env_lines.append(f"OPENAI_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "anthropic":
        api_key = Prompt.ask("Anthropic API key")
        model = Prompt.ask("Model", default="claude-sonnet-4-5")
        env_lines.append(f"ANTHROPIC_API_KEY={api_key}")
        env_lines.append(f"LLM_MODEL={model}")
    elif provider == "local":
        url = Prompt.ask("Local LLM endpoint URL", default="http://localhost:8080/v1")
        model = Prompt.ask("Model name", default="local-model")
        env_lines.append(f"LLM_URL={url}")
        env_lines.append(f"LLM_MODEL={model}")

    env_lines.append("")
    from selfapply import llm
    if provider == "gemini":
        base = llm._GEMINI_COMPAT_BASE
    elif provider == "openai":
        base = "https://api.openai.com/v1"
    elif provider == "anthropic":
        base = "https://api.anthropic.com/v1"
    else:
        base = llm.normalize_local_url(url)
        api_key = ""
    test_client = llm.LLMClient(base, model, api_key)
    try:
        reply = test_client.ask("Reply with exactly OK.", max_tokens=8, temperature=0)
        if not reply.strip():
            raise RuntimeError("provider returned an empty response")
    except Exception as exc:
        console.print(
            f"[red]AI connection failed; settings were not saved:[/red] {exc}\n"
            "Check the key, endpoint, and model, then run setup again."
        )
        return
    finally:
        test_client.close()
    write_secret_file(ENV_PATH, "\n".join(env_lines))
    console.print(f"[green]AI connection verified and saved to {ENV_PATH}[/green]")


# ---------------------------------------------------------------------------
# Auto-Apply
# ---------------------------------------------------------------------------


def _setup_auto_apply() -> None:
    """Configure autonomous job application (requires Codex or Claude)."""
    console.print(
        Panel(
            "[bold]Step 5: Auto-Apply (optional)[/bold]\n"
            "SelfApply can autonomously fill and submit job applications\n"
            "using Codex (default) or Claude Code as the browser agent."
        )
    )

    if not Confirm.ask("Enable autonomous job applications?", default=True):
        console.print("[dim]You can apply manually using the tailored resumes SelfApply generates.[/dim]")
        return

    # Check for a supported browser agent
    if shutil.which("codex"):
        console.print("[green]Codex CLI detected.[/green]")
    elif shutil.which("claude"):
        console.print("[green]Claude Code CLI detected.[/green]")
    else:
        console.print(
            "[yellow]Codex or Claude Code CLI not found on PATH.[/yellow]\n"
            "Auto-apply won't work until a supported browser agent is installed.\n"
            f"  Codex: [bold]{CODEX_INSTALL}[/bold]  "
            "[link=https://github.com/openai/codex]installation guide[/link]\n"
            f"  Claude: [bold]{CLAUDE_INSTALL}[/bold]  "
            "[link=https://docs.anthropic.com/en/docs/claude-code/setup]installation guide[/link]"
        )

    # Optional: CapSolver for CAPTCHAs
    console.print("\n[dim]Some job sites use CAPTCHAs. CapSolver can handle them automatically.[/dim]")
    if Confirm.ask("Configure CapSolver API key? (optional)", default=False):
        capsolver_key = Prompt.ask("CapSolver API key")
        # Append to existing .env or create
        if ENV_PATH.exists():
            existing = ENV_PATH.read_text(encoding="utf-8")
            if "CAPSOLVER_API_KEY" not in existing:
                write_secret_file(
                    ENV_PATH,
                    existing.rstrip() + f"\nCAPSOLVER_API_KEY={capsolver_key}\n",
                )
        else:
            write_secret_file(
                ENV_PATH,
                f"# SelfApply configuration\nCAPSOLVER_API_KEY={capsolver_key}\n",
            )
        console.print("[green]CapSolver key saved.[/green]")
    else:
        console.print("[dim]Skipped. Add CAPSOLVER_API_KEY to .env later if needed.[/dim]")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def run_wizard() -> None:
    """Run the full interactive setup wizard."""
    console.print()
    console.print(
        Panel.fit(
            "[bold green]SelfApply Setup Wizard[/bold green]\n\n"
            "This will create your configuration at:\n"
            f"  [cyan]{APP_DIR}[/cyan]\n\n"
            "You can re-run this anytime with [bold]selfapply init[/bold].",
            border_style="green",
        )
    )

    ensure_dirs()
    console.print(f"[dim]Created {APP_DIR}[/dim]\n")

    # Step 1: Resume
    _setup_resume()
    console.print()

    # Step 2: Profile
    _setup_profile()
    console.print()

    # Step 3: Search config
    _setup_searches()
    console.print()

    # Step 4: AI features (optional LLM)
    _setup_ai_features()
    console.print()

    # Step 5: Auto-apply (browser-agent detection)
    _setup_auto_apply()
    console.print()

    # Done — show tier status
    from selfapply.config import get_tier, TIER_LABELS, TIER_COMMANDS

    tier = get_tier()

    tier_lines: list[str] = []
    for t in range(1, 4):
        label = TIER_LABELS[t]
        cmds = ", ".join(f"[bold]{c}[/bold]" for c in TIER_COMMANDS[t])
        if t <= tier:
            tier_lines.append(f"  [green]✓ Tier {t} — {label}[/green]  ({cmds})")
        elif t == tier + 1:
            tier_lines.append(f"  [yellow]→ Tier {t} — {label}[/yellow]  ({cmds})")
        else:
            tier_lines.append(f"  [dim]✗ Tier {t} — {label}  ({cmds})[/dim]")

    unlock_hint = ""
    if tier == 1:
        unlock_hint = "\n[dim]To unlock Tier 2: configure an LLM API key (re-run [bold]selfapply init[/bold]).[/dim]"
    elif tier == 2:
        unlock_hint = "\n[dim]To unlock Tier 3: install Codex or Claude Code CLI + Chrome.[/dim]"

    console.print(
        Panel.fit(
            "[bold green]Setup complete![/bold green]\n\n"
            f"[bold]Your tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]\n\n" + "\n".join(tier_lines) + unlock_hint,
            border_style="green",
        )
    )

    console.print("\n[bold]Final setup check[/bold]")
    from selfapply.cli import doctor

    doctor()

    _offer_autostart(tier)


def _offer_autostart(tier: int) -> None:
    """Offer to start SelfApply now -- strictly opt-in, never a default-yes.

    Continuous unattended applying must never begin, or begin submitting
    real applications, without the user explicitly choosing to start it and
    (at tier 3) explicitly setting a spend cap.
    """
    if tier == 3:
        console.print(
            "\n[dim]Tip: run 'selfapply connect-google' first for smoother "
            "SSO logins across employer portals (optional).[/dim]"
        )
        console.print(
            "\n[bold]SelfApply can start running continuously now[/bold] -- "
            "discovering, scoring, tailoring, and (once out of dry-run) "
            "[bold]submitting real job applications[/bold] on its own."
        )
        if Confirm.ask("Start it now?", default=False):
            max_spend_raw = Prompt.ask(
                "Spend cap in USD (stops automatically once reached; required)"
            )
            try:
                max_spend_usd = float(max_spend_raw)
                if max_spend_usd <= 0:
                    raise ValueError
            except ValueError:
                console.print(
                    "[red]Invalid amount; not starting. Run "
                    "'selfapply run-worker --max-spend <amount>' when ready.[/red]"
                )
                return
            dry_run_first = Confirm.ask(
                "Run in dry-run mode (fill forms, don't submit) until you "
                "manually switch it to live?",
                default=True,
            )
            agent = "codex" if shutil.which("codex") else "claude"
            from selfapply import config as _config
            from selfapply.worker_supervisor import run_worker_supervisor

            console.print(
                "\n[bold green]Starting SelfApply...[/bold green] "
                "[dim](Ctrl+C to stop)[/dim]\n"
            )
            run_worker_supervisor(
                agent=agent,
                browser="managed",
                dry_run=dry_run_first,
                target_applied=None,
                max_spend_usd=max_spend_usd,
                min_score=_config.DEFAULTS["min_score"],
                min_delay=_config.DEFAULTS["apply_min_delay_seconds"],
                max_delay=_config.DEFAULTS["apply_max_delay_seconds"],
                domain_cooldown=_config.DEFAULTS["apply_domain_cooldown_seconds"],
                retry_backoff_seconds=_config.DEFAULTS["apply_retry_backoff_seconds"],
                max_per_employer=_config.DEFAULTS["apply_max_per_employer_per_window"],
                employer_window=_config.DEFAULTS["apply_employer_window_seconds"],
                queue_threshold=_config.DEFAULTS["worker_queue_threshold"],
                discover_interval=_config.DEFAULTS["worker_discover_interval_seconds"],
                replenish_poll_interval=_config.DEFAULTS["worker_replenish_poll_seconds"],
            )
            return
        console.print(
            "[dim]Not starting now. When ready: "
            "selfapply run-worker --max-spend <amount>[/dim]"
        )
    elif tier == 2:
        if Confirm.ask(
            "\nAuto-apply isn't set up yet, but I can start discovering, "
            "scoring, and tailoring resumes now (no applications are ever "
            "submitted at this tier). Start that continuously?",
            default=False,
        ):
            from selfapply.pipeline import run_pipeline

            console.print(
                "\n[bold green]Starting discovery/scoring/tailoring...[/bold green] "
                "[dim](Ctrl+C to stop)[/dim]\n"
            )
            run_pipeline(stream=True)
