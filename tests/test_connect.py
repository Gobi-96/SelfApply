import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from selfapply.apply import launcher
from selfapply.cli import app


class ConnectTests(unittest.TestCase):
    def test_google_account_page_reuses_one_tab_and_closes_duplicates(self):
        signin = Mock(url="https://accounts.google.com/signin")
        account = Mock(url="https://myaccount.google.com/")
        unrelated = Mock(url="https://example.test/")
        context = Mock(pages=[account, unrelated, signin])

        selected = launcher._google_account_page(context, close_duplicates=True)

        self.assertIs(selected, signin)
        account.close.assert_called_once()
        unrelated.close.assert_not_called()
        signin.close.assert_not_called()

    def test_google_signin_uses_one_stable_browser_connection(self):
        page = Mock(url="https://accounts.google.com/signin")
        context = Mock(pages=[page])
        browser = Mock(contexts=[context])
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=playwright)
        manager.__exit__ = Mock(return_value=False)
        cancel = Mock()
        cancel.wait.side_effect = [False, True]

        with (
            patch.object(launcher, "_cdp_is_ready", return_value=True),
            patch("playwright.sync_api.sync_playwright", return_value=manager),
        ):
            self.assertEqual(
                launcher.wait_for_google_signin(9222, cancel_event=cancel),
                (False, None),
            )

        playwright.chromium.connect_over_cdp.assert_called_once()
        context.new_page.assert_not_called()

    def test_google_identity_parser_requires_live_myaccount_page(self):
        self.assertEqual(
            launcher._parse_google_identity(
                "https://myaccount.google.com/", "Google Account a@example.test",
            ),
            (True, "a@example.test"),
        )
        self.assertEqual(
            launcher._parse_google_identity(
                "https://accounts.google.com/v3/signin/identifier", "Sign in",
            ),
            (False, None),
        )

    def test_portal_agent_result_parsing(self):
        with patch(
            "selfapply.apply.launcher._run_one_shot_browser_agent",
            return_value="RESULT:CONNECTED",
        ):
            self.assertEqual(
                launcher.connect_portal_with_google(
                    "https://jobs.example.test", "a@example.test",
                ),
                "connected",
            )
        with patch(
            "selfapply.apply.launcher._run_one_shot_browser_agent",
            return_value="RESULT:NO_GOOGLE_SSO",
        ):
            self.assertEqual(
                launcher.connect_portal_with_google(
                    "https://jobs.example.test", "a@example.test",
                ),
                "no_google_sso",
            )

    def test_google_session_expired_is_permanent_and_alerts_once(self):
        self.assertTrue(
            launcher._is_permanent_failure("failed:google_session_expired")
        )
        original = launcher._google_session_alerted
        launcher._google_session_alerted = False
        try:
            with patch("selfapply.apply.launcher.add_event") as event:
                launcher._alert_google_session_expired()
                launcher._alert_google_session_expired()
            event.assert_called_once()
            self.assertIn("connect-google", event.call_args.args[0])
        finally:
            launcher._google_session_alerted = original

    def test_connect_portals_refuses_without_verified_google_state(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "google_session.json"
            with (
                patch("selfapply.cli._bootstrap"),
                patch.object(launcher, "GOOGLE_SESSION_PATH", missing),
                patch("selfapply.cli._managed_connect_browser") as browser,
            ):
                result = CliRunner().invoke(app, ["connect-portals"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("connect-google", result.stdout)
        browser.assert_not_called()

    def test_queued_portals_only_uses_hosts_represented_in_jobs(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [
            {
                "url": "https://www.linkedin.com/jobs/view/1",
                "application_url": "https://jobs.ashbyhq.com/acme/abc",
            },
            {
                "url": "https://www.indeed.com/viewjob?jk=2",
                "application_url": "https://tenant.wd5.myworkdayjobs.com/job/2",
            },
            {
                "url": "https://unknown.example.test/job/3",
                "application_url": None,
            },
        ]
        with patch("selfapply.apply.launcher.get_connection", return_value=connection):
            targets = launcher.queued_portal_targets()
        self.assertEqual(len(targets), 2)
        self.assertIn("jobs.ashbyhq.com", next(iter(targets)))
        self.assertTrue(any("myworkdayjobs.com" in key for key in targets))


if __name__ == "__main__":
    unittest.main()
