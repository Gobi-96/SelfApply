import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from selfapply.worker_supervisor import (
    _replenish_loop,
    _replenish_once,
    queue_snapshot,
)
from selfapply.cli import app
from selfapply.pipeline import _count_pending


class WorkerSupervisorTests(unittest.TestCase):
    def test_replenish_once_includes_discover_only_when_low_and_due(self):
        with (
            patch("selfapply.worker_supervisor.queue_snapshot", return_value=(2, 3)),
            patch("selfapply.worker_supervisor.time.time", return_value=1000),
            patch("selfapply.worker_supervisor.pipeline.run_pipeline", return_value={"errors": {}}) as run,
            patch("selfapply.worker_supervisor.add_event"),
        ):
            _replenish_once(7, 5, None, 0, 300, "normal", 1, False)
        self.assertIn("discover", run.call_args.kwargs["stages"])

        with (
            patch("selfapply.worker_supervisor.queue_snapshot", return_value=(2, 8)),
            patch(
                "selfapply.worker_supervisor.pipeline._count_pending",
                side_effect=lambda stage, _score: 1 if stage == "tailor" else 0,
            ),
            patch("selfapply.worker_supervisor.pipeline.run_pipeline", return_value={"errors": {}}) as run,
            patch("selfapply.worker_supervisor.add_event"),
        ):
            _replenish_once(7, 5, None, 0, 300, "normal", 1, False)
        self.assertNotIn("discover", run.call_args.kwargs["stages"])
        self.assertEqual(run.call_args.kwargs["stages"], ["tailor"])

    def test_replenish_skips_pipeline_when_queue_is_healthy_and_nothing_is_pending(self):
        with (
            patch("selfapply.worker_supervisor.queue_snapshot", return_value=(2, 8)),
            patch("selfapply.worker_supervisor.pipeline._count_pending", return_value=0),
            patch("selfapply.worker_supervisor.pipeline.run_pipeline") as run,
            patch("selfapply.worker_supervisor.add_event") as event,
        ):
            stopped, timestamp = _replenish_once(
                7, 5, None, 0, 300, "normal", 1, False,
            )
        self.assertFalse(stopped)
        self.assertEqual(timestamp, 0)
        run.assert_not_called()
        self.assertIn("no preparation needed", event.call_args.args[0])

    def test_replenish_loop_stops_when_target_reached(self):
        supervisor_stop = threading.Event()
        launcher_stop = threading.Event()
        with (
            patch("selfapply.worker_supervisor.queue_snapshot", return_value=(10, 0)),
            patch("selfapply.worker_supervisor.launcher._stop_event", launcher_stop),
            patch("selfapply.worker_supervisor.pipeline.run_pipeline") as run,
            patch("selfapply.worker_supervisor.add_event"),
        ):
            _replenish_loop(
                supervisor_stop, 1, min_score=7, queue_threshold=5,
                target_applied=10, discover_interval=300,
                validation_mode="normal", workers=1, smart_extract=False,
            )
        self.assertTrue(launcher_stop.is_set())
        run.assert_not_called()

    def test_queue_snapshot_uses_apply_eligibility_filters(self):
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value=(4,))),
            Mock(fetchall=Mock(return_value=[])),
            Mock(fetchone=Mock(return_value=(7,))),
        ]
        with (
            patch("selfapply.worker_supervisor.database.get_connection", return_value=connection),
            patch("selfapply.worker_supervisor.launcher._load_blocked", return_value=(set(), [])),
        ):
            self.assertEqual(queue_snapshot(8, 3), (4, 7))
        sql, params = connection.execute.call_args.args
        self.assertIn("tailored_resume_path IS NOT NULL", sql)
        self.assertIn("apply_status IS NULL", sql)
        self.assertIn("apply_attempts < ?", sql)
        self.assertIn("fit_score >= ?", sql)
        self.assertEqual(params, [3, 8])

    def test_run_worker_cli_wires_flags_through(self):
        settings = {
            "agent": "codex", "browser_mode": "managed", "model": "test-model",
            "max_actions": 20, "max_seconds": 120,
        }
        with (
            patch("selfapply.cli._bootstrap"),
            patch("selfapply.cli._resolve_apply_settings", return_value=settings),
            patch("selfapply.worker_supervisor.run_worker_supervisor") as run,
        ):
            result = CliRunner().invoke(app, [
                "run-worker", "--max-spend", "5.00",
                "--target-applied", "25", "--queue-threshold", "9",
                "--discover-interval", "600", "--replenish-poll-interval", "30",
                "--dry-run", "--test-limit", "1", "--skip-smart-extract",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(run.call_args.kwargs["max_spend_usd"], 5.00)
        self.assertEqual(run.call_args.kwargs["target_applied"], 25)
        self.assertEqual(run.call_args.kwargs["queue_threshold"], 9)
        self.assertEqual(run.call_args.kwargs["discover_interval"], 600)
        self.assertEqual(run.call_args.kwargs["replenish_poll_interval"], 30)
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertEqual(run.call_args.kwargs["test_limit"], 1)
        self.assertFalse(run.call_args.kwargs["smart_extract"])

    def test_run_worker_cli_requires_max_spend(self):
        with (
            patch("selfapply.cli._bootstrap"),
            patch("selfapply.worker_supervisor.run_worker_supervisor") as run,
        ):
            result = CliRunner().invoke(app, ["run-worker", "--target-applied", "1"])
        self.assertNotEqual(result.exit_code, 0)
        run.assert_not_called()

    def test_replenish_once_stops_when_spend_cap_reached(self):
        with (
            patch("selfapply.worker_supervisor.queue_snapshot", return_value=(0, 100)),
            patch("selfapply.worker_supervisor.llm.get_cumulative_cost_usd", return_value=4.0),
            patch("selfapply.worker_supervisor.get_totals", return_value={"applied": 0, "failed": 0, "cost": 1.5}),
            patch("selfapply.worker_supervisor.pipeline.run_pipeline") as run,
            patch("selfapply.worker_supervisor.add_event"),
        ):
            target_reached, _ = _replenish_once(
                7, 5, None, 0, 300, "normal", 1, False, max_spend_usd=5.0,
            )
        self.assertTrue(target_reached)
        run.assert_not_called()

    def test_replenish_once_continues_under_spend_cap(self):
        with (
            patch("selfapply.worker_supervisor.queue_snapshot", return_value=(0, 2)),
            patch("selfapply.worker_supervisor.llm.get_cumulative_cost_usd", return_value=0.10),
            patch("selfapply.worker_supervisor.get_totals", return_value={"applied": 0, "failed": 0, "cost": 0.0}),
            patch("selfapply.worker_supervisor.time.time", return_value=1000),
            patch("selfapply.worker_supervisor.pipeline.run_pipeline", return_value={"errors": {}}) as run,
            patch("selfapply.worker_supervisor.add_event"),
        ):
            target_reached, _ = _replenish_once(
                7, 5, None, 0, 300, "normal", 1, False, max_spend_usd=5.0,
            )
        self.assertFalse(target_reached)
        run.assert_called_once()

    def test_run_worker_requires_max_spend_usd(self):
        from selfapply.worker_supervisor import run_worker_supervisor
        with self.assertRaises(ValueError):
            run_worker_supervisor(max_spend_usd=None)

    def test_test_run_stops_after_one_successful_dry_form_check(self):
        from selfapply.worker_supervisor import run_worker_supervisor

        def run_launcher(**kwargs):
            self.assertTrue(kwargs["dry_run"])
            self.assertTrue(kwargs["stop_condition"]())

        fake_thread = Mock()
        with (
            patch("selfapply.worker_supervisor.config.ensure_dirs"),
            patch("selfapply.worker_supervisor.database.init_db"),
            patch("selfapply.worker_supervisor.threading.Thread", return_value=fake_thread),
            patch("selfapply.worker_supervisor.launcher.main", side_effect=run_launcher),
            patch("selfapply.worker_supervisor.launcher._stop_event", threading.Event()),
            patch("selfapply.worker_supervisor.get_totals", return_value={
                "applied": 1, "failed": 0, "cost": 0,
            }),
            patch("selfapply.worker_supervisor._set_stop_reason") as reason,
            patch("selfapply.worker_supervisor._write_summary"),
            patch("selfapply.worker_supervisor.add_event"),
        ):
            run_worker_supervisor(
                max_spend_usd=5, dry_run=True, test_limit=1,
            )
        reason.assert_any_call("test_run_complete")

    def test_pdf_pending_count_stops_when_outputs_exist(self):
        with tempfile.TemporaryDirectory() as root:
            resume = Path(root) / "resume.txt"
            resume.write_text("resume")
            resume.with_suffix(".pdf").write_bytes(b"%PDF")
            connection = Mock()
            connection.execute.return_value.fetchall.return_value = [{
                "tailored_resume_path": str(resume),
                "cover_letter_path": None,
            }]
            with patch("selfapply.pipeline.get_connection", return_value=connection):
                self.assertEqual(_count_pending("pdf"), 0)
            resume.with_suffix(".pdf").unlink()
            with patch("selfapply.pipeline.get_connection", return_value=connection):
                self.assertEqual(_count_pending("pdf"), 1)


if __name__ == "__main__":
    unittest.main()
