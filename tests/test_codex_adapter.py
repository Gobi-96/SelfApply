import unittest
import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock, patch

from selfapply import database
from selfapply.apply.chrome import (
    _kill_process_tree,
    cleanup_worker,
    cleanup_on_exit,
    has_google_auth_session,
    launch_chrome,
    mark_shared_session_updated,
    prune_worker_profile_cache,
    setup_worker_profile,
)
from selfapply.apply.launcher import (
    _active_exclusions,
    _adapt_agent_prompt,
    _apply_tenant_key,
    _basic_profile_value,
    _build_agent_command,
    _choose_staged_upload,
    _clean_optional_url,
    _inter_job_delay,
    _extract_terminal_result,
    _is_known_application_url,
    _is_permanent_failure,
    _job_action_limits,
    _managed_playwright_mcp_pids,
    _mcp_startup_failure_message,
    _prepare_agent_job,
    _profile_select_candidates,
    _provider_limit_message,
    _resolved_resume_pdf,
    _verify_submission_confirmation,
    _workday_tenant_host,
    acquire_job,
    mark_result,
    release_orphaned_locks,
    release_stale_locks,
    worker_loop,
)
from selfapply.apply.prompt import (
    _build_location_check,
    _build_profile_summary,
    _build_screening_section,
    build_prompt,
)
from selfapply.cli import _normalize_extension_token
from selfapply.pipeline import _run_discover


