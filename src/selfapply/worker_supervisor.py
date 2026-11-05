"""Unified supervisor for queue replenishment and continuous applications."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from selfapply import config, database, llm, pipeline
from selfapply.apply import launcher
from selfapply.apply.dashboard import add_event, get_totals

SUMMARY_PATH = config.APP_DIR / "last_run_summary.json"

_stop_reason_lock = threading.Lock()
_stop_reason: str = ""


def _set_stop_reason(reason: str) -> None:
    """Record the first reason Self Apply decided to stop."""
    global _stop_reason
    with _stop_reason_lock:
        if not _stop_reason:
            _stop_reason = reason


def _get_stop_reason() -> str:
    with _stop_reason_lock:
        return _stop_reason or "unknown"


def _reset_stop_reason() -> None:
    global _stop_reason
    with _stop_reason_lock:
        _stop_reason = ""


def queue_snapshot(min_score: int, max_attempts: int) -> tuple[int, int]:
    """Return applied and currently eligible ready-job counts."""
    conn = database.get_connection()
    applied_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]
    blocked_sites, blocked_patterns = launcher._load_blocked()
    params: list[object] = [max_attempts, min_score]
    site_clause = ""
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND site NOT IN ({placeholders})"
        params.extend(sorted(blocked_sites))
    url_clause = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
    params.extend(blocked_patterns)
    login_tenant_hosts = {
        host
        for row in conn.execute(
            """
            SELECT url FROM jobs
            WHERE apply_error IN ('login_issue', 'login_issue_tenant_blocked')
            """
        ).fetchall()
        if (host := launcher._workday_tenant_host(row["url"]))
    }
    tenant_clause = ""
    if login_tenant_hosts:
        tenant_clause = " ".join(
            "AND lower(url) NOT LIKE ? "
            "AND lower(COALESCE(application_url, '')) NOT LIKE ?"
            for _ in login_tenant_hosts
        )
        for host in sorted(login_tenant_hosts):
            params.extend((f"%{host}%", f"%{host}%"))

    ready_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM jobs
        WHERE tailored_resume_path IS NOT NULL
          AND apply_status IS NULL
          AND (apply_attempts IS NULL OR apply_attempts < ?)
          AND fit_score >= ?
          {site_clause}
          {url_clause}
          {tenant_clause}
        """,
        params,
    ).fetchone()[0]
    return int(applied_count), int(ready_count)


def _replenish_once(
    min_score: int,
    queue_threshold: int,
    target_applied: int | None,
    last_discover_ts: float,
    discover_interval: float,
    validation_mode: str,
    workers: int,
    smart_extract: bool,
    max_spend_usd: float | None = None,
) -> tuple[bool, float]:
    """Run at most one replenishment pass and report whether the target is met."""
    applied_count, ready_count = queue_snapshot(
        min_score, config.DEFAULTS["max_apply_attempts"],
    )
    if target_applied is not None and applied_count >= target_applied:
        _set_stop_reason("target_applied_reached")
        return True, last_discover_ts

    if max_spend_usd is not None:
        spend = llm.get_cumulative_cost_usd() + get_totals()["cost"]
        if spend >= max_spend_usd:
            add_event(
                f"[ALERT] Spend cap reached (${spend:.2f} >= "
                f"${max_spend_usd:.2f}) — stopping"
            )
            _set_stop_reason("spend_cap_reached")
            return True, last_discover_ts

    now = time.time()
    downstream = ["enrich", "score", "tailor", "pdf"]
    discovery_due = (
        ready_count < queue_threshold
        and now - last_discover_ts >= discover_interval
    )
    if discovery_due:
        # A discovery pass can create work for every downstream stage.
        stages = ["discover", *downstream]
        last_discover_ts = now
    else:
        stages = [
            stage for stage in downstream
            if pipeline._count_pending(stage, min_score) > 0
        ]
        if not stages:
            add_event(
                f"[worker] {ready_count} jobs ready; no preparation needed"
            )
            return False, last_discover_ts

    add_event(
        f"[worker] Replenishing queue ({ready_count} ready): "
        f"{' → '.join(stages)}"
    )
    result = pipeline.run_pipeline(
        stages=stages,
        min_score=min_score,
        stream=True,
        quiet=True,
        workers=workers,
        validation_mode=validation_mode,
        smart_extract=smart_extract,
    )
    if result.get("errors"):
        add_event(f"[worker] Replenishment errors: {len(result['errors'])}")
    else:
        add_event("[worker] Queue replenishment completed")
    return False, last_discover_ts


