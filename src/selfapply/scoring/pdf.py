"""Text-to-PDF conversion for tailored resumes and cover letters.

Parses the structured text resume format, renders via an HTML/CSS template,
and exports to PDF using headless Chromium via Playwright.
"""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime
from html import escape
from pathlib import Path

from selfapply.config import RESUME_PDF_PATH, TAILORED_DIR, load_profile

log = logging.getLogger(__name__)


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
        return payload.get("source") == _source_fingerprint(text_path)
    except (OSError, ValueError, TypeError):
        return False


def _quarantine_pdf_source(text_path: Path, error: Exception) -> None:
    payload = {
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
    "TECHNICAL SKILLS": "TECHNICAL SKILLS",
    "SKILLS": "TECHNICAL SKILLS",
    "EXPERIENCE": "EXPERIENCE",
    "PROFESSIONAL EXPERIENCE": "EXPERIENCE",
    "WORK EXPERIENCE": "EXPERIENCE",
    "PROJECTS": "PROJECTS",
    "EDUCATION": "EDUCATION",
    "EDUCATION & CERTIFICATIONS": "EDUCATION",
}


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
    """Recover non-profile credential links embedded in the master PDF."""
    if not RESUME_PDF_PATH.exists():
        return []
    try:
        from pypdf import PdfReader

        uris: list[str] = []
        for page in PdfReader(RESUME_PDF_PATH).pages:
            for annotation in page.get("/Annots") or []:
                obj = annotation.get_object()
                action = obj.get("/A") or {}
                uri = str(action.get("/URI") or "").strip()
                if uri and uri not in uris:
                    uris.append(uri)
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
        # Detect section headers (all caps, no leading dash/bullet, longer than 3 chars)
        if (
            stripped
            and stripped == stripped.upper()
            and not stripped.startswith("-")
            and len(stripped) > 3
            and not stripped.startswith("\u2022")
        ):
            if current_section:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = _SECTION_ALIASES.get(stripped, stripped)
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

    # Skills
    skills_html = ""
    if "TECHNICAL SKILLS" in sections:
        skills = parse_skills(sections["TECHNICAL SKILLS"])
        rows = ""
        for cat, val in skills:
            rows += f'<div class="skill-row"><span class="skill-cat">{cat}:</span> {val}</div>\n'
        skills_html = f'<div class="section"><div class="section-title">Technical Skills</div>{rows}</div>'

    # Experience
    exp_html = ""
    if "EXPERIENCE" in sections:
        entries = parse_entries(sections["EXPERIENCE"])
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            technologies = (
                f'<div class="entry-tech"><b>Technologies:</b> {e["technologies"]}</div>'
                if e.get("technologies") else ""
            )
            items += f'<div class="entry"><div class="entry-title">{_linkify_labels(e["title"], profile)}</div>{subtitle}<ul>{bullets}</ul>{technologies}</div>'
        exp_html = f'<div class="section"><div class="section-title">Experience</div>{items}</div>'

    # Projects
    proj_html = ""
    if "PROJECTS" in sections:
        entries = parse_entries(sections["PROJECTS"])
        total_body_chars = sum(len(value) for value in sections.values())
        project_class = "section page-break-before" if total_body_chars > 5000 else "section"
        items = ""
        for e in entries:
            bullets = "".join(f"<li>{b}</li>" for b in e["bullets"])
            subtitle = f'<div class="entry-subtitle">{e["subtitle"]}</div>' if e["subtitle"] else ""
            technologies = (
                f'<div class="entry-tech"><b>Technologies:</b> {e["technologies"]}</div>'
                if e.get("technologies") else ""
            )
            items += f'<div class="entry"><div class="entry-title">{_linkify_labels(e["title"], profile)}</div>{subtitle}<ul>{bullets}</ul>{technologies}</div>'
        proj_html = f'<div class="{project_class}"><div class="section-title">Projects</div>{items}</div>'

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
        edu_html = f'<div class="section education"><div class="section-title">Education</div><div class="edu">{edu_rows}</div></div>'

    # Summary
    summary_html = ""
    if "SUMMARY" in sections:
        summary_html = f'<div class="section"><div class="section-title">Summary</div><div class="summary">{sections["SUMMARY"].strip()}</div></div>'

    # Contact line parsing
    contact = resume["contact"]
    contact_parts = [p.strip() for p in contact.split("|")] if contact else []
    contact_html = " &nbsp;|&nbsp; ".join(
        _contact_part_html(part, profile) for part in contact_parts if part
    )

    # Location line (may be empty)
    location_html = f'<div class="location">{resume["location"]}</div>' if resume["location"] else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
@page {{
    size: letter;
    margin: 0.3in 0.45in;
}}
* {{
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}}
body {{
    font-family: 'Calibri', 'Segoe UI', Arial, sans-serif;
    font-size: 9pt;
    line-height: 1.25;
    color: #1a1a1a;
}}
.header {{
    text-align: center;
    margin-bottom: 4px;
    padding-bottom: 4px;
    border-bottom: 1.5px solid #2a7ab5;
}}
.name {{
    font-size: 17pt;
    font-weight: 700;
    color: #1a3a5c;
    letter-spacing: 0.5px;
}}
.title {{
    font-size: 10pt;
    color: #3a6b8c;
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
    margin-top: 3px;
}}
.page-break-before {{
    break-before: page;
}}
.section-title {{
    font-size: 9.5pt;
    font-weight: 700;
    color: #1a3a5c;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1.5px solid #2a7ab5;
    padding-bottom: 1px;
    margin-bottom: 2px;
}}
.summary {{
    font-size: 9pt;
    color: #333;
    line-height: 1.3;
}}
.skill-row {{
    font-size: 9pt;
    margin: 0;
    line-height: 1.25;
}}
.skill-cat {{
    font-weight: 600;
    color: #1a3a5c;
}}
.entry {{
    margin-bottom: 2px;
    break-inside: avoid;
}}
.entry-title {{
    font-weight: 600;
    font-size: 9.5pt;
    color: #1a3a5c;
}}
.entry-subtitle {{
    font-size: 8.5pt;
    color: #4a7a9b;
    font-style: italic;
    margin-bottom: 1px;
}}
.entry-tech {{
    font-size: 8.5pt;
    color: #1a3a5c;
    margin-top: 1px;
}}
ul {{
    margin-left: 14px;
    padding: 0;
}}
li {{
    font-size: 9pt;
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
    <div class="name">{resume['name']}</div>
    <div class="title">{resume['title']}</div>
    {location_html}
    <div class="contact">{contact_html}</div>
</div>
{summary_html}
{skills_html}
{exp_html}
{proj_html}
{edu_html}
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
    required = {
        "summary": ("summary", "profile", "objective"),
        "skills": ("skills", "technologies", "competencies"),
        "experience": ("experience", "work history", "employment"),
        "education": ("education", "academic", "degree"),
    }
    missing = [
        section for section, variants in required.items()
        if not any(term in rendered_lower for term in variants)
    ]
    if missing:
        raise RuntimeError(
            "Generated PDF is missing readable sections: " + ", ".join(missing)
        )
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
            format="Letter",
            # Leave a small pagination safety margin so one final education
            # or credential line cannot spill onto an otherwise blank page.
            scale=0.96,
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
    profile = load_profile()
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
