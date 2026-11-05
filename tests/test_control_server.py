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


def test_test_run_arguments_fill_one_job_without_submission():
    args = control_server.build_test_program_arguments(
        control_server.validate_start_payload(valid_payload(agent="claude"))
    )
    assert args[1] == "run-worker"
    assert "--dry-run" in args
    assert args[args.index("--test-limit") + 1] == "1"
    assert args[args.index("--workers") + 1] == "1"
    assert args[args.index("--agent") + 1] == "claude"


def test_start_test_run_requires_a_ready_job_and_is_one_shot():
    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "settings_snapshot", return_value={}),
        patch.object(control_server, "setup_readiness", return_value={
            "complete": True, "missing": [],
        }),
        patch.object(control_server, "ensure_browser_runtime"),
        patch.object(control_server, "_install_worker_service") as install,
    ):
        result = control_server.start_test_run(valid_payload())
    assert result["ok"] is True
    assert "final submission blocked" in result["message"]
    assert "--dry-run" in install.call_args.args[0]
    assert install.call_args.kwargs["continuous"] is False


def test_self_apply_ui_exposes_clear_no_submit_test_run():
    html = control_server.HTML
    assert 'id="testRunButton"' in html
    assert "Test one application — no submit" in html
    assert "/api/worker/test-run" in html
    assert "A browser-level safety guard blocks the final Submit button." in html
    launcher_source = (
        Path(control_server.__file__).with_name("apply") / "launcher.py"
    ).read_text(encoding="utf-8")
    assert 'if dry_run and browser_mode == "managed":' in launcher_source


def test_self_apply_ui_offers_master_resume_mode_and_project_links():
    html = control_server.HTML
    assert 'id="useMasterResume"' in html
    assert "Use my master resume for every application" in html
    assert "saveApplicationResumeMode()" in html
    assert "https://github.com/Gobi-96" in html
    assert "https://github.com/Gobi-96/SelfApply/issues" in html
    assert "https://github.com/sponsors/Gobi-96" in html
    assert "\u00e9" not in html


def test_document_vault_lists_master_and_all_tailored_formats(tmp_path):
    master_pdf = tmp_path / "resume.pdf"
    source = tmp_path / "resume-source.json"
    tailored = tmp_path / "tailored"
    master_pdf.write_bytes(b"%PDF-1.4 master")
    source.write_text(json.dumps({
        "name": "My Original Resume.pdf", "format": ".pdf",
    }))
    tailored.mkdir()
    text = tailored / "company_role.txt"
    text.write_text("resume")
    text.with_suffix(".pdf").write_bytes(b"%PDF-1.4 tailored")
    text.with_suffix(".docx").write_bytes(b"PK word")
    text.with_name("company_role_REPORT.json").write_text(json.dumps({
        "changes": [{"section": "summary", "before": "Old", "after": "New"}],
    }))
    connection = Mock()
    connection.execute.return_value.fetchall.return_value = [{
        "title": "Product Manager", "tailored_resume_path": str(text),
    }]
    with (
        patch.object(control_server.config, "RESUME_SOURCE_PATH", source),
        patch.object(control_server.config, "RESUME_PDF_PATH", master_pdf),
        patch.object(control_server.config, "RESUME_DOCX_PATH", tmp_path / "resume.docx"),
        patch.object(control_server.config, "RESUME_PATH", tmp_path / "resume.txt"),
        patch.object(control_server.config, "TAILORED_DIR", tailored),
        patch.object(control_server.database, "get_connection", return_value=connection),
    ):
        result = control_server.document_vault_files()
    assert result["master"]["name"] == "My Original Resume.pdf"
    assert result["master"]["view"].endswith("format=pdf&view=1")
    assert result["master"]["download"].endswith("format=pdf")
    assert result["documents"][0]["name"] == "Product Manager"
    assert result["documents"][0]["pdf_view"].endswith("&view=1")
    assert result["documents"][0]["docx"].endswith("company_role.docx")
    assert result["documents"][0]["compare_name"] == "company_role"
    assert "txt" not in result["documents"][0]


def test_resume_change_report_returns_only_real_changes(tmp_path):
    tailored = tmp_path / "tailored"
    tailored.mkdir()
    (tailored / "company_role_REPORT.json").write_text(json.dumps({
        "format_fidelity": "exact-structure",
        "changes": [
            {"section": "summary", "before": "Old summary", "after": "New summary"},
            {"section": "skills", "before": "Python", "after": "Python"},
        ],
    }))
    with patch.object(control_server.config, "TAILORED_DIR", tailored):
        result = control_server.resume_change_report("company_role")
    assert result["changed_block_count"] == 1
    assert result["changes"][0] == {
        "section": "summary", "before": "Old summary", "after": "New summary",
        "page": None, "rect": None,
    }
    with patch.object(control_server.config, "TAILORED_DIR", tailored):
        with pytest.raises(ValueError, match="Invalid"):
            control_server.resume_change_report("../company_role")


def test_document_vault_ui_has_view_and_download_controls():
    html = control_server.HTML
    assert 'id="masterDocument"' in html
    assert "View ${esc(label)}" in html
    assert "Download ${esc(label)}" in html
    assert "documentView(master.view,master.format)" in html
    assert "documentDownload(x.docx,'Word')" in html
    assert "Download TXT" not in html
    assert "/api/resumes/changes" in html
    assert 'class="diff-line removed"' in html
    assert 'class="diff-line added"' in html
    assert "showResumeCompareMode('unified')" in html
    assert "showResumeCompareMode('split')" in html
    assert "showResumeCompareMode('pdf')" in html
    assert "/api/resumes/preview" in html
    assert 'class="pdf-page"' in html


