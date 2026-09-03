import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from io import BytesIO
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import httpx

from selfapply import control_server, database


def valid_payload(**overrides):
    value = {
        "confirmed": True, "max_spend": 12.5, "target_applied": 500,
        "workers": 1, "max_actions": 60, "max_minutes": 8,
        "queue_threshold": 50,
    }
    value.update(overrides)
    return value


def test_worker_limits_default_to_150_actions_and_15_minutes():
    values = control_server.validate_start_payload({
        "confirmed": True,
        "max_spend": 10,
        "target_applied": 500,
        "workers": 1,
        "queue_threshold": 50,
        "agent": "codex",
        "browser": "managed",
    })
    assert values["max_actions"] == 150
    assert values["max_minutes"] == 15


def test_start_requires_confirmation_and_positive_spend():
    with pytest.raises(ValueError, match="confirmation"):
        control_server.validate_start_payload(valid_payload(confirmed=False))
    with pytest.raises(ValueError, match="Spending cap"):
        control_server.validate_start_payload(valid_payload(max_spend=0))


def test_worker_arguments_include_guardrails():
    args = control_server.build_program_arguments(
        control_server.validate_start_payload(valid_payload())
    )
    assert args[1] == "run-worker"
    assert args[args.index("--max-spend") + 1] == "12.5"
    assert args[args.index("--workers") + 1] == "1"
    assert "--skip-smart-extract" in args
    expected = Path(control_server.__file__).resolve().parents[2] / ".venv/bin/selfapply"
    assert Path(args[0]) == expected


def test_portals_include_six_boards_and_one_representative_workday():
    queued = {
        "brookfield.wd5.myworkdayjobs.com":
            "https://brookfield.wd5.myworkdayjobs.com/job/123",
        "greenhouse": "https://boards.greenhouse.io/example",
    }
    with patch("selfapply.apply.launcher.queued_portal_targets", return_value=queued):
        targets = control_server.portal_targets()
    assert set(control_server.BASE_PORTALS).issubset(targets)
    assert targets["Workday"] == queued["brookfield.wd5.myworkdayjobs.com"]
    assert len([key for key in targets if key.startswith("Workday")]) == 1
    assert all("greenhouse" not in key.casefold() for key in targets)


def test_start_repairs_plist_and_refuses_duplicate(tmp_path):
    plist = tmp_path / "worker.plist"
    completed = control_server.subprocess.CompletedProcess([], 0, "", "")
    with (
        patch.object(control_server, "PLIST_PATH", plist),
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "settings_snapshot", return_value={}),
        patch.object(control_server, "setup_readiness", return_value={
            "complete": True, "missing": [],
        }),
        patch.object(control_server, "_launchctl", return_value=completed) as launch,
        patch.object(control_server.subprocess, "run", return_value=completed),
    ):
        result = control_server.start_worker(valid_payload())
    assert result["ok"] is True
    content = control_server.plistlib.loads(plist.read_bytes())
    assert "--max-spend" in content["ProgramArguments"]
    assert content["WorkingDirectory"] == str(Path(control_server.__file__).resolve().parents[2])
    assert content["ProgramArguments"][0] == str(
        Path(control_server.__file__).resolve().parents[2] / ".venv/bin/selfapply"
    )
    assert any(call.args[0] == "bootstrap" for call in launch.call_args_list)
    assert not any(call.args[0] == "kickstart" for call in launch.call_args_list)

    with patch.object(control_server, "service_running", return_value=True):
        with pytest.raises(RuntimeError, match="already running"):
            control_server.start_worker(valid_payload())


def test_public_worker_label_uses_selfapply_service():
    running = control_server.subprocess.CompletedProcess([], 0, "state = running", "")
    with (
        patch.object(control_server.platform, "system", return_value="Darwin"),
        patch.object(control_server, "_launchctl", return_value=running) as launch,
    ):
        assert control_server._active_service_label() == "io.selfapply.worker"
    assert control_server.LABEL == "io.selfapply.worker"
    assert launch.call_count == 1


