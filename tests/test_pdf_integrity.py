import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from selfapply.scoring.pdf import (
    _entry_header_html,
    _pdf_failure_marker,
    _verify_pdf_integrity,
    batch_convert,
    build_html,
    convert_to_pdf,
    infer_docx_layout,
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

    def test_build_html_uses_master_typography_geometry_and_section_order(self):
        resume = parse_resume(
            "Candidate\nArchitect\ncandidate@example.com\n\n"
            "SUMMARY\nSummary text.\n\nTECHNICAL SKILLS\nCloud: AWS\n\n"
            "EXPERIENCE\nExample Corp\n- Delivered systems.\n\n"
            "EDUCATION\nExample University"
        )
        layout = {
            "page_width_pt": 595.0, "page_height_pt": 842.0,
            "margin_top_pt": 50.0, "margin_right_pt": 45.0,
            "margin_bottom_pt": 40.0, "margin_left_pt": 42.0,
            "body_font": "Times New Roman", "body_size_pt": 10.5,
            "name_size_pt": 22.0, "heading_size_pt": 12.0,
            "line_height": 1.35, "section_gap_pt": 8.0,
            "header_align": "left", "accent_color": "#1f2937",
            "section_order": ["SUMMARY", "EDUCATION", "TECHNICAL SKILLS", "EXPERIENCE"],
        }
        with (
            patch("selfapply.scoring.pdf.load_master_layout", return_value=layout),
            patch("selfapply.scoring.pdf._master_external_links", return_value=[]),
        ):
            html = build_html(resume, {"personal": {}})
        self.assertIn("font-family: 'Times New Roman'", html)
        self.assertIn("font-size: 10.50pt", html)
        self.assertIn("text-align: left", html)
        self.assertLess(html.index("EDUCATION</div>"), html.index("TECHNICAL SKILLS</div>"))

    def test_docx_layout_is_inferred_without_candidate_specific_rules(self):
        document = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:jc w:val="left"/></w:pPr><w:r><w:rPr><w:sz w:val="40"/></w:rPr><w:t>Candidate</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="900" w:right="800" w:bottom="900" w:left="800"/></w:sectPr></w:body></w:document>'''
        styles = b'''<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos"/><w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>'''
        package = BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/styles.xml", styles)
        layout = infer_docx_layout(package.getvalue(), "SUMMARY\nText\nEDUCATION\nSchool")
        self.assertEqual(layout["body_font"], "Aptos")
        self.assertEqual(layout["body_size_pt"], 10.5)
        self.assertEqual(layout["name_size_pt"], 20.0)
        self.assertEqual(layout["margin_left_pt"], 40.0)
        self.assertEqual(layout["section_order"], ["SUMMARY", "EDUCATION"])

    def test_master_section_labels_and_work_header_spacing_are_preserved(self):
        header = _entry_header_html(
            "Example Corp - TXSep 2022 to April 2026 | Solutions Architect", {}
        )
        self.assertIn("Example Corp - TX", header)
        self.assertIn("Sep 2022 to April 2026", header)
        self.assertIn("Solutions Architect", header)
        self.assertIn("entry-date", header)

        document = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Candidate</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'''
        package = BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("word/document.xml", document)
        layout = infer_docx_layout(
            package.getvalue(),
            "PROFESSIONAL SUMMARY\nText\nEDUCATION DETAILS:\nSchool\nWORK EXPERIENCE\nRole",
        )
        self.assertEqual(layout["section_labels"]["SUMMARY"], "PROFESSIONAL SUMMARY")
        self.assertEqual(layout["section_labels"]["EDUCATION"], "EDUCATION DETAILS")
        self.assertEqual(layout["section_labels"]["EXPERIENCE"], "WORK EXPERIENCE")

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

    def test_uppercase_role_inside_experience_is_not_a_section_heading(self):
        parsed = parse_resume(
            "Candidate\nArchitect\nemail@example.com\n\nSUMMARY\nProfile.\n\n"
            "EXPERIENCE\nExample Bank - Oct 2022 to Aug 2025\nSAP/GPC\n"
            "- Built a platform.\nSecond Company - Jan 2020 to Sep 2022\n"
            "Data Engineer\n- Built pipelines.\n\nEDUCATION\nExample University"
        )
        entries = parse_entries(parsed["sections"]["EXPERIENCE"])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["subtitle"], "SAP/GPC")
        self.assertEqual(entries[0]["bullets"], ["Built a platform."])

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

    def test_accepts_complete_resume_without_hard_coded_section_headings(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "resume.txt"
            source.write_text(
                """Test Candidate
Operations Leader
candidate@example.com

CAREER PROFILE
Operations leader improving delivery quality and customer outcomes.

SELECTED CAPABILITIES
Planning, vendor management, reporting, process improvement

PROFESSIONAL HISTORY
Example Company | Operations Lead | 2021 - Present
- Led planning and delivery across regional teams and suppliers.
- Improved reporting quality and operating consistency.

ACADEMIC BACKGROUND
Example University | Bachelor of Business Administration
""",
                encoding="utf-8",
            )
            path = convert_to_pdf(source)
            _verify_pdf_integrity(
                path, min_bytes=2000, expected_text=source.read_text(),
            )


if __name__ == "__main__":
    unittest.main()