def test_resume_preview_info_lists_every_full_pdf_page(tmp_path):
    import fitz

    tailored = tmp_path / "tailored"
    tailored.mkdir()
    pdf = tailored / "role.pdf"
    document = fitz.open()
    for text in ("Complete first page", "Complete second page"):
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(pdf)
    document.close()
    with patch.object(control_server.config, "TAILORED_DIR", tailored):
        result = control_server.resume_preview_info("role.pdf", "tailored")
    assert result["page_count"] == 2
    assert len(result["pages"]) == 2
    assert len(result["sizes"]) == 2
    assert result["pages"][1].endswith("kind=tailored&name=role.pdf&page=1")


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
            patch.object(control_server, "ensure_browser_runtime", return_value={"ready": True}),
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


def test_successfully_finished_restart_on_failure_service_is_not_running(tmp_path):
    loaded = control_server.subprocess.CompletedProcess(
        [], 0, "active count = 0\nstate = not running\nlast exit code = 0", "",
    )
    plist = tmp_path / "worker.plist"
    plist.write_bytes(control_server.plistlib.dumps({
        "KeepAlive": {"SuccessfulExit": False},
    }))
    with (
        patch.object(control_server.platform, "system", return_value="Darwin"),
        patch.object(control_server, "_launchctl", return_value=loaded),
        patch.object(control_server, "PLIST_PATH", plist),
        patch.object(control_server, "_managed_worker_pids", return_value=[]),
    ):
        assert control_server._active_service_label() is None
        assert control_server.service_running() is False


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
    assert "Connect enabled sites with Google" in control_server.HTML
    assert "Use Google" in control_server.HTML
    assert "Sign in manually" in control_server.HTML
    assert "Disconnect" in control_server.HTML
    assert "Stop connecting" in control_server.HTML
    assert "post('/api/connections/manual',{name:n})" in control_server.HTML


def test_google_connection_ui_reports_progress_and_prevents_duplicate_actions():
    html = control_server.HTML
    assert "googleBusy=busy&&c.action.kind==='Google sign-in'" in html
    assert "googleBusy?'Opening Google…':agentBusy?'Connect Google instead':g.stale?'Reconnect Google':'Connect Google account'" in html
    assert "The previous Google session is no longer authenticated." in html
    assert "${busy?'disabled':''}" in html
    assert "portalBusy=busy&&" in html
    assert "portalBusy?'Connecting…':'Use Google'" in html


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
        patch.object(controller, "begin", side_effect=lambda kind, task, **_kwargs: {"result": task()}),
        patch("selfapply.cli._managed_connect_browser", side_effect=browser),
        patch("selfapply.apply.launcher.check_google_session", return_value=(False, None)),
        patch("selfapply.apply.launcher.wait_for_google_signin", side_effect=wait),
    ):
        result = controller.connect_google()

    assert result["result"] == "Google sign-in stopped."


def test_web_google_connection_rejects_existing_chrome_mode(tmp_path):
    controller = control_server.Controller()
    with pytest.raises(ValueError, match="managed Chrome"):
        controller.connect_google("existing")


def test_connections_ui_uses_managed_chrome_without_a_mode_selector():
    html = control_server.HTML
    assert 'id="connectionBrowser"' in html
    assert '<select id="connectionBrowser"' not in html
    assert '<select id="browserMode"' not in html
    assert "connectionBrowser.value='managed';browserMode.value='managed'" in html
    assert "function chooseBrowserMode(value)" not in html
    assert "'/api/connections/google',{browser:'managed'}" in html
    assert "name:n,browser:val('connectionBrowser')" in html


def test_ui_has_light_default_dark_theme_and_accessible_responsive_shell():
    html = control_server.HTML
    assert "localStorage.getItem('selfapply-theme')||'light'" in html
    assert 'id="themeLight"' in html
    assert 'class="profile-switcher"' in html
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
        {"name": "Dice", "status": "connected", "enabled": True},
        {"name": "LinkedIn", "status": "failed:login_required", "enabled": True},
        {"name": "Glassdoor", "status": "not_tested", "enabled": False},
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


def test_abandoned_browser_agent_connection_can_be_replaced():
    controller = control_server.Controller()
    first_started = threading.Event()

    def abandoned_login():
        cancel_event = controller.cancel_event
        first_started.set()
        cancel_event.wait(5)
        return "Codex account connection cancelled."

    controller.begin("Connect Codex account", abandoned_login)
    assert first_started.wait(1)
    result = controller.begin(
        "Connect Claude account",
        lambda: "Claude account connected.",
        replace_kinds=("Connect Codex account", "Connect Claude account"),
    )
    assert result["ok"] is True
    deadline = time.monotonic() + 1
    while controller.action.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller.action.kind == "Connect Claude account"
    assert controller.action.message == "Claude account connected."


def test_browser_agent_connect_marks_other_agent_login_as_replaceable():
    controller = control_server.Controller()
    with (
        patch.object(control_server.shutil, "which", return_value="/usr/bin/agent"),
        patch.object(controller, "begin", return_value={"ok": True}) as begin,
    ):
        controller.connect_browser_agent("codex")
    assert begin.call_args.kwargs["replace_kinds"] == (
        "Connect Codex account", "Connect Claude account",
    )


def test_google_connect_can_replace_abandoned_browser_agent_login():
    controller = control_server.Controller()
    with patch.object(controller, "begin", return_value={"ok": True}) as begin:
        controller.connect_google()
    assert begin.call_args.kwargs["replace_kinds"] == (
        "Connect Codex account", "Connect Claude account",
    )