def test_windows_ui_starts_a_detached_background_worker(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    state_path = tmp_path / "worker-service.json"
    process = Mock(pid=4321)
    with (
        patch.object(control_server.platform, "system", return_value="Windows"),
        patch.object(control_server.config, "APP_DIR", tmp_path),
        patch.object(control_server.config, "LOG_DIR", log_dir),
        patch.object(control_server.config, "ensure_dirs"),
        patch.object(control_server, "WORKER_STATE_PATH", state_path),
        patch.object(control_server.subprocess, "Popen", return_value=process) as popen,
    ):
        control_server._install_worker_service(
            ["selfapply.exe", "run-worker", "--agent", "codex"], continuous=True,
        )
    kwargs = popen.call_args.kwargs
    assert kwargs["cwd"] == str(Path(control_server.__file__).resolve().parents[2])
    assert kwargs["env"]["SELFAPPLY_DIR"] == str(tmp_path)
    assert "creationflags" in kwargs
    assert json.loads(state_path.read_text())["pid"] == 4321


def test_windows_background_worker_state_is_detected_and_stale_state_removed(tmp_path):
    state_path = tmp_path / "worker-service.json"
    state_path.write_text(json.dumps({"pid": 4321, "arguments": ["selfapply.exe"]}))
    with (
        patch.object(control_server.platform, "system", return_value="Windows"),
        patch.object(control_server, "WORKER_STATE_PATH", state_path),
        patch.object(control_server, "_pid_is_running", return_value=True),
    ):
        assert control_server._background_worker_state()["pid"] == 4321
    with (
        patch.object(control_server.platform, "system", return_value="Windows"),
        patch.object(control_server, "WORKER_STATE_PATH", state_path),
        patch.object(control_server, "_pid_is_running", return_value=False),
    ):
        assert control_server._background_worker_state() is None
    assert not state_path.exists()


def test_ui_copy_is_platform_neutral():
    assert "this Mac" not in control_server.HTML
    assert "this device" in control_server.HTML


def test_loaded_worker_is_detected_between_keepalive_restarts(tmp_path):
    loaded = control_server.subprocess.CompletedProcess(
        [], 0, "state = waiting\nlast exit code = 1", "",
    )
    plist = tmp_path / "worker.plist"
    plist.write_bytes(control_server.plistlib.dumps({"KeepAlive": {"SuccessfulExit": False}}))
    with (
        patch.object(control_server.platform, "system", return_value="Darwin"),
        patch.object(control_server, "_launchctl", return_value=loaded),
        patch.object(control_server, "PLIST_PATH", plist),
    ):
        assert control_server.service_running() is True
        assert control_server._active_service_label() == control_server.LABEL


def test_completed_one_shot_worker_is_not_reported_as_running(tmp_path):
    loaded = control_server.subprocess.CompletedProcess(
        [], 0, "active count = 0\nstate = not running\nlast exit code = 0", "",
    )
    plist = tmp_path / "worker.plist"
    plist.write_bytes(control_server.plistlib.dumps({"KeepAlive": False}))
    with (
        patch.object(control_server.platform, "system", return_value="Darwin"),
        patch.object(control_server, "_launchctl", return_value=loaded),
        patch.object(control_server, "PLIST_PATH", plist),
        patch.object(control_server, "_managed_worker_pids", return_value=[]),
    ):
        assert control_server._active_service_label() is None
        assert control_server.service_running() is False


def test_worker_configuration_reports_agent_browser_and_count(tmp_path):
    plist = tmp_path / "worker.plist"
    plist.write_bytes(control_server.plistlib.dumps({
        "ProgramArguments": [
            "selfapply", "run-worker", "--agent", "claude",
            "--browser", "managed", "--workers", "2",
        ],
    }))
    assert control_server._worker_configuration(plist) == {
        "agent": "claude", "browser": "managed", "workers": 2,
        "mode": "self apply",
    }


def test_managed_worker_pid_scan_excludes_normal_user_sessions(tmp_path):
    app_dir = tmp_path / ".selfapply"
    chrome_dir = app_dir / "chrome-workers"
    process_list = "\n".join([
        f"101 claude --mcp-config {app_dir}/.mcp-apply-1.json -p",
        f"102 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
        f"--user-data-dir={chrome_dir}/worker-1",
        "105 npm exec @playwright/mcp@latest "
        "--cdp-endpoint=http://localhost:9223 --viewport-size=1280x900",
        "103 claude",
        "104 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ])
    completed = control_server.subprocess.CompletedProcess([], 0, process_list, "")
    with (
        patch.object(control_server.platform, "system", return_value="Darwin"),
        patch.object(control_server.config, "APP_DIR", app_dir),
        patch.object(control_server.config, "CHROME_WORKER_DIR", chrome_dir),
        patch.object(control_server.subprocess, "run", return_value=completed),
    ):
        assert control_server._managed_worker_pids() == [101, 102, 105]


def test_local_http_ui_and_csrf_protection():
    controller = control_server.Controller()
    token = "t" * 40
    with (
        patch.object(control_server, "database_status", return_value={
            "running": False, "counts": {"total": 0, "applied": 0, "failed": 0,
            "in_progress": 0, "ready": 0}, "current_job": None,
        }),
        patch.object(controller, "connections", return_value={
            "google": {}, "portals": [], "action": {},
        }),
    ):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), control_server.make_handler(controller, token)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            assert b"Connections" in urllib.request.urlopen(base + "/").read()
            status = json.load(urllib.request.urlopen(base + "/api/status"))
            assert status["running"] is False
            request = urllib.request.Request(
                base + "/api/worker/stop", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(request)
            assert exc.value.code == 403
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_connection_center_hides_representative_workday_tenant():
    controller = control_server.Controller()
    queued = {
        "brookfield": "https://brookfield.wd5.myworkdayjobs.com/job/123",
    }
    with (
        patch("selfapply.apply.launcher.queued_portal_targets", return_value=queued),
        patch.object(control_server, "_read_json", return_value={}),
    ):
        workday = next(
            item for item in controller.connections()["portals"]
            if item["name"] == "Workday"
        )
    assert workday["host"] == "workday.com"
    assert "brookfield" not in workday["host"]


def test_connected_google_goes_directly_to_portal_without_account_check(tmp_path):
    controller = control_server.Controller()

    @contextmanager
    def browser():
        yield 9222

    (tmp_path / "google_session.json").write_text(json.dumps({
        "verified": True, "email": "person@example.com",
    }))
    with (
        patch.object(control_server.config, "APP_DIR", tmp_path),
        patch.object(control_server, "portal_targets", return_value={
            "Dice": "https://www.dice.com/dashboard/login",
        }),
        patch.object(controller, "begin", side_effect=lambda kind, task: {"result": task()}),
        patch("selfapply.cli._managed_connect_browser", side_effect=browser),
        patch("selfapply.apply.launcher.connect_portal_with_google", return_value="connected") as connect,
        patch("selfapply.apply.launcher.check_google_session") as account_check,
    ):
        result = controller.test_portal("Dice")
    assert result["result"] == "Dice: connected"
    connect.assert_called_once_with(
        "https://www.dice.com/dashboard/login", "person@example.com", 9222,
        cancel_event=controller.cancel_event,
        browser_mode="managed",
    )
    account_check.assert_not_called()


def test_ui_offers_connect_all_google_and_manual_login_without_credential_fields():
    assert "Connect remaining with Google" in control_server.HTML
    assert "Connect with Google" in control_server.HTML
    assert "Manual login" in control_server.HTML
    assert "Unlink" in control_server.HTML
    assert "Stop connecting" in control_server.HTML
    assert "post('/api/connections/manual',{name:n})" in control_server.HTML


def test_google_connection_ui_reports_progress_and_prevents_duplicate_actions():
    html = control_server.HTML
    assert "googleBusy=busy&&c.action.kind==='Google sign-in'" in html
    assert "googleBusy?'Connecting…':'Connect'" in html
    assert "${busy?'disabled':''}" in html
    assert "portalBusy=busy&&" in html
    assert "portalBusy?'Connecting…':'Connect with Google'" in html


def test_google_connection_wait_can_be_cancelled(tmp_path):
    controller = control_server.Controller()

    @contextmanager
    def browser():
        yield 9222

    def wait(_port, *, cancel_event, timeout):
        cancel_event.set()
        return False, None

    with (
        patch.object(control_server.config, "APP_DIR", tmp_path),
        patch.object(controller, "begin", side_effect=lambda kind, task: {"result": task()}),
        patch("selfapply.cli._managed_connect_browser", side_effect=browser),
        patch("selfapply.apply.launcher.check_google_session", return_value=(False, None)),
        patch("selfapply.apply.launcher.wait_for_google_signin", side_effect=wait),
    ):
        result = controller.connect_google()

    assert result["result"] == "Google sign-in stopped."


def test_google_connection_can_verify_existing_chrome_without_managed_browser(tmp_path):
    controller = control_server.Controller()
    with (
        patch.object(control_server.config, "APP_DIR", tmp_path),
        patch.object(controller, "begin", side_effect=lambda kind, task: {"result": task()}),
        patch("selfapply.cli._managed_connect_browser") as managed,
        patch(
            "selfapply.apply.launcher.check_google_with_browser_agent",
            return_value=(True, "person@example.com"),
        ),
    ):
        result = controller.connect_google("existing")

    assert "existing Chrome" in result["result"]
    managed.assert_not_called()
    saved = json.loads((tmp_path / "google_session.json").read_text())
    assert saved["browser_mode"] == "existing"


def test_connections_ui_defaults_to_existing_chrome_and_sends_mode():
    html = control_server.HTML
    assert 'id="connectionBrowser"' in html
    assert "selfapply-connection-browser')||'existing'" in html
    assert "'/api/connections/google',{browser:val('connectionBrowser')}" in html
    assert "name:n,browser:val('connectionBrowser')" in html


def test_ui_has_light_default_dark_theme_and_accessible_responsive_shell():
    html = control_server.HTML
    assert "localStorage.getItem('selfapply-theme')||'light'" in html
    assert 'class="sidebar-theme"' in html
    assert "toggleTheme()" in html
    assert 'html[data-theme="dark"]' in html
    assert 'aria-label="Open navigation"' in html
    assert "prefers-reduced-motion" in html
    assert ":focus-visible" in html


def test_ui_navigation_uses_deep_links_without_changing_api_actions():
    html = control_server.HTML
    for tab in (
        "overview", "ats", "review-apply", "applications",
        "connected-accounts", "find-jobs", "account",
    ):
        assert f'data-tab="{tab}"' in html
    # Secondary settings pages remain directly reachable without cluttering
    # the primary navigation.
    for tab in ("document-vault", "api"):
        assert f'id="{tab}"' in html
    assert "history.pushState({tab:id},'',`#${id}`)" in html
    assert "window.addEventListener('popstate'" in html


def test_self_apply_controls_are_state_separated_and_report_start_progress():
    html = control_server.HTML
    assert 'id="stopSelfApply" class="danger" style="display:none"' in html
    assert "startSelfApply.textContent='Starting…'" in html
    assert "startSelfApply.style.display=s.running?'none':'inline-flex'" in html
    assert "stopSelfApply.style.display=s.running?'inline-flex':'none'" in html
    assert "Check the authorization box before starting Self Apply." in html
    assert "let authorization=el('confirm')" in html
    assert "if(!confirm.checked)" not in html
    assert "onclick=\"stopWorker()\"" in html


def test_connect_all_skips_already_connected_portals(tmp_path):
    controller = control_server.Controller()
    (tmp_path / "google_session.json").write_text(json.dumps({
        "verified": True, "email": "person@example.com",
    }))
    rows = [
        {"name": "Dice", "status": "connected"},
        {"name": "LinkedIn", "status": "failed:login_required"},
    ]

    @contextmanager
    def browser():
        yield 9222

    with (
        patch.object(control_server.config, "APP_DIR", tmp_path),
        patch.object(controller, "connections", return_value={"portals": rows}),
        patch.object(control_server, "portal_targets", return_value={
            "Dice": "https://www.dice.com/login",
            "LinkedIn": "https://www.linkedin.com/login",
        }),
        patch.object(controller, "begin", side_effect=lambda kind, task: {"result": task()}),
        patch("selfapply.cli._managed_connect_browser", side_effect=browser),
        patch("selfapply.apply.launcher.connect_portal_with_google", return_value="connected") as connect,
    ):
        result = controller.connect_all()
    assert result["result"] == "Connect all finished: 1/1 connected."
    assert connect.call_count == 1
    assert "linkedin.com" in connect.call_args.args[0]


def test_cancel_and_unlink_are_explicit_local_operations(tmp_path):
    controller = control_server.Controller()
    controller.action = control_server.ActionState(True, "Connect all with Google", "")
    assert controller.cancel_connection_action()["ok"] is True
    assert controller.cancel_event.is_set()

    controller.action = control_server.ActionState()
    (tmp_path / "google_session.json").write_text("{}")
    with patch.object(control_server.config, "APP_DIR", tmp_path):
        result = controller.unlink("Google")
    assert result["ok"] is True
    assert not (tmp_path / "google_session.json").exists()


def test_settings_snapshot_never_returns_saved_secrets(tmp_path):
    profile = tmp_path / "profile.json"
    env = tmp_path / ".env"
    searches = tmp_path / "searches.yaml"
    profile.write_text(json.dumps({
        "personal": {"full_name": "Person", "password": "secret-password"},
    }))
    env.write_text("OPENAI_API_KEY=sk-secret\nLLM_MODEL=gpt-4o-mini\n")
    searches.write_text("defaults: {}\n")
    with (
        patch.object(control_server.config, "PROFILE_PATH", profile),
        patch.object(control_server.config, "ENV_PATH", env),
        patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches),
        patch.object(control_server.config, "RESUME_PATH", tmp_path / "resume.txt"),
        patch.object(control_server.config, "RESUME_PDF_PATH", tmp_path / "resume.pdf"),
        patch.object(control_server.config, "RESUME_DOCX_PATH", tmp_path / "resume.docx"),
        patch.object(control_server, "browser_agent_status", return_value={}),
    ):
        snapshot = control_server.settings_snapshot()
    serialized = json.dumps(snapshot)
    assert snapshot["has_job_site_password"] is True
    assert snapshot["ai"]["has_openai_key"] is True
    assert "secret-password" not in serialized
    assert "sk-secret" not in serialized


def test_profile_settings_save_to_wizard_file_and_preserve_advanced_facts(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "personal": {"password": "keep-me"},
        "resume_facts": {"experience_entries": [{"company": "Example"}]},
    }))
    payload = {
        "personal": {"full_name": "Person Name", "email": "person@example.com",
                     "postal_code": "97205"},
        "work_authorization": {"legally_authorized_to_work": True,
                               "require_sponsorship": False},
        "compensation": {"salary_expectation": "120000",
                         "salary_range_min": "100000", "salary_range_max": "150000"},
        "experience": {}, "availability": {}, "eeo_voluntary": {},
        "skills_boundary": {"tools": "Git, Docker"},
        "resume_facts": {"preserved_companies": "Example, Other"},
    }
    with patch.object(control_server.config, "PROFILE_PATH", profile):
        control_server.save_profile_settings(payload)
    saved = json.loads(profile.read_text())
    assert saved["personal"]["password"] == "keep-me"
    assert saved["resume_facts"]["experience_entries"] == [{"company": "Example"}]
    assert saved["resume_facts"]["preserved_companies"] == ["Example", "Other"]
    assert saved["skills_boundary"]["tools"] == ["Git", "Docker"]


