import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfapply import config


@unittest.skipIf(config.platform.system() == "Windows", "Unix permissions only")
class SecretsPermissionTests(unittest.TestCase):
    def test_ensure_dirs_creates_private_directories(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / "app"
            paths = {
                "APP_DIR": app_dir,
                "TAILORED_DIR": app_dir / "tailored",
                "COVER_LETTER_DIR": app_dir / "covers",
                "LOG_DIR": app_dir / "logs",
                "CHROME_WORKER_DIR": app_dir / "chrome",
                "APPLY_WORKER_DIR": app_dir / "workers",
                "PROFILE_PATH": app_dir / "profile.json",
                "ENV_PATH": app_dir / ".env",
                "DB_PATH": app_dir / "selfapply.db",
            }
            with patch.multiple(config, **paths):
                config.ensure_dirs()
            self.assertEqual(stat.S_IMODE(app_dir.stat().st_mode), 0o700)

    def test_write_secret_file_sets_0600(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "secret"
            config.write_secret_file(path, "token=value")
            self.assertEqual(path.read_text(), "token=value")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_ensure_dirs_self_heals_existing_private_files(self):
        with tempfile.TemporaryDirectory() as root:
            app_dir = Path(root) / "app"
            app_dir.mkdir()
            profile = app_dir / "profile.json"
            env = app_dir / ".env"
            db = app_dir / "selfapply.db"
            for path in (profile, env, db):
                path.write_text("private")
                path.chmod(0o644)
            paths = {
                "APP_DIR": app_dir,
                "TAILORED_DIR": app_dir / "tailored",
                "COVER_LETTER_DIR": app_dir / "covers",
                "LOG_DIR": app_dir / "logs",
                "CHROME_WORKER_DIR": app_dir / "chrome",
                "APPLY_WORKER_DIR": app_dir / "workers",
                "PROFILE_PATH": profile, "ENV_PATH": env, "DB_PATH": db,
            }
            with patch.multiple(config, **paths):
                config.ensure_dirs()
            for path in (profile, env, db):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