def test_google_button_explains_and_replaces_pending_agent_login():
    html = control_server.HTML
    assert "Connect Google instead" in html
    assert "Connecting Google will cancel it." in html
    assert "busy&&!agentBusy?'disabled':''" in html


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
        "resume_preferences": {"application_mode": "master"},
    }
    with patch.object(control_server.config, "PROFILE_PATH", profile):
        control_server.save_profile_settings(payload)
    saved = json.loads(profile.read_text())
    assert saved["personal"]["password"] == "keep-me"
    assert saved["resume_facts"]["experience_entries"] == [{"company": "Example"}]
    assert saved["resume_facts"]["preserved_companies"] == ["Example", "Other"]
    assert saved["skills_boundary"]["tools"] == ["Git", "Docker"]
    assert saved["resume_preferences"]["application_mode"] == "master"


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
        patch.object(control_server, "logout_browser_agents", return_value=[]),
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
    assert not paths["ENV_PATH"].exists()


def test_restore_lists_and_restores_a_reset_backup(tmp_path):
    app_dir = tmp_path / "profiles" / "profile-b"
    app_dir.mkdir(parents=True)
    (tmp_path / "profiles.json").write_text(json.dumps({
        "version": 1,
        "active_profile_id": "profile-b",
        "profiles": [
            {"id": "profile-a", "name": "Original profile"},
            {"id": "profile-b", "name": "Current profile"},
        ],
    }), encoding="utf-8")
    paths = {
        "ROOT_DIR": tmp_path,
        "APP_DIR": app_dir,
        "DB_PATH": app_dir / "selfapply.db",
        "PROFILE_PATH": app_dir / "profile.json",
        "RESUME_PATH": app_dir / "resume.txt",
        "RESUME_PDF_PATH": app_dir / "resume.pdf",
        "RESUME_DOCX_PATH": app_dir / "resume.docx",
        "SEARCH_CONFIG_PATH": app_dir / "searches.yaml",
        "ENV_PATH": app_dir / ".env",
        "TAILORED_DIR": app_dir / "tailored_resumes",
        "APPLIED_RESUME_DIR": app_dir / "applied_resumes",
        "COVER_LETTER_DIR": app_dir / "cover_letters",
        "LOG_DIR": app_dir / "logs",
        "CHROME_WORKER_DIR": app_dir / "chrome-workers",
        "APPLY_WORKER_DIR": app_dir / "apply-workers",
    }
    backup = tmp_path / "backups" / "profile-a" / "reset-20260901-120000"
    backup.mkdir(parents=True)
    (backup / "profile.json").write_text(
        '{"personal":{"full_name":"Restored User"}}', encoding="utf-8",
    )
    (backup / ".env").write_text("OPENAI_API_KEY=restored\n", encoding="utf-8")
    (backup / "searches.yaml").write_text("location: Remote\n", encoding="utf-8")
    (backup / "resume.pdf").write_bytes(b"restored-master-pdf")
    (backup / "google_session.json").write_text('{"verified":true}', encoding="utf-8")
    (backup / "browser-agent-links.json").write_text(
        '{"codex":{"linked":true}}', encoding="utf-8",
    )
    (backup / "chrome-workers" / "worker-0").mkdir(parents=True)
    (backup / "chrome-workers" / "worker-0" / "Cookies").write_bytes(b"portal-session")
    (backup / "reset-manifest.json").write_text(json.dumps({
        "created_at": "2026-09-01T12:00:00+00:00",
        "reason": "User-requested SelfApply reset",
        "profile_id": "profile-a",
        "profile_name": "Original profile",
        "counts": {"applied": 9},
    }), encoding="utf-8")
    (app_dir / "profile.json").write_text(
        '{"personal":{"full_name":"Current User"}}', encoding="utf-8",
    )
    with (
        patch.multiple(control_server.config, **paths),
        patch.object(database, "DB_PATH", paths["DB_PATH"]),
        patch.object(control_server, "service_running", return_value=False),
    ):
        listing = control_server.available_profile_backups()
        result = control_server.restore_profile_backup({
            "backup_id": f"profile-a/{backup.name}", "confirmation": "RESTORE",
        })
    assert listing["backups"][0]["counts"]["applied"] == 9
    assert listing["backups"][0]["source_profile"] == "Original profile"
    assert result["restart"] is True
    assert json.loads(paths["PROFILE_PATH"].read_text())["personal"]["full_name"] == "Restored User"
    assert paths["ENV_PATH"].read_text() == "OPENAI_API_KEY=restored\n"
    assert paths["SEARCH_CONFIG_PATH"].read_text() == "location: Remote\n"
    assert paths["RESUME_PDF_PATH"].read_bytes() == b"restored-master-pdf"
    assert (app_dir / "google_session.json").read_text() == '{"verified":true}'
    assert json.loads((app_dir / "browser-agent-links.json").read_text())["codex"]["linked"] is True
    assert (app_dir / "chrome-workers" / "worker-0" / "Cookies").read_bytes() == b"portal-session"
    registry = json.loads((tmp_path / "profiles.json").read_text())
    restored_entry = next(item for item in registry["profiles"] if item["id"] == "profile-b")
    assert restored_entry["name"] == "Original profile"
    recovery = tmp_path / "backups" / "profile-b" / result["recovery_backup"]
    assert json.loads((recovery / "profile.json").read_text())["personal"]["full_name"] == "Current User"


def test_restore_rejects_unknown_or_unconfirmed_backup(tmp_path):
    with (
        patch.object(control_server.config, "ROOT_DIR", tmp_path),
        patch.object(control_server.config, "APP_DIR", tmp_path / "active"),
        pytest.raises(ValueError, match="RESTORE"),
    ):
        control_server.restore_profile_backup({"backup_id": "missing"})