def test_ai_settings_are_write_only_and_preserve_unedited_optional_secret(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CAPSOLVER_API_KEY=existing-optional\n")
    with patch.object(control_server.config, "ENV_PATH", env):
        control_server.save_ai_settings({
            "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-new",
        })
    text = env.read_text()
    assert "OPENAI_API_KEY=sk-new" in text
    assert "CAPSOLVER_API_KEY=existing-optional" in text


@pytest.mark.parametrize(("key", "provider"), [
    ("sk-proj-example", "openai"),
    ("sk-ant-example", "anthropic"),
    ("AIza-example", "gemini"),
    ("custom-key", None),
    ("", None),
])
def test_ai_provider_detection_is_conservative(key, provider):
    assert control_server.detect_ai_provider(key) == provider


def test_auto_ai_provider_saves_detected_provider_and_default_model(tmp_path):
    env = tmp_path / ".env"
    with patch.object(control_server.config, "ENV_PATH", env):
        result = control_server.save_ai_settings({
            "provider": "auto", "model": "", "api_key": "sk-ant-example",
        })
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-haiku-4-5"
    assert "ANTHROPIC_API_KEY=sk-ant-example" in env.read_text()
    assert "LLM_MODEL=claude-haiku-4-5" in env.read_text()


def test_auto_ai_provider_refuses_unknown_prefix(tmp_path):
    with (
        patch.object(control_server.config, "ENV_PATH", tmp_path / ".env"),
        pytest.raises(ValueError, match="does not identify"),
    ):
        control_server.save_ai_settings({
            "provider": "auto", "model": "", "api_key": "unknown",
        })


def test_reset_moves_local_data_to_private_backup_and_reinitializes(tmp_path):
    paths = {
        "APP_DIR": tmp_path,
        "DB_PATH": tmp_path / "selfapply.db",
        "PROFILE_PATH": tmp_path / "profile.json",
        "RESUME_PATH": tmp_path / "resume.txt",
        "RESUME_PDF_PATH": tmp_path / "resume.pdf",
        "RESUME_DOCX_PATH": tmp_path / "resume.docx",
        "SEARCH_CONFIG_PATH": tmp_path / "searches.yaml",
        "ENV_PATH": tmp_path / ".env",
        "TAILORED_DIR": tmp_path / "tailored_resumes",
        "APPLIED_RESUME_DIR": tmp_path / "applied_resumes",
        "COVER_LETTER_DIR": tmp_path / "cover_letters",
        "LOG_DIR": tmp_path / "logs",
        "CHROME_WORKER_DIR": tmp_path / "chrome-workers",
        "APPLY_WORKER_DIR": tmp_path / "apply-workers",
    }
    (tmp_path / "profile.json").write_text('{"personal":{"full_name":"Sample User"}}')
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-example\n")
    (tmp_path / "control-token").write_text("keep")
    with (
        patch.multiple(control_server.config, **paths),
        patch.object(database, "DB_PATH", paths["DB_PATH"]),
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "database_status", return_value={
            "counts": {"applied": 12, "failed": 3},
        }),
    ):
        result = control_server.reset_local_data({"confirmation": "RESET"})
    backup = Path(result["backup"])
    manifest = json.loads((backup / "reset-manifest.json").read_text())
    assert (backup / "profile.json").exists()
    assert (backup / ".env").exists()
    assert manifest["counts"]["applied"] == 12
    assert (tmp_path / "control-token").read_text() == "keep"
    assert paths["DB_PATH"].exists()
    assert not paths["PROFILE_PATH"].exists()


