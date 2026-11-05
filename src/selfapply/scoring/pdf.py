"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import json
import io
import logging
import re
import sqlite3
import statistics
import zipfile
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from xml.etree import ElementTree

from selfapply.config import (
    RESUME_DOCX_PATH,
    RESUME_LAYOUT_PATH,
    RESUME_PDF_PATH,
    RESUME_SOURCE_PATH,
    TAILORED_DIR,
    load_profile,
)

log = logging.getLogger(__name__)

# Increment when validation or rendering changes so files quarantined by an
# obsolete rule are retried automatically without requiring manual cleanup.
_PDF_PIPELINE_VERSION = 2


def _pdf_failure_marker(text_path: Path) -> Path:
    return text_path.with_suffix(".pdf.failed.json")


def _source_fingerprint(text_path: Path) -> dict[str, int]:
    stat = text_path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def is_pdf_quarantined(text_path: Path) -> bool:
    """Return true when an unchanged source already failed PDF validation."""
    marker = _pdf_failure_marker(text_path)
    if not marker.exists() or not text_path.exists():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        return (
            payload.get("pipeline_version") == _PDF_PIPELINE_VERSION
            and payload.get("source") == _source_fingerprint(text_path)
        )
    except (OSError, ValueError, TypeError):
        return False


def _quarantine_pdf_source(text_path: Path, error: Exception) -> None:
    payload = {
        "pipeline_version": _PDF_PIPELINE_VERSION,
        "source": _source_fingerprint(text_path),
        "failed_at": datetime.now(UTC).isoformat(),
        "error": str(error)[:1000],
    }
    _pdf_failure_marker(text_path).write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )


def _restore_jobs_after_pdf_repair(text_path: Path) -> None:
    """Return document-blocked jobs to Ready after a successful rebuild."""
    try:
        from selfapply.database import get_connection

        connection = get_connection()
        connection.execute(
            """
            UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                            apply_attempts = 0, agent_id = NULL
            WHERE tailored_resume_path = ?
              AND apply_error = 'resume_pdf_unavailable'
            """,
            (str(text_path),),
        )
        connection.commit()
    except (OSError, RuntimeError, sqlite3.Error):
        log.exception("Could not restore jobs after PDF repair for %s", text_path)

_SECTION_ALIASES = {
    "SUMMARY": "SUMMARY",
    "PROFESSIONAL SUMMARY": "SUMMARY",
    "CAREER PROFILE": "SUMMARY",
    "PROFESSIONAL PROFILE": "SUMMARY",
    "PROFILE": "SUMMARY",
    "EXECUTIVE SUMMARY": "SUMMARY",
    "TECHNICAL SKILLS": "TECHNICAL SKILLS",
    "SKILLS": "TECHNICAL SKILLS",
    "SELECTED CAPABILITIES": "TECHNICAL SKILLS",
    "CORE COMPETENCIES": "TECHNICAL SKILLS",
    "AREAS OF EXPERTISE": "TECHNICAL SKILLS",
    "EXPERIENCE": "EXPERIENCE",
    "PROFESSIONAL EXPERIENCE": "EXPERIENCE",
    "WORK EXPERIENCE": "EXPERIENCE",
    "PROFESSIONAL HISTORY": "EXPERIENCE",
    "CAREER HISTORY": "EXPERIENCE",
    "EMPLOYMENT HISTORY": "EXPERIENCE",
    "PROJECTS": "PROJECTS",
    "EDUCATION": "EDUCATION",
    "ACADEMIC BACKGROUND": "EDUCATION",
    "ACADEMIC HISTORY": "EDUCATION",
    "EDUCATION DETAILS": "EDUCATION",
    "EDUCATION DETAILS:": "EDUCATION",
    "EDUCATION & CERTIFICATIONS": "EDUCATION",
}