def test_restore_link_validation_allows_internal_runtime_and_stale_chrome_links(tmp_path):
    runtime = tmp_path / "node-runtime"
    runtime.mkdir()
    target = runtime / "cli.js"
    target.write_text("runtime", encoding="utf-8")
    try:
        (runtime / "playwright").symlink_to("cli.js")
        (tmp_path / "SingletonSocket").symlink_to("/tmp/old-chrome-socket")
    except OSError:
        pytest.skip("Symbolic links are unavailable on this platform")
    control_server._validate_backup_links(tmp_path)
    assert "SingletonSocket" in control_server._restore_copy_ignore(
        str(tmp_path), ["SingletonSocket", "Preferences"],
    )


def test_restore_link_validation_rejects_external_links(tmp_path):
    outside = tmp_path.parent / "outside-secret"
    outside.write_text("secret", encoding="utf-8")
    try:
        (tmp_path / "external").symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are unavailable on this platform")
    with pytest.raises(ValueError, match="unsafe external link"):
        control_server._validate_backup_links(tmp_path)


def test_profile_reset_does_not_disconnect_host_wide_browser_agents(tmp_path):
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
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-example\n")
    (tmp_path / "google_session.json").write_text('{"verified":true}')
    with (
        patch.multiple(control_server.config, **paths),
        patch.object(database, "DB_PATH", paths["DB_PATH"]),
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "database_status", return_value={"counts": {}}),
        patch.object(control_server, "logout_browser_agents", return_value=["Codex", "Claude"]) as logout,
    ):
        result = control_server.reset_local_data({"confirmation": "RESET"})
    logout.assert_not_called()
    assert result["ok"] is True
    assert not paths["ENV_PATH"].exists()
    assert not (tmp_path / "google_session.json").exists()


def test_extension_verification_is_bound_to_the_saved_token(tmp_path):
    env = tmp_path / ".env"
    state = tmp_path / "chrome-extension-test.json"
    env.write_text("PLAYWRIGHT_MCP_EXTENSION_TOKEN=first-token\n")
    state.write_text(json.dumps({
        "token_hash": control_server.hashlib.sha256(b"first-token").hexdigest(),
    }))
    with (
        patch.object(control_server.config, "ENV_PATH", env),
        patch.object(control_server, "EXTENSION_TEST_PATH", state),
    ):
        assert control_server.extension_connection_verified() is True
        env.write_text("PLAYWRIGHT_MCP_EXTENSION_TOKEN=replaced-token\n")
        assert control_server.extension_connection_verified() is False


def test_live_extension_test_records_verification(tmp_path):
    env = tmp_path / ".env"
    state = tmp_path / "chrome-extension-test.json"
    env.write_text("PLAYWRIGHT_MCP_EXTENSION_TOKEN=test-token\n")
    with (
        patch.object(control_server.config, "ENV_PATH", env),
        patch.object(control_server, "EXTENSION_TEST_PATH", state),
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server, "browser_agent_status", return_value={
            "codex": {"authenticated": True},
        }),
        patch("selfapply.apply.launcher._run_one_shot_browser_agent",
              return_value="RESULT:EXTENSION_CONNECTED"),
    ):
        result = control_server.test_chrome_extension()
        assert result["ok"] is True
        assert control_server.extension_connection_verified() is True