def test_control_center_has_first_run_onboarding_and_reset_controls():
    assert 'id="onboarding"' in control_server.HTML
    assert 'id="onboardingProgress"' in control_server.HTML
    assert 'data-tab="settings"' in control_server.HTML
    assert "/api/settings/reset" in control_server.HTML
    assert 'class="app-loader"' in control_server.HTML
    assert "function advanceOnboarding()" in control_server.HTML
    assert "document.body.classList.remove('booting')" in control_server.HTML
    assert control_server.HTML.count('class="nav-icon"><svg') == 8


def test_ats_preference_patch_preserves_work_authorization(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "personal": {
            "full_name": "Person Name", "email": "person@example.com",
            "postal_code": "97205",
        },
        "work_authorization": {
            "legally_authorized_to_work": True,
            "require_sponsorship": True,
        },
    }))
    with patch.object(control_server.config, "PROFILE_PATH", profile):
        control_server.save_profile_settings({
            "skills_boundary": {"target_aggressiveness": 0.9},
        })
    saved = json.loads(profile.read_text())
    assert saved["work_authorization"] == {
        "legally_authorized_to_work": True,
        "require_sponsorship": True,
    }
    assert saved["skills_boundary"]["target_aggressiveness"] == 0.9


def test_search_settings_and_resume_upload_use_canonical_files(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text("location:\n  accept_patterns: [Bay Area]\n")
    resume = tmp_path / "resume.txt"
    with (
        patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches),
        patch.object(control_server.config, "RESUME_PATH", resume),
    ):
        control_server.save_search_settings({
            "location": "Oregon", "primary_location": "Portland, OR",
            "distance": 100, "hours_old": 168, "results_per_site": 100,
            "remote_only": False, "sites": ["indeed", "invalid"],
            "target_titles": "Software Engineer\nPlatform Engineer",
        })
        control_server.save_resume({
            "name": "resume.txt",
            "data": control_server.base64.b64encode(b"Resume text").decode(),
        })
    saved = control_server.yaml.safe_load(searches.read_text())
    assert saved["sites"] == ["indeed"]
    assert saved["location"]["accept_patterns"] == ["Bay Area"]
    assert resume.read_text() == "Resume text"


def test_search_settings_accept_arbitrary_candidate_locations(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text("location: {}\n")
    with patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches):
        control_server.save_search_settings({
            "country": "Germany", "state": "Brandenburg", "city": "Potsdam",
            "primary_location": "Potsdam, Brandenburg, Germany",
            "accept_patterns": "Berlin\nPotsdam\nBrandenburg",
            "distance": 50, "hours_old": 72, "results_per_site": 25,
            "remote_only": False, "sites": ["linkedin"],
            "target_titles": "Product Designer",
        })
    saved = control_server.yaml.safe_load(searches.read_text())
    assert saved["location"]["accept_patterns"] == [
        "Berlin", "Potsdam", "Brandenburg",
    ]
    assert saved["defaults"]["location"] == "Potsdam, Brandenburg, Germany"
    assert saved["location"]["country"] == "Germany"
    assert saved["location"]["state"] == "Brandenburg"
    assert saved["location"]["city"] == "Potsdam"


def test_daily_activity_is_seven_applied_only_days():
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = []
    with patch.object(control_server.database, "get_connection", return_value=connection):
        result = control_server.daily_activity_stats(days=30)
    assert len(result["days"]) == 7
    assert all(set(day) == {"date", "label", "applied"} for day in result["days"])
    assert all(day["applied"] == 0 for day in result["days"])
    assert result["recent_applied"] == []


def test_overview_shows_recent_applications_and_defaults_to_four_workers():
    assert "Applications sent" in control_server.HTML
    assert 'id="recentApplied"' in control_server.HTML
    assert '<option selected>4</option>' in control_server.HTML
    assert "renderRecentApplied(daily.recent_applied||[])" in control_server.HTML


def test_overview_hides_job_board_when_actual_employer_is_unknown():
    assert control_server._overview_employer("Acme", "linkedin") == "Acme"
    assert control_server._overview_employer(None, "linkedin") == ""
    assert control_server._overview_employer(None, "Thomson Reuters") == "Thomson Reuters"


def test_ui_has_separate_settings_tabs_and_complete_save_actions():
    for label in ("Profile", "Job preferences", "Resume files", "AI & browser settings"):
        assert label in control_server.HTML
    for action in ("saveProfile()", "saveJobs()", "saveAI()", "uploadResume()"):
        assert action in control_server.HTML
    for field in ("s_country", "s_state", "s_city"):
        assert f'id="{field}"' in control_server.HTML


