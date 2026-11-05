"""Regression coverage for the format-preserving resume pipeline.

The fixtures are anonymized archetypes rather than copies of real candidates.
They cover the structural patterns found in public resume-template projects.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from selfapply.scoring.document_tailor import (
    _pdf_text_segments,
    apply_docx_patch,
    apply_pdf_patch,
    build_resume_manifest,
    extract_docx_text,
    tailor_document,
    validate_docx_preservation,
    validate_patch_set,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _docx(path: Path, archetype: str = "single-sans") -> Path:
    table = archetype in {"two-column", "table", "sidebar"}
    columns = '<w:cols w:num="2" w:space="360"/>' if archetype == "two-column" else ""
    font = "Times New Roman" if archetype == "single-serif" else "Arial"
    paragraphs = """
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Jordan Candidate</w:t></w:r></w:p>
      <w:p><w:hyperlink r:id="rId5"><w:r><w:t>Portfolio</w:t></w:r></w:hyperlink></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>SUMMARY</w:t></w:r></w:p>
      <w:p><w:r><w:t>Operations leader improving reliable customer workflows.</w:t></w:r></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>TECHNICAL SKILLS</w:t></w:r></w:p>
      <w:p><w:r><w:t>Planning, reporting, process improvement</w:t></w:r></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>EXPERIENCE</w:t></w:r></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Operations Lead | Northwind Group | Jan 2020 - Present</w:t></w:r></w:p>
      <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Improved reporting workflows for regional teams.</w:t></w:r></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>PROJECTS</w:t></w:r></w:p>
      <w:p><w:r><w:t>Service Dashboard - Built a reporting view for team leaders.</w:t></w:r></w:p>
      <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>EDUCATION</w:t></w:r></w:p>
      <w:p><w:r><w:t>Bachelor of Arts | Example University | 2019</w:t></w:r></w:p>
    """
    if table:
        paragraphs = f"<w:tbl><w:tr><w:tc>{paragraphs}</w:tc><w:tc><w:p><w:r><w:t>Sidebar</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    if archetype == "multi-page-cv":
        paragraphs += "".join(
            f"<w:p><w:r><w:t>Publication and research contribution number {i}.</w:t></w:r></w:p>"
            for i in range(80)
        )
    document = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{paragraphs}
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/>{columns}</w:sectPr>
    </w:body></w:document>'''
    styles = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{font}"/><w:sz w:val="20"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/portfolio" TargetMode="External"/>
    </Relationships>'''
    content_types = '''<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
    package_rels = '''<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", rels)
    return path


@pytest.mark.parametrize("archetype", [
    "single-sans", "single-serif", "two-column", "table", "sidebar", "multi-page-cv",
])
def test_six_docx_archetypes_preserve_package_and_locked_history(tmp_path, archetype):
    master = _docx(tmp_path / f"{archetype}.docx", archetype)
    text = extract_docx_text(master)
    manifest = build_resume_manifest(master, text)
    experience = next(block for block in manifest["blocks"] if block["role"] == "experience_bullet")
    patch_set = validate_patch_set({"edits": [{
        "operation": "replace_text", "block_id": experience["id"],
        "text": "Improved customer reporting and forecasting workflows for regional teams.",
        "jd_evidence_ids": ["jd:1"],
    }]}, manifest)
    output = tmp_path / f"{archetype}-tailored.docx"
    apply_docx_patch(master, manifest, patch_set, output)
    checks = validate_docx_preservation(master, output, manifest)
    tailored = extract_docx_text(output)
    assert checks == {
        "package_parts_preserved": True,
        "locked_blocks_preserved": True,
        "geometry_preserved": True,
    }
    assert "Northwind Group | Jan 2020 - Present" in tailored
    assert "Example University | 2019" in tailored
    assert "customer reporting and forecasting" in tailored
    with zipfile.ZipFile(output) as archive:
        assert b"https://example.invalid/portfolio" in archive.read("word/_rels/document.xml.rels")


def _pdf(path: Path, columns: bool = False, scanned: bool = False) -> Path:
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    if not scanned:
        x = 310 if columns else 72
        rows = [
            ("Taylor Candidate", 72, 48), ("SUMMARY", 72, 85),
            ("Customer success professional improving retention workflows.", 72, 108),
            ("SKILLS", 72, 140), ("Customer support, reporting", 72, 163),
            ("EXPERIENCE", x, 200), ("Customer Success Lead | Example Co | 2021 - Present", x, 225),
            ("Improved onboarding for enterprise customers.", x, 250),
            ("EDUCATION", x, 285), ("Example University | 2020", x, 310),
        ]
        for text, left, top in rows:
            page.insert_text((left, top), text, fontsize=9)
    else:
        page.draw_rect(fitz.Rect(50, 50, 500, 700), fill=(0.95, 0.95, 0.95))
    document.save(path)
    document.close()
    return path


@pytest.mark.parametrize("columns", [False, True])
def test_born_digital_pdf_archetypes_capture_layout_and_preserve_locked_text(tmp_path, columns):
    master = _pdf(tmp_path / ("two.pdf" if columns else "single.pdf"), columns=columns)
    manifest = build_resume_manifest(master, "")
    editable = next(block for block in manifest["blocks"] if block["section"] == "summary" and block["editable"])
    patch_set = {"edits": [{"operation": "replace_text", "block_id": editable["id"],
                             "text": "Customer success professional improving retention.",
                             "jd_evidence_ids": ["jd:1"]}], "warnings": []}
    output = tmp_path / "tailored.pdf"
    apply_pdf_patch(master, manifest, patch_set, output)
    fitz = pytest.importorskip("fitz")
    result = "\n".join(page.get_text() for page in fitz.open(output))
    assert "Taylor Candidate" in result
    assert "Example University | 2020" in result
    assert "improving retention" in result


def test_scanned_pdf_is_detected_as_best_effort_without_editable_blocks(tmp_path):
    master = _pdf(tmp_path / "scanned.pdf", scanned=True)
    manifest = build_resume_manifest(master, "OCR supplied resume text")
    assert manifest["source_format"] == "pdf"
    assert manifest["editable_block_count"] == 0


def test_pdf_block_is_split_into_individual_bullets_and_locked_heading():
    raw = {"lines": [
        {"spans": [{"text": "\uf0b7", "bbox": [10, 10, 14, 20], "font": "SymbolMT", "size": 10},
                    {"text": " Built customer workflows", "bbox": [22, 10, 160, 20], "font": "Cambria", "size": 10}]},
        {"spans": [{"text": "\uf0b7", "bbox": [10, 24, 14, 34], "font": "SymbolMT", "size": 10},
                    {"text": " Improved reporting", "bbox": [22, 24, 140, 34], "font": "Cambria", "size": 10}]},
        {"spans": [{"text": "Example Company — Operations Lead", "bbox": [10, 40, 220, 52],
                    "font": "Cambria-Bold", "flags": 16, "size": 11}]},
    ]}
    segments = _pdf_text_segments(raw)
    assert [item["text"] for item in segments] == [
        "- Built customer workflows", "- Improved reporting",
        "Example Company — Operations Lead",
    ]
    assert segments[0]["rect"][0] == 22
    assert segments[1]["rect"][0] == 22
    assert not segments[2]["bullet"]


def test_plain_text_archetype_uses_stable_anchored_lines(tmp_path):
    master = tmp_path / "resume.txt"
    master.write_text("SUMMARY\nSales leader\nEXPERIENCE\n- Grew key accounts\nEDUCATION\nExample College | 2018\n", encoding="utf-8")
    manifest = build_resume_manifest(master, master.read_text())
    assert any(block["role"] == "summary" for block in manifest["blocks"])
    assert any(block["role"] == "experience_bullet" for block in manifest["blocks"])


def test_locked_project_prefix_is_never_duplicated(tmp_path):
    master = _docx(tmp_path / "project.docx")
    text = extract_docx_text(master)
    manifest = build_resume_manifest(master, text)
    project = next(block for block in manifest["blocks"] if block.get("locked_prefix"))
    patch_set = {"edits": [{
        "operation": "replace_text", "block_id": project["id"],
        "text": "Service Dashboard - Reframed reporting for the target role.",
        "jd_evidence_ids": ["jd:1"],
    }], "warnings": []}
    output = tmp_path / "project-tailored.docx"
    apply_docx_patch(master, manifest, patch_set, output)
    tailored = extract_docx_text(output)
    assert "Service Dashboard - Reframed reporting" in tailored
    assert "Service Dashboard - Service Dashboard" not in tailored


@pytest.mark.parametrize("domain,term", [
    ("software", "Python"), ("data", "SQL"), ("cloud", "AWS"),
    ("product", "roadmap"), ("sales", "forecasting"), ("marketing", "campaigns"),
    ("finance", "reconciliation"), ("healthcare", "patient workflows"),
    ("operations", "inventory"), ("customer success", "retention"),
])
def test_ten_domain_patch_contract_improves_job_term_coverage(tmp_path, domain, term):
    source = tmp_path / f"{domain.replace(' ', '-')}.txt"
    source.write_text("SUMMARY\nExperienced professional\nEXPERIENCE\n- Improved team workflows\nEDUCATION\nExample University | 2020\n", encoding="utf-8")
    manifest = build_resume_manifest(source, source.read_text())
    editable = next(block for block in manifest["blocks"] if block["role"] == "summary")
    response = json.dumps({"edits": [{
        "operation": "replace_text", "block_id": editable["id"],
        "text": f"Experienced {domain} professional delivering {term} outcomes",
        "jd_evidence_ids": ["jd:1"],
    }], "warnings": []})
    fake = type("Client", (), {"chat": lambda *args, **kwargs: response})()
    job = {"title": f"{domain.title()} Specialist", "site": "Example", "full_description": f"Lead {term}. {term} is required."}
    with patch("selfapply.scoring.document_tailor.get_client", return_value=fake):
        tailored, report, patch_set, result_manifest = tailor_document(
            source.read_text(), job, {}, source_path=source, max_retries=0,
        )
    assert term.casefold() in tailored.casefold()
    assert report["pipeline"] == "anchored_patch"
    assert report["ats_match"]["overall_score"] >= report["master_ats_match"]["overall_score"]
    assert patch_set["edits"]
    assert result_manifest["master_fingerprint"] == manifest["master_fingerprint"]
