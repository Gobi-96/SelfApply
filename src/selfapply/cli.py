"""SelfApply CLI — the main entry point."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from selfapply import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="selfapply",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")
EXTENSION_TOKEN_KEY = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from selfapply.config import load_env, ensure_dirs
    from selfapply.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]selfapply[/bold] {__version__}")
        raise typer.Exit()


def _normalize_extension_token(raw_value: str) -> str:
    """Accept either a bare extension token or a copied KEY=value line."""
    value = raw_value.strip().strip("\"'").strip()
    prefix = f"{EXTENSION_TOKEN_KEY}="
    while value.startswith(prefix):
        value = value[len(prefix):].strip().strip("\"'").strip()
    return value


@contextmanager
def _managed_connect_browser():
    """Pause new acquisitions and safely attach/launch persistent worker Chrome."""
    from selfapply import config
    from selfapply.apply import launcher
    from selfapply.apply.chrome import BASE_CDP_PORT, cleanup_worker, launch_chrome
    from selfapply.database import get_connection

    config.ensure_dirs()
    launcher.CONNECT_LOCK_PATH.write_text(
        f"pid={os.getpid()} started={time.time()}\n", encoding="utf-8",
    )
    chrome_proc = None
    try:
        # Give an idle worker one polling beat to observe the connection lock.
        time.sleep(0.25)
        active = get_connection().execute(
            "SELECT COUNT(*) FROM jobs WHERE apply_status = 'in_progress'"
        ).fetchone()[0]
        if active:
            raise RuntimeError(
                "An application is currently in progress. Wait for it to finish, "
                "then run this command again."
            )
        if not launcher._cdp_is_ready(BASE_CDP_PORT):
            chrome_proc = launch_chrome(0, port=BASE_CDP_PORT, headless=False)
        yield BASE_CDP_PORT
    finally:
        if chrome_proc is not None:
            cleanup_worker(0, chrome_proc)
        launcher.CONNECT_LOCK_PATH.unlink(missing_ok=True)


def _resolve_apply_settings(
    *, agent: str, browser: str, model: str | None, max_actions: int,
    max_minutes: int, workers: int, headless: bool,
) -> dict:
    """Validate shared browser-agent settings and return launcher kwargs."""
    from selfapply.config import check_tier, get_chrome_path

    agent = agent.lower().strip()
    browser = browser.lower().strip()
    if agent not in {"codex", "claude"}:
        console.print("[red]--agent must be 'codex' or 'claude'.[/red]")
        raise typer.Exit(code=1)
    if browser not in {"existing", "managed"}:
        console.print("[red]--browser must be 'existing' or 'managed'.[/red]")
        raise typer.Exit(code=1)
    if browser == "existing" and agent != "codex":
        console.print(
            "[red]Existing-tab mode requires --agent codex.[/red] "
            "Use --browser managed with Claude."
        )
        raise typer.Exit(code=1)
    if browser == "existing" and workers != 1:
        console.print("[red]Existing-tab mode supports one worker/tab at a time.[/red]")
        raise typer.Exit(code=1)
    if browser == "existing" and headless:
        console.print("[red]Existing-tab mode cannot be headless.[/red]")
        raise typer.Exit(code=1)
    extension_token = _normalize_extension_token(
        os.environ.get(EXTENSION_TOKEN_KEY, ""),
    )
    if browser == "existing" and not extension_token:
        console.print(
            "[red]Playwright Chrome extension token is not configured.[/red]\n"
            "Run [bold]selfapply browser-setup[/bold], or use "
            "[bold]--browser managed[/bold]."
        )
        raise typer.Exit(code=1)
    if max_actions < 1 or max_minutes < 1:
        console.print("[red]--max-actions and --max-minutes must be at least 1.[/red]")
        raise typer.Exit(code=1)
    if agent == "claude":
        check_tier(3, "auto-apply")
    else:
        if not shutil.which("codex"):
            console.print("[red]Codex CLI not found.[/red] Install/login to Codex, then retry.")
            raise typer.Exit(code=1)
        if not shutil.which("npx"):
            console.print("[red]npx not found.[/red] Install Node.js, then retry.")
            raise typer.Exit(code=1)
        try:
            get_chrome_path()
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    return {
        "agent": agent,
        "browser_mode": browser,
        "model": model or ("gpt-5.6-sol" if agent == "codex" else "haiku"),
        "max_actions": max_actions,
        "max_seconds": max_minutes * 60,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """SelfApply — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from selfapply.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    skip_smart_extract: bool = typer.Option(
        False, "--skip-smart-extract",
        help="Use JobSpy + Workday discovery only; skip the AI scraping fallback.",
    ),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    max_spend: Optional[float] = typer.Option(
        None, "--max-spend",
        help="Optional LLM spend cap in USD; checked before every model request.",
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from selfapply.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from selfapply.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    if max_spend is not None and max_spend <= 0:
        console.print("[red]--max-spend must be greater than 0.[/red]")
        raise typer.Exit(code=1)

    if not dry_run and (any(s in stage_list for s in llm_stages) or "all" in stage_list):
        from selfapply import llm
        llm.set_cost_limit(max_spend)
        try:
            provider, selected_model = llm.smoke_test()
        except Exception as exc:
            console.print(
                "[red]LLM connection test failed before the batch started.[/red]\n"
                f"{exc}\nCheck API & AI settings, the selected model, and your network."
            )
            raise typer.Exit(code=1) from exc
        console.print(f"[green]LLM ready:[/green] {provider} / {selected_model}")
        if max_spend is not None:
            console.print(f"[green]Pipeline LLM cap:[/green] ${max_spend:.2f}")

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        smart_extract=not skip_smart_extract,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    agent: str = typer.Option("codex", "--agent", help="Browser agent: codex or claude."),
    browser: str = typer.Option(
        "managed", "--browser",
        help="Browser mode: existing Chrome tab or managed SelfApply Chrome.",
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Agent model (Codex default: gpt-5.6-sol)."),
    max_actions: int = typer.Option(150, "--max-actions", help="Maximum browser actions per job."),
    max_minutes: int = typer.Option(15, "--max-minutes", help="Maximum minutes per job."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from selfapply import config
    from selfapply.config import PROFILE_PATH as _profile_path
    from selfapply.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from selfapply.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from selfapply.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from selfapply.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    settings = _resolve_apply_settings(
        agent=agent, browser=browser, model=model, max_actions=max_actions,
        max_minutes=max_minutes, workers=workers, headless=headless,
    )
    agent = settings["agent"]
    browser = settings["browser_mode"]

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]selfapply init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)
    missing_profile = config.profile_missing_fields()
    if missing_profile:
        console.print(
            "[red]Profile setup is incomplete.[/red]\nMissing: "
            + ", ".join(missing_profile)
            + ".\nOpen [bold]selfapply ui[/bold] → Candidate profile, or rerun "
              "[bold]selfapply init[/bold]."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]selfapply run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        if agent != "claude":
            console.print("[red]--gen is currently available only with --agent claude.[/red]")
            raise typer.Exit(code=1)
        from selfapply.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        claude_model = model or "haiku"
        prompt_file = gen_prompt(target, min_score=min_score, model=claude_model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {claude_model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from selfapply.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)
    resolved_model = settings["model"]

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Agent:    {agent}")
    console.print(f"  Browser:  {browser}")
    console.print(f"  Model:    {resolved_model}")
    console.print(f"  Limits:   {max_actions} actions / {max_minutes} minutes per job")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        **settings,
        min_delay=config.DEFAULTS["apply_min_delay_seconds"],
        max_delay=config.DEFAULTS["apply_max_delay_seconds"],
        domain_cooldown=config.DEFAULTS["apply_domain_cooldown_seconds"],
        retry_backoff_seconds=config.DEFAULTS["apply_retry_backoff_seconds"],
        max_per_employer=config.DEFAULTS["apply_max_per_employer_per_window"],
        employer_window=config.DEFAULTS["apply_employer_window_seconds"],
    )


@app.command("run-worker")
def run_worker(
    max_spend: float = typer.Option(
        ...,
        "--max-spend",
        help=(
            "Stop automatically once cumulative LLM/apply cost reaches this "
            "many USD. Required -- continuous unattended applying must not "
            "run without a budget limit."
        ),
    ),
    target_applied: int | None = typer.Option(
        None, "--target-applied", help="Stop after this total applied count."
    ),
    queue_threshold: int | None = typer.Option(
        None, "--queue-threshold", help="Replenish when fewer jobs are ready."
    ),
    discover_interval: int | None = typer.Option(
        None, "--discover-interval", help="Minimum seconds between discovery runs."
    ),
    replenish_poll_interval: int | None = typer.Option(
        None, "--replenish-poll-interval", help="Seconds between queue checks."
    ),
    workers: int = typer.Option(1, "--workers", "-w"),
    min_score: int = typer.Option(7, "--min-score"),
    agent: str = typer.Option("codex", "--agent"),
    browser: str = typer.Option("managed", "--browser"),
    model: str | None = typer.Option(None, "--model", "-m"),
    max_actions: int = typer.Option(150, "--max-actions"),
    max_minutes: int = typer.Option(15, "--max-minutes"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    test_limit: int = typer.Option(
        0, "--test-limit", hidden=True,
        help="Stop after this many successful dry-run form checks.",
    ),
    headless: bool = typer.Option(False, "--headless"),
    validation: str = typer.Option("normal", "--validation"),
    skip_smart_extract: bool = typer.Option(False, "--skip-smart-extract"),
) -> None:
    """Continuously prepare and apply to jobs in one supervised process."""
    _bootstrap()
    from selfapply import config
    from selfapply.worker_supervisor import run_worker_supervisor

    if validation not in {"strict", "normal", "lenient"}:
        console.print("[red]--validation must be strict, normal, or lenient.[/red]")
        raise typer.Exit(code=1)
    if target_applied is not None and target_applied < 1:
        console.print("[red]--target-applied must be at least 1.[/red]")
        raise typer.Exit(code=1)
    if max_spend <= 0:
        console.print("[red]--max-spend must be greater than 0.[/red]")
        raise typer.Exit(code=1)
    if test_limit < 0 or (test_limit and not dry_run):
        console.print("[red]--test-limit requires --dry-run and cannot be negative.[/red]")
        raise typer.Exit(code=1)
    settings = _resolve_apply_settings(
        agent=agent, browser=browser, model=model, max_actions=max_actions,
        max_minutes=max_minutes, workers=workers, headless=headless,
    )
    run_worker_supervisor(
        max_spend_usd=max_spend,
        target_applied=target_applied,
        queue_threshold=(queue_threshold if queue_threshold is not None else
                         config.DEFAULTS["worker_queue_threshold"]),
        discover_interval=(discover_interval if discover_interval is not None else
                           config.DEFAULTS["worker_discover_interval_seconds"]),
        replenish_poll_interval=(
            replenish_poll_interval if replenish_poll_interval is not None else
            config.DEFAULTS["worker_replenish_poll_seconds"]
        ),
        validation_mode=validation,
        pipeline_workers=workers,
        smart_extract=not skip_smart_extract,
        min_score=min_score,
        headless=headless,
        dry_run=dry_run,
        test_limit=test_limit,
        workers=workers,
        **settings,
        min_delay=config.DEFAULTS["apply_min_delay_seconds"],
        max_delay=config.DEFAULTS["apply_max_delay_seconds"],
        domain_cooldown=config.DEFAULTS["apply_domain_cooldown_seconds"],
        retry_backoff_seconds=config.DEFAULTS["apply_retry_backoff_seconds"],
        max_per_employer=config.DEFAULTS["apply_max_per_employer_per_window"],
        employer_window=config.DEFAULTS["apply_employer_window_seconds"],
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from selfapply.database import get_stats

    stats = get_stats()

    console.print("\n[bold]SelfApply Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    from selfapply.worker_supervisor import SUMMARY_PATH
    if SUMMARY_PATH.exists():
        import json
        try:
            last_run = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
            console.print(
                Panel(
                    f"Finished: [bold]{last_run.get('finished_at', '?')}[/bold]\n"
                    f"Applied: [green]{last_run.get('applied', 0)}[/green]  "
                    f"Failed: [red]{last_run.get('failed', 0)}[/red]  "
                    f"Cost: ${last_run.get('cost_usd', 0):.2f}\n"
                    f"Stop reason: {last_run.get('stop_reason', 'unknown')}",
                    title="Last Self Apply run",
                    border_style="dim",
                )
            )
        except (json.JSONDecodeError, OSError):
            pass

    console.print()


@app.command()
def report() -> None:
    """Open the static HTML results report in your browser."""
    _bootstrap()

    from selfapply.view import open_dashboard

    open_dashboard()


@app.command()
def dashboard() -> None:
    """Alias for 'report'; the live dashboard appears during apply/run-worker."""
    console.print(
        "[cyan]Opening the static results report.[/cyan] The live worker dashboard "
        "appears automatically while [bold]selfapply apply[/bold] or "
        "[bold]selfapply run-worker[/bold] is running."
    )
    report()


@app.command("browser-setup")
def browser_setup() -> None:
    """Connect SelfApply to an existing logged-in Chrome tab."""
    import webbrowser

    from dotenv import set_key
    from rich.prompt import Prompt

    from selfapply.config import ENV_PATH, ensure_dirs, secure_file

    extension_url = (
        "https://chromewebstore.google.com/detail/"
        "playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm"
    )
    ensure_dirs()
    webbrowser.open(extension_url)
    console.print(
        "\n[bold]Existing Chrome setup[/bold]\n"
        "1. Install the Playwright Extension in the Chrome page that opened.\n"
        "2. Open its extension icon/status page.\n"
        "3. Copy the PLAYWRIGHT_MCP_EXTENSION_TOKEN value."
    )
    token = _normalize_extension_token(
        Prompt.ask("Paste the extension token or full KEY=value line", password=True),
    )
    if not token:
        console.print("[red]No token entered; configuration was not changed.[/red]")
        raise typer.Exit(code=1)
    ENV_PATH.touch(exist_ok=True)
    secure_file(ENV_PATH)
    set_key(
        str(ENV_PATH), EXTENSION_TOKEN_KEY, token,
        quote_mode="auto",
    )
    secure_file(ENV_PATH)
    console.print(
        "[green]Existing Chrome connection configured.[/green]\n"
        "Keep Chrome open on the tab you want SelfApply to reuse, then run:\n"
        "[bold]selfapply apply --limit 1 --dry-run[/bold]"
    )


@app.command("connect-google")
def connect_google() -> None:
    """Interactively verify Google in SelfApply's persistent Chrome profile."""
    import json
    from datetime import datetime, timezone

    from rich.prompt import Prompt

    from selfapply import config
    from selfapply.apply import launcher

    _bootstrap()
    console.print(
        "\n[bold]Connect Google to SelfApply[/bold]\n"
        "A Chrome window will open. Sign in to Google normally, including any "
        "2-step verification. SelfApply never sees or stores your Google password."
    )
    try:
        with _managed_connect_browser() as port:
            signed_in, email = launcher.check_google_session(port)
            if not signed_in:
                launcher.open_google_signin(port)
                Prompt.ask(
                    "Press Enter once you have finished signing in in Chrome",
                    default="",
                    show_default=False,
                )
                signed_in, email = launcher.check_google_session(port)
            if not signed_in or not email:
                console.print(
                    "[red]Google sign-in could not be verified.[/red] "
                    "No session state was saved."
                )
                raise typer.Exit(code=1)
            state = {
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "account_email": email,
            }
            config.write_secret_file(
                launcher.GOOGLE_SESSION_PATH,
                json.dumps(state, indent=2),
            )
            console.print(
                f"[green]Google session verified:[/green] {email}\n"
                "You can now run [bold]selfapply connect-portals[/bold]."
            )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command("connect-portals")
def connect_portals() -> None:
    """Warm Google SSO on ATS tenants represented in the queued jobs."""
    import json
    from datetime import datetime, timezone

    from selfapply import config
    from selfapply.apply import launcher

    _bootstrap()
    if not launcher.GOOGLE_SESSION_PATH.exists():
        console.print(
            "[red]Google is not connected yet.[/red] "
            "Run [bold]selfapply connect-google[/bold] first."
        )
        raise typer.Exit(code=1)

    try:
        with _managed_connect_browser() as port:
            signed_in, email = launcher.check_google_session(port)
            if not signed_in or not email:
                console.print(
                    "[red]The saved Google session is no longer live.[/red] "
                    "Run [bold]selfapply connect-google[/bold] again."
                )
                raise typer.Exit(code=1)

            targets = launcher.queued_portal_targets()
            previous: dict = {}
            if launcher.CONNECTED_PORTALS_PATH.exists():
                try:
                    previous = json.loads(
                        launcher.CONNECTED_PORTALS_PATH.read_text(encoding="utf-8")
                    )
                except (json.JSONDecodeError, OSError):
                    previous = {}

            results = dict(previous)
            summary = {"connected": 0, "no_google_sso": 0, "failed": 0, "skipped": 0}
            for tenant, target_url in sorted(targets.items()):
                prior_status = str(previous.get(tenant, {}).get("status", ""))
                if prior_status in {"connected", "no_google_sso"}:
                    summary["skipped"] += 1
                    continue
                console.print(f"Connecting [cyan]{tenant}[/cyan]...")
                status = launcher.connect_portal_with_google(
                    target_url, email, port,
                )
                results[tenant] = {
                    "status": status,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "target_url": target_url,
                }
                config.write_secret_file(
                    launcher.CONNECTED_PORTALS_PATH,
                    json.dumps(results, indent=2, sort_keys=True),
                )
                if status == "connected":
                    summary["connected"] += 1
                elif status == "no_google_sso":
                    summary["no_google_sso"] += 1
                else:
                    summary["failed"] += 1
                if status == "google_session_expired":
                    console.print(
                        "[red]Google session expired during portal connection.[/red] "
                        "Run selfapply connect-google again."
                    )
                    break

            table = Table(title="Portal connection summary")
            table.add_column("Outcome")
            table.add_column("Count", justify="right")
            table.add_row("Connected", str(summary["connected"]))
            table.add_row("No Google SSO", str(summary["no_google_sso"]))
            table.add_row("Failed", str(summary["failed"]))
            table.add_row("Previously checked", str(summary["skipped"]))
            console.print(table)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from selfapply.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'selfapply init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'selfapply init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'selfapply init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append((
            "python-jobspy", fail_mark,
            (
                "Discovery is blocked until installed: pip install --no-deps "
                "python-jobspy && pip install pydantic tls-client requests markdownify regex"
            ),
        ))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_anthropic:
        model = os.environ.get("LLM_MODEL", "claude-sonnet-4-5")
        results.append(("LLM API key", ok_mark, f"Anthropic ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.selfapply/.env (run 'selfapply init')"))

    # --- Tier 3 checks ---
    # Browser agents
    codex_bin = shutil.which("codex")
    if codex_bin:
        results.append(("Codex CLI", ok_mark, f"{codex_bin} (default auto-apply agent)"))
    else:
        results.append((
            "Codex CLI", warn_mark,
            (
                "Install: npm install -g @openai/codex "
                "(https://github.com/openai/codex), or use Claude"
            ),
        ))

    if os.environ.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN"):
        results.append(("Existing Chrome", ok_mark, "Playwright extension token configured"))
    else:
        results.append(("Existing Chrome", warn_mark,
                        "Run 'selfapply browser-setup' or use --browser managed"))

    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, f"{claude_bin} (optional fallback)"))
    else:
        results.append((
            "Claude Code CLI", warn_mark,
            (
                "Optional fallback: npm install -g @anthropic-ai/claude-code "
                "(https://docs.anthropic.com/en/docs/claude-code/setup)"
            ),
        ))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append((
            "Chrome/Chromium", fail_mark,
            (
                "Install: https://www.google.com/chrome/ or set CHROME_PATH "
                "(needed for auto-apply)"
            ),
        ))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append((
            "Node.js (npx)", fail_mark,
            "Install Node.js 18+ from https://nodejs.org/ (needed for auto-apply)",
        ))

    try:
        check = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True, timeout=15,
        )
        locations = re.findall(r"Install location:\s+(.+)", check.stdout)
        installed = next((Path(value) for value in locations if Path(value).exists()), None)
        if check.returncode == 0 and installed:
            results.append(("Playwright Chromium", ok_mark, str(installed)))
        else:
            results.append((
                "Playwright Chromium", fail_mark,
                "Install with: playwright install chromium",
            ))
    except Exception as exc:
        results.append((
            "Playwright Chromium", fail_mark,
            f"Install with: playwright install chromium ({str(exc)[:80]})",
        ))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark,
                        "Available to legacy flows; Codex reports CAPTCHAs for manual handling"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Codex reports CAPTCHAs for manual handling"))

    # --- Render results ---
    console.print()
    console.print("[bold]SelfApply Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from selfapply.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Codex or Claude + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Codex or Claude + Chrome + Node.js)[/dim]")

    console.print()


@app.command("ui")
def control_ui(
    port: int = typer.Option(8766, "--port", help="Local Control Center port."),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open a browser."),
) -> None:
    """Open the local Connection Center and guarded Self Apply controls."""
    if not 1 <= port <= 65535:
        raise typer.BadParameter("port must be between 1 and 65535")
    from selfapply.control_server import serve

    serve(port=port, open_browser=not no_open)


if __name__ == "__main__":
    app()