class CodexAdapterTests(unittest.TestCase):
    def test_mcp_startup_failure_is_an_infrastructure_failure(self):
        message = (
            "Fatal error: required MCP servers failed to initialize: "
            "playwright: timed out handshaking with MCP server after 30s"
        )
        self.assertEqual(
            _mcp_startup_failure_message(message),
            "required mcp servers failed to initialize",
        )
        self.assertIsNone(_mcp_startup_failure_message("Job form unavailable"))

    def test_playwright_mcp_scan_is_scoped_to_one_worker_port(self):
        process_list = "\n".join([
            "101 npm exec @playwright/mcp@latest "
            "--cdp-endpoint=http://localhost:9222 --viewport-size=1280x900",
            "102 npm exec @playwright/mcp@latest "
            "--cdp-endpoint=http://localhost:9223 --viewport-size=1280x900",
            "103 npm exec unrelated-package --cdp-endpoint=http://localhost:9222",
        ])
        completed = __import__("subprocess").CompletedProcess(
            [], 0, process_list, "",
        )
        with (
            patch("selfapply.apply.launcher.platform.system", return_value="Darwin"),
            patch("selfapply.apply.launcher.subprocess.run", return_value=completed),
        ):
            self.assertEqual(_managed_playwright_mcp_pids(9222), [101])

    def test_provider_session_limit_is_not_treated_as_a_job_failure(self):
        self.assertEqual(
            _provider_limit_message(
                "You've hit your session limit · resets 6:20pm"
            ),
            "you've hit your session limit",
        )
        self.assertIsNone(_provider_limit_message("Application rate question"))

    def test_worker_preflight_requires_the_actual_upload_pdf(self):
        with TemporaryDirectory() as root:
            text_path = Path(root) / "resume.txt"
            text_path.write_text("resume", encoding="utf-8")
            self.assertIsNone(
                _resolved_resume_pdf({"tailored_resume_path": str(text_path)})
            )
            pdf_path = text_path.with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-test")
            self.assertEqual(
                _resolved_resume_pdf({"tailored_resume_path": str(text_path)}),
                pdf_path,
            )

    def test_terminal_result_parser_requires_an_exact_protocol_line(self):
        self.assertEqual(
            _extract_terminal_result("Done\n**RESULT:APPLIED**\nDetails"),
            "APPLIED",
        )
        self.assertEqual(
            _extract_terminal_result("RESULT:FAILED:validation_loop"),
            "FAILED:validation_loop",
        )
        self.assertIsNone(
            _extract_terminal_result("I might later output RESULT:APPLIED when done"),
        )

    def test_submission_verifier_rejects_a_form_with_validation_errors(self):
        page = Mock(url="https://example.test/apply")
        page.locator.return_value.inner_text.return_value = (
            "Submit application\nThis field is required"
        )
        context = Mock(pages=[page])
        browser = Mock(contexts=[context])
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            verified, evidence = _verify_submission_confirmation(9222)
        self.assertFalse(verified)
        self.assertIn("no browser submission confirmation", evidence)

    def test_submission_verifier_accepts_a_visible_receipt(self):
        page = Mock(url="https://example.test/application/confirmation")
        page.locator.return_value.inner_text.return_value = (
            "Thank you for applying. Your application has been submitted."
        )
        context = Mock(pages=[page])
        browser = Mock(contexts=[context])
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            verified, evidence = _verify_submission_confirmation(9222)
        self.assertTrue(verified)
        self.assertIn("application has been submitted", evidence)

    def test_discovery_can_skip_smart_extract_without_changing_default_sources(self):
        with (
            patch("selfapply.discovery.jobspy.run_discovery") as jobspy,
            patch("selfapply.discovery.workday.run_workday_discovery") as workday,
            patch("selfapply.discovery.smartextract.run_smart_extract") as smart,
        ):
            result = _run_discover(workers=2, smart_extract=False)
        jobspy.assert_called_once_with()
        workday.assert_called_once_with(workers=2)
        smart.assert_not_called()
        self.assertEqual(result["smartextract"], "skipped")

    def test_extension_token_accepts_full_assignment(self):
        self.assertEqual(
            _normalize_extension_token(
                "PLAYWRIGHT_MCP_EXTENSION_TOKEN=PLAYWRIGHT_MCP_EXTENSION_TOKEN=secret-token"
            ),
            "secret-token",
        )

    def test_textual_null_application_url_uses_source_url(self):
        job = {"url": "https://example.test/job/1", "application_url": "None"}
        prepared = _prepare_agent_job("codex", job)
        self.assertIsNone(prepared["application_url"])
        self.assertEqual(job["application_url"], "None")
        self.assertIsNone(_clean_optional_url(" null "))
        self.assertIsNone(_clean_optional_url("/redirect?id=123"))
        self.assertEqual(
            _clean_optional_url("https://example.test/apply"),
            "https://example.test/apply",
        )

    def test_profile_summary_includes_saved_restrictive_covenant_answer(self):
        profile = {
            "personal": {
                "full_name": "Applicant", "email": "a@example.test",
                "phone": "555", "city": "Portland",
            },
            "work_authorization": {
                "legally_authorized_to_work": True,
                "require_sponsorship": True,
            },
            "employment_disclosures": {"restrictive_covenant": False},
            "compensation": {"salary_expectation": "150000", "salary_currency": "USD"},
        }
        summary = _build_profile_summary(profile)
        self.assertIn("Subject to Non-compete/Restrictive Agreement: No", summary)

    def test_workday_login_circuit_breaker_is_tenant_scoped(self):
        self.assertEqual(
            _workday_tenant_host(
                "https://thomsonreuters.wd5.myworkdayjobs.com/en-US/jobs/123"
            ),
            "thomsonreuters.wd5.myworkdayjobs.com",
        )
        self.assertIsNone(
            _workday_tenant_host("https://www.linkedin.com/jobs/view/123")
        )

    def test_acquire_job_excludes_tenant_with_known_login_issue(self):
        connection = Mock()
        connection.execute.side_effect = [
            Mock(),
            Mock(fetchall=Mock(return_value=[{
                "url": "https://tenant.wd5.myworkdayjobs.com/en-US/job/1"
            }])),
            Mock(fetchone=Mock(return_value=None)),
        ]
        with (
            patch("selfapply.apply.launcher.get_connection", return_value=connection),
            patch("selfapply.apply.launcher._load_blocked", return_value=([], [])),
        ):
            self.assertIsNone(acquire_job())
        selection_sql, selection_params = connection.execute.call_args_list[2].args
        self.assertIn("lower(url) NOT LIKE", selection_sql)
        self.assertIn("AND apply_status IS NULL", selection_sql)
        self.assertNotIn("apply_status = 'failed'", selection_sql)
        self.assertEqual(selection_params.count("%tenant.wd5.myworkdayjobs.com%"), 2)

    def test_acquire_job_clears_stale_error_when_claimed(self):
        connection = Mock()
        selected = {
            "url": "https://example.test/job/1",
            "title": "Engineer",
            "site": "example",
            "application_url": "https://example.test/apply/1",
            "tailored_resume_path": "/tmp/resume.pdf",
            "fit_score": 9,
            "location": "Remote",
            "full_description": "Build software.",
            "cover_letter_path": None,
        }
        connection.execute.side_effect = [
            Mock(),
            Mock(fetchone=Mock(return_value=selected)),
            Mock(),
        ]
        with (
            patch("selfapply.apply.launcher.get_connection", return_value=connection),
            patch("selfapply.config.is_manual_ats", return_value=False),
        ):
            claimed = acquire_job(target_url=selected["url"])
        self.assertEqual(claimed["url"], selected["url"])
        selection_sql = connection.execute.call_args_list[1].args[0]
        self.assertIn("applied_at IS NULL", selection_sql)
        self.assertIn("COALESCE(apply_status, '') != 'applied'", selection_sql)
        claim_sql = connection.execute.call_args_list[2].args[0]
        self.assertIn("apply_error = NULL", claim_sql)

    def test_codex_command_has_browser_and_noninteractive_limits(self):
        with patch("pathlib.Path.exists", return_value=False):
            command = _build_agent_command("codex", "gpt-5.6-luna", 9222, Path("/tmp/mcp.json"))
        rendered = " ".join(command)
        self.assertIn("codex exec", rendered)
        self.assertIn('model_reasoning_effort="low"', rendered)
        self.assertNotIn('model_reasoning_effort="medium"', rendered)
        self.assertIn("approval_policy=\"never\"", rendered)
        self.assertIn("mcp_servers.playwright", rendered)
        self.assertIn("mcp_servers.playwright.startup_timeout_sec=90", rendered)
        self.assertIn("default_tools_approval_mode=\"approve\"", rendered)
        self.assertIn("--sandbox read-only", rendered)
        self.assertNotIn("mcp_servers.gmail", rendered)

    def test_existing_browser_uses_playwright_extension_not_cdp(self):
        with patch("pathlib.Path.exists", return_value=False):
            command = _build_agent_command(
                "codex", "gpt-5.6-luna", 9222, Path("/tmp/mcp.json"),
                browser_mode="existing", mcp_cwd=Path("/tmp/apply-worker-0"),
            )
        rendered = " ".join(command)
        self.assertIn("--extension", rendered)
        self.assertNotIn("--cdp-endpoint", rendered)
        self.assertIn("PLAYWRIGHT_MCP_EXTENSION_TOKEN", rendered)
        self.assertIn('mcp_servers.playwright.cwd="/tmp/apply-worker-0"', rendered)

    def test_codex_command_can_reuse_shared_playwright_runtime(self):
        with patch("pathlib.Path.exists", return_value=False):
            command = _build_agent_command(
                "codex", None, 9222, Path("/tmp/mcp.json"),
                playwright_mcp_command="/tmp/runtime/playwright-mcp",
            )
        rendered = " ".join(command)
        self.assertIn(
            'mcp_servers.playwright.command="/tmp/runtime/playwright-mcp"',
            rendered,
        )
        self.assertNotIn("@playwright/mcp", rendered)

    def test_gmail_is_restricted_to_search_and_read(self):
        with patch("pathlib.Path.exists", return_value=True):
            command = _build_agent_command("codex", None, 9222, Path("/tmp/mcp.json"))
        rendered = " ".join(command)
        self.assertIn("search_emails", rendered)
        self.assertIn("read_email", rendered)
        self.assertNotIn("send_email", rendered)
        self.assertNotIn("modify_email", rendered)

    def test_claude_mcp_config_disables_interactive_npx_install_prompt(self):
        from selfapply.apply.launcher import _make_mcp_config

        args = _make_mcp_config(9222)["mcpServers"]["playwright"]["args"]
        self.assertEqual(args[:2], ["-y", "@playwright/mcp@latest"])

    def test_codex_prompt_preserves_submit_boundary(self):
        dry = _adapt_agent_prompt("codex", "base prompt", dry_run=True)
        live = _adapt_agent_prompt("codex", "base prompt", dry_run=False)
        self.assertIn("Final submission is NOT authorized", dry)
        self.assertIn("RESULT:DRY_RUN_OK", dry)
        self.assertIn("Do not output RESULT:APPLIED", dry)
        self.assertIn("authorized final submission", live)
        self.assertIn("Reject talent networks", live)
        self.assertIn("untrusted content", live)
        self.assertIn("invisible reCAPTCHA v3", live)
        self.assertIn("visible interactive challenge", live)
        self.assertIn("React/ARIA combobox", live)
        self.assertIn('[id="605"]', live)
        self.assertIn("Never pass a relative href", live)
        self.assertIn("Google, LinkedIn, or Indeed", live)
        self.assertIn("already signed in", live)
        self.assertIn("saved job-site password", live)
        self.assertIn("create exactly one portal/tenant account", live)
        self.assertIn("read-only Gmail", live)
        self.assertIn("email-first Begin submission page", live)
        self.assertIn("Every browser_fill_form value MUST be a string", live)
        self.assertIn("treat all previous element refs as invalid", live)
        self.assertIn("never loop reload/upload attempts", live)

    def test_captcha_waiting_marker_triggers_prominent_alert(self):
        from selfapply.apply.launcher import _CAPTCHA_WAITING_RE, _alert_captcha_waiting
        match = _CAPTCHA_WAITING_RE.search("blah CAPTCHA_WAITING:recaptchav2 blah")
        self.assertEqual(match.group(1), "recaptchav2")
        with patch("selfapply.apply.launcher.add_event") as add_event:
            _alert_captcha_waiting(2, "recaptchav2")
        add_event.assert_called_once()
        message = add_event.call_args.args[0]
        self.assertIn("[ALERT]", message)
        self.assertIn("W2", message)
        self.assertIn("recaptchav2", message)
        self.assertIn("YOU to click the checkbox", message)
        self.assertIn("will not do this itself", message)

    def test_captcha_manual_fallback_never_authorizes_clicking(self):
        from selfapply.apply.prompt import _build_captcha_section
        section = _build_captcha_section()
        self.assertIn("Never click, solve, or bypass a visible interactive CAPTCHA", section)
        self.assertIn("reCAPTCHA \"I'm not a robot\" checkbox", section)
        self.assertIn("CAPTCHA_WAITING:", section)
        self.assertIn("Do not wait, refresh, retry, or interact", section)
        self.assertIn("worker proceeds", section)
        self.assertNotIn("browser_click the reCAPTCHA checkbox", section)

    def test_codex_adapter_skips_visible_captcha_without_waiting(self):
        live = _adapt_agent_prompt("codex", "base prompt", dry_run=False)
        self.assertIn("Do not wait for human attention", live)
        self.assertIn("Immediately output RESULT:CAPTCHA", live)
        self.assertNotIn("Wait up to 120 seconds", live)

    def test_codex_adapter_login_override_mentions_password_recovery_once_per_job(self):
        live = _adapt_agent_prompt("codex", "base prompt", dry_run=False)
        self.assertIn("forgot-password flow", live)
        self.assertIn("account creation fails because the email is already registered", live)
        self.assertIn("never loop any of them", live)

    def test_existing_browser_prompt_reuses_selected_tab(self):
        prompt = _adapt_agent_prompt(
            "codex", "base prompt", dry_run=False, browser_mode="existing",
        )
        self.assertIn("existing Chrome tab", prompt)
        self.assertIn("do not create another tab", prompt)

    def test_managed_browser_prompt_uses_mcp_upload_with_watcher_fallback(self):
        prompt = _adapt_agent_prompt(
            "codex", "base prompt", dry_run=False, browser_mode="managed",
        )
        self.assertIn("immediately call browser_file_upload", prompt)
        self.assertIn("direct-input watcher as a fallback", prompt)
        self.assertIn("Never wait on an open chooser", prompt)
        self.assertNotIn("NEVER call browser_file_upload", prompt)
        self.assertNotIn("CODEX MANAGED-BROWSER OVERRIDE", prompt)
        self.assertIn("never close the page, tab, or browser", prompt)

    def test_managed_upload_selection_restricts_generic_controls_to_known_ats(self):
        resume = Path("/tmp/Applicant_Resume.pdf")
        cover = Path("/tmp/Applicant_Cover_Letter.pdf")
        self.assertEqual(_choose_staged_upload("resume upload", 0, resume, cover), resume)
        self.assertEqual(_choose_staged_upload("cover letter", 0, resume, cover), cover)
        self.assertIsNone(_choose_staged_upload("application attachment", 0, resume, cover))
        self.assertEqual(
            _choose_staged_upload(
                "application attachment", 0, resume, cover, allow_generic=True,
            ),
            resume,
        )
        self.assertEqual(
            _choose_staged_upload(
                "application attachment", 1, resume, cover, allow_generic=True,
            ),
            cover,
        )
        self.assertIsNone(_choose_staged_upload("profile photo image/png", 0, resume, cover))

    def test_managed_prefill_uses_only_verified_basic_profile_fields(self):
        profile = {"personal": {
            "full_name": "Example Applicant",
            "email": "applicant@example.test",
            "phone": "555-0100",
            "address": "123 Main Street",
            "city": "Portland",
            "province_state": "Oregon",
            "country": "United States",
            "postal_code": "97205",
            "linkedin_url": "https://linkedin.com/in/example",
        }}
        self.assertEqual(
            _basic_profile_value("email address", "email", profile),
            "applicant@example.test",
        )
        self.assertEqual(
            _basic_profile_value("applicant first name", "text", profile),
            "Example",
        )
        self.assertEqual(
            _basic_profile_value("applicant last name", "text", profile),
            "Applicant",
        )
        self.assertEqual(
            _basic_profile_value("autocomplete given-name", "text", profile),
            "Example",
        )
        self.assertEqual(
            _basic_profile_value("autocomplete address-level2", "text", profile),
            "Portland",
        )
        self.assertEqual(
            _basic_profile_value("postal zip code", "text", profile), "97205",
        )
        self.assertIsNone(
            _basic_profile_value("reference email", "email", profile),
        )
        self.assertIsNone(
            _basic_profile_value("account password", "password", profile),
        )

    def test_google_is_never_treated_as_an_application_page(self):
        self.assertFalse(_is_known_application_url("https://www.google.com/"))
        self.assertFalse(_is_known_application_url("chrome://newtab/"))
        self.assertTrue(_is_known_application_url("https://jobs.ashbyhq.com/acme/123"))
        self.assertTrue(_is_known_application_url("https://grnh.se/example"))
        self.assertTrue(_is_known_application_url(
            "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/123",
        ))
        self.assertTrue(_is_known_application_url(
            "https://www.linkedin.com/jobs/view/123",
        ))
        self.assertTrue(_is_known_application_url(
            "https://recruit.hirebridge.com/v3/applicationv2/QuickApply.aspx",
        ))
        self.assertTrue(_is_known_application_url(
            "https://brightvisiontechnologies.applytojob.com/apply/example",
        ))

    def test_managed_prefill_maps_saved_profile_to_native_dropdowns(self):
        profile = {
            "personal": {"country": "United States", "province_state": "Oregon"},
            "work_authorization": {"work_permit_type": "Permanent Resident"},
            "_selfapply_source_site": "indeed",
            "eeo_voluntary": {
                "gender": "Woman",
                "race_ethnicity": "White",
                "sexual_orientation": "Decline to self-identify",
                "veteran_status": "Decline to self-identify",
                "disability_status": "Decline to self-identify",
            },
        }
        self.assertEqual(
            _profile_select_candidates("Gender Identity", profile), ["Woman"],
        )
        self.assertIn(
            "White", _profile_select_candidates("Race/Ethnicity", profile),
        )
        self.assertIn(
            "Decline to self-identify", _profile_select_candidates("Veteran Status", profile),
        )
        self.assertEqual(
            _profile_select_candidates("State / Province", profile),
            ["Oregon"],
        )
        self.assertEqual(
            _profile_select_candidates("Reference gender", profile), [],
        )
        self.assertEqual(
            _profile_select_candidates("How did you hear about us?", profile),
            ["Indeed", "Job Board", "Internet"],
        )
        self.assertEqual(
            _profile_select_candidates(
                "What is your current immigration status in the United States?",
                profile,
            )[0],
            "Permanent Resident",
        )
        self.assertEqual(
            _profile_select_candidates(
                "Are you living in the United States at present?", profile,
            ),
            ["Yes"],
        )

    def test_managed_prefill_splits_full_address_for_street_field(self):
        profile = {"personal": {
            "address": "100 Example Avenue Portland Oregon 97205",
            "city": "Portland",
            "province_state": "Oregon",
            "postal_code": "97205",
        }}
        self.assertEqual(
            _basic_profile_value("Street Address *", "text", profile),
            "100 Example Avenue",
        )

    def test_limits_are_treated_as_transient_not_permanent(self):
        self.assertFalse(_is_permanent_failure("failed:action_limit"))
        self.assertFalse(_is_permanent_failure("failed:time_limit"))

    def test_hirebridge_gets_a_bounded_postback_budget(self):
        job = {"application_url": (
            "https://recruit.hirebridge.com/v3/applicationv2/QuickApply.aspx?cid=1"
        )}
        self.assertEqual(_job_action_limits(job, 45, 360), (160, 720))
        self.assertEqual(
            _job_action_limits({
                "application_url": "https://acme.wd5.myworkdayjobs.com/job/123",
            }, 45, 360),
            (160, 720),
        )
        self.assertEqual(
            _job_action_limits({"url": "https://jobs.ashbyhq.com/acme/1"}, 45, 360),
            (100, 600),
        )

    def test_captcha_and_login_issue_remain_permanent(self):
        for reason in ("captcha", "login_issue", "cloudflare_blocked"):
            self.assertTrue(_is_permanent_failure(f"failed:{reason}"))

    def test_work_authorization_mismatch_is_permanent(self):
        self.assertTrue(_is_permanent_failure("failed:not_eligible_work_auth"))

    def test_worker_checks_supervisor_cap_before_acquiring_another_job(self):
        try:
            with patch("selfapply.apply.launcher.acquire_job") as acquire:
                applied, failed = worker_loop(limit=0, stop_condition=lambda: True)
            self.assertEqual((applied, failed), (0, 0))
            acquire.assert_not_called()
        finally:
            from selfapply.apply import launcher
            launcher._stop_event.clear()

    def test_mcp_outage_isolated_to_worker_and_returns_job_to_ready(self):
        job = {
            "url": "https://example.test/job/1", "title": "Engineer",
            "site": "test", "fit_score": 8,
        }
        try:
            with (
                patch("selfapply.apply.launcher.acquire_job", return_value=job),
                patch(
                    "selfapply.apply.launcher.run_job",
                    return_value=("infrastructure_unavailable", 30_000),
                ),
                patch("selfapply.apply.launcher.release_lock") as release,
                patch("selfapply.apply.launcher.mark_result") as mark,
                patch("selfapply.apply.launcher.update_state"),
                patch("selfapply.apply.launcher.add_event"),
                patch.object(
                    __import__("selfapply.apply.launcher", fromlist=["_stop_event"])._stop_event,
                    "wait", return_value=True,
                ),
            ):
                self.assertEqual(
                    worker_loop(limit=1, browser_mode="existing"), (0, 0),
                )
            release.assert_called_once_with(job["url"])
            mark.assert_not_called()
            from selfapply.apply import launcher
            self.assertFalse(launcher._stop_event.is_set())
        finally:
            from selfapply.apply import launcher

            launcher._stop_event.clear()

    def test_managed_codex_workers_have_staggered_startup(self):
        from selfapply.apply.launcher import _worker_start_delay

        self.assertEqual(_worker_start_delay(0, 4, "codex", "managed"), 0)
        self.assertEqual(_worker_start_delay(1, 4, "codex", "managed"), 8)
        self.assertEqual(_worker_start_delay(3, 4, "codex", "managed"), 24)
        self.assertEqual(_worker_start_delay(3, 4, "claude", "managed"), 0)
        self.assertEqual(_worker_start_delay(3, 4, "codex", "existing"), 0)

    def test_active_exclusions_expire_after_backoff_window(self):
        seen = {"recent": 95.0, "old": 10.0}
        self.assertEqual(_active_exclusions(seen, 20.0, 100.0), {"recent"})
        self.assertEqual(_active_exclusions(seen, 0.0, 100.0), set(seen))

    def test_apply_tenant_key_groups_shared_ats_hosts_by_company_slug(self):
        self.assertNotEqual(
            _apply_tenant_key("https://boards.greenhouse.io/acme/jobs/1"),
            _apply_tenant_key("https://boards.greenhouse.io/other/jobs/2"),
        )
        self.assertEqual(
            _apply_tenant_key("https://acme.wd5.myworkdayjobs.com/job/1"),
            "acme.wd5.myworkdayjobs.com",
        )

    def test_job_aggregators_are_not_treated_as_one_employer(self):
        self.assertIsNone(_apply_tenant_key("https://www.linkedin.com/jobs/view/123"))
        self.assertIsNone(_apply_tenant_key("https://www.indeed.com/viewjob?jk=abc"))
        self.assertIsNone(_apply_tenant_key("https://www.ziprecruiter.com/jobs/abc"))

    def test_tenant_application_count_only_counts_within_window(self):
        from selfapply.apply.launcher import _tenant_application_count
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [
            {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "application_url": None},
            {"url": "https://acme.wd5.myworkdayjobs.com/job/2", "application_url": None},
            {"url": "https://other.wd5.myworkdayjobs.com/job/3", "application_url": None},
        ]
        with patch("selfapply.apply.launcher.get_connection", return_value=connection):
            count = _tenant_application_count("acme.wd5.myworkdayjobs.com", 604800)
        self.assertEqual(count, 2)
        sql, params = connection.execute.call_args.args
        self.assertIn("apply_status = 'applied'", sql)
        self.assertIn("applied_at >= ?", sql)

    def test_tenant_application_count_disabled_when_window_is_zero(self):
        from selfapply.apply.launcher import _tenant_application_count
        with patch("selfapply.apply.launcher.get_connection") as get_conn:
            self.assertEqual(_tenant_application_count("acme.com", 0), 0)
        get_conn.assert_not_called()

    def test_worker_loop_defers_job_when_employer_cap_reached(self):
        job = {
            "url": "https://acme.wd5.myworkdayjobs.com/job/1",
            "application_url": None,
            "title": "Engineer",
        }
        with (
            patch("selfapply.apply.launcher.acquire_job", side_effect=[job, None]),
            patch("selfapply.apply.launcher.release_lock"),
            patch("selfapply.apply.launcher._tenant_in_cooldown", return_value=False),
            patch("selfapply.apply.launcher._tenant_application_count", return_value=3),
            patch("selfapply.apply.launcher._mark_tenant_attempted") as mark,
            patch("selfapply.apply.launcher.update_state"),
            patch("selfapply.apply.launcher.add_event"),
        ):
            applied, failed = worker_loop(
                worker_id=0, limit=1, max_per_employer=3, employer_window=604800,
            )
        self.assertEqual((applied, failed), (0, 0))
        mark.assert_not_called()

    def test_inter_job_delay_respects_bounds_and_disables_at_zero(self):
        self.assertEqual(_inter_job_delay(10, 0), 0.0)
        with patch("selfapply.apply.launcher.random.uniform", return_value=17.5) as uniform:
            self.assertEqual(_inter_job_delay(15, 45), 17.5)
        uniform.assert_called_once_with(15, 45)

    def test_prepare_worker_page_closes_stale_tabs_and_navigates_keeper(self):
        from selfapply.apply.launcher import _prepare_worker_page
        keeper = Mock()
        keeper.url = "https://old.example/confirmation"
        stale = Mock()
        stale.url = "https://older.example/job"
        context = Mock()
        context.pages = [keeper, stale]
        browser = Mock()
        browser.contexts = [context]
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        with (
            patch("selfapply.apply.launcher._cdp_is_ready", return_value=True),
            patch("playwright.sync_api.sync_playwright", return_value=manager),
        ):
            _prepare_worker_page(9222, "https://next.example/job")
        stale.close.assert_called_once_with()
        keeper.goto.assert_called_once_with(
            "https://next.example/job", wait_until="domcontentloaded", timeout=30_000,
        )
        keeper.bring_to_front.assert_called_once_with()

    def test_claude_command_remains_available(self):
        command = _build_agent_command("claude", "haiku", 9222, Path("/tmp/mcp.json"))
        self.assertEqual(command[:4], ["claude", "--model", "haiku", "-p"])
        self.assertIn("--effort", command)
        self.assertEqual(command[command.index("--effort") + 1], "low")
        self.assertIn("bypassPermissions", command)

    def test_stale_locks_are_released_with_a_bounded_age(self):
        connection = Mock()
        connection.execute.return_value.rowcount = 2
        with patch("selfapply.apply.launcher.get_connection", return_value=connection):
            self.assertEqual(release_stale_locks(30), 2)
        sql, params = connection.execute.call_args.args
        self.assertIn("julianday", sql)
        self.assertIn("applied_at IS NULL", sql)
        self.assertEqual(params, ("-30 minutes",))
        connection.commit.assert_called_once()

    def test_orphaned_pid_lock_is_released_immediately(self):
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=[
                {"url": "https://example.test/job", "agent_id": "worker-0:pid=12345"}
            ])),
            Mock(rowcount=1),
        ]
        with (
            patch("selfapply.apply.launcher.get_connection", return_value=connection),
            patch("selfapply.apply.launcher._pid_is_alive", return_value=False),
        ):
            self.assertEqual(release_orphaned_locks(), 1)
        release_sql = connection.execute.call_args_list[1].args[0]
        self.assertIn("applied_at IS NULL", release_sql)
        connection.commit.assert_called_once()

    def test_process_tree_cleanup_never_kills_own_process_group(self):
        with (
            patch("selfapply.apply.chrome.platform.system", return_value="Darwin"),
            patch("os.getpgid", return_value=777),
            patch("os.getpgrp", return_value=777),
            patch("os.killpg") as kill_group,
            patch("os.kill") as kill_process,
        ):
            _kill_process_tree(12345)
        kill_group.assert_not_called()
        kill_process.assert_called_once()

    def test_atexit_cleanup_does_not_sweep_an_unowned_worker_port(self):
        with (
            patch("selfapply.apply.chrome._chrome_procs", {}),
            patch("selfapply.apply.chrome._kill_on_port") as kill_port,
        ):
            cleanup_on_exit()
        kill_port.assert_not_called()

    def test_managed_chrome_starts_at_selected_job_without_fake_media_flags(self):
        process = Mock(pid=4321)
        process.poll.return_value = None
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with (
            patch("selfapply.apply.chrome.setup_worker_profile", return_value=Path("/tmp/profile")),
            patch("selfapply.apply.chrome._kill_on_port"),
            patch("selfapply.apply.chrome._suppress_restore_nag"),
            patch("selfapply.apply.chrome.config.get_chrome_path", return_value="chrome"),
            patch("selfapply.apply.chrome.subprocess.Popen", return_value=process) as popen,
            patch("selfapply.apply.chrome.urllib.request.urlopen", return_value=response),
        ):
            self.assertIs(
                launch_chrome(
                    0, port=9222, start_url="https://jobs.example.test/apply/7",
                ),
                process,
            )
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "https://jobs.example.test/apply/7")
        self.assertNotIn("--use-fake-device-for-media-stream", command)
        self.assertNotIn("--use-fake-ui-for-media-stream", command)
        self.assertIn("--disk-cache-size=52428800", command)
        self.assertIn("--media-cache-size=10485760", command)

    def test_cache_pruning_keeps_authentication_state(self):
        with TemporaryDirectory() as root:
            profile = Path(root)
            for path in (
                profile / "Profile 1" / "Cache",
                profile / "Default" / "Cache",
                profile / "Default" / "Code Cache",
                profile / "Default" / "Service Worker" / "CacheStorage",
                profile / "Default" / "Local Storage",
                profile / "Default" / "Extensions",
            ):
                path.mkdir(parents=True)
                (path / "data").write_text("x")
            (profile / "Default" / "Cookies").write_text("session")
            self.assertGreater(prune_worker_profile_cache(profile), 0)
            self.assertFalse((profile / "Profile 1").exists())
            self.assertFalse((profile / "Default" / "Cache").exists())
            self.assertFalse((profile / "Default" / "Code Cache").exists())
            self.assertTrue((profile / "Default" / "Cookies").exists())
            self.assertTrue((profile / "Default" / "Local Storage").exists())
            self.assertTrue((profile / "Default" / "Extensions").exists())

    def test_master_resume_mode_resolves_the_uploaded_master_pdf(self):
        with TemporaryDirectory() as root:
            master = Path(root) / "resume.pdf"
            master.write_bytes(b"master")
            with (
                patch(
                    "selfapply.apply.launcher.config.application_resume_mode",
                    return_value="master",
                ),
                patch("selfapply.apply.launcher.config.RESUME_PDF_PATH", master),
            ):
                from selfapply.apply.launcher import _resolved_resume_pdf
                self.assertEqual(
                    _resolved_resume_pdf({
                        "tailored_resume_path": "/missing/tailored.txt",
                    }),
                    master,
                )

    def test_google_session_requires_auth_cookie_not_remembered_account(self):
        with TemporaryDirectory() as root:
            profile = Path(root)
            cookie_db = profile / "Default" / "Cookies"
            cookie_db.parent.mkdir(parents=True)
            connection = sqlite3.connect(cookie_db)
            connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
            connection.execute(
                "INSERT INTO cookies VALUES (?, ?)",
                ("accounts.google.com", "ACCOUNT_CHOOSER"),
            )
            connection.commit()
            connection.close()
            self.assertFalse(has_google_auth_session(profile))
            connection = sqlite3.connect(cookie_db)
            connection.execute(
                "INSERT INTO cookies VALUES (?, ?)", (".google.com", "SID"),
            )
            connection.commit()
            connection.close()
            self.assertTrue(has_google_auth_session(profile))

    def test_existing_secondary_worker_refreshes_shared_sign_in_state(self):
        with TemporaryDirectory() as root:
            workers = Path(root)
            primary = workers / "worker-0"
            secondary = workers / "worker-1"
            (primary / "Default").mkdir(parents=True)
            (secondary / "Default").mkdir(parents=True)
            (primary / "Default" / "Cookies").write_text("new-google-session")
            (secondary / "Default" / "Cookies").write_text("stale-session")
            with patch("selfapply.apply.chrome.config.CHROME_WORKER_DIR", workers):
                mark_shared_session_updated()
                self.assertEqual(setup_worker_profile(1), secondary)
            self.assertEqual(
                (secondary / "Default" / "Cookies").read_text(),
                "new-google-session",
            )

    def test_worker_cleanup_lets_chrome_flush_before_forced_cleanup(self):
        process = Mock(pid=4321)
        process.poll.return_value = None
        with patch("selfapply.apply.chrome._kill_process_tree") as hard_kill:
            cleanup_worker(0, process)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=8)
        hard_kill.assert_not_called()

    def test_applied_result_archives_the_exact_job_resume(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "archive.db"
            archive_dir = Path(root) / "applied"
            resume = Path(root) / "tailored.pdf"
            resume.write_bytes(b"job-specific-pdf")
            connection = database.init_db(db_path)
            url = "https://example.test/jobs/42"
            connection.execute(
                "INSERT INTO jobs (url,title,tailored_resume_path) VALUES (?,?,?)",
                (url, "Platform Engineer", str(resume)),
            )
            connection.commit()
            with (
                patch("selfapply.apply.launcher.get_connection", return_value=connection),
                patch("selfapply.apply.launcher.config.APPLIED_RESUME_DIR", archive_dir),
            ):
                mark_result(url, "applied")
            row = connection.execute(
                "SELECT apply_status,applied_resume_path FROM jobs WHERE url=?", (url,),
            ).fetchone()
            self.assertEqual(row["apply_status"], "applied")
            archived = Path(row["applied_resume_path"])
            self.assertEqual(archived.parent, archive_dir)
            self.assertEqual(archived.read_bytes(), b"job-specific-pdf")
            database.close_connection(db_path)

    def test_exact_indeed_retry_never_selects_first_indeed_row(self):
        with TemporaryDirectory() as root:
            db_path = Path(root) / "exact-retry.db"
            connection = database.init_db(db_path)
            first = "https://www.indeed.com/viewjob?jk=first"
            selected = "https://www.indeed.com/viewjob?jk=selected"
            connection.executemany(
                """INSERT INTO jobs
                   (url,title,site,tailored_resume_path,fit_score)
                   VALUES (?,?,?,?,?)""",
                [
                    (first, "First job", "indeed", "/tmp/first.txt", 10),
                    (selected, "Selected job", "indeed", "/tmp/selected.txt", 7),
                ],
            )
            connection.commit()
            with patch(
                "selfapply.apply.launcher.get_connection", return_value=connection,
            ):
                job = acquire_job(target_url=selected, worker_id=0)
            self.assertEqual(job["url"], selected)
            first_state = connection.execute(
                "SELECT apply_status FROM jobs WHERE url=?", (first,),
            ).fetchone()[0]
            self.assertIsNone(first_state)
            database.close_connection(db_path)

    def test_location_check_infers_wizard_search_locations(self):
        profile = {"personal": {"city": "Portland"}}
        search_config = {
            "locations": [{"location": "('Oregon, OR', 'Portland Metro')"}],
            "defaults": {"location": "('Oregon, OR', 'Portland Metro')"},
        }
        prompt = _build_location_check(profile, search_config)
        self.assertIn("Portland, Oregon, OR, Portland Metro", prompt)
        self.assertIn("Fully remote and explicitly open to the candidate's country", prompt)
        self.assertIn("employer headquarters", prompt)

    def test_eeo_answers_come_from_profile_without_blank_required_fields(self):
        profile = {
            "personal": {
                "full_name": "Example Applicant",
                "email": "applicant@example.test",
                "phone": "555-0100",
                "city": "Portland",
            },
            "work_authorization": {
                "legally_authorized_to_work": "Yes",
                "require_sponsorship": "No",
            },
            "compensation": {"salary_expectation": "100000"},
            "experience": {"target_role": "Software Engineer"},
            "eeo_voluntary": {
                "gender": "Woman",
                "race_ethnicity": "White",
                "sexual_orientation": "Decline to self-identify",
                "veteran_status": "Decline to self-identify",
                "disability_status": "Decline to self-identify",
            },
        }
        summary = _build_profile_summary(profile)
        screening = _build_screening_section(profile)
        self.assertIn("Gender: Woman", summary)
        self.assertIn("Race: White", summary)
        self.assertIn("Sexual Orientation: Decline to self-identify", summary)
        self.assertIn("veteran status=Decline to self-identify", screening)
        self.assertIn("Never leave a required demographic dropdown blank", screening)
        self.assertIn("same-domain similarity is NOT sufficient evidence", screening)
        self.assertIn("generic AWS or cloud experience alone is not enough", screening)
        self.assertNotIn("Software engineers learn tools fast", screening)

    def test_application_prompt_gates_every_intermediate_page(self):
        profile = {
            "personal": {
                "full_name": "Example Applicant", "email": "a@example.test",
                "phone": "2025550147", "city": "Portland", "province_state": "Oregon",
                "country": "United States", "postal_code": "97205", "address": "1 Main",
                "password": "saved-password",
            },
            "work_authorization": {
                "legally_authorized_to_work": True, "require_sponsorship": True,
                "work_permit_type": "Permanent Resident",
            },
            "compensation": {
                "salary_expectation": "100000", "minimum_salary": "90000",
                "salary_currency": "USD",
            },
            "experience": {}, "availability": {"earliest_start_date": "Immediately"},
            "eeo_voluntary": {},
        }
        with TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"%PDF-test")
            with (
                patch("selfapply.apply.prompt.config.load_profile", return_value=profile),
                patch("selfapply.apply.prompt.config.load_search_config", return_value={}),
                patch("selfapply.config.load_blocked_sso", return_value=[]),
            ):
                prompt = build_prompt(
                    {
                        "url": "https://example.test/job", "title": "Engineer",
                        "site": "test", "fit_score": 8,
                        "tailored_resume_path": str(resume),
                    },
                    tailored_resume="Example resume",
                    upload_dir=Path(directory) / "uploads",
                )
        self.assertIn("REQUIRED-FIELD GATE (RUN BEFORE EVERY PAGE-ADVANCE ACTION)", prompt)
        self.assertIn("Do not advance", prompt)
        self.assertIn("newly revealed required fields", prompt)
        self.assertIn("Validation errors after ANY Next/Continue/Review/Submit", prompt)
        self.assertIn("Embedded form/EEO iframe present?", prompt)
        self.assertIn("RESULT:FAILED:missing_profile_answer", prompt)
        self.assertIn("a visible selected label such as Yes/No is authoritative", prompt)
        self.assertIn("Never keep clearing or reselecting an already-filled Greenhouse combobox", prompt)
        self.assertIn("RESULT:FAILED:validation_loop", prompt)
        self.assertIn("Narration, a completed-looking form, or clicking Submit is not proof", prompt)
        self.assertIn('FIRST: look for "Continue with Google"', prompt)
        self.assertIn("Only when no supported existing-session SSO option is offered", prompt)
        self.assertNotIn("any SSO/OAuth page -> STOP", prompt)

    def test_email_verification_can_use_consent_gated_gmail_browser_fallback(self):
        profile = {
            "personal": {
                "full_name": "Example Applicant", "email": "a@example.test",
                "phone": "2025550147", "city": "Portland", "province_state": "Oregon",
                "country": "United States", "postal_code": "97205", "address": "1 Main",
                "password": "saved-password", "allow_gmail_verification_read": True,
            },
            "work_authorization": {
                "legally_authorized_to_work": True, "require_sponsorship": True,
            },
            "compensation": {"salary_expectation": "100000"},
            "experience": {}, "availability": {},
            "eeo_voluntary": {},
        }
        with TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"%PDF-test")
            with (
                patch("selfapply.apply.prompt.config.load_profile", return_value=profile),
                patch("selfapply.apply.prompt.config.load_search_config", return_value={}),
                patch("selfapply.config.load_blocked_sso", return_value=[]),
            ):
                prompt = build_prompt(
                    {"url": "https://example.test/job", "title": "Engineer",
                     "site": "test", "fit_score": 8,
                     "tailored_resume_path": str(resume)},
                    tailored_resume="Example resume",
                    upload_dir=Path(directory) / "uploads",
                )
        self.assertIn("open a NEW browser", prompt)
        self.assertIn("tab to https://mail.google.com/", prompt)
        self.assertIn("Search only for the newest message", prompt)
        self.assertIn("Never open unrelated messages", prompt)
        self.assertIn("close the Gmail", prompt)
        self.assertIn("tab. Never open unrelated messages", prompt)

    def test_prompt_includes_forgot_password_step_with_trigger_condition(self):
        profile = {
            "personal": {
                "full_name": "Example Applicant", "email": "a@example.test",
                "phone": "2025550147", "city": "Portland", "province_state": "Oregon",
                "country": "United States", "postal_code": "97205", "address": "1 Main",
                "password": "saved-password",
            },
            "work_authorization": {
                "legally_authorized_to_work": True, "require_sponsorship": True,
                "work_permit_type": "Permanent Resident",
            },
            "compensation": {
                "salary_expectation": "100000", "minimum_salary": "90000",
                "salary_currency": "USD",
            },
            "experience": {}, "availability": {"earliest_start_date": "Immediately"},
            "eeo_voluntary": {},
        }
        with TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.pdf"
            resume.write_bytes(b"%PDF-test")
            with (
                patch("selfapply.apply.prompt.config.load_profile", return_value=profile),
                patch("selfapply.apply.prompt.config.load_search_config", return_value={}),
                patch("selfapply.config.load_blocked_sso", return_value=[]),
            ):
                prompt = build_prompt(
                    {
                        "url": "https://example.test/job", "title": "Engineer",
                        "site": "test", "fit_score": 8,
                        "tailored_resume_path": str(resume),
                    },
                    tailored_resume="Example resume",
                    upload_dir=Path(directory) / "uploads",
                )
        self.assertIn("account already exists", prompt)
        self.assertIn("Forgot password", prompt)
        self.assertIn("authorized email-verification procedure", prompt)
        self.assertIn("No Gmail-read consent is saved", prompt)
        self.assertIn("Attempt this once per job", prompt)

    def test_multiple_dry_run_jobs_are_released_not_marked_applied(self):
        jobs = [
            {"url": "https://example.test/job/1", "title": "Job One", "site": "test", "fit_score": 8},
            {"url": "https://example.test/job/2", "title": "Job Two", "site": "test", "fit_score": 8},
        ]
        with (
            patch("selfapply.apply.launcher.acquire_job", side_effect=jobs) as acquire,
            patch("selfapply.apply.launcher.run_job", side_effect=[("applied", 100), ("applied", 200)]),
            patch("selfapply.apply.launcher.release_lock") as release,
            patch("selfapply.apply.launcher.mark_result") as mark,
            patch("selfapply.apply.launcher.launch_chrome") as launch,
            patch("selfapply.apply.launcher.update_state"),
            patch("selfapply.apply.launcher.add_event"),
        ):
            result = worker_loop(
                limit=2, dry_run=True, agent="codex", browser_mode="existing",
            )
        self.assertEqual(result, (2, 0))
        self.assertEqual(acquire.call_count, 2)
        self.assertEqual(acquire.call_args_list[0].kwargs["excluded_urls"], set())
        self.assertEqual(
            acquire.call_args_list[1].kwargs["excluded_urls"],
            {"https://example.test/job/1"},
        )
        self.assertEqual(release.call_count, 2)
        mark.assert_not_called()
        launch.assert_not_called()

    def test_managed_worker_reuses_one_chrome_for_multiple_jobs(self):
        jobs = [
            {"url": "https://example.test/job/1", "title": "Job One", "site": "test", "fit_score": 8},
            {"url": "https://example.test/job/2", "title": "Job Two", "site": "test", "fit_score": 8},
        ]
        chrome_process = Mock()
        chrome_process.poll.return_value = None
        with (
            patch("selfapply.apply.launcher.acquire_job", side_effect=jobs),
            patch("selfapply.apply.launcher.run_job", side_effect=[("applied", 100), ("applied", 200)]),
            patch("selfapply.apply.launcher.mark_result"),
            patch("selfapply.apply.launcher.launch_chrome", return_value=chrome_process) as launch,
            patch("selfapply.apply.launcher.cleanup_worker") as cleanup,
            patch("selfapply.apply.launcher.update_state"),
            patch("selfapply.apply.launcher.add_event"),
        ):
            self.assertEqual(
                worker_loop(limit=2, agent="codex", browser_mode="managed"),
                (2, 0),
            )
        launch.assert_called_once()
        self.assertEqual(
            launch.call_args.kwargs["start_url"], "https://example.test/job/1",
        )
        cleanup.assert_called_once_with(0, chrome_process)

    def test_worker_loop_defers_job_whose_tenant_is_cooling_down(self):
        jobs = [
            {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "title": "One",
             "site": "workday", "fit_score": 8},
            {"url": "https://acme.wd5.myworkdayjobs.com/job/2", "title": "Two",
             "site": "workday", "fit_score": 8},
            None,
        ]
        with (
            patch("selfapply.apply.launcher.acquire_job", side_effect=jobs) as acquire,
            patch("selfapply.apply.launcher.run_job", return_value=("applied", 100)),
            patch("selfapply.apply.launcher.mark_result"),
            patch("selfapply.apply.launcher.release_lock") as release,
            patch("selfapply.apply.launcher.update_state"),
            patch("selfapply.apply.launcher.add_event"),
            patch("selfapply.apply.launcher._tenant_last_applied", {}),
        ):
            result = worker_loop(
                limit=2, dry_run=True, browser_mode="existing",
                domain_cooldown=999,
            )
        self.assertEqual(result, (1, 0))
        release.assert_any_call("https://acme.wd5.myworkdayjobs.com/job/2")
        self.assertIn(
            "https://acme.wd5.myworkdayjobs.com/job/2",
            acquire.call_args_list[2].kwargs["excluded_urls"],
        )

    def test_explicit_try_now_bypasses_automatic_employer_caps(self):
        target = "https://acme.wd5.myworkdayjobs.com/job/selected"
        job = {
            "url": target, "title": "Selected job", "site": "workday",
            "fit_score": 8,
        }
        with (
            patch("selfapply.apply.launcher.acquire_job", return_value=job),
            patch("selfapply.apply.launcher.run_job", return_value=("applied", 100)) as run,
            patch("selfapply.apply.launcher.mark_result"),
            patch("selfapply.apply.launcher._tenant_in_cooldown", return_value=True),
            patch("selfapply.apply.launcher._tenant_application_count", return_value=99),
            patch("selfapply.apply.launcher.update_state"),
            patch("selfapply.apply.launcher.add_event"),
        ):
            result = worker_loop(
                limit=1, target_url=target, agent="codex",
                browser_mode="existing", domain_cooldown=3600,
                max_per_employer=1, employer_window=86400,
            )
        self.assertEqual(result, (1, 0))
        run.assert_called_once()

    def test_worker_loop_paces_between_jobs_when_configured(self):
        jobs = [
            {"url": "https://one.test/job", "title": "One", "site": "test", "fit_score": 8},
            {"url": "https://two.test/job", "title": "Two", "site": "test", "fit_score": 8},
        ]
        stop_event = Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = False
        with (
            patch("selfapply.apply.launcher.acquire_job", side_effect=jobs),
            patch("selfapply.apply.launcher.run_job", return_value=("applied", 100)),
            patch("selfapply.apply.launcher.mark_result"),
            patch("selfapply.apply.launcher.update_state"),
            patch("selfapply.apply.launcher.add_event"),
            patch("selfapply.apply.launcher._inter_job_delay", return_value=23),
            patch("selfapply.apply.launcher._stop_event", stop_event),
        ):
            worker_loop(limit=2, browser_mode="existing", min_delay=15, max_delay=45)
        stop_event.wait.assert_called_once_with(timeout=23)


if __name__ == "__main__":
    unittest.main()
