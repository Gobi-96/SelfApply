#!/usr/bin/env python3
# Superseded by `selfapply run-worker`; retained for older background services.
"""Replenish SelfApply's ready queue without overlapping pipeline runs."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


APP_DIR = Path(os.environ.get("SELFAPPLY_DIR", Path.home() / ".selfapply"))
DB_PATH = APP_DIR / "selfapply.db"
LOCK_PATH = APP_DIR / "replenish.lock"
WORKDIR = Path(os.environ.get("SELFAPPLY_PROJECT_DIR", Path(__file__).resolve().parents[1]))
SELFAPPLY = Path(os.environ.get("SELFAPPLY_EXECUTABLE", WORKDIR / ".venv" / "bin" / "selfapply"))
TARGET_APPLIED = 500
QUEUE_THRESHOLD = 50
MAX_APPLY_ATTEMPTS = 3


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def counts() -> tuple[int, int]:
    connection = sqlite3.connect(DB_PATH)
    try:
        applied = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE apply_status='applied'"
        ).fetchone()[0]
        ready = connection.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE tailored_resume_path IS NOT NULL
              AND fit_score >= 7
              AND (apply_status IS NULL OR apply_status='failed')
              AND COALESCE(apply_attempts, 0) < ?
            """,
            (MAX_APPLY_ATTEMPTS,),
        ).fetchone()[0]
        return applied, ready
    finally:
        connection.close()


def another_pipeline_is_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "selfapply run"],
        capture_output=True,
        text=True,
        check=False,
    )
    return any(
        line.strip().isdigit() and int(line.strip()) != os.getpid()
        for line in result.stdout.splitlines()
    )


def run_stage(arguments: list[str], timeout: int) -> int:
    command = [str(SELFAPPLY), "run", *arguments]
    log("running: " + " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=WORKDIR,
            check=False,
            timeout=timeout,
        )
        return completed.returncode
    except subprocess.TimeoutExpired:
        log(f"pipeline timed out after {timeout}s")
        return 124


def main() -> int:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another replenisher owns the lock; skipping")
            return 0

        if not DB_PATH.exists():
            log("database not initialized; skipping")
            return 0

        applied, ready = counts()
        log(f"state before: applied={applied}, ready_eligible={ready}")
        if applied >= TARGET_APPLIED:
            log(f"target reached ({TARGET_APPLIED}); no replenishment needed")
            return 0
        if ready >= QUEUE_THRESHOLD:
            log(f"queue is at or above threshold ({QUEUE_THRESHOLD}); skipping")
            return 0
        if another_pipeline_is_running():
            log("a SelfApply pipeline is already running; skipping")
            return 0

        discovery_rc = run_stage(
            [
                "discover", "--workers", "1", "--validation", "normal",
                "--skip-smart-extract",
            ],
            timeout=3_600,
        )
        if discovery_rc not in (0, 1):
            log(f"discovery returned {discovery_rc}; downstream stages not started")
            return discovery_rc

        downstream_rc = run_stage(
            [
                # Cover letters remain paused until they receive the same
                # deterministic fact validation as tailored resumes.
                "enrich", "score", "tailor", "pdf",
                "--workers", "1", "--validation", "normal",
            ],
            timeout=7_200,
        )
        applied_after, ready_after = counts()
        log(
            f"state after: applied={applied_after}, "
            f"ready_eligible={ready_after}, rc={downstream_rc}"
        )
        return downstream_rc


if __name__ == "__main__":
    sys.exit(main())