def test_existing_chrome_is_not_available_from_web_worker_controls(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PLAYWRIGHT_MCP_EXTENSION_TOKEN=untested\n")
    with (
        patch.object(control_server.config, "ENV_PATH", env),
        patch.object(control_server, "EXTENSION_TEST_PATH", tmp_path / "missing.json"),
        pytest.raises(ValueError, match="uses managed Chrome"),
    ):
        control_server.validate_start_payload(valid_payload(browser="existing"))


def test_control_center_has_first_run_onboarding_and_reset_controls():
    assert 'id="onboarding"' in control_server.HTML
    assert 'id="onboardingProgress"' in control_server.HTML
    assert 'data-tab="settings"' in control_server.HTML
    assert "/api/settings/reset" in control_server.HTML
    assert "/api/settings/backups" in control_server.HTML
    assert "/api/settings/backup/restore" in control_server.HTML
    assert "Restore from a backup" in control_server.HTML
    assert 'class="app-loader"' in control_server.HTML
    assert "function advanceOnboarding()" in control_server.HTML
    assert "document.body.classList.remove('booting')" in control_server.HTML
    assert control_server.HTML.count('class="nav-icon"><svg') == 8
    assert 'id="primaryConnections"' in control_server.HTML
    assert "Required automation" in control_server.HTML
    assert "focusAutomation('ai')" in control_server.HTML
    assert "focusAutomation('agent')" in control_server.HTML
    assert "focusAutomation('runtime')" not in control_server.HTML
    assert "of 2 ready" in control_server.HTML
    assert "function organizeAutomationSettings()" in control_server.HTML
    assert "Complete five essentials" in control_server.HTML
    assert "Missing: ${esc(missingText)}" in control_server.HTML
    assert "['connected-accounts','Google sign-in'" in control_server.HTML
    assert "{confirmation,backup}" in control_server.HTML
    assert "/api/profiles/create" in control_server.HTML
    assert "Use my existing Chrome tab" not in control_server.HTML
    assert "Test Chrome extension" not in control_server.HTML
    assert "Install Chrome extension" not in control_server.HTML


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
    profile = tmp_path / "profile.json"
    with (
        patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches),
        patch.object(control_server.config, "RESUME_PATH", resume),
        patch.object(control_server.config, "PROFILE_PATH", profile),
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


def test_search_settings_supports_local_radius_plus_remote_mode(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text("{}\n")
    with patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches):
        control_server.save_search_settings({
            "country": "United States", "state": "California", "city": "Fremont",
            "distance": 50, "hours_old": 72, "results_per_site": 50,
            "location_mode": "local_and_remote", "sites": [
                "indeed", "linkedin", "glassdoor", "zip_recruiter", "google",
            ],
            "target_titles": "Solution Architect",
        })
    saved = control_server.yaml.safe_load(searches.read_text())
    assert saved["location_mode"] == "local_and_remote"
    assert saved["locations"] == [
        {"location": "Fremont, California, United States", "remote": False},
        {"location": "Remote", "remote": True},
    ]
    assert saved["defaults"]["distance"] == 50


def test_job_preferences_defaults_missing_board_config_to_all_major_boards():
    for board in ("indeed", "linkedin", "glassdoor", "zip_recruiter", "google"):
        assert f"'{board}'" in control_server.HTML
    assert "Object.prototype.hasOwnProperty.call(s,'sites')" in control_server.HTML
    assert "Remote + on-site/hybrid within my radius" in control_server.HTML
    assert "location_mode:val('s_location_mode')" in control_server.HTML


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


def test_resume_upload_starts_immediately_after_file_selection():
    assert 'id="resumeFile" type="file" accept=".pdf,.docx,.txt" onchange="uploadResume()"' in control_server.HTML
    assert "let resumeUploadRunning=false" in control_server.HTML
    assert "Uploading ${f.name}…" in control_server.HTML
    assert '>Upload and convert resume</button>' not in control_server.HTML


def test_docx_resume_is_preserved_and_converted_to_plain_text(tmp_path):
    buffer = BytesIO()
    xml = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Software Engineer</w:t></w:r></w:p><w:p><w:r><w:t>Python and TypeScript</w:t></w:r></w:p></w:body></w:document>'''
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    with (
        patch.object(control_server.config, "RESUME_DOCX_PATH", tmp_path / "resume.docx"),
        patch.object(control_server.config, "RESUME_PATH", tmp_path / "resume.txt"),
        patch.object(control_server.config, "PROFILE_PATH", tmp_path / "profile.json"),
        patch.object(control_server.config, "SEARCH_CONFIG_PATH", tmp_path / "searches.yaml"),
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


def test_ai_ui_auto_detects_provider_without_obsolete_extension_setup():
    assert "Claude / Anthropic" in control_server.HTML
    assert "Enter an API key or local URL, then test the connection" in control_server.HTML
    assert "Test connection" in control_server.HTML
    assert "Save connection" in control_server.HTML
    assert "/api/settings/ai/probe" in control_server.HTML
    assert "Install Chrome extension" not in control_server.HTML
    assert "PLAYWRIGHT_MCP_EXTENSION_TOKEN" not in control_server.HTML


def test_probe_ai_provider_detects_unsaved_key_without_persisting(tmp_path):
    client = Mock()
    client.ask.return_value = "SelfApply OK"
    with (
        patch.object(control_server.config, "ENV_PATH", tmp_path / ".env"),
        patch("selfapply.llm.LLMClient", return_value=client),
    ):
        result = control_server.probe_ai_provider({"api_key": "sk-ant-example"})
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-haiku-4-5"
    assert result["saved"] is False
    assert not (tmp_path / ".env").exists()
    client.close.assert_called_once()


def test_removing_ai_connection_also_removes_local_url_and_model(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_URL=http://localhost:11434/v1\nLLM_MODEL=qwen\n")
    with patch.object(control_server.config, "ENV_PATH", env):
        control_server.delete_saved_secret("provider")
    assert "LLM_URL" not in env.read_text()
    assert "LLM_MODEL" not in env.read_text()


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
        patch.object(control_server, "_set_browser_agent_link") as set_link,
    ):
        assert controller.unlink_browser_agent("codex")["ok"] is True
    assert run.call_args.args[0] == ["codex", "logout"]
    set_link.assert_called_once_with("codex", False)


def test_connection_completion_refreshes_onboarding_and_verifies_browser_auth():
    assert "completedConnectionAction&&completedConnectionAction!==lastConnectionAction" in control_server.HTML
    assert "!busy&&c.action.kind" in control_server.HTML
    assert "await loadSettings()" in control_server.HTML
    source = open(control_server.__file__, encoding="utf-8").read()
    assert 'browser_agent_status().get(name, {}).get("cli_authenticated")' in source
    assert "_set_browser_agent_link(name, True)" in source


def test_browser_agent_requires_selfapply_link_even_when_cli_is_logged_in(tmp_path):
    links = tmp_path / "browser-agent-links.json"
    completed = control_server.subprocess.CompletedProcess([], 0, "Logged in", "")
    with (
        patch.object(control_server, "BROWSER_AGENT_LINKS_PATH", links),
        patch.object(control_server.shutil, "which", return_value="/usr/bin/agent"),
        patch.object(control_server.subprocess, "run", return_value=completed),
    ):
        status = control_server.browser_agent_status()
        assert status["codex"]["cli_authenticated"] is True
        assert status["codex"]["authenticated"] is False
        assert status["codex"]["linked"] is False
        assert "link to SelfApply required" in status["codex"]["detail"]

        control_server._set_browser_agent_link("codex", True)
        status = control_server.browser_agent_status()
        assert status["codex"]["authenticated"] is True
        assert status["codex"]["linked"] is True


def test_self_apply_agent_selector_uses_only_linked_accounts():
    assert "function syncAgentChoices(agents)" in control_server.HTML
    assert "Connect Codex or Claude first" in control_server.HTML
    assert "applyAgent.disabled=linked.length<2" in control_server.HTML
    assert "syncAgentChoices(agents);renderReadiness(d)" in control_server.HTML
    assert "localStorage.setItem('selfapply-agent',applyAgent.value)" in control_server.HTML


def test_playwright_runtime_is_internal_not_an_onboarding_field():
    snapshot = {
        "profile": {}, "searches": {}, "discovery": {}, "ai": {"provider": "none"},
        "resume": {"text": False}, "browser_agent": {},
        "browser_runtime": {"ready": False},
    }
    readiness = control_server.setup_readiness(snapshot)
    assert "Playwright browser runtime" not in {
        item["label"] for item in readiness["missing"]
    }
    assert 'id="browserRuntime" hidden aria-hidden="true"' in control_server.HTML
    assert "/api/settings/browser-runtime/install" in control_server.HTML
    assert "/api/settings/browser-runtime/test" in control_server.HTML


def test_internal_browser_runtime_repairs_missing_packaged_chromium():
    repaired = {"ready": True, "missing": []}
    with (
        patch.object(control_server, "browser_runtime_status", return_value={
            "ready": False,
            "python_package": True,
            "missing": ["Playwright Chromium"],
        }),
        patch.object(control_server, "install_playwright_browser", return_value={
            "ok": True, "runtime": repaired,
        }) as install,
    ):
        assert control_server.ensure_browser_runtime() == repaired
    install.assert_called_once_with()


def test_playwright_runtime_test_launches_and_automates_a_page():
    page = Mock()
    page.title.return_value = "SelfApply browser check"
    browser = Mock()
    browser.new_page.return_value = page
    playwright = Mock()
    playwright.chromium.launch.return_value = browser

    @contextmanager
    def running_playwright():
        yield playwright

    with (
        patch.object(control_server, "browser_runtime_status", return_value={
            "ready": True, "detail": "Browser runtime ready",
        }),
        patch("playwright.sync_api.sync_playwright", side_effect=running_playwright),
    ):
        result = control_server.test_playwright_browser()

    assert result["ok"] is True
    playwright.chromium.launch.assert_called_once_with(headless=True)
    page.set_content.assert_called_once()
    browser.close.assert_called_once()


def test_google_is_required_but_portal_accounts_do_not_block_readiness():
    snapshot = {
        "profile": {}, "searches": {}, "discovery": {}, "ai": {"provider": "none"},
        "resume": {"text": False}, "browser_agent": {},
        "login_connections": {"google_verified": False, "enabled_remaining": 2},
    }
    missing = {
        item["label"] for item in control_server.setup_readiness(snapshot)["missing"]
    }
    assert "Google sign-in" in missing
    google = next(
        item for item in control_server.setup_readiness(snapshot)["missing"]
        if item["label"] == "Google sign-in"
    )
    assert google["tab"] == "connected-accounts"
    assert not any("job-site sign-in" in label for label in missing)
    assert "Job-site sign-ins" in control_server.HTML
    assert "Missing portal accounts do not block SelfApply" in control_server.HTML
    assert "Managed Chrome</h2>" not in control_server.HTML
    assert "Monster" not in control_server.BASE_PORTALS


def test_browser_runtime_status_reports_missing_dependencies():
    with (
        patch.object(control_server.importlib.util, "find_spec", return_value=None),
        patch.object(control_server.shutil, "which", return_value=None),
        patch.object(control_server.config, "get_chrome_path", side_effect=FileNotFoundError),
    ):
        status = control_server.browser_runtime_status()
    assert status["ready"] is False
    assert status["python_package"] is False
    assert status["node_runner"] is False
    assert status["chrome"] is False
    assert status["chromium"] is False


def test_ui_has_cascading_location_dropdowns_examples_and_readiness_gate():
    for field in ('id="p_country"', 'id="p_province_state"', 'id="p_city"'):
        assert field in control_server.HTML
    for text in (
        "Example University, City College", "Python, Java, Rust, SQL",
        "Direct and remote boards", "Remove AI connection", "Test",
        "Setup incomplete", "Start Self Apply",
    ):
        assert text in control_server.HTML
    assert "startSelfApply.disabled=!complete" in control_server.HTML
    assert "item.tab==='document-vault'?'review-apply'" in control_server.HTML
    assert "item.tab==='document-vault'?'ats'" not in control_server.HTML
    assert "function renderTestedAIState()" in control_server.HTML
    assert "Click Save connection to keep this provider." in control_server.HTML
    assert "if(pending){testedAI=pending;renderTestedAIState()}" in control_server.HTML
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
            "documentView(x.resume_url,'resume PDF',x.resume_compare_name)", "Why this status?", "Filters", "Retry all failed applications",
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
    assert "profile-avatar" in control_server.HTML
    assert 'id="appActionDialog"' in control_server.HTML
    assert "function appConfirm(" in control_server.HTML
    assert "window.confirm" not in control_server.HTML
    assert "window.prompt" not in control_server.HTML
    assert "filterFailureReason" in control_server.HTML
    assert "setHistoryStatus('applied')" in control_server.HTML


def test_ats_matcher_ui_and_plain_text_analysis(tmp_path):
    for text in (
        "Resume tools", "Check match", "Create tailored resume",
        "Employer job link", "Download PDF", "Download DOCX",
        "Mapping editable resume blocks", "Resume was not created",
        "Automatically add relevant JD skills", "atsAggressiveness",
        "Skill-tailoring strength", "p_skill_expansion_enabled",
            "See match details", "targeted changes",
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
    report = {"status": "approved", "master_ats_match": ats, "ats_match": ats,
              "format_fidelity": "best-effort", "changed_block_count": 1}
    manifest = {"source_format": "txt", "master_fingerprint": "test", "blocks": []}

    def fake_pdf(text_path):
        path = Path(text_path).with_suffix(".pdf")
        path.write_bytes(b"%PDF-test")
        return path

    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
        patch.object(control_server.config, "load_profile", return_value={}),
        patch("selfapply.scoring.document_tailor.tailor_document",
              return_value=(tailored_text, report, {"edits": [], "warnings": []}, manifest)) as tailor,
        patch("selfapply.scoring.pdf.convert_to_pdf", side_effect=fake_pdf),
    ):
        result = control_server.tailor_and_score_job({
            "job_description": jd, "title": "Platform Engineer", "company": "Example",
        })
    assert result["ok"] is True
    assert result["master_result"]["overall_score"] == 75
    assert result["downloads"]["pdf"].endswith(".pdf")
    assert Path(result["text_path"]).is_file()
    assert isinstance(result["pdf_path"], str)
    # Completed asynchronous tasks are returned through a JSON API.  Keep
    # filesystem paths from leaking into that response as Path instances.
    json.dumps(result)
    assert tailor.call_args.kwargs["source_path"] == master


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
    manifest = {"source_format": "txt", "master_fingerprint": "test", "blocks": []}

    def fake_pdf(text_path):
        path = Path(text_path).with_suffix(".pdf")
        path.write_bytes(b"%PDF-test")
        return path

    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
        patch.object(control_server.config, "load_profile", return_value={}),
        patch("selfapply.scoring.document_tailor.tailor_document",
              return_value=(text, report, {"edits": [], "warnings": []}, manifest)),
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
    manifest = {"source_format": "txt", "master_fingerprint": "test", "blocks": []}

    with (
        patch.object(control_server, "service_running", return_value=False),
        patch.object(control_server.config, "RESUME_PATH", master),
        patch.object(control_server.config, "TAILORED_DIR", tailored_dir),
        patch.object(control_server.config, "load_profile", return_value={}),
        patch("selfapply.scoring.document_tailor.tailor_document",
              return_value=(text, report, {"edits": [], "warnings": []}, manifest)),
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
    assert "PDF fallback could not be rendered" in result["message"]
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
        patch("selfapply.scoring.document_tailor.tailor_document",
              return_value=("CREATIVE RESUME", report,
                            {"edits": [], "warnings": []},
                            {"source_format": "txt", "master_fingerprint": "test", "blocks": []})) as generate,
        patch("selfapply.scoring.pdf.convert_to_pdf", return_value=tmp_path / "resume.pdf"),
    ):
        result = tailor_module.run_tailoring(limit=1)

    assert result["approved"] == 1
    assert generate.call_args.kwargs["source_path"] == master


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


def test_resume_upload_extracts_profile_without_an_ai_provider():
    resume = """Jordan Lee
Platform Engineer
Portland, Oregon, United States
jordan@example.com | +1 503 555 0199 | https://linkedin.com/in/jordan | https://github.com/jordan

SUMMARY
Platform engineer with 8+ years of experience.

TECHNICAL SKILLS
Languages: Python, Go, SQL
Frameworks: Django, React
Cloud & Tools: AWS, Docker, Kubernetes

EXPERIENCE
Senior Platform Engineer | Example Corp | 2021-Present
- Built internal services.

EDUCATION
Master of Science, Example University
"""
    result = control_server.extract_profile_from_resume(resume)
    assert result["personal"]["full_name"] == "Jordan Lee"
    assert result["personal"]["email"] == "jordan@example.com"
    assert result["personal"]["city"] == "Portland"
    assert result["experience"]["current_title"] == "Platform Engineer"
    assert result["experience"]["years_of_experience_total"] == "8"
    assert result["experience"]["education_level"] == "Master's"
    assert result["suggested_job_titles"] == [
        "Platform Engineer", "Senior Platform Engineer",
    ]
    assert result["skills_boundary"]["programming_languages"] == ["Python", "Go", "SQL"]
    assert result["skills_boundary"]["frameworks"] == ["Django", "React"]
    assert "AWS" in result["skills_boundary"]["tools"]


def test_resume_import_handles_alternating_skill_categories_and_plain_responsibilities():
    resume = """Sample Candidate
Solution Architect
candidate@example.com | +1 212 555 0100

SUMMARY
Solution architect with 12+ years of experience.

TECHNICAL SKILLS
AI / Data
Predictive Analytics, Data Engineering, Spark, PySpark, Kafka
Cloud Platforms
Google Cloud Platform, AWS
Databases
Oracle, SQL Server, MySQL

EDUCATION DETAILS:
Masters in Technology at Sample University - May 2020

EXPERIENCE
Health Company - Texas Sep 2022 to April 2026
Solution Architect Responsibilities:
Designed cloud services for healthcare products.
Built reporting pipelines and improved processing by 30%.
Finance Company - New York Jan 2018 to Aug 2022
Data Engineer Responsibilities:
Maintained SQL data platforms.
"""
    result = control_server.extract_profile_from_resume(resume)
    assert "SQL" in result["skills_boundary"]["programming_languages"]
    assert "Spark" in result["skills_boundary"]["frameworks"]
    assert "Google Cloud Platform" in result["skills_boundary"]["tools"]
    assert len(result["resume_facts"]["experience_entries"]) == 2
    assert result["resume_facts"]["preserved_companies"] == [
        "Health Company", "Finance Company",
    ]
    assert result["resume_facts"]["preserved_school"] == "Sample University"
    assert result["resume_facts"]["real_metrics"] == ["30%"]
    assert result["suggested_job_titles"] == ["Solution Architect", "Data Engineer"]


def test_resume_job_titles_replace_old_suggestions_and_keep_manual_titles(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text(control_server.yaml.safe_dump({
        "queries": [
            {"query": "Product Engineer", "tier": 1},
            {"query": "Old Resume Role", "tier": 2},
        ],
        "resume_suggested_titles": ["Old Resume Role"],
        "sites": ["linkedin"],
    }))
    with patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches):
        imported = control_server._merge_resume_job_titles([
            "Solution Architect", "Data Engineer",
        ])
    saved = control_server.yaml.safe_load(searches.read_text())
    assert [item["query"] for item in saved["queries"]] == [
        "Product Engineer", "Solution Architect", "Data Engineer",
    ]
    assert saved["resume_suggested_titles"] == ["Solution Architect", "Data Engineer"]
    assert imported == ["Solution Architect", "Data Engineer"]


def test_saving_job_preferences_preserves_only_retained_resume_suggestions(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text(control_server.yaml.safe_dump({
        "queries": [{"query": "Solution Architect", "tier": 1}],
        "resume_suggested_titles": ["Solution Architect", "Data Engineer"],
    }))
    with patch.object(control_server.config, "SEARCH_CONFIG_PATH", searches):
        control_server.save_search_settings({
            "location": "Remote", "distance": 0, "hours_old": 72,
            "results_per_site": 50, "sites": ["linkedin"],
            "target_titles": "Solution Architect\nCloud Architect",
        })
    saved = control_server.yaml.safe_load(searches.read_text())
    assert saved["resume_suggested_titles"] == ["Solution Architect"]


def test_resume_profile_merge_fills_blanks_without_overwriting_user_values(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "personal": {"full_name": "Chosen Name", "email": ""},
        "experience": {"target_role": "Product Engineer"},
    }))
    with patch.object(control_server.config, "PROFILE_PATH", profile):
        imported = control_server._merge_resume_profile({
            "personal": {"full_name": "Resume Name", "email": "resume@example.com"},
            "experience": {"target_role": "Resume Role", "current_title": "Engineer"},
        })
    saved = json.loads(profile.read_text())
    assert saved["personal"]["full_name"] == "Chosen Name"
    assert saved["personal"]["email"] == "resume@example.com"
    assert saved["experience"]["target_role"] == "Product Engineer"
    assert saved["experience"]["current_title"] == "Engineer"
    assert "personal.full_name" not in imported


def test_replacement_resume_refreshes_resume_fields_but_preserves_user_answers(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "personal": {"full_name": "Old Name", "address": "123 User Entered St"},
        "experience": {"current_title": "Old Role", "target_role": "Chosen Target"},
        "work_authorization": {"legally_authorized_to_work": True},
        "skills_boundary": {"programming_languages": ["OldLang"], "tools": ["OldTool"]},
        "resume_facts": {"experience_entries": [{"header": "Old Job"}]},
    }))
    with patch.object(control_server.config, "PROFILE_PATH", profile):
        imported = control_server._merge_resume_profile({
            "personal": {"full_name": "New Name"},
            "experience": {"current_title": "New Role", "target_role": "Resume Target"},
            "skills_boundary": {"programming_languages": ["Python"]},
            "resume_facts": {"experience_entries": [{"header": "New Job"}]},
        }, replace_existing=True)
    saved = json.loads(profile.read_text())
    assert saved["personal"]["full_name"] == "New Name"
    assert saved["personal"]["address"] == "123 User Entered St"
    assert saved["experience"]["current_title"] == "New Role"
    assert saved["experience"]["target_role"] == "Chosen Target"
    assert saved["work_authorization"]["legally_authorized_to_work"] is True
    assert saved["skills_boundary"]["programming_languages"] == ["Python"]
    assert "tools" not in saved["skills_boundary"]
    assert saved["resume_facts"]["experience_entries"] == [{"header": "New Job"}]
    assert "personal.full_name" in imported


def test_preparation_status_explains_zero_ready_while_scoring(tmp_path):
    conn = database.init_db(tmp_path / "progress.db")
    conn.executemany(
        """INSERT INTO jobs
           (url, title, site, full_description, detail_scraped_at, fit_score)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("https://example.com/1", "Role 1", "Example", "JD", "now", 8),
            ("https://example.com/2", "Role 2", "Example", "JD", "now", None),
            ("https://example.com/3", "Role 3", "Example", "JD", "now", None),
        ],
    )
    conn.commit()
    with patch.object(control_server, "_recent_scoring_progress", return_value=(1, 3)):
        progress = control_server._preparation_status(
            conn, running=True, current_job=None, ready=0,
        )
    assert progress == {
        "stage": "scoring",
        "label": "Checking job matches",
        "detail": "Scoring 1 of 3 discovered jobs.",
        "current": 1,
        "total": 3,
        "percent": 33,
    }


def test_self_apply_ui_shows_live_background_pipeline_before_jobs_are_ready():
    html = control_server.HTML
    assert "Working now:" in html
    assert "Self Apply is running" in html
    assert "1. Find matching jobs" in html
    assert "2. Score and tailor matches" in html
    assert "3. Apply automatically" in html
    assert 'class="worker-progress-track"' in html


def test_self_apply_ui_hides_editable_setup_during_an_active_run():
    html = control_server.HTML
    css = (
        Path(control_server.__file__).with_name("ui") / "control_center.css"
    ).read_text(encoding="utf-8")
    assert 'id="selfApplyIntro"' in html
    assert 'id="activeRunSummary"' in html
    assert 'class="form worker-setup-controls"' in html
    assert 'class="test-run-note worker-setup-controls"' in html
    assert "selfApplyPage.classList.toggle('worker-active',s.running)" in html
    assert "Settings are locked until you stop." in html
    assert "#review-apply.worker-active .worker-setup-controls" in css