def test_docx_resume_is_preserved_and_converted_to_plain_text(tmp_path):
    buffer = BytesIO()
    xml = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Software Engineer</w:t></w:r></w:p><w:p><w:r><w:t>Python and TypeScript</w:t></w:r></w:p></w:body></w:document>'''
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    with (
        patch.object(control_server.config, "RESUME_DOCX_PATH", tmp_path / "resume.docx"),
        patch.object(control_server.config, "RESUME_PATH", tmp_path / "resume.txt"),
    ):
        result = control_server.save_resume({
            "name": "resume.docx",
            "data": control_server.base64.b64encode(buffer.getvalue()).decode(),
        })
    assert result["ok"] is True
    assert (tmp_path / "resume.docx").exists()
    assert (tmp_path / "resume.txt").read_text() == "Software Engineer\nPython and TypeScript\n"


def test_settings_exposes_all_direct_boards_selected_by_default(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text("defaults: {}\n")
    profile = tmp_path / "profile.json"; profile.write_text("{}")
    env = tmp_path / ".env"; env.write_text("")
    sites = {"sites": [{"name": "Dice"}, {"name": "RemoteOK"}]}
    with (
        patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches),
        patch.object(control_server.config, "PROFILE_PATH", profile),
        patch.object(control_server.config, "ENV_PATH", env),
        patch.object(control_server.config, "RESUME_PATH", tmp_path / "resume.txt"),
        patch.object(control_server.config, "RESUME_PDF_PATH", tmp_path / "resume.pdf"),
        patch.object(control_server.config, "RESUME_DOCX_PATH", tmp_path / "resume.docx"),
        patch.object(control_server.config, "load_sites_config", return_value=sites),
        patch.object(control_server, "browser_agent_status", return_value={}),
    ):
        discovery = control_server.settings_snapshot()["discovery"]
    assert discovery["selected_direct_sites"] == ["Dice", "RemoteOK"]
    assert discovery["jobspy"] is True
    assert discovery["workday"] is True


def test_provider_specific_ui_includes_claude_custom_and_chrome_guidance():
    assert "Claude / Anthropic" in control_server.HTML
    assert "claude-sonnet-4-5" in control_server.HTML
    assert "Custom model" in control_server.HTML
    assert "Install Chrome extension" in control_server.HTML
    assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" in control_server.HTML


def test_setup_readiness_marks_required_sections_and_can_complete():
    snapshot = {
        "profile": {}, "searches": {}, "discovery": {}, "ai": {"provider": "none"},
        "resume": {"text": False}, "browser_agent": {},
    }
    result = control_server.setup_readiness(snapshot)
    assert result["complete"] is False
    assert {item["tab"] for item in result["missing"]} == {"account", "find-jobs", "document-vault", "api"}
    assert "Full name" in {item["label"] for item in result["missing"]}

    snapshot = {
        "profile": {
            "personal": {key: "value" for key in (
                "full_name", "email", "phone", "address", "city",
                "province_state", "country", "postal_code",
            )},
            "work_authorization": {"legally_authorized_to_work": True,
                                   "require_sponsorship": False},
            "employment_disclosures": {"restrictive_covenant": False},
            "experience": {"target_role": "Software Engineer"},
        },
        "searches": {"defaults": {"location": "Portland"},
                     "queries": [{"query": "Software Engineer"}], "sites": ["indeed"]},
        "discovery": {"jobspy": True, "workday": False, "direct": False,
                      "selected_direct_sites": []},
        "ai": {"provider": "openai", "model": "gpt-4o-mini", "has_openai_key": True},
        "resume": {"text": True},
        "browser_agent": {"codex": {"authenticated": True}},
    }
    assert control_server.setup_readiness(snapshot, agent="codex", browser="managed") == {
        "complete": True, "missing": [],
    }


def test_setup_readiness_blocks_contradictory_work_authorization_answers():
    snapshot = {
        "profile": {
            "personal": {key: "value" for key in (
                "full_name", "email", "phone", "address", "city",
                "province_state", "country", "postal_code",
            )},
            "work_authorization": {
                "legally_authorized_to_work": False,
                "require_sponsorship": False,
                "work_permit_type": "Permanent Resident",
            },
            "experience": {"target_role": "Software Engineer"},
        },
        "searches": {"defaults": {"location": "Portland"},
                     "queries": [{"query": "Software Engineer"}], "sites": ["indeed"]},
        "discovery": {"jobspy": True, "workday": False, "direct": False,
                      "selected_direct_sites": []},
        "ai": {"provider": "openai", "model": "gpt-4o-mini", "has_openai_key": True},
        "resume": {"text": True},
        "browser_agent": {"codex": {"authenticated": True}},
    }

    readiness = control_server.setup_readiness(snapshot, agent="codex", browser="managed")
    assert readiness["complete"] is False
    assert "Review contradictory work-authorization answers" in {
        item["label"] for item in readiness["missing"]
    }


def test_secret_deletion_never_returns_saved_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-private\nLLM_MODEL=gpt-4o-mini\n")
    with patch.object(control_server.config, "ENV_PATH", env):
        result = control_server.delete_saved_secret("provider")
    assert result["ok"] is True
    assert "sk-private" not in json.dumps(result)
    assert "OPENAI_API_KEY" not in env.read_text()


def test_browser_agent_test_and_unlink_controls_are_real():
    controller = control_server.Controller()
    with patch.object(control_server, "browser_agent_status", return_value={
        "codex": {"installed": True, "authenticated": True},
    }):
        assert controller.test_browser_agent("codex")["ok"] is True
    completed = control_server.subprocess.CompletedProcess([], 0, "", "")
    with (
        patch.object(control_server.shutil, "which", return_value="/usr/bin/codex"),
        patch.object(control_server.subprocess, "run", return_value=completed) as run,
    ):
        assert controller.unlink_browser_agent("codex")["ok"] is True
    assert run.call_args.args[0] == ["codex", "logout"]


def test_browser_login_completion_refreshes_account_cards_and_verifies_auth():
    assert "agentAction&&agentAction!==lastAgentAction" in control_server.HTML
    assert "await loadSettings()" in control_server.HTML
    source = open(control_server.__file__, encoding="utf-8").read()
    assert 'browser_agent_status().get(name, {}).get("authenticated")' in source


def test_ui_has_cascading_location_dropdowns_examples_and_readiness_gate():
    for field in ('id="p_country"', 'id="p_province_state"', 'id="p_city"'):
        assert field in control_server.HTML
    for text in (
        "Example University, City College", "Python, Java, Rust, SQL",
        "Direct and remote boards", "Delete saved API key", "Test",
        "Setup incomplete", "Start Self Apply",
    ):
        assert text in control_server.HTML
    assert "startSelfApply.disabled=!complete" in control_server.HTML
    assert "${ok?`<button class=secondary" in control_server.HTML
    assert (
        "names={account:'Profile','find-jobs':'Job preferences',"
        "'document-vault':'Resume tools',api:'Connections'}"
    ) in control_server.HTML


def test_application_history_explains_failures_and_protects_applied_jobs(tmp_path):
    db_path = tmp_path / "history.db"
    conn = database.init_db(db_path)
    conn.execute(
        """INSERT INTO jobs
        (url,title,site,location,application_url,fit_score,applied_at,apply_status,
         apply_attempts,last_attempted_at,apply_duration_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("https://example.com/applied", "Applied Engineer", "Example", "Remote",
         "https://example.com/apply/1", 9, "2026-09-01T10:00:00+00:00",
         "applied", 1, "2026-09-01T09:58:00+00:00", 120000),
    )
    conn.execute(
        """INSERT INTO jobs
        (url,title,site,location,application_url,fit_score,apply_status,apply_error,
         apply_attempts,last_attempted_at,apply_duration_ms,tailored_resume_path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("https://example.com/failed", "Failed Engineer", "Example", "Remote",
         "https://example.com/apply/2", 8, "failed", "captcha", 1,
         "2026-09-02T10:00:00+00:00", 45000, "/tmp/resume.pdf"),
    )
    conn.commit()
    with patch.object(control_server.database, "get_connection", return_value=conn):
        history = control_server.application_history(
            "2026-09-01", "2026-09-02", "all",
        )
        assert history["counts"] == {
            "applied": 1, "failed": 1, "ineligible": 0,
            "queued": 0, "running": 0,
            "dismissed": 0, "retryable": 1,
        }
        failed = next(row for row in history["rows"] if row["status"] == "failed")
        assert failed["reason_category"] == "Security check"
        assert failed["retryable"] is True
        assert control_server.queue_failed_retry(failed["url"])["ok"] is True
        with pytest.raises(ValueError, match="already applied"):
            control_server.queue_failed_retry("https://example.com/applied")
    queued = conn.execute(
        "SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url = ?",
        ("https://example.com/failed",),
    ).fetchone()
    assert tuple(queued) == (None, None, 0)
    database.close_connection(db_path)


def test_application_history_links_exact_applied_resume(tmp_path):
    db_path = tmp_path / "applied-resume.db"
    archive_dir = tmp_path / "applied"
    archive_dir.mkdir()
    resume = archive_dir / "submitted.pdf"
    resume.write_bytes(b"pdf")
    conn = database.init_db(db_path)
    conn.execute(
        """INSERT INTO jobs
           (url,title,applied_at,apply_status,applied_resume_path)
           VALUES (?,?,?,?,?)""",
        ("https://example.test/job", "Engineer", "2026-09-03", "applied", str(resume)),
    )
    conn.commit()
    with (
        patch.object(control_server.database, "get_connection", return_value=conn),
        patch.object(control_server.config, "APPLIED_RESUME_DIR", archive_dir),
    ):
        row = control_server.application_history(status="applied")["rows"][0]
    assert row["resume_url"].endswith("kind=applied&name=submitted.pdf")
    database.close_connection(db_path)


def test_application_history_separates_ineligible_from_failed(tmp_path):
    db_path = tmp_path / "ineligible-history.db"
    conn = database.init_db(db_path)
    for index, reason in enumerate((
        "not_eligible_location", "not_eligible_work_auth", "not_eligible_salary",
    )):
        conn.execute(
            """INSERT INTO jobs
            (url,title,application_url,apply_status,apply_error,last_attempted_at)
            VALUES (?,?,?,?,?,?)""",
            (f"https://example.com/{index}", f"Role {index}",
             f"https://example.com/apply/{index}", "failed", reason, "2026-09-02"),
        )
    conn.execute(
        """INSERT INTO jobs
        (url,title,application_url,apply_status,apply_error,last_attempted_at)
        VALUES (?,?,?,?,?,?)""",
        ("https://example.com/failed", "Failed role", "https://example.com/apply/f",
         "failed", "time_limit", "2026-09-02"),
    )
    conn.commit()
    with patch.object(control_server.database, "get_connection", return_value=conn):
        history = control_server.application_history(status="all")
        ineligible = control_server.application_history(status="ineligible")
    assert history["counts"]["ineligible"] == 3
    assert history["counts"]["failed"] == 1
    assert history["all_total"] == 1
    assert all(row["status"] != "ineligible" for row in history["rows"])
    assert len(ineligible["rows"]) == 3
    assert all(row["retryable"] is False for row in ineligible["rows"])
    assert all(row["job_url"] for row in ineligible["rows"])
    database.close_connection(db_path)


def test_application_history_ui_is_simple_and_has_direct_actions():
    for text in (
        "Applications", "What needs attention?", "Apply myself", "Apply now",
        "Technical detail", "Highest fit score", "Running now",
        "Needs attention", "Waiting to apply", "Show again",
        "Not eligible",
        "View résumé", "Why this status?", "Filters", "Retry all failed applications",
        "Add next",
    ):
        assert text in control_server.HTML
    assert "/api/applications?" in control_server.HTML
    assert "/api/applications/retry" in control_server.HTML
    assert "/api/applications/retry-now" in control_server.HTML
    assert "/api/applications/retry-all" in control_server.HTML
    assert "/api/applications/dismiss" in control_server.HTML
    assert "/api/applications/restore" in control_server.HTML
    assert "${historyActions(x)}" in control_server.HTML
    assert "signature===lastHistorySignature" in control_server.HTML
    assert "if(!applicationRows.children.length)" in control_server.HTML
    assert "Add selected to queue" not in control_server.HTML
    assert "Select retryable" not in control_server.HTML
    assert "retry-check" not in control_server.HTML
    assert "retryAllPanel.hidden=active!=='failed'" in control_server.HTML
    assert "workers:+workers.value" in control_server.HTML
    assert "brand-mark" not in control_server.HTML
    assert "profile-avatar" not in control_server.HTML
    assert "window.confirm" in control_server.HTML
    assert "filterFailureReason" in control_server.HTML
    assert "setHistoryStatus('applied')" in control_server.HTML


def test_ats_matcher_ui_and_plain_text_analysis(tmp_path):
    for text in (
        "Resume tools", "Check match",
        "Create tailored résumé", "Safe to add from master resume",
        "Missing and unsupported", "Employer job link", "Download PDF",
        "Starting whole-resume tailoring", "Resume was not created",
        "Automatically add relevant JD skills", "atsAggressiveness",
        "Skill-tailoring strength", "p_skill_expansion_enabled",
            "New JD technologies added", "See match details",
    ):
        assert text in control_server.HTML
    assert "Confirmed additional skills" not in control_server.HTML
    assert "p_transferable_skills" not in control_server.HTML
    assert "/api/ats/jobs" in control_server.HTML
    assert "/api/ats/analyze" in control_server.HTML
    assert "/api/ats/tailor" in control_server.HTML
    assert "/api/ats/task" in control_server.HTML

    db_path = tmp_path / "ats.db"
    conn = database.init_db(db_path)
    master = tmp_path / "resume.txt"
    tailored_dir = tmp_path / "tailored"
    tailored_dir.mkdir()
    tailored = tailored_dir / "job.txt"
    master.write_text(
        "SUMMARY\nPython SQL AWS engineer\nEXPERIENCE\nBuilt APIs\n"
        "EDUCATION\nBS\nTECHNICAL SKILLS\nPython SQL AWS",
    )
    tailored.write_text(
        "SUMMARY\nPython engineer\nEXPERIENCE\nBuilt APIs\n"
        "EDUCATION\nBS\nTECHNICAL SKILLS\nPython",
    )
    job_description = (
        "Python, SQL, and AWS are required for this backend engineering role. "
        "Build and operate reliable APIs and distributed production services."
    )
    conn.execute(
        """INSERT INTO jobs
        (url,title,site,full_description,fit_score,tailored_resume_path)
        VALUES (?,?,?,?,?,?)""",
        ("https://example.com/job", "Backend Engineer", "Example",
         job_description, 9, str(tailored)),
    )
    conn.commit()
    with (
        patch.object(control_server.database, "get_connection", return_value=conn),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
    ):
        jobs = control_server.ats_jobs()
        result = control_server.analyze_job_ats({
            "url": "https://example.com/job", "resume_source": "tailored",
        })
    assert jobs["jobs"][0]["has_tailored"] is True
    assert result["ok"] is True
    assert "sql" in result["result"]["injectable_keywords"]
    assert "aws" in result["result"]["injectable_keywords"]
    database.close_connection(db_path)


def test_ats_matcher_imports_public_json_ld_job_link():
    page = """<html><head><script type="application/ld+json">{
      "@context":"https://schema.org","@type":"JobPosting",
      "title":"Platform Engineer","description":"<p>Build Python and AWS services.</p><p>Operate reliable distributed APIs and improve production observability with the engineering team.</p>"
    }</script></head></html>"""

    class Response:
        content = page.encode()
        text = page
        url = "https://jobs.example.com/platform"

        def raise_for_status(self):
            return None

    with patch.object(httpx, "get", return_value=Response()):
        job = control_server._fetch_job_description("https://jobs.example.com/platform")
    assert job["title"] == "Platform Engineer"
    assert "Python and AWS" in job["full_description"]


def test_ats_matcher_tailors_pasted_jd_and_returns_downloads(tmp_path):
    master = tmp_path / "resume.txt"
    tailored_dir = tmp_path / "tailored"
    master.write_text("SUMMARY\nPython engineer\nEXPERIENCE\nBuilt APIs\nEDUCATION\nBS\nSKILLS\nPython")
    jd = "Python and AWS are required. Build reliable distributed services and production APIs. " * 3
    tailored_text = "SUMMARY\nPython engineer\nEXPERIENCE\nBuilt APIs\nEDUCATION\nBS\nSKILLS\nPython"
    ats = {"overall_score": 75, "sub_scores": {}, "matched_keywords": []}
    report = {"status": "approved", "master_ats_match": ats, "ats_match": ats}

    def fake_pdf(text_path):
        path = Path(text_path).with_suffix(".pdf")
        path.write_bytes(b"%PDF-test")
        return path

    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
        patch.object(control_server.config, "load_profile", return_value={}),
        patch("selfapply.scoring.tailor.tailor_resume", return_value=(tailored_text, report)) as tailor,
        patch("selfapply.scoring.pdf.convert_to_pdf", side_effect=fake_pdf),
    ):
        result = control_server.tailor_and_score_job({
            "job_description": jd, "title": "Platform Engineer", "company": "Example",
        })
    assert result["ok"] is True
    assert result["master_result"]["overall_score"] == 75
    assert result["downloads"]["pdf"].endswith(".pdf")
    assert Path(result["text_path"]).is_file()
    assert tailor.call_args.kwargs["validation_mode"] == "creative"
    assert tailor.call_args.kwargs["allow_unverified_claims"] is True
    assert tailor.call_args.kwargs["require_judge"] is False


def test_ats_tailoring_task_reports_progress_and_result():
    def completed(payload, on_progress):
        on_progress(72, "Checking factual accuracy")
        return {"ok": True, "result": {"overall_score": 88}}

    with patch.object(control_server, "tailor_and_score_job", side_effect=completed):
        started = control_server.start_ats_tailor_task({"url": "https://example.com/job"})
        for _ in range(100):
            task = control_server.ats_tailor_task(started["task_id"])
            if task["status"] != "running":
                break
            time.sleep(0.005)
    assert task["status"] == "completed"
    assert task["percent"] == 100
    assert task["result"]["result"]["overall_score"] == 88


def test_ats_accepts_unverified_employer_skill_without_confirmation(tmp_path):
    master = tmp_path / "resume.txt"
    master.write_text("SUMMARY\nTypeScript engineer", encoding="utf-8")
    tailored_dir = tmp_path / "tailored"
    text = "SUMMARY\nCloud engineer\nSKILLS\nRust\nEXPERIENCE\nBuilt Rust services"
    ats = {"overall_score": 90, "sub_scores": {}, "matched_keywords": []}
    report = {"status": "approved", "master_ats_match": ats, "ats_match": ats}

    def fake_pdf(text_path):
        path = Path(text_path).with_suffix(".pdf")
        path.write_bytes(b"%PDF-test")
        return path

    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
        patch.object(control_server.config, "load_profile", return_value={}),
        patch("selfapply.scoring.tailor.tailor_resume", return_value=(text, report)),
        patch("selfapply.scoring.pdf.convert_to_pdf", side_effect=fake_pdf),
    ):
        result = control_server.tailor_and_score_job({
            "job_description": "Rust is required. Build Rust services. " * 4,
            "confirmed_experience_skills": ["rust"],
        })
    assert result["ok"] is True
    assert Path(result["text_path"]).read_text() == text


def test_ats_returns_text_download_when_pdf_verification_fails(tmp_path):
    master = tmp_path / "resume.txt"
    master.write_text("SUMMARY\nTypeScript engineer", encoding="utf-8")
    tailored_dir = tmp_path / "tailored"
    text = "SUMMARY\nCloud engineer\nSKILLS\nRust\nEXPERIENCE\nBuilt Rust services"
    ats = {"overall_score": 90, "sub_scores": {}, "matched_keywords": []}
    report = {"status": "approved", "master_ats_match": ats, "ats_match": ats}

    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
        patch.object(control_server.config, "load_profile", return_value={}),
        patch("selfapply.scoring.tailor.tailor_resume", return_value=(text, report)),
        patch(
            "selfapply.scoring.pdf.convert_to_pdf",
            side_effect=RuntimeError("Generated PDF contains a blank page"),
        ),
    ):
        result = control_server.tailor_and_score_job({
            "job_description": "Rust is required. Build Rust services. " * 4,
        })

    assert result["ok"] is True
    assert result["downloads"]["txt"]
    assert result["downloads"]["pdf"] is None
    assert "PDF creation failed" in result["message"]
    assert "blank page" in result["report"]["pdf_warning"]


def test_batch_tailoring_uses_nonblocking_creative_mode_for_automatic_apply(tmp_path):
    from selfapply.scoring import tailor as tailor_module

    master = tmp_path / "resume.txt"
    master.write_text("MASTER RESUME", encoding="utf-8")
    tailored_dir = tmp_path / "tailored"
    job = {
        "url": "https://example.com/job/creative",
        "title": "Quantum Platform Engineer",
        "site": "Example",
        "location": "Remote",
        "full_description": "Build a new-domain quantum platform.",
    }
    report = {
        "status": "approved",
        "attempts": 1,
        "unverified_claims_allowed": True,
    }
    conn = Mock()

    with (
        patch.object(tailor_module, "RESUME_PATH", master),
        patch.object(tailor_module, "TAILORED_DIR", tailored_dir),
        patch.object(
            tailor_module,
            "load_profile",
            return_value={"skills_boundary": {"allow_unverified_claims": True}},
        ),
        patch.object(tailor_module, "get_connection", return_value=conn),
        patch.object(tailor_module, "get_jobs_by_stage", return_value=[job]),
        patch.object(
            tailor_module,
            "tailor_resume",
            return_value=("CREATIVE RESUME", report),
        ) as generate,
        patch("selfapply.scoring.pdf.convert_to_pdf", return_value=tmp_path / "resume.pdf"),
    ):
        result = tailor_module.run_tailoring(limit=1)

    assert result["approved"] == 1
    assert generate.call_args.kwargs["validation_mode"] == "creative"
    assert generate.call_args.kwargs["allow_unverified_claims"] is True
    assert generate.call_args.kwargs["require_judge"] is False


def test_ats_activates_provider_saved_by_ui():
    saved = {"OPENAI_API_KEY": "saved-ui-key", "LLM_MODEL": "gpt-4o-mini"}
    with (
        patch.dict("os.environ", {}, clear=True),
        patch.object(control_server, "_env_values", return_value=saved),
        patch("selfapply.llm.reset_client") as reset,
    ):
        control_server._activate_saved_ai_settings()
        assert control_server.os.environ["OPENAI_API_KEY"] == "saved-ui-key"
        assert control_server.os.environ["LLM_MODEL"] == "gpt-4o-mini"
        reset.assert_called_once()


def test_retry_now_targets_one_exact_job_and_uses_one_shot_worker(tmp_path):
    db_path = tmp_path / "retry-now.db"
    conn = database.init_db(db_path)
    conn.execute(
        """INSERT INTO jobs
        (url,title,apply_status,apply_error,apply_attempts,last_attempted_at,
         tailored_resume_path,application_url)
        VALUES (?,?,?,?,?,?,?,?)""",
        ("https://example.com/job/7", "Retry Engineer", "failed", "time_limit",
         2, "2026-09-02", "/tmp/resume.pdf", "https://example.com/apply/7"),
    )
    conn.commit()
    payload = {
        "url": "https://example.com/job/7", "confirmed": True,
        "max_spend": 5, "target_applied": 10, "workers": 1,
        "queue_threshold": 10, "max_actions": 60, "max_minutes": 8,
        "agent": "codex", "browser": "managed",
    }
    readiness = {"complete": True, "missing": []}
    with (
        patch.object(control_server.database, "get_connection", return_value=conn),
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "settings_snapshot", return_value={}),
        patch.object(control_server, "setup_readiness", return_value=readiness),
        patch.object(control_server, "_install_worker_service") as install,
    ):
        result = control_server.start_application_retry(payload)
    assert result["ok"] is True
    arguments = install.call_args.args[0]
    assert arguments[0] == str(
        Path(control_server.__file__).resolve().parents[2] / ".venv/bin/selfapply"
    )
    assert arguments[1:5] == ["apply", "--limit", "1", "--url"]
    assert arguments[5] == "https://example.com/job/7"
    assert install.call_args.kwargs == {"continuous": False}
    queued = conn.execute(
        "SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url=?",
        ("https://example.com/job/7",),
    ).fetchone()
    assert tuple(queued) == (None, None, 0)
    database.close_connection(db_path)


def test_retry_now_refuses_to_compete_with_running_worker():
    payload = {
        "url": "https://example.com/job/7", "confirmed": True,
        "max_spend": 5, "target_applied": 10, "workers": 1,
        "queue_threshold": 10, "max_actions": 60, "max_minutes": 8,
        "agent": "codex", "browser": "managed",
    }
    with patch.object(control_server, "service_running", return_value=True):
        with pytest.raises(RuntimeError, match="already running"):
            control_server.start_application_retry(payload)


def test_dismiss_and_restore_application_preserves_previous_failure(tmp_path):
    db_path = tmp_path / "dismiss.db"
    conn = database.init_db(db_path)
    conn.execute(
        """INSERT INTO jobs (url,title,apply_status,apply_error,last_attempted_at)
           VALUES (?,?,?,?,?)""",
        ("https://example.com/job", "Example Engineer", "failed", "time_limit",
         "2026-09-02"),
    )
    conn.commit()
    with patch.object(control_server.database, "get_connection", return_value=conn):
        assert control_server.dismiss_application("https://example.com/job")["ok"] is True
        dismissed = control_server.application_history(status="dismissed")
        assert dismissed["counts"]["dismissed"] == 1
        assert dismissed["rows"][0]["reason_category"] == "Removed from queue"
        assert control_server.restore_application("https://example.com/job")["ok"] is True
    state = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url='https://example.com/job'",
    ).fetchone()
    assert tuple(state) == ("failed", "time_limit")
    database.close_connection(db_path)


def test_stop_worker_releases_in_progress_job_only_after_service_stops(tmp_path):
    db_path = tmp_path / "stop.db"
    conn = database.init_db(db_path)
    conn.execute(
        "INSERT INTO jobs (url,title,apply_status,agent_id) VALUES (?,?,?,?)",
        ("https://example.com/job", "Running", "in_progress", "worker-0"),
    )
    conn.commit()
    completed = control_server.subprocess.CompletedProcess([], 0, "", "")
    with (
        patch.object(control_server.database, "get_connection", return_value=conn),
        patch.object(control_server, "_launchctl", return_value=completed),
        patch.object(control_server, "_stop_orphaned_worker_processes", return_value=2),
        patch.object(control_server, "service_running", return_value=False),
    ):
        result = control_server.stop_worker()
    assert "Released 1 job" in result["message"]
    assert "Closed 2 orphaned worker processes" in result["message"]
    state = conn.execute(
        "SELECT apply_status, agent_id FROM jobs WHERE url='https://example.com/job'",
    ).fetchone()
    assert tuple(state) == (None, None)
    database.close_connection(db_path)


def test_bulk_retry_only_queues_retryable_failures(tmp_path):
    db_path = tmp_path / "bulk-history.db"
    conn = database.init_db(db_path)
    conn.executemany(
        """INSERT INTO jobs
        (url,title,apply_status,apply_error,apply_attempts,last_attempted_at,
         tailored_resume_path,application_url)
        VALUES (?,?,?,?,?,?,?,?)""",
        [
            ("https://example.com/captcha", "Captcha", "failed", "captcha", 1,
             "2026-09-02", "/tmp/resume.pdf", "https://example.com/apply/captcha"),
            ("https://example.com/location", "Location", "failed", "not_eligible_location", 1,
             "2026-09-02", "/tmp/resume.pdf", "https://example.com/apply/location"),
            ("https://example.com/applied", "Applied", "applied", None, 1,
             "2026-09-02", "/tmp/resume.pdf", "https://example.com/apply/applied"),
        ],
    )
    conn.execute(
        "UPDATE jobs SET applied_at='2026-09-02' WHERE url='https://example.com/applied'",
    )
    conn.commit()
    with patch.object(control_server.database, "get_connection", return_value=conn):
        result = control_server.queue_failed_retries([
            "https://example.com/captcha", "https://example.com/location",
            "https://example.com/applied",
        ])
    assert result["queued"] == 1
    assert result["skipped"] == 2
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url='https://example.com/captcha'",
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url='https://example.com/location'",
    ).fetchone()[0] == "failed"
    database.close_connection(db_path)


def test_retry_all_queues_recoverable_failures_and_starts_continuous_worker(tmp_path):
    db_path = tmp_path / "retry-all.db"
    conn = database.init_db(db_path)
    conn.executemany(
        """INSERT INTO jobs
        (url,title,apply_status,apply_error,apply_attempts,last_attempted_at,
         tailored_resume_path,application_url)
        VALUES (?,?,?,?,?,?,?,?)""",
        [
            ("https://example.com/retry", "Retry", "failed", "time_limit", 2,
             "2026-09-02", "/tmp/resume.pdf", "https://example.com/apply/retry"),
            ("https://example.com/location", "Location", "failed",
             "not_eligible_location", 1, "2026-09-02", "/tmp/resume.pdf",
             "https://example.com/apply/location"),
        ],
    )
    conn.commit()
    payload = valid_payload()
    payload["workers"] = 3
    readiness = {"complete": True, "missing": []}
    with (
        patch.object(control_server.database, "get_connection", return_value=conn),
        patch.object(control_server, "settings_snapshot", return_value={}),
        patch.object(control_server, "setup_readiness", return_value=readiness),
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "_install_worker_service") as install,
    ):
        result = control_server.retry_all_applications(payload)
    assert result["queued"] == 1
    assert result["skipped"] == 1
    assert result["started"] is True
    assert install.call_args.kwargs == {"continuous": True}
    assert install.call_args.args[0][1] == "run-worker"
    worker_index = install.call_args.args[0].index("--workers") + 1
    assert install.call_args.args[0][worker_index] == "3"
    database.close_connection(db_path)


def test_retry_all_adds_to_running_worker_without_replacing_it(tmp_path):
    db_path = tmp_path / "retry-all-running.db"
    conn = database.init_db(db_path)
    conn.execute(
        """INSERT INTO jobs
        (url,title,apply_status,apply_error,apply_attempts,last_attempted_at,
         tailored_resume_path,application_url)
        VALUES (?,?,?,?,?,?,?,?)""",
        ("https://example.com/retry", "Retry", "failed", "time_limit", 2,
         "2026-09-02", "/tmp/resume.pdf", "https://example.com/apply/retry"),
    )
    conn.commit()
    readiness = {"complete": True, "missing": []}
    with (
        patch.object(control_server.database, "get_connection", return_value=conn),
        patch.object(control_server, "settings_snapshot", return_value={}),
        patch.object(control_server, "setup_readiness", return_value=readiness),
        patch.object(control_server, "service_running", return_value=True),
        patch.object(control_server, "_install_worker_service") as install,
    ):
        result = control_server.retry_all_applications(valid_payload())
    assert result["queued"] == 1
    assert "current run" in result["message"]
    install.assert_not_called()
    database.close_connection(db_path)


def test_local_model_url_is_normalized_and_placeholder_model_is_replaced(tmp_path):
    from selfapply.llm import normalize_local_url

    assert normalize_local_url("http://localhost:1234") == "http://localhost:1234/v1"
    assert normalize_local_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    env = tmp_path / ".env"
    env.write_text("LLM_URL=http://localhost:1234\nLLM_MODEL=local-model\n")
    request = httpx.Request("GET", "http://localhost:1234/v1/models")
    response = httpx.Response(200, request=request, json={"data": [
        {"id": "text-embedding-model"}, {"id": "qwen-coder-7b"},
    ]})
    with (
        patch.object(control_server.config, "ENV_PATH", env),
        patch("httpx.get", return_value=response) as get,
    ):
        result = control_server.list_ai_models()
    assert get.call_args.args[0] == "http://localhost:1234/v1/models"
    assert result["models"] == ["qwen-coder-7b"]
    assert result["selected_model"] == "qwen-coder-7b"
    assert "LLM_URL=http://localhost:1234/v1" in env.read_text()
