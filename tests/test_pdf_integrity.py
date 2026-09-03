import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from selfapply.scoring.pdf import (
    _pdf_failure_marker,
    _verify_pdf_integrity,
    batch_convert,
    build_html,
    convert_to_pdf,
    is_pdf_quarantined,
    normalize_resume_text,
    parse_entries,
    parse_resume,
)


class PdfIntegrityTests(unittest.TestCase):
    def test_failed_pdf_source_is_quarantined_until_its_text_changes(self):
        with tempfile.TemporaryDirectory() as root:
            tailored = Path(root)
            source = tailored / "broken.txt"
            source.write_text("broken resume", encoding="utf-8")
            with (
                patch("selfapply.scoring.pdf.TAILORED_DIR", tailored),
                patch(
                    "selfapply.scoring.pdf.convert_to_pdf",
                    side_effect=RuntimeError("blank page"),
                ) as convert,
            ):
                self.assertEqual(batch_convert(), 0)
                self.assertTrue(is_pdf_quarantined(source))
                self.assertTrue(_pdf_failure_marker(source).exists())
                self.assertEqual(batch_convert(), 0)
                self.assertEqual(convert.call_count, 1)
                source.write_text("changed resume text", encoding="utf-8")
                self.assertEqual(batch_convert(), 0)
                self.assertEqual(convert.call_count, 2)

    def test_build_html_preserves_profile_and_project_links(self):
        resume = parse_resume(
            """Test Candidate
Software Engineer
Portland, Oregon
candidate@example.com | LinkedIn | GitHub | Portfolio

SUMMARY
Software engineer building reliable systems.

TECHNICAL SKILLS
Languages: Python

EXPERIENCE
Example Company | Engineer
- Built reliable systems.

PROJECTS
Portfolio | Live Demo | GitHub
- Built a useful application.

EDUCATION
Example University | Bachelor of Science
"""
        )
        profile = {"personal": {
            "email": "candidate@example.com",
            "linkedin_url": "https://linkedin.example/test",
            "github_url": "https://github.example/test",
            "website_url": "https://portfolio.example/test",
        }}
        with patch("selfapply.scoring.pdf._master_external_links", return_value=[]):
            html = build_html(resume, profile)
        self.assertIn('href="mailto:candidate@example.com"', html)
        self.assertIn('href="https://linkedin.example/test"', html)
        self.assertIn('href="https://github.example/test"', html)
        self.assertIn('href="https://portfolio.example/test"', html)

    def test_build_html_restores_master_credential_links(self):
        resume = parse_resume(
            """Test Candidate
Software Engineer
test@example.com

SUMMARY
Software engineer building reliable systems.

TECHNICAL SKILLS
Languages: Python

EXPERIENCE
Example Company | Engineer
- Built reliable systems.

EDUCATION
Example University | Bachelor of Science
Cloud Engineering Certificate
"""
        )
        credential = "https://certificates.example/credential/123"
        with patch(
            "selfapply.scoring.pdf._master_external_links",
            return_value=[credential],
        ):
            html = build_html(resume, {"personal": {}})
        self.assertIn(f'href="{credential}"', html)
        self.assertIn(">Credential</a>", html)

    def test_normalizes_layout_extraction_and_section_aliases(self):
        text = """Candidate Name
Engineer
Portland, OR | +1 \ue081202\ue082 555\ue0880147

PROFESSIONAL SUMMARY
Seven\ue09d years building software.

PROFESSIONAL EXPERIENCE
  Example Company - Engineer
● Built a service.
   Continued the same bullet.

EDUCATION & CERTIFICATIONS
Example University
"""
        normalized = normalize_resume_text(text)
        parsed = parse_resume(normalized)
        self.assertEqual(
            list(parsed["sections"]), ["SUMMARY", "EXPERIENCE", "EDUCATION"]
        )
        self.assertIn("+1 (202) 555-0147", normalized)
        self.assertIn("- Built a service. Continued the same bullet.", normalized)

    def test_normalizes_running_header_for_any_candidate_name(self):
        normalized = normalize_resume_text(
            "ALEXANDRA MORGAN | 2\nSUMMARY\nOperations leader.\n"
        )
        self.assertNotIn("ALEXANDRA MORGAN | 2", normalized)
        self.assertIn("Operations leader.", normalized)

    def test_technologies_stay_with_their_entry(self):
        entries = parse_entries(
            "Project One\n- Built it.\nTechnologies: Python, SQL\n"
            "Project Two\n- Shipped it."
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["technologies"], "Python, SQL")
        self.assertEqual(entries[1]["title"], "Project Two")

    def test_rejects_missing_file(self):
        with self.assertRaises(RuntimeError):
            _verify_pdf_integrity(Path("/tmp/does-not-exist-selfapply.pdf"))

    def test_rejects_too_small_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "resume.pdf"
            path.write_bytes(b"%PDF-1.4\n%%EOF")
            with self.assertRaises(RuntimeError):
                _verify_pdf_integrity(path, min_bytes=2000)

    def test_rejects_non_pdf_content(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "resume.pdf"
            path.write_bytes(b"<html>not a pdf</html>" + b"x" * 3000)
            with self.assertRaises(RuntimeError):
                _verify_pdf_integrity(path, min_bytes=2000)

    def test_accepts_readable_resume_pdf(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "resume.txt"
            source.write_text(
                """Test Candidate
Software Engineer
Portland, Oregon
candidate@example.com

SUMMARY
Software engineer building reliable web services and user-facing systems.

TECHNICAL SKILLS
Languages: Python, TypeScript, SQL
Frameworks: React, REST APIs
Tools: Git

EXPERIENCE
Example Company | Software Engineer | 2022 - Present
- Built and maintained production software used by internal teams.
- Improved service reliability through testing and monitoring.

EDUCATION
Example University | Bachelor of Science in Computer Science
""",
                encoding="utf-8",
            )
            path = convert_to_pdf(source)
            _verify_pdf_integrity(path, min_bytes=2000, expected_text=source.read_text())


if __name__ == "__main__":
    unittest.main()