_DEFAULT_LAYOUT = {
    "page_width_pt": 612.0,
    "page_height_pt": 792.0,
    "margin_top_pt": 28.0,
    "margin_right_pt": 34.0,
    "margin_bottom_pt": 28.0,
    "margin_left_pt": 34.0,
    "body_font": "Arial",
    "body_size_pt": 9.0,
    "name_size_pt": 17.0,
    "heading_size_pt": 10.0,
    "line_height": 1.25,
    "section_gap_pt": 4.0,
    "header_align": "center",
    "accent_color": "#1f2937",
    "skills_layout": "rows",
    "heading_rule": True,
    "header_rule": True,
    "contact_layout": "inline",
    "summary_layout": "paragraph",
    "section_labels": {
        "SUMMARY": "SUMMARY",
        "TECHNICAL SKILLS": "TECHNICAL SKILLS",
        "EXPERIENCE": "EXPERIENCE",
        "PROJECTS": "PROJECTS",
        "EDUCATION": "EDUCATION",
    },
    "section_order": [
        "SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION",
    ],
}


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _font_family(value: object) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", str(value or "")).replace("-", " ").strip()
    if not name:
        return "Arial"
    # PDF font names frequently append style words that browsers interpret as
    # a different family. Preserve the family while CSS handles weight/style.
    name = re.sub(r"\b(?:bold|italic|regular|roman|medium|light)\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip()
    aliases = {
        "palatino linotype": "Palatino",
        "timesnewromanpsmt": "Times New Roman",
        "arialmt": "Arial",
    }
    return aliases.get(name.casefold(), name) or "Arial"


def _section_order(text: str) -> list[str]:
    found: list[str] = []
    for raw in text.splitlines():
        canonical = _SECTION_ALIASES.get(re.sub(r"\s+", " ", raw.strip().upper()))
        if canonical and canonical not in found:
            found.append(canonical)
    return found or list(_DEFAULT_LAYOUT["section_order"])


def _section_labels(text: str) -> dict[str, str]:
    """Keep the master document's visible section names, not our defaults."""
    labels = dict(_DEFAULT_LAYOUT["section_labels"])
    for raw in text.splitlines():
        visible = re.sub(r"\s+", " ", raw.strip())
        canonical = _SECTION_ALIASES.get(visible.upper())
        if canonical:
            labels[canonical] = visible.rstrip(":")
    return labels


def _contact_layout(text: str) -> str:
    header: list[str] = []
    for line in text.splitlines():
        if _SECTION_ALIASES.get(line.strip().upper()):
            break
        if line.strip():
            header.append(line.strip())
    has_email_line = any("@" in line for line in header)
    has_phone_line = any(re.search(r"(?:phone|ph\s*#|\+?\d[\d() .-]{7,})", line, re.I) for line in header)
    return "lines" if has_email_line and has_phone_line and len(header) >= 4 else "inline"


def infer_pdf_layout(content: bytes, extracted_text: str = "") -> dict:
    """Infer reusable typography and page geometry from an uploaded PDF."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    samples: list[tuple[str, float, float, float, str]] = []
    for page in reader.pages:
        def visitor(text, _cm, tm, font_dict, font_size):
            value = str(text or "").strip()
            if value and font_size:
                samples.append((
                    value, float(font_size), float(tm[4]), float(tm[5]),
                    str((font_dict or {}).get("/BaseFont") or ""),
                ))
        page.extract_text(visitor_text=visitor)
    first = reader.pages[0]
    width, height = float(first.mediabox.width), float(first.mediabox.height)
    useful = [sample for sample in samples if 6 <= sample[1] <= 18]
    weighted_sizes = [size for text, size, *_ in useful for _ in range(min(80, len(text)))]
    body_size = statistics.median(weighted_sizes) if weighted_sizes else 9.0
    font_weights: dict[str, int] = {}
    for text, size, _x, _y, font in useful:
        if abs(size - body_size) <= 2:
            family = _font_family(font)
            font_weights[family] = font_weights.get(family, 0) + len(text)
    body_font = max(font_weights, key=font_weights.get) if font_weights else "Arial"
    xs = [x for _text, size, x, _y, _font in useful if abs(size - body_size) <= 2 and x >= 0]
    left = _bounded(min(xs) if xs else 34.0, 18.0, width * .18)
    first_lines = samples[:8]
    header_align = "center" if first_lines and first_lines[0][2] > width * .18 else "left"
    uppercase_sizes = [
        size for text, size, *_ in samples
        if len(text) > 3 and text == text.upper() and re.search(r"[A-Z]", text)
    ]
    heading_size = statistics.median(uppercase_sizes) if uppercase_sizes else body_size + 1
    return {
        **_DEFAULT_LAYOUT,
        "page_width_pt": round(width, 2),
        "page_height_pt": round(height, 2),
        "margin_left_pt": round(left, 2),
        "margin_right_pt": round(left, 2),
        "body_font": body_font,
        "body_size_pt": round(_bounded(body_size, 7.5, 12.0), 2),
        "name_size_pt": round(_bounded(max((s[1] for s in first_lines), default=17), 13, 28), 2),
        "heading_size_pt": round(_bounded(heading_size, body_size, 15), 2),
        "line_height": 1.2,
        "header_align": header_align,
        "section_order": _section_order(extracted_text),
        "section_labels": _section_labels(extracted_text),
        "contact_layout": _contact_layout(extracted_text),
        "source_format": "pdf",
    }


def infer_docx_layout(content: bytes, extracted_text: str = "") -> dict:
    """Infer page, font, spacing, and header alignment from a DOCX package."""
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        document = ElementTree.fromstring(archive.read("word/document.xml"))
        try:
            styles = ElementTree.fromstring(archive.read("word/styles.xml"))
        except KeyError:
            styles = ElementTree.fromstring(
                b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
            )
    defaults = styles.find(".//w:docDefaults/w:rPrDefault/w:rPr", ns)
    font_node = defaults.find("w:rFonts", ns) if defaults is not None else None
    size_node = defaults.find("w:sz", ns) if defaults is not None else None
    default_font = (
        font_node.get(f"{{{ns['w']}}}ascii") if font_node is not None else None
    ) or "Arial"
    default_size = float(size_node.get(f"{{{ns['w']}}}val", "18")) / 2 if size_node is not None else 9.0
    style_map: dict[str, dict[str, object]] = {}
    for style in styles.findall(".//w:style", ns):
        style_id = str(style.get(f"{{{ns['w']}}}styleId") or "")
        style_size = style.find(".//w:rPr/w:sz", ns)
        style_font = style.find(".//w:rPr/w:rFonts", ns)
        style_map[style_id] = {
            "size": float(style_size.get(f"{{{ns['w']}}}val")) / 2
            if style_size is not None and style_size.get(f"{{{ns['w']}}}val") else None,
            "font": style_font.get(f"{{{ns['w']}}}ascii")
            if style_font is not None else None,
        }
    sect = document.find(".//w:sectPr", ns)
    page = sect.find("w:pgSz", ns) if sect is not None else None
    margins = sect.find("w:pgMar", ns) if sect is not None else None
    def twips(node, name, default):
        return float(node.get(f"{{{ns['w']}}}{name}", default)) / 20 if node is not None else float(default) / 20
    paragraphs = document.findall(".//w:p", ns)
    first_align = "left"
    body_sizes: list[float] = []
    first_size = default_size
    headings: list[float] = []
    font_weights: dict[str, int] = {}
    content_index = 0
    for paragraph in paragraphs:
        text = "".join(paragraph.itertext()).strip()
        if not text:
            continue
        style_node = paragraph.find("w:pPr/w:pStyle", ns)
        style_id = style_node.get(f"{{{ns['w']}}}val") if style_node is not None else ""
        style = (
            style_map.get(str(style_id), style_map.get("Normal", {}))
            if style_id else style_map.get("Normal", {})
        )
        normal_style = style_map.get("Normal", {})
        style_size_value = style.get("size") or normal_style.get("size")
        style_font_value = style.get("font") or normal_style.get("font")
        if content_index < 4:
            jc = paragraph.find("w:pPr/w:jc", ns)
            if jc is not None and jc.get(f"{{{ns['w']}}}val") in {"center", "right"}:
                first_align = jc.get(f"{{{ns['w']}}}val")
        run_sizes = [
            float(node.get(f"{{{ns['w']}}}val", "0")) / 2
            for node in paragraph.findall(".//w:sz", ns)
            if node.get(f"{{{ns['w']}}}val")
        ]
        effective_sizes = run_sizes or ([float(style_size_value)] if style_size_value else [default_size])
        effective_size = statistics.median(effective_sizes)
        if content_index == 0:
            first_size = effective_size
        if content_index > 0 and not (text == text.upper() and re.search(r"[A-Z]", text)):
            body_sizes.extend([effective_size] * min(80, len(text)))
        if text == text.upper() and re.search(r"[A-Z]", text):
            headings.append(effective_size)
        explicit_fonts = [
            node.get(f"{{{ns['w']}}}ascii")
            for node in paragraph.findall(".//w:rFonts", ns)
            if node.get(f"{{{ns['w']}}}ascii")
        ]
        family = explicit_fonts[0] if explicit_fonts else style_font_value or default_font
        font_weights[str(family)] = font_weights.get(str(family), 0) + len(text)
        content_index += 1
    body_size = statistics.median(body_sizes) if body_sizes else default_size
    body_font = max(font_weights, key=font_weights.get) if font_weights else default_font
    heading_paragraphs = [
        paragraph for paragraph in paragraphs
        if (text := "".join(paragraph.itertext()).strip())
        and text == text.upper() and re.search(r"[A-Z]", text)
    ]
    heading_rule = any(
        paragraph.find("w:pPr/w:pBdr/w:bottom", ns) is not None
        for paragraph in heading_paragraphs
    )
    header_rule = any(
        paragraph.find("w:pPr/w:pBdr/w:bottom", ns) is not None
        for paragraph in paragraphs[:4]
    )
    summary_started = False
    summary_paragraphs = 0
    summary_list_paragraphs = 0
    for paragraph in paragraphs:
        text = "".join(paragraph.itertext()).strip()
        canonical = _SECTION_ALIASES.get(text.upper())
        if canonical:
            if canonical == "SUMMARY":
                summary_started = True
                continue
            if summary_started:
                break
        if summary_started and text:
            summary_paragraphs += 1
            style_node = paragraph.find("w:pPr/w:pStyle", ns)
            style_id = style_node.get(f"{{{ns['w']}}}val", "") if style_node is not None else ""
            if paragraph.find("w:pPr/w:numPr", ns) is not None or "list" in style_id.casefold():
                summary_list_paragraphs += 1
    return {
        **_DEFAULT_LAYOUT,
        "page_width_pt": twips(page, "w", "12240"),
        "page_height_pt": twips(page, "h", "15840"),
        "margin_top_pt": twips(margins, "top", "720"),
        "margin_right_pt": twips(margins, "right", "720"),
        "margin_bottom_pt": twips(margins, "bottom", "720"),
        "margin_left_pt": twips(margins, "left", "720"),
        "body_font": _font_family(body_font),
        "body_size_pt": round(_bounded(body_size, 7.5, 12.0), 2),
        "name_size_pt": round(_bounded(first_size, 13, 28), 2),
        "heading_size_pt": round(_bounded(statistics.median(headings) if headings else body_size + 1, body_size, 15), 2),
        "header_align": first_align,
        "skills_layout": "table" if document.find(".//w:tbl", ns) is not None else "rows",
        "heading_rule": heading_rule,
        "header_rule": header_rule,
        "contact_layout": _contact_layout(extracted_text),
        "summary_layout": "bullets"
        if summary_paragraphs and summary_list_paragraphs >= summary_paragraphs / 2
        else "paragraph",
        "section_order": _section_order(extracted_text),
        "section_labels": _section_labels(extracted_text),
        "source_format": "docx",
    }


def save_master_layout(
    content: bytes, suffix: str, extracted_text: str,
    output_path: Path | None = None,
) -> dict:
    layout = (
        infer_pdf_layout(content, extracted_text)
        if suffix == ".pdf" else infer_docx_layout(content, extracted_text)
        if suffix == ".docx" else {
            **_DEFAULT_LAYOUT,
            "section_order": _section_order(extracted_text),
            "section_labels": _section_labels(extracted_text),
            "source_format": "txt",
        }
    )
    (output_path or RESUME_LAYOUT_PATH).write_text(
        json.dumps(layout, indent=2), encoding="utf-8",
    )
    return layout


def load_master_layout() -> dict:
    try:
        saved = json.loads(RESUME_LAYOUT_PATH.read_text(encoding="utf-8"))
        return {**_DEFAULT_LAYOUT, **saved}
    except (OSError, ValueError, TypeError):
        pass
    # Backward compatibility for profiles uploaded before layout capture.
    candidates = [path for path in (RESUME_PDF_PATH, RESUME_DOCX_PATH) if path.exists()]
    if candidates:
        source = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        text = ""
        try:
            source_text = RESUME_SOURCE_PATH.read_text(encoding="utf-8")
            selected = json.loads(source_text).get("format")
            if selected in {".pdf", ".docx"}:
                selected_path = RESUME_PDF_PATH if selected == ".pdf" else RESUME_DOCX_PATH
                if selected_path.exists():
                    source = selected_path
        except (OSError, ValueError, TypeError):
            pass
        try:
            text = source.with_suffix(".txt").read_text(encoding="utf-8")
        except OSError:
            pass
        content = source.read_bytes()
        layout = infer_pdf_layout(content, text) if source.suffix == ".pdf" else infer_docx_layout(content, text)
        try:
            RESUME_LAYOUT_PATH.write_text(json.dumps(layout, indent=2), encoding="utf-8")
        except OSError:
            pass
        return layout
    return dict(_DEFAULT_LAYOUT)


def _profile_links(profile: dict) -> dict[str, str]:
    personal = profile.get("personal", {})
    website = personal.get("website_url") or personal.get("portfolio_url") or ""
    return {
        "LinkedIn": str(personal.get("linkedin_url") or "").strip(),
        "GitHub": str(personal.get("github_url") or "").strip(),
        "Live Demo": str(website).strip(),
        "Portfolio": str(personal.get("portfolio_url") or website).strip(),
        "Website": str(website).strip(),
    }


def _canonical_url(value: str) -> str:
    value = value.strip().casefold()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    return value.rstrip("/")


def _master_external_links(profile: dict) -> list[str]:
    """Recover non-profile credential links from the authoritative master file."""
    candidates = [path for path in (RESUME_PDF_PATH, RESUME_DOCX_PATH) if path.exists()]
    if not candidates:
        return []
    source = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    try:
        selected = json.loads(RESUME_SOURCE_PATH.read_text(encoding="utf-8")).get("format")
        selected_path = RESUME_PDF_PATH if selected == ".pdf" else RESUME_DOCX_PATH
        if selected_path.exists():
            source = selected_path
    except (OSError, ValueError, TypeError):
        pass
    try:
        uris: list[str] = []
        if source.suffix.casefold() == ".pdf":
            from pypdf import PdfReader
            for page in PdfReader(source).pages:
                for annotation in page.get("/Annots") or []:
                    obj = annotation.get_object()
                    uri = str((obj.get("/A") or {}).get("/URI") or "").strip()
                    if uri and uri not in uris:
                        uris.append(uri)
        else:
            with zipfile.ZipFile(source) as archive:
                rels = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
            uris = [
                str(node.get("Target") or "").strip() for node in rels
                if str(node.get("TargetMode") or "").casefold() == "external"
                and str(node.get("Target") or "").strip()
            ]
    except Exception:
        return []
    standard = {_canonical_url(value) for value in _profile_links(profile).values() if value}
    email = str(profile.get("personal", {}).get("email") or "").casefold()
    return [
        uri for uri in uris
        if _canonical_url(uri) not in standard
        and not uri.casefold().startswith("mailto:")
        and (not email or email not in uri.casefold())
    ]


def _anchor(label: str, url: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def _linkify_labels(text: str, profile: dict) -> str:
    links = {label: url for label, url in _profile_links(profile).items() if url}
    if not links:
        return escape(text)
    pattern = re.compile(
        r"(?<![\w])(" + "|".join(
            re.escape(label) for label in sorted(links, key=len, reverse=True)
        ) + r")(?![\w])",
        re.IGNORECASE,
    )
    labels = {label.casefold(): (label, url) for label, url in links.items()}
    rendered: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        rendered.append(escape(text[cursor:match.start()]))
        label, url = labels[match.group(0).casefold()]
        rendered.append(_anchor(match.group(0), url))
        cursor = match.end()
    rendered.append(escape(text[cursor:]))
    return "".join(rendered)


def _contact_part_html(part: str, profile: dict) -> str:
    value = part.strip()
    if not value:
        return ""
    if re.match(r"^https?://", value, re.IGNORECASE):
        for label, url in _profile_links(profile).items():
            if url and value.rstrip("/").casefold() == url.rstrip("/").casefold():
                return _anchor(label, value)
        return _anchor(value, value)
    if "@" in value and " " not in value:
        return _anchor(value, "mailto:" + value)
    linked = _linkify_labels(value, profile)
    if linked != escape(value):
        return linked
    return escape(value)


def normalize_resume_text(text: str) -> str:
    """Normalize PDF-extracted resume text without changing its factual content."""
    replacements = {
        "\ue09d": "+",
        "\ue081": "(",
        "\ue082": ")",
        "\ue088": "-",
        "\u00a0": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    raw_lines = text.splitlines()

    cleaned: list[str] = []
    body_started = False
    for raw in raw_lines:
        stripped = raw.strip()
        upper = re.sub(r"\s+", " ", stripped.upper())
        # PDF extractors often repeat a running header such as
        # "ALEX MORGAN | 2" on every page.  Recognize the layout, not a
        # particular candidate's name.
        if re.fullmatch(r"[A-Z][A-Z .,'’\-]{1,79}\s*\|\s*\d+", upper):
            continue
        canonical = _SECTION_ALIASES.get(upper)
        if canonical:
            body_started = True
            cleaned.extend(["", canonical])
            continue
        if stripped.startswith(("●", "•")):
            cleaned.append("- " + stripped[1:].strip())
            continue
        indent = len(raw) - len(raw.lstrip())
        if body_started and indent >= 3 and stripped and cleaned:
            # Layout extraction wraps long bullets with indentation. Rejoin them
            # so one source bullet remains one rendered bullet.
            cleaned[-1] = cleaned[-1].rstrip() + " " + stripped
            continue
        cleaned.append(stripped if body_started else stripped)

    normalized = "\n".join(cleaned)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


# ── Resume Parser ────────────────────────────────────────────────────────

def parse_resume(text: str) -> dict:
    """Parse a structured text resume into sections.

    Expects a format with header lines (name, title, location, contact)
    followed by ALL-CAPS section headers (SUMMARY, TECHNICAL SKILLS, etc.).

    Args:
        text: Full resume text.

    Returns:
        {"name": str, "title": str, "location": str, "contact": str, "sections": dict}
    """
    lines = [line.rstrip() for line in normalize_resume_text(text).split("\n")]

    # Header: first few lines before SUMMARY
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        if _SECTION_ALIASES.get(line.strip().upper()) == "SUMMARY":
            body_start = i
            break
        if line.strip():
            header_lines.append(line.strip())

    name = header_lines[0] if len(header_lines) > 0 else ""
    title = header_lines[1] if len(header_lines) > 1 else ""
    # The header may have 3 or 4 lines depending on whether location is included
    location = ""
    contact = ""
    if len(header_lines) > 3:
        location = header_lines[2]
        contact = header_lines[3]
    elif len(header_lines) > 2:
        # Could be location or contact -- check for email/phone indicators
        if "@" in header_lines[2] or "|" in header_lines[2]:
            contact = header_lines[2]
        else:
            location = header_lines[2]

    # Split body into sections by ALL-CAPS headers
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in lines[body_start:]:
        stripped = line.strip()
        canonical = _SECTION_ALIASES.get(stripped.rstrip(":"), "")
        # Keep support for short custom headings, but do not mistake an
        # all-caps role or technology (for example SAP/GPC) for a new section.
        generic_heading = bool(
            stripped == stripped.upper()
            and 3 < len(stripped) <= 40
            and len(stripped.split()) <= 5
            and not re.search(r"[/|@]", stripped)
            and not re.search(r"\d", stripped)
        )
        if (
            stripped
            and not stripped.startswith("-")
            and not stripped.startswith("\u2022")
            and (canonical or generic_heading)
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = canonical or stripped.rstrip(":")
            current_lines = []
        else:
            current_lines.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "name": name,
        "title": title,
        "location": location,
        "contact": contact,
        "sections": sections,
    }


def parse_skills(text: str) -> list[tuple[str, str]]:
    """Parse skills section into (category, value) pairs.

    Args:
        text: The TECHNICAL SKILLS section text.

    Returns:
        List of (category_name, skills_string) tuples.
    """
    skills: list[tuple[str, str]] = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if ":" in line:
            cat, val = line.split(":", 1)
            skills.append((cat.strip(), val.strip()))
    return skills


def parse_entries(text: str) -> list[dict]:
    """Parse experience/project entries from section text.

    Args:
        text: The EXPERIENCE or PROJECTS section text.

    Returns:
        List of entry dictionaries with title, subtitle, bullets, and technologies.
    """
    entries: list[dict] = []
    lines = text.strip().split("\n")
    current: dict | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("\u2022 "):
            if current:
                current["bullets"].append(stripped[2:].strip())
        elif stripped.casefold().startswith("technologies:") and current:
            current["technologies"] = stripped.split(":", 1)[1].strip()
        elif current is None or current.get("bullets") or current.get("technologies"):
            # New entry
            if current:
                entries.append(current)
            current = {
                "title": stripped,
                "subtitle": "",
                "bullets": [],
                "technologies": "",
            }
        elif current and not current["subtitle"]:
            current["subtitle"] = stripped
        else:
            if current:
                current["bullets"].append(stripped)

    if current:
        entries.append(current)

    return entries


_ENTRY_DATE_RE = re.compile(
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{4}\s+(?:to|-|–|—)\s+(?:Present|Current|"
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)?"
    r"\s*\d{0,4}\b",
    re.IGNORECASE,
)


def _entry_header_html(title: str, profile: dict) -> str:
    """Render common master-style company/date/title headers without inventing data."""
    parts = [part.strip() for part in title.split("|") if part.strip()]
    date = ""
    role = ""
    company = parts[0] if parts else title.strip()
    for index, part in enumerate(parts):
        match = _ENTRY_DATE_RE.search(part)
        if match:
            date = match.group(0).strip()
            before_date = part[:match.start()].strip(" ,-–—")
            after_date = part[match.end():].strip(" ,-–—")
            if index == 0:
                company = before_date or company
                if after_date:
                    role = re.sub(r"^role\s*:\s*", "", after_date, flags=re.I)
            elif before_date or after_date:
                role = after_date or before_date
        elif index > 0 and not role:
            role = part
    if not date:
        match = _ENTRY_DATE_RE.search(company)
        if match:
            date = match.group(0).strip()
            after_date = company[match.end():].strip(" ,-–—")
            company = company[:match.start()].strip(" ,-–—")
            if after_date and not role:
                role = re.sub(r"^role\s*:\s*", "", after_date, flags=re.I)
    if len(parts) >= 3 and not role:
        role = parts[1]
    company_html = _linkify_labels(company or title, profile)
    date_html = f'<span class="entry-date">{escape(date)}</span>' if date else ""
    role_html = f'<div class="entry-role">{escape(role)}</div>' if role else ""
    return (
        f'<div class="entry-company-row"><span>{company_html}</span>{date_html}</div>'
        f'{role_html}'
    )


# ── HTML Template ────────────────────────────────────────────────────────

def build_html(resume: dict, profile: dict | None = None) -> str:
    """Build professional resume HTML from parsed data.

    Args:
        resume: Parsed resume dict from parse_resume().

    Returns:
        Complete HTML string ready for PDF rendering.
    """
    sections = resume["sections"]
    profile = profile or {}
    layout = load_master_layout()
    labels = {**_DEFAULT_LAYOUT["section_labels"], **layout.get("section_labels", {})}

    def section_title(name: str) -> str:
        return escape(str(labels.get(name) or name))

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        if layout.get("skills_layout") == "table":
            rows = "".join(
                f'<tr><th>{escape(cat)}</th><td>{escape(val)}</td></tr>'
                for cat, val in skills
            )
            skill_body = f'<table class="skills-table">{rows}</table>'
        else:
            skill_body = "".join(
                f'<div class="skill-row"><span class="skill-cat">{escape(cat)}:</span> {escape(val)}</div>\n'
                for cat, val in skills
            )
        skills_html = f'<div class="section"><div class="section-title">{section_title("TECHNICAL SKILLS")}</div>{skill_body}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle_value = str(e["subtitle"] or "").strip()
            subtitle = (
                f'<div class="entry-subtitle">{escape(subtitle_value)}</div>'
                if subtitle_value and subtitle_value.casefold() not in str(e["title"]).casefold()
                else ""
            )
            technologies = (
                f'<div class="entry-tech"><b>Technologies:</b> {escape(e["technologies"])}</div>'
                if e.get("technologies") else ""
            )
            items += f'<div class="entry"><div class="entry-title">{_entry_header_html(e["title"], profile)}</div>{subtitle}<ul>{bullets}</ul>{technologies}</div>'
        exp_html = f'<div class="section"><div class="section-title">{section_title("EXPERIENCE")}</div>{items}</div>'

    # Projects
    proj_html = ""
    if sections.get("PROJECTS", "").strip():
        entries = parse_entries(sections["PROJECTS"])
        total_body_chars = sum(len(value) for value in sections.values())
        project_class = "section page-break-before" if total_body_chars > 5000 else "section"
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{escape(b)}</li>" for b in e["bullets"])
            subtitle_value = str(e["subtitle"] or "").strip()
            subtitle = (
                f'<div class="entry-subtitle">{escape(subtitle_value)}</div>'
                if subtitle_value and subtitle_value.casefold() not in str(e["title"]).casefold()
                else ""
            )
            technologies = (
                f'<div class="entry-tech"><b>Technologies:</b> {escape(e["technologies"])}</div>'
                if e.get("technologies") else ""
            )
            items += f'<div class="entry"><div class="entry-title">{_entry_header_html(e["title"], profile)}</div>{subtitle}<ul>{bullets}</ul>{technologies}</div>'
        proj_html = f'<div class="{project_class}"><div class="section-title">{section_title("PROJECTS")}</div>{items}</div>'

    # Education
    edu_html = ""
    if "EDUCATION" in sections:
        edu_text = sections["EDUCATION"].strip()
        education_lines = [line.strip() for line in edu_text.splitlines() if line.strip()]
        credential_links = iter(_master_external_links(profile))
        edu_rows_list: list[str] = []
        for line in education_lines:
            rendered = _linkify_labels(line, profile)
            if not re.search(
                r"\b(?:master|bachelor|phd|university|college)\b", line, re.IGNORECASE,
            ):
                credential = next(credential_links, "")
                if credential:
                    rendered += " " + _anchor("Credential", credential)
            edu_rows_list.append(f'<div class="edu-row">{rendered}</div>')
        edu_rows = "".join(edu_rows_list)
        edu_html = f'<div class="section education"><div class="section-title">{section_title("EDUCATION")}</div><div class="edu">{edu_rows}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_text = sections["SUMMARY"].strip()
        if layout.get("summary_layout") == "bullets":
            summary_items = [
                item.strip() for item in re.split(r"(?<=[.!?])\s+|\n+", summary_text)
                if item.strip()
            ]
            summary_body = "<ul>" + "".join(
                f"<li>{escape(item)}</li>" for item in summary_items
            ) + "</ul>"
        else:
            summary_body = f'<div class="summary">{escape(summary_text)}</div>'
        summary_html = f'<div class="section"><div class="section-title">{section_title("SUMMARY")}</div>{summary_body}</div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    if layout.get("contact_layout") == "lines":
        personal = profile.get("personal", {})
        email = str(personal.get("email") or "").strip()
        phone = str(personal.get("phone") or "").strip()
        contact_lines = []
        if email:
            contact_lines.append(f"Email: {_anchor(email, 'mailto:' + email)}")
        if phone:
            contact_lines.append("Ph #: " + escape(phone))
        extras = [
            _contact_part_html(part, profile) for part in contact_parts
            if part and "@" not in part and not re.search(r"\d{7,}", re.sub(r"\D", "", part))
        ]
        contact_lines.extend(extras)
        contact_html = "".join(f"<div>{line}</div>" for line in contact_lines)
    else:
        contact_html = " &nbsp;|&nbsp; ".join(
            _contact_part_html(part, profile) for part in contact_parts if part
        )

    # Location line (may be empty)
    location_value = str(resume["location"] or "").strip()
    if layout.get("contact_layout") == "lines" and "@" in location_value:
        location_value = ""
    location_html = f'<div class="location">{escape(location_value)}</div>' if location_value else ""
    section_html = {
        "SUMMARY": summary_html,
        "TECHNICAL SKILLS": skills_html,
        "EXPERIENCE": exp_html,
        "PROJECTS": proj_html,
        "EDUCATION": edu_html,
    }
    order = list(layout.get("section_order") or _DEFAULT_LAYOUT["section_order"])
    ordered_sections = "".join(
        section_html.get(name, "") for name in [
            *order, *(name for name in section_html if name not in order),
        ]
    )
    font = escape(str(layout["body_font"]), quote=True)
    accent = escape(str(layout["accent_color"]), quote=True)
    page_width = float(layout["page_width_pt"]) / 72
    page_height = float(layout["page_height_pt"]) / 72

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: {page_width:.3f}in {page_height:.3f}in;
    margin: {float(layout['margin_top_pt']) / 72:.3f}in {float(layout['margin_right_pt']) / 72:.3f}in {float(layout['margin_bottom_pt']) / 72:.3f}in {float(layout['margin_left_pt']) / 72:.3f}in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: '{font}', Arial, sans-serif;
    font-size: {float(layout['body_size_pt']):.2f}pt;
    line-height: {float(layout['line_height']):.2f};
    color: #1a1a1a;
}}
.header {{
    text-align: {layout['header_align']};
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: {'1px solid ' + accent if layout.get('header_rule', True) else '0'};
}}
.name {{
    font-size: {float(layout['name_size_pt']):.2f}pt;
    font-weight: 700;
    color: {accent};
    letter-spacing: 0;
}}
.title {{
    font-size: {max(float(layout['body_size_pt']), float(layout['heading_size_pt']) - .5):.2f}pt;
    color: {accent};
    margin: 1px 0;
}}
.location {{
    font-size: 8.5pt;
    color: #555;
}}
.contact {{
    font-size: 8.5pt;
    color: #444;
    margin-top: 1px;
}}
.contact a {{
    color: #2c3e50;
    text-decoration: none;
}}
.section {{
    margin-top: {float(layout['section_gap_pt']):.2f}pt;
}}
.page-break-before {{
    break-before: page;
}}
.section-title {{
    font-size: {float(layout['heading_size_pt']):.2f}pt;
    font-weight: 700;
    color: {accent};
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: {'1px solid ' + accent if layout.get('heading_rule', True) else '0'};
    padding-bottom: 1px;
    margin-bottom: 2px;
}}
.summary {{
    font-size: {float(layout['body_size_pt']):.2f}pt;
    color: #333;
    line-height: 1.3;
}}
.skill-row {{
    font-size: {float(layout['body_size_pt']):.2f}pt;
    margin: 0;
    line-height: 1.25;
}}
.skill-cat {{
    font-weight: 600;
    color: {accent};
}}
.skills-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: {float(layout['body_size_pt']):.2f}pt;
}}
.skills-table th, .skills-table td {{
    border: 0.7px solid {accent};
    padding: 1px 5px;
    text-align: left;
    vertical-align: top;
}}
.skills-table th {{
    width: 27%;
    font-weight: 400;
}}
.entry {{
    margin-bottom: 2px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: {max(float(layout['body_size_pt']), float(layout['heading_size_pt']) - .5):.2f}pt;
    color: {accent};
}}
.entry-company-row {{
    display: flex;
    justify-content: space-between;
    gap: 12px;
}}
.entry-date {{
    white-space: nowrap;
}}
.entry-role {{
    font-weight: 600;
}}
.entry-subtitle {{
    font-size: 8.5pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
.entry-tech {{
    font-size: 8.5pt;
    color: {accent};
    margin-top: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: {float(layout['body_size_pt']):.2f}pt;
    margin-bottom: 0;
    line-height: 1.25;
}}
.edu {{
    font-size: 8.5pt;
    line-height: 1.2;
}}
.edu-row {{
    margin-bottom: 1px;
}}
</style>
</head>
<body>
<div class="header">
    <div class="name">{escape(str(resume['name']))}</div>
    <div class="title">{escape(str(resume['title']))}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{ordered_sections}
</body>
</html>"""


# ── PDF Renderer ─────────────────────────────────────────────────────────

def _verify_pdf_integrity(
    path: Path, min_bytes: int = 2000, expected_text: str = "",
    expected_links: list[str] | None = None,
) -> None:
    """Reject missing, blank, truncated, malformed, or structurally broken PDFs."""
    if not path.exists():
        raise RuntimeError(f"PDF generation produced no file: {path}")
    data = path.read_bytes()
    if len(data) < min_bytes:
        raise RuntimeError(
            f"Generated PDF is suspiciously small ({len(data)} bytes): {path}"
        )
    if not data.startswith(b"%PDF-"):
        raise RuntimeError(f"Generated file is not a valid PDF: {path}")
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if not 1 <= len(reader.pages) <= 3:
            raise RuntimeError(
                f"Generated resume has an unexpected {len(reader.pages)} pages: {path}"
            )
        page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
        rendered_links = {
            str((annotation.get_object().get("/A") or {}).get("/URI") or "").strip()
            for page in reader.pages
            for annotation in (page.get("/Annots") or [])
            if annotation.get_object().get("/Subtype") == "/Link"
        }
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Generated PDF cannot be parsed: {path}") from exc
    if any(len(text) < 80 for text in page_texts):
        raise RuntimeError(f"Generated PDF contains a blank or unreadable page: {path}")
    rendered_text = "\n".join(page_texts)
    rendered_lower = rendered_text.casefold()
    # Resumes do not share one mandatory heading vocabulary. Requiring literal
    # Summary/Skills/Experience/Education headings rejected valid CVs and
    # domain-specific formats even when the rendered document was complete.
    # The size, page readability, expected-text coverage, glyph, and hyperlink
    # checks below are format-neutral and still catch blank/truncated output.
    if expected_text and len(rendered_text) < max(300, int(len(expected_text) * 0.50)):
        raise RuntimeError("Generated PDF text is unexpectedly truncated.")
    if "�" in rendered_text or "placeholder" in rendered_lower or "\\http" in rendered_lower:
        raise RuntimeError("Generated PDF contains broken glyphs, placeholders, or malformed links.")
    missing_links = [link for link in (expected_links or []) if link not in rendered_links]
    if missing_links:
        raise RuntimeError(
            "Generated PDF is missing clickable links: " + ", ".join(missing_links)
        )


def render_pdf(html: str, output_path: str) -> None:
    """Render HTML to PDF using Playwright's headless Chromium.

    Args:
        html: Complete HTML string.
        output_path: Path to write the PDF file.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(
            path=output_path,
            prefer_css_page_size=True,
            scale=1.0,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
        )
        browser.close()


# ── Public API ───────────────────────────────────────────────────────────

def convert_to_pdf(
    text_path: Path, output_path: Path | None = None, html_only: bool = False
) -> Path:
    """Convert a text resume/cover letter to PDF.

    Args:
        text_path: Path to the .txt file to convert.
        output_path: Optional override for the output path. Defaults to same
            name with .pdf extension.
        html_only: If True, output HTML instead of PDF.

    Returns:
        Path to the generated PDF (or HTML) file.
    """
    text_path = Path(text_path)
    text = text_path.read_text(encoding="utf-8")
    resume = parse_resume(text)
    # Standalone conversion and first-run previews must not depend on a saved
    # onboarding profile. The resume text remains the source of truth.
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = {}
    html = build_html(resume, profile)
    expected_links = list(dict.fromkeys(
        re.findall(r'<a\s+href="([^"]+)"', html, re.IGNORECASE)
    ))

    if html_only:
        out = output_path or text_path.with_suffix(".html")
        out = Path(out)
        out.write_text(html, encoding="utf-8")
        log.info("HTML generated: %s", out)
        return out

    out = output_path or text_path.with_suffix(".pdf")
    out = Path(out)
    render_pdf(html, str(out))
    try:
        _verify_pdf_integrity(out, expected_text=text, expected_links=expected_links)
    except Exception:
        out.unlink(missing_ok=True)
        raise
    log.info("PDF generated: %s", out)
    return out


def batch_convert(limit: int = 50) -> int:
    """Convert .txt files in TAILORED_DIR that don't have corresponding PDFs.

    Scans for .txt files (excluding _JOB.txt and _REPORT.json), checks if a
    .pdf with the same stem already exists, and converts any that are missing.

    Args:
        limit: Maximum number of files to convert.

    Returns:
        Number of PDFs generated.
    """
    if not TAILORED_DIR.exists():
        log.warning("Tailored directory does not exist: %s", TAILORED_DIR)
        return 0

    txt_files = sorted(TAILORED_DIR.glob("*.txt"))
    # Exclude _JOB.txt and _CL.txt files from resume conversion
    # (they get their own conversion calls)
    candidates = [
        f for f in txt_files
        if not f.name.endswith("_JOB.txt")
    ]

    # Filter to those without a corresponding PDF
    to_convert: list[Path] = []
    for f in candidates:
        pdf_path = f.with_suffix(".pdf")
        if not pdf_path.exists() and not is_pdf_quarantined(f):
            to_convert.append(f)
        if len(to_convert) >= limit:
            break

    if not to_convert:
        log.info("All text files already have PDFs.")
        return 0

    log.info("Converting %d files to PDF...", len(to_convert))
    converted = 0
    for f in to_convert:
        try:
            convert_to_pdf(f)
            _pdf_failure_marker(f).unlink(missing_ok=True)
            _restore_jobs_after_pdf_repair(f)
            converted += 1
        except Exception as e:
            log.error("Failed to convert %s: %s", f.name, e)
            try:
                _quarantine_pdf_source(f, e)
            except OSError:
                log.exception("Could not quarantine failed PDF source %s", f)

    log.info("Done: %d/%d PDFs generated in %s", converted, len(to_convert), TAILORED_DIR)
    return converted