def _replenish_loop(
    stop_event: threading.Event,
    poll_seconds: float,
    **kwargs,
) -> None:
    """Replenish until stopped or the requested applied count is reached."""
    last_discover_ts = 0.0
    while not stop_event.is_set():
        target_reached, last_discover_ts = _replenish_once(
            last_discover_ts=last_discover_ts,
            **kwargs,
        )
        if target_reached:
            add_event("[worker] Application target reached; stopping")
            launcher._stop_event.set()
            break
        if stop_event.wait(poll_seconds):
            break


def run_worker_supervisor(
    *,
    max_spend_usd: float,
    target_applied: int | None = None,
    queue_threshold: int = 50,
    discover_interval: float = 1800,
    replenish_poll_interval: float = 300,
    validation_mode: str = "normal",
    pipeline_workers: int = 1,
    smart_extract: bool = True,
    test_limit: int = 0,
    **apply_kwargs,
) -> None:
    """Run queue replenishment in the background and apply in the main thread.

    max_spend_usd is required (not optional/defaulted) -- continuous
    unattended applying must never launch without an explicit budget limit.
    """
    if max_spend_usd is None:
        raise ValueError("run_worker_supervisor() requires an explicit max_spend_usd")
    config.ensure_dirs()
    database.init_db()
    _reset_stop_reason()
    if target_applied is not None:
        applied_count, _ = queue_snapshot(
            apply_kwargs.get("min_score", config.DEFAULTS["min_score"]),
            config.DEFAULTS["max_apply_attempts"],
        )
        if applied_count >= target_applied:
            add_event("[worker] Application target already reached")
            _set_stop_reason("target_applied_reached")
            _write_summary()
            return
    stop_event = threading.Event()

    def apply_stop_condition() -> bool:
        """Enforce caps at every job boundary, not only on the poll timer."""
        if test_limit and apply_kwargs.get("dry_run"):
            completed_tests = int(get_totals().get("applied") or 0)
            if completed_tests >= test_limit:
                _set_stop_reason("test_run_complete")
                add_event(
                    f"[worker] Test run complete — {completed_tests} form"
                    f"{'s' if completed_tests != 1 else ''} checked without submission"
                )
                return True
        applied_count, _ = queue_snapshot(
            apply_kwargs.get("min_score", config.DEFAULTS["min_score"]),
            config.DEFAULTS["max_apply_attempts"],
        )
        if target_applied is not None and applied_count >= target_applied:
            _set_stop_reason("target_applied_reached")
            add_event("[ALERT] Applied-job target reached — stopping before another job")
            return True
        spend = llm.get_cumulative_cost_usd() + get_totals()["cost"]
        if spend >= max_spend_usd:
            _set_stop_reason("spend_cap_reached")
            add_event(
                f"[ALERT] Spend cap reached (${spend:.2f} >= ${max_spend_usd:.2f}) "
                "— stopping before another job"
            )
            return True
        return False
    replenish_thread = threading.Thread(
        target=_replenish_loop,
        kwargs={
            "stop_event": stop_event,
            "poll_seconds": replenish_poll_interval,
            "min_score": apply_kwargs.get("min_score", config.DEFAULTS["min_score"]),
            "queue_threshold": queue_threshold,
            "target_applied": target_applied,
            "discover_interval": discover_interval,
            "validation_mode": validation_mode,
            "workers": pipeline_workers,
            "smart_extract": smart_extract,
            "max_spend_usd": max_spend_usd,
        },
        name="selfapply-replenisher",
        daemon=True,
    )
    replenish_thread.start()
    try:
        launcher.main(
            continuous=True, limit=0,
            stop_condition=apply_stop_condition,
            **apply_kwargs,
        )
    except KeyboardInterrupt:
        _set_stop_reason("user_interrupt")
        raise
    except Exception:
        _set_stop_reason("error")
        raise
    finally:
        stop_event.set()
        launcher._stop_event.set()
        replenish_thread.join(timeout=5)
        _write_summary()


def _write_summary() -> None:
    """Write a durable end-of-run summary so an unattended run leaves a trace."""
    totals = get_totals()
    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "applied": totals["applied"],
        "failed": totals["failed"],
        "cost_usd": round(llm.get_cumulative_cost_usd() + totals["cost"], 4),
        "stop_reason": _get_stop_reason(),
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    add_event(
        f"[worker] Finished: {summary['applied']} applied, "
        f"{summary['failed']} failed, ${summary['cost_usd']:.2f} spent "
        f"({summary['stop_reason']})"
    )
