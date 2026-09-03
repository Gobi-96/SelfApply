import json
import sys
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from selfapply import config, pipeline
from selfapply.cli import app
from selfapply.wizard import init as wizard


def test_profile_validators_accept_and_normalize_common_values():
    assert wizard._validate_email(" person@example.com ") == "person@example.com"
    assert wizard._validate_optional_url("https://example.com/me") == "https://example.com/me"
    assert wizard._validate_optional_url("  ") == ""
    assert wizard._validate_postal_code("97205") == "97205"
    assert wizard._validate_postal_code("K1A 0B1") == "K1A 0B1"
    assert wizard._validate_money("$120,000") == "120000"
    assert wizard._validate_salary_range("80,000 - 120,000") == "80000-120000"
    assert wizard._validate_nonnegative_int("25") == 25


@pytest.mark.parametrize(
    ("validator", "value"),
    [
        (wizard._validate_email, "not-an-email"),
        (wizard._validate_optional_url, "linkedin.com/in/example"),
        (wizard._validate_postal_code, "?"),
        (wizard._validate_money, "a lot"),
        (wizard._validate_salary_range, "120000-80000"),
        (wizard._validate_nonnegative_int, "-1"),
    ],
)
def test_profile_validators_reject_values_that_break_downstream_logic(validator, value):
    with pytest.raises(ValueError):
        validator(value)


def test_profile_checkpoint_is_immediately_reusable(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile = {"personal": {"email": "person@example.com"}}

    with patch.object(wizard, "PROFILE_PATH", profile_path):
        wizard._save_profile_checkpoint(profile, "personal information")
        assert wizard._load_profile_checkpoint() == profile

    assert json.loads(profile_path.read_text()) == profile


def test_profile_completeness_names_missing_fields_instead_of_keyerror():
    assert config.profile_missing_fields({"personal": {"full_name": "Person"}}) == [
        "email", "phone", "city", "country", "work authorization",
        "sponsorship answer", "salary expectation", "target role",
    ]


def test_discovery_stops_with_exact_jobspy_recovery_command():
    with patch.dict(sys.modules, {"jobspy": None}):
        with pytest.raises(RuntimeError) as exc_info:
            pipeline._require_jobspy()

    message = str(exc_info.value)
    assert "pip install --no-deps python-jobspy" in message
    assert "selfapply doctor" in message


def test_report_is_primary_static_command_and_dashboard_explains_live_view():
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "report" in help_result.stdout

    with patch("selfapply.view.open_dashboard"):
        result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert "static results report" in result.stdout
    assert "live worker dashboard" in result.stdout


def test_wizard_runs_doctor_after_all_setup_steps():
    setup_patches = [
        patch.object(wizard, "ensure_dirs"),
        patch.object(wizard, "_setup_resume"),
        patch.object(wizard, "_setup_profile"),
        patch.object(wizard, "_setup_searches"),
        patch.object(wizard, "_setup_ai_features"),
        patch.object(wizard, "_setup_auto_apply"),
        patch.object(wizard, "_offer_autostart"),
        patch("selfapply.cli.doctor"),
    ]
    entered = [item.__enter__() for item in setup_patches]
    try:
        wizard.run_wizard()
    finally:
        for item in reversed(setup_patches):
            item.__exit__(None, None, None)

    entered[-1].assert_called_once_with()


def test_wizard_autostart_requires_explicit_yes_not_default():
    with (
        patch.object(wizard, "Confirm") as confirm,
        patch("selfapply.worker_supervisor.run_worker_supervisor") as run,
    ):
        confirm.ask.return_value = False  # simulates bare-Enter/default response
        wizard._offer_autostart(tier=3)
    run.assert_not_called()


def test_wizard_autostart_requires_valid_spend_cap():
    with (
        patch.object(wizard, "Confirm") as confirm,
        patch.object(wizard, "Prompt") as prompt,
        patch("selfapply.worker_supervisor.run_worker_supervisor") as run,
    ):
        confirm.ask.return_value = True
        prompt.ask.return_value = "not-a-number"
        wizard._offer_autostart(tier=3)
    run.assert_not_called()


def test_wizard_autostart_passes_spend_cap_through():
    with (
        patch.object(wizard, "Confirm") as confirm,
        patch.object(wizard, "Prompt") as prompt,
        patch.object(wizard, "shutil") as shutil_mock,
        patch("selfapply.worker_supervisor.run_worker_supervisor") as run,
    ):
        confirm.ask.side_effect = [True, True]  # start now, dry-run first
        prompt.ask.return_value = "12.50"
        shutil_mock.which.return_value = "/usr/bin/codex"
        wizard._offer_autostart(tier=3)
    run.assert_called_once()
    assert run.call_args.kwargs["max_spend_usd"] == 12.50
    assert run.call_args.kwargs["dry_run"] is True
    assert run.call_args.kwargs["browser"] == "managed"


def test_wizard_tier2_autostart_also_defaults_to_no():
    with (
        patch.object(wizard, "Confirm") as confirm,
        patch("selfapply.pipeline.run_pipeline") as run,
    ):
        confirm.ask.return_value = False
        wizard._offer_autostart(tier=2)
    run.assert_not_called()
