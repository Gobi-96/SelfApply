"""Format-preserving resume tailoring based on anchored text patches.

The master document is the source of truth.  The LLM never returns a complete
replacement resume; it returns operations against stable block identifiers.
DOCX operations are applied directly to a clone of ``word/document.xml`` so
styles, numbering, tables, columns, relationships, links, headers and footers
remain in the package.  PDF editing is best-effort and falls back to the
existing visually inferred renderer when a safe in-place replacement is not
possible.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from selfapply import config
from selfapply.llm import get_client
from selfapply.scoring.ats_matcher import analyze_ats_match, extract_job_keywords

log = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
W = f"{{{W_NS}}}"
R = f"{{{R_NS}}}"
ElementTree.register_namespace("w", W_NS)
ElementTree.register_namespace("r", R_NS)

_SECTION_HEADINGS = {
    "summary": {"summary", "professional summary", "profile", "objective", "career summary"},
    "skills": {"skills", "technical skills", "core skills", "core competencies", "technologies", "expertise"},
    "experience": {"experience", "work experience", "professional experience", "employment", "work history"},
    "projects": {"projects", "key projects", "personal projects", "selected projects", "project experience"},
    "education": {"education", "academic background", "education details"},
    "certifications": {"certifications", "licenses & certifications", "licenses and certifications"},
}
_EDITABLE_SECTIONS = {"summary", "skills", "experience", "projects"}
_DATE_RE = re.compile(
    r"(?i)\b(?:19|20)\d{2}\b|\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)
_BULLET_RE = re.compile(r"^\s*[\uf0b7•●▪◦‣*-]\s+")


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _block_id(kind: str, index: int, text: str) -> str:
    digest = hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()[:10]
    return f"{kind}:{index}:{digest}"


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    values: list[str] = []
    for node in paragraph.iter():
        if node.tag == W + "t":
            values.append(node.text or "")
        elif node.tag == W + "tab":
            values.append("\t")
        elif node.tag in {W + "br", W + "cr"}:
            values.append("\n")
    return "".join(values).strip()


def _heading_for(text: str) -> str | None:
    value = _normalized(text).casefold().rstrip(":")
    for section, aliases in _SECTION_HEADINGS.items():
        if value in aliases:
            return section
    return None


def _pdf_text_segments(raw: dict) -> list[dict]:
    """Split a PDF extraction block at bullets and bold headings."""
    segments: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        spans = [span for line in current for span in line.get("spans", [])]
        values = [str(span.get("text") or "") for span in spans]
        text = _normalized(" ".join(values))
        if not text:
            current.clear()
            return
        bullet = bool(re.match(r"^\s*[\uf0b7•●▪◦‣]", text))
        if bullet:
            text = "- " + re.sub(r"^\s*[\uf0b7•●▪◦‣]\s*", "", text)
        body_spans = [
            span for span in spans
            if str(span.get("text") or "").strip()
            if not re.match(r"^\s*[\uf0b7•●▪◦‣]\s*$", str(span.get("text") or ""))
        ] or spans
        boxes = [span.get("bbox") for span in body_spans if span.get("bbox")]
        rect = [
            min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes),
        ]
        sizes = [float(span.get("size") or 0) for span in body_spans if span.get("size")]
        colors = [int(span.get("color") or 0) for span in body_spans]
        fonts = [str(span.get("font") or "") for span in body_spans]
        segments.append({
            "text": text, "rect": rect, "bullet": bullet,
            "font_size": min(sizes) if sizes else 9.0,
            "color": colors[0] if colors else 0,
            "font": fonts[0] if fonts else "",
        })
        current.clear()

    for line in raw.get("lines", []):
        spans = line.get("spans", [])
        line_text = _normalized(" ".join(str(span.get("text") or "") for span in spans))
        if not line_text:
            continue
        starts_bullet = bool(re.match(r"^\s*[\uf0b7•●▪◦‣]", line_text))
        is_bold_heading = bool(
            not starts_bullet
            and any(
                "bold" in str(span.get("font") or "").casefold()
                or int(span.get("flags") or 0) & 16
                for span in spans if str(span.get("text") or "").strip()
            )
        )
        current_text = _normalized(" ".join(
            str(span.get("text") or "")
            for item in current for span in item.get("spans", [])
        ))
        current_is_marker = bool(re.fullmatch(r"[\uf0b7•●▪◦‣]", current_text))
        if (starts_bullet or is_bold_heading or _DATE_RE.search(line_text)) and not current_is_marker:
            flush()
        current.append(line)
        if is_bold_heading or _DATE_RE.search(line_text):
            flush()
    flush()
    return segments


def _paragraph_style(paragraph: ElementTree.Element) -> str:
    node = paragraph.find(f"{W}pPr/{W}pStyle")
    return str(node.get(W + "val") or "") if node is not None else ""


def _has_numbering(paragraph: ElementTree.Element) -> bool:
    return paragraph.find(f"{W}pPr/{W}numPr") is not None


def _fully_emphasized(paragraph: ElementTree.Element | None) -> bool:
    if paragraph is None:
        return False
    runs = [run for run in paragraph.findall(f".//{W}r") if _paragraph_text(run)]
    return bool(runs) and all(run.find(f"{W}rPr/{W}b") is not None for run in runs)


def _is_editable(section: str | None, text: str, paragraph: ElementTree.Element | None = None) -> tuple[bool, str]:
    if section not in _EDITABLE_SECTIONS or _heading_for(text):
        return False, "locked"
    if section == "summary":
        return True, "summary"
    if section == "skills":
        return True, "skills"
    numbered = bool(paragraph is not None and _has_numbering(paragraph))
    if numbered or _BULLET_RE.match(text):
        return True, f"{section}_bullet"
    # Employer/project headings are generally short and commonly contain a
    # date. Prose descriptions are longer or sentence-like. This deliberately
    # errs toward locking ambiguous lines; the model cannot modify a block that
    # is not explicitly marked editable.
    looks_like_header = (
        bool(_DATE_RE.search(text)) or _fully_emphasized(paragraph)
        or (text.count("|") >= 2 and len(text) < 180)
    )
    sentence_like = len(text) >= 20
    if sentence_like and not looks_like_header:
        return True, f"{section}_description"
    return False, "locked"


def _docx_parts(content: bytes) -> tuple[ElementTree.Element, list[ElementTree.Element]]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return root, list(root.iter(W + "p"))


def _docx_geometry(root: ElementTree.Element) -> dict:
    section = next(root.iter(W + "sectPr"), None)
    page = section.find(W + "pgSz") if section is not None else None
    margins = section.find(W + "pgMar") if section is not None else None
    columns = section.find(W + "cols") if section is not None else None
    return {
        "page_width_twips": page.get(W + "w") if page is not None else None,
        "page_height_twips": page.get(W + "h") if page is not None else None,
        "orientation": page.get(W + "orient") if page is not None else None,
        "margins": dict(margins.attrib) if margins is not None else {},
        "columns": dict(columns.attrib) if columns is not None else {},
    }


def build_resume_manifest(source_path: Path, resume_text: str = "") -> dict:
    """Create the stable, serializable document model used by the LLM."""
    source_path = Path(source_path)
    content = source_path.read_bytes()
    fingerprint = hashlib.sha256(content).hexdigest()
    suffix = source_path.suffix.casefold()
    blocks: list[dict] = []
    section: str | None = None
    geometry: dict = {}
    if suffix == ".docx":
        root, paragraphs = _docx_parts(content)
        geometry = _docx_geometry(root)
        for index, paragraph in enumerate(paragraphs):
            text = _paragraph_text(paragraph)
            if not text:
                continue
            heading = _heading_for(text)
            if heading:
                section = heading
            editable, role = _is_editable(section, text, paragraph)
            has_link = paragraph.find(f".//{W}hyperlink") is not None
            # Inline hyperlinks are retained byte-for-byte; patching their
            # paragraph could detach visible link labels from relationships.
            if has_link:
                editable, role = False, "locked_link"
            blocks.append({
                "id": _block_id("docx", index, text), "source_index": index,
                "text": text, "section": section or "header", "role": role,
                "editable": editable, "style_id": _paragraph_style(paragraph),
                "numbered": _has_numbering(paragraph), "has_hyperlink": has_link,
            })
            if editable and section == "projects":
                split = re.match(r"^(.{2,80}?)(\s+[–—-]\s+|:\s+)(.+)$", text)
                if split:
                    blocks[-1]["locked_prefix"] = split.group(1) + split.group(2)
                    blocks[-1]["editable_text"] = split.group(3)
    elif suffix == ".pdf":
        try:
            import fitz  # type: ignore
            document = fitz.open(stream=content, filetype="pdf")
            for page_index, page in enumerate(document):
                page_links = [fitz.Rect(link["from"]) for link in page.get_links() if link.get("from")]
                page_dict = page.get_text("dict")
                segment_index = 0
                for block_index, raw in enumerate(page_dict.get("blocks", [])):
                    if "lines" not in raw:
                        continue
                    for segment in _pdf_text_segments(raw):
                        text = segment["text"]
                        rect = fitz.Rect(segment["rect"])
                        heading = _heading_for(text)
                        if heading:
                            section = heading
                        editable, role = _is_editable(section, text)
                        if section in {"experience", "projects"} and not segment["bullet"]:
                            editable, role = False, "locked"
                        if any(rect.intersects(link_rect) for link_rect in page_links):
                            editable, role = False, "locked_link"
                        source_index = page_index * 10000 + segment_index
                        blocks.append({
                            "id": _block_id("pdf", source_index, text),
                            "source_index": source_index, "source_block": block_index,
                            "page": page_index, "rect": [float(value) for value in rect],
                            "text": text, "section": section or "header", "role": role,
                            "editable": editable, "font_size": segment["font_size"],
                            "color": segment["color"], "font": segment["font"],
                            "bullet": segment["bullet"],
                        })
                        segment_index += 1
            geometry = {"pages": len(document)}
            document.close()
        except (ImportError, RuntimeError, ValueError):
            # The renderer can still create a best-effort result from text.
            blocks = _text_blocks(resume_text, kind="pdf-text")
            geometry = {"pages": None, "layout_capture": "unavailable"}
    else:
        blocks = _text_blocks(resume_text or source_path.read_text(encoding="utf-8"), kind="text")
    return {
        "schema_version": 1, "source_format": suffix.lstrip("."),
        "source_name": source_path.name, "master_fingerprint": fingerprint,
        "geometry": geometry, "blocks": blocks,
        "editable_block_count": sum(bool(block["editable"]) for block in blocks),
    }


def _text_blocks(text: str, kind: str = "text") -> list[dict]:
    blocks: list[dict] = []
    section: str | None = None
    for index, raw in enumerate(text.splitlines()):
        value = raw.strip()
        if not value:
            continue
        heading = _heading_for(value)
        if heading:
            section = heading
        editable, role = _is_editable(section, value)
        blocks.append({
            "id": _block_id(kind, index, value), "source_index": index,
            "text": value, "section": section or "header", "role": role,
            "editable": editable,
        })
    return blocks


def save_resume_manifest(source_path: Path, resume_text: str, output_path: Path) -> dict:
    manifest = build_resume_manifest(source_path, resume_text)
    output_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _requirements(job: dict) -> list[dict]:
    description = str(job.get("full_description") or "")
    extracted = extract_job_keywords(description, company_name=str(job.get("site") or ""))
    ordered = list(dict.fromkeys(
        extracted["required_skills"] + extracted["preferred_skills"]
        + extracted["skills"] + extracted["keywords"]
    ))
    return [
        {"id": f"jd:{index + 1}", "text": term,
         "priority": "required" if term in extracted["required_skills"] else
                     "preferred" if term in extracted["preferred_skills"] else "important"}
        for index, term in enumerate(ordered)
    ]


def _extract_json(raw: str) -> dict:
    value = raw.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", value, re.DOTALL | re.IGNORECASE)
    if match:
        value = match.group(1).strip()
    if not value.startswith("{"):
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            value = value[start:end + 1]
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("Patch response must be a JSON object.")
    return parsed


def _patch_prompt(manifest: dict, job: dict, requirements: list[dict], intensity: float, issues: list[str]) -> str:
    editable = [
        {key: block.get(key) for key in ("id", "section", "role", "text", "locked_prefix", "editable_text")}
        for block in manifest["blocks"] if block.get("editable")
    ]
    percent = round(intensity * 100)
    issue_text = "\n".join(f"- {issue}" for issue in issues[-6:]) or "- none"
    return f"""You tailor resumes by editing only explicitly permitted text blocks.
Return ONLY valid JSON matching this shape:
{{"edits":[{{"operation":"replace_text|insert_after|reorder","block_id":"existing id","text":"replacement or new bullet","block_ids":["ids for reorder"],"jd_evidence_ids":["jd:id"]}}],"warnings":["unsupported claim warnings"]}}

Rules:
- Never output a complete resume.
- Never edit names, contact details, employers, titles, dates, locations, education, certifications, links, or headings.
- When a block has locked_prefix, return only a replacement for editable_text; the prefix is restored by code.
- Use only block_id values shown below. replace_text and insert_after require text. reorder requires block_ids from one section.
- Keep each replacement close to the original block length to preserve layout; concise natural language is preferred.
- Preserve concrete metrics from the original block. Do not add employers, dates, degrees, certifications, or numeric achievements.
- Unsupported JD technologies/responsibilities ARE allowed at the user's request, but list each in warnings and integrate them naturally rather than keyword stuffing.
- At {percent}% tailoring, modify approximately that share of relevant editable blocks. At 50%, make moderate changes. At 100%, cover all important JD requirements across summary, skills, projects and relevant experience.
- Rephrase work and projects around the JD. Do not delete unrelated legitimate history.
- Each edit must cite the JD requirement ids that justify it.

TARGET JOB:
Title: {job.get('title', '')}
Company: {job.get('site', '')}
Description: {str(job.get('full_description') or '')[:10000]}

JD REQUIREMENTS:
{json.dumps(requirements, ensure_ascii=False)}

EDITABLE MASTER BLOCKS:
{json.dumps(editable, ensure_ascii=False)}

ISSUES TO CORRECT FROM A PREVIOUS PASS:
{issue_text}
"""


def validate_patch_set(raw: dict, manifest: dict) -> dict:
    editable = {block["id"]: block for block in manifest["blocks"] if block.get("editable")}
    edits = raw.get("edits")
    if not isinstance(edits, list):
        raise TypeError("Patch response is missing an edits list.")
    clean: list[dict] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        operation = str(edit.get("operation") or "")
        block_id = str(edit.get("block_id") or "")
        if operation not in {"replace_text", "insert_after", "reorder"}:
            continue
        if block_id not in editable:
            continue
        normalized = {"operation": operation, "block_id": block_id}
        if operation in {"replace_text", "insert_after"}:
            text = _normalized(str(edit.get("text") or ""))
            if not text or len(text) > 1000:
                continue
            normalized["text"] = text
        else:
            ids = [str(value) for value in edit.get("block_ids") or []]
            section = editable[block_id]["section"]
            if not ids or set(ids) - editable.keys() or any(editable[value]["section"] != section for value in ids):
                continue
            normalized["block_ids"] = ids
        normalized["jd_evidence_ids"] = [str(value) for value in edit.get("jd_evidence_ids") or []]
        clean.append(normalized)
    return {"schema_version": 1, "edits": clean,
            "warnings": [str(value) for value in raw.get("warnings") or []]}


def _apply_to_lines(text: str, manifest: dict, patch_set: dict) -> str:
    if manifest.get("source_format") != "txt" and not all(
        int(block.get("source_index", -1)) < len(text.splitlines())
        for block in manifest["blocks"]
    ):
        return _apply_to_extracted_text(text, manifest, patch_set)
    lines = text.splitlines()
    lookup = {block["id"]: block for block in manifest["blocks"]}
    inserts: dict[int, list[str]] = {}
    replacements: dict[int, str] = {}
    for edit in patch_set["edits"]:
        block = lookup[edit["block_id"]]
        index = int(block["source_index"])
        if edit["operation"] == "replace_text":
            original = lines[index]
            prefix = re.match(r"^\s*[•●▪◦‣*-]\s+", original)
            replacements[index] = (prefix.group(0) if prefix else "") + _replacement_for_block(block, edit["text"])
        elif edit["operation"] == "insert_after":
            original = lines[index]
            prefix = re.match(r"^\s*[•●▪◦‣*-]\s+", original)
            inserts.setdefault(index, []).append((prefix.group(0) if prefix else "- ") + edit["text"])
    output: list[str] = []
    for index, line in enumerate(lines):
        output.append(replacements.get(index, line))
        output.extend(inserts.get(index, []))
    return "\n".join(output).rstrip() + "\n"


def _apply_to_extracted_text(text: str, manifest: dict, patch_set: dict) -> str:
    """Mirror document edits into extraction text for ATS scoring/reporting."""
    output = text
    lookup = {block["id"]: block for block in manifest["blocks"]}
    for edit in patch_set["edits"]:
        block = lookup[edit["block_id"]]
        original = str(block["text"])
        if edit["operation"] == "replace_text" and original in output:
            output = output.replace(original, _replacement_for_block(block, edit["text"]), 1)
        elif edit["operation"] == "replace_text" and manifest.get("source_format") == "pdf":
            original_body = re.sub(r"^\s*-\s*", "", original)
            words = re.findall(r"\S+", original_body)
            if words:
                pattern = r"(?:[\uf0b7•●▪◦‣]\s*)?" + r"\s+".join(
                    re.escape(word) for word in words
                )
                output = re.sub(
                    pattern, _replacement_for_block(block, edit["text"]),
                    output, count=1, flags=re.DOTALL,
                )
        elif edit["operation"] == "insert_after" and original in output:
            output = output.replace(original, original + "\n- " + edit["text"], 1)
    return output.rstrip() + "\n"


def _replace_paragraph_text(paragraph: ElementTree.Element, text: str) -> None:
    text_nodes = list(paragraph.iter(W + "t"))
    if not text_nodes:
        run = paragraph.find(W + "r")
        if run is None:
            run = ElementTree.SubElement(paragraph, W + "r")
        text_node = ElementTree.SubElement(run, W + "t")
        text_nodes = [text_node]
    text_nodes[0].text = text
    text_nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for node in text_nodes[1:]:
        node.text = ""


def _replacement_for_block(block: dict, replacement: str) -> str:
    prefix = str(block.get("locked_prefix") or "")
    if not prefix:
        return replacement
    # Models occasionally echo the locked project label despite being asked
    # for only editable_text. Strip that echo before code restores the exact
    # source prefix, preventing "Project - Project - description" output.
    label = re.sub(r"\s+[–—-]\s+$|:\s+$", "", prefix).strip()
    repeated = re.compile(
        rf"^\s*{re.escape(label)}\s*(?:[–—-]|:)\s*", re.IGNORECASE,
    )
    return prefix + repeated.sub("", replacement, count=1)


def apply_docx_patch(master_path: Path, manifest: dict, patch_set: dict, output_path: Path) -> Path:
    content = Path(master_path).read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["master_fingerprint"]:
        raise RuntimeError("Master resume changed after its block manifest was created.")
    root, paragraphs = _docx_parts(content)
    lookup = {block["id"]: block for block in manifest["blocks"]}
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for edit in patch_set["edits"]:
        block = lookup[edit["block_id"]]
        paragraph = paragraphs[int(block["source_index"])]
        if _paragraph_text(paragraph) != block["text"]:
            raise RuntimeError(f"Resume block no longer matches manifest: {block['id']}")
        if edit["operation"] == "replace_text":
            _replace_paragraph_text(paragraph, _replacement_for_block(block, edit["text"]))
        elif edit["operation"] == "insert_after":
            parent = parent_map[paragraph]
            cloned = copy.deepcopy(paragraph)
            _replace_paragraph_text(cloned, edit["text"])
            parent.insert(list(parent).index(paragraph) + 1, cloned)
        # Reordering is intentionally applied after text operations below.
    for edit in patch_set["edits"]:
        if edit["operation"] != "reorder":
            continue
        ordered = [paragraphs[int(lookup[value]["source_index"])] for value in edit["block_ids"]]
        if not ordered:
            continue
        parent = parent_map[ordered[0]]
        if any(parent_map[node] is not parent for node in ordered):
            continue
        position = min(list(parent).index(node) for node in ordered)
        for node in ordered:
            parent.remove(node)
        for offset, node in enumerate(ordered):
            parent.insert(position + offset, node)
    document_xml = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output_path, "w") as target:
        for item in source.infolist():
            target.writestr(item, document_xml if item.filename == "word/document.xml" else source.read(item.filename))
    return output_path


def extract_docx_text(path: Path) -> str:
    root, paragraphs = _docx_parts(Path(path).read_bytes())
    del root
    return "\n".join(value for value in (_paragraph_text(p) for p in paragraphs) if value).rstrip() + "\n"


def validate_docx_preservation(master_path: Path, tailored_path: Path, manifest: dict) -> dict:
    """Verify package structure and every locked block survived unchanged."""
    with zipfile.ZipFile(master_path) as master, zipfile.ZipFile(tailored_path) as tailored:
        if set(master.namelist()) != set(tailored.namelist()):
            raise RuntimeError("Tailored DOCX changed the master package structure.")
        protected_parts = [
            name for name in master.namelist()
            if name != "word/document.xml"
        ]
        changed = [name for name in protected_parts if master.read(name) != tailored.read(name)]
        if changed:
            raise RuntimeError("Tailored DOCX changed protected styles, links, headers, or assets.")
    output_manifest = build_resume_manifest(tailored_path, extract_docx_text(tailored_path))
    output_texts = [block["text"] for block in output_manifest["blocks"]]
    missing_locked = [
        block["text"] for block in manifest["blocks"]
        if not block.get("editable") and block["text"] not in output_texts
    ]
    if missing_locked:
        raise RuntimeError("Tailored DOCX changed locked identity or career-history blocks.")
    if output_manifest.get("geometry") != manifest.get("geometry"):
        raise RuntimeError("Tailored DOCX changed page geometry, margins, or columns.")
    return {
        "package_parts_preserved": True, "locked_blocks_preserved": True,
        "geometry_preserved": True,
    }


def _pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(path).pages)


def render_docx_pdf(docx_path: Path, output_path: Path) -> Path:
    """Render DOCX through LibreOffice, keeping Word layout instead of HTML."""
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        common = [
            Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
            Path("C:/Program Files/LibreOffice/program/soffice.exe"),
            Path("C:/Program Files (x86)/LibreOffice/program/soffice.exe"),
        ]
        executable = next((str(path) for path in common if path.is_file()), "")
    if not executable:
        raise RuntimeError("LibreOffice is unavailable for format-preserving DOCX-to-PDF rendering.")
    with tempfile.TemporaryDirectory(prefix="selfapply-docx-") as folder:
        result = subprocess.run(
            [executable, "--headless", "--convert-to", "pdf", "--outdir", folder, str(docx_path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        generated = Path(folder) / f"{docx_path.stem}.pdf"
        if result.returncode or not generated.is_file():
            raise RuntimeError((result.stderr or result.stdout or "DOCX-to-PDF conversion failed").strip())
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated, output_path)
    return output_path


def apply_pdf_patch(master_path: Path, manifest: dict, patch_set: dict, output_path: Path) -> Path:
    """Overlay bounded replacements without rewriting the master PDF objects."""
    try:
        import fitz  # type: ignore
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("PDF overlay dependencies are unavailable.") from exc
    if any(edit["operation"] != "replace_text" for edit in patch_set["edits"]):
        raise RuntimeError("This PDF needs inserted or reordered content and must use visual reconstruction.")
    lookup = {block["id"]: block for block in manifest["blocks"]}
    source = PdfReader(master_path)
    page_edits: dict[int, list[dict]] = {}
    for edit in patch_set["edits"]:
        block = lookup[edit["block_id"]]
        rect = fitz.Rect(block["rect"])
        color_value = int(block.get("color") or 0)
        color = (
            ((color_value >> 16) & 255) / 255,
            ((color_value >> 8) & 255) / 255,
            (color_value & 255) / 255,
        )
        clean_text = re.sub(
            r"\s*[\uf0b7•●▪◦‣]\s*", " ", str(edit["text"]),
        ).strip()
        if block.get("bullet"):
            clean_text = re.sub(r"^\s*-\s*", "", clean_text)
        original_font = str(block.get("font") or "").casefold()
        font_name = "tiro" if any(
            name in original_font for name in ("cambria", "times", "serif")
        ) else "helv"
        reportlab_font = "Times-Roman" if font_name == "tiro" else "Helvetica"
        original_size = max(7.5, float(block.get("font_size") or 9.0))
        selected: tuple[float, list[str]] | None = None
        minimum_size = max(8.5, original_size - 1.5)
        for font_size in [original_size - step * 0.5 for step in range(4)]:
            if font_size < minimum_size:
                break
            lines: list[str] = []
            current = ""
            for word in clean_text.split():
                candidate = f"{current} {word}".strip()
                if current and stringWidth(candidate, reportlab_font, font_size) > rect.width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                lines.append(current)
            line_height = font_size * 1.08
            if lines and len(lines) * line_height <= rect.height + 2.5:
                selected = (font_size, lines)
                break
        if selected:
            page_edits.setdefault(int(block["page"]), []).append({
                "rect": rect, "font": reportlab_font, "size": selected[0],
                "lines": selected[1], "color": color, "edit": edit,
            })
    pending = [item for items in page_edits.values() for item in items]
    if not pending:
        raise RuntimeError("No tailored PDF paragraph fit safely in its original text box.")
    accepted = {id(item["edit"]) for item in pending}
    patch_set["edits"] = [edit for edit in patch_set["edits"] if id(edit) in accepted]

    overlay_bytes = io.BytesIO()
    first_page = source.pages[0]
    overlay = canvas.Canvas(
        overlay_bytes,
        pagesize=(float(first_page.mediabox.width), float(first_page.mediabox.height)),
    )
    for page_index, source_page in enumerate(source.pages):
        width = float(source_page.mediabox.width)
        height = float(source_page.mediabox.height)
        overlay.setPageSize((width, height))
        for item in page_edits.get(page_index, []):
            rect = item["rect"]
            # PDF extraction coordinates use a top-left origin; ReportLab uses
            # bottom-left. The bullet glyph lies outside this content rect and
            # remains an original master object.
            left = float(rect.x0) - 0.5
            bottom = height - float(rect.y1) - 0.5
            box_height = float(rect.height) + 1.0
            overlay.setFillColorRGB(1, 1, 1)
            overlay.rect(left, bottom, float(rect.width) + 1.0, box_height, stroke=0, fill=1)
            overlay.setFillColorRGB(*item["color"])
            overlay.setFont(item["font"], item["size"])
            baseline = height - float(rect.y0) - item["size"]
            for line in item["lines"]:
                overlay.drawString(float(rect.x0), baseline, line)
                baseline -= item["size"] * 1.08
        overlay.showPage()
    overlay.save()
    overlay_bytes.seek(0)
    overlays = PdfReader(overlay_bytes)
    writer = PdfWriter()
    # Clone the document once so shared resources keep one stable translation
    # map. Adding malformed exported pages one-by-one can drop referenced font
    # objects when edits exist on more than one page.
    writer.clone_document_from_reader(source)
    for page_index, source_page in enumerate(writer.pages):
        # Many exported resumes share one indirect /Resources dictionary
        # across pages. merge_page mutates it; a second merge can then corrupt
        # font references on every page. Give each page an independent copy.
        from pypdf.generic import NameObject
        source_page[NameObject("/Resources")] = copy.deepcopy(source_page["/Resources"])
        source_page.merge_page(overlays.pages[page_index], over=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as stream:
        writer.write(stream)
    _validate_pdf_locked_regions(master_path, output_path, manifest)
    return output_path


def _region_ink(page, rect, zoom: float = 2.0) -> int:
    import fitz  # type: ignore
    pix = page.get_pixmap(
        matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(rect), alpha=False,
    )
    channels = pix.n
    samples = pix.samples
    return sum(
        1 for index in range(0, len(samples), channels)
        if min(samples[index:index + min(3, channels)]) < 240
    )


def _validate_pdf_locked_regions(
    master_path: Path, output_path: Path, manifest: dict,
) -> None:
    """Prove untouched PDF regions and hyperlinks remain visibly present."""
    import fitz  # type: ignore
    master = fitz.open(master_path)
    output = fitz.open(output_path)
    try:
        if len(master) != len(output):
            raise RuntimeError("Tailored PDF changed the master page count.")
        master_links = sorted(
            str(link.get("uri") or "") for page in master
            for link in page.get_links() if link.get("uri")
        )
        output_links = sorted(
            str(link.get("uri") or "") for page in output
            for link in page.get_links() if link.get("uri")
        )
        if master_links != output_links:
            raise RuntimeError("Tailored PDF changed or removed master hyperlinks.")
        for block in manifest.get("blocks", []):
            if block.get("editable") or not block.get("rect"):
                continue
            page_index = int(block.get("page") or 0)
            before = _region_ink(master[page_index], block["rect"])
            after = _region_ink(output[page_index], block["rect"])
            if before >= 12 and after < before * 0.65:
                raise RuntimeError(
                    "Tailored PDF lost visible locked content; the master PDF will be used."
                )
    finally:
        master.close()
        output.close()


def _source_path(resume_text_path: Path | None = None) -> Path:
    # Test/custom deployments may redirect resume.txt without redirecting all
    # global config constants. In that case the explicitly supplied resume is
    # the only authoritative source.
    if resume_text_path is not None:
        explicit = Path(resume_text_path)
        if explicit.is_file() and explicit.parent != config.APP_DIR:
            return explicit
    try:
        source = json.loads(config.RESUME_SOURCE_PATH.read_text(encoding="utf-8"))
        suffix = str(source.get("format") or "").casefold()
    except (OSError, ValueError, TypeError):
        suffix = ""
    candidate = {".docx": config.RESUME_DOCX_PATH, ".pdf": config.RESUME_PDF_PATH}.get(suffix)
    if candidate and candidate.is_file():
        return candidate
    for path in (config.RESUME_DOCX_PATH, config.RESUME_PDF_PATH, config.RESUME_PATH):
        if path.is_file():
            return path
    raise RuntimeError("No master resume is available.")


def tailor_document(
    resume_text: str, job: dict, profile: dict, *, target_aggressiveness: float | None = None,
    max_retries: int = 3, source_path: Path | None = None,
) -> tuple[str, dict, dict, dict]:
    """Generate and score a bounded patch set without replacing the resume."""
    del profile  # Reserved for provider-specific policy expansion.
    source = Path(source_path) if source_path else _source_path()
    manifest = build_resume_manifest(source, resume_text)
    intensity = max(0.0, min(1.0, float(0.5 if target_aggressiveness is None else target_aggressiveness)))
    requirements = _requirements(job)
    master_ats = analyze_ats_match(resume_text, str(job.get("full_description") or ""), company_name=str(job.get("site") or ""))
    best: tuple[str, dict, dict] | None = None
    issues: list[str] = []
    client = get_client()
    for attempt in range(max_retries + 1):
        raw = client.chat(
            [{"role": "system", "content": _patch_prompt(manifest, job, requirements, intensity, issues)},
             {"role": "user", "content": "Create the anchored resume patch set now."}],
            max_tokens=4000, temperature=0.2,
        )
        try:
            patch_set = validate_patch_set(_extract_json(raw), manifest)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f"Return valid patch JSON: {exc}")
            continue
        candidate = _apply_to_lines(resume_text, manifest, patch_set)
        ats = analyze_ats_match(candidate, str(job.get("full_description") or ""), master_resume_text=resume_text, company_name=str(job.get("site") or ""))
        if best is None or ats["overall_score"] > best[1]["overall_score"]:
            best = (candidate, ats, patch_set)
        reach = 90.0 if intensity >= 0.95 else master_ats["overall_score"] + max(1.0, (90.0 - master_ats["overall_score"]) * intensity)
        if ats["overall_score"] >= min(90.0, reach) and patch_set["edits"]:
            break
        issues.append(
            f"Improve JD coverage without changing locked blocks. Current internal score {ats['overall_score']}; target {min(90.0, reach):.1f}."
        )
    if best is None:
        patch_set = {"schema_version": 1, "edits": [], "warnings": ["The AI did not return a valid patch set."]}
        candidate = resume_text
        ats = master_ats
    else:
        candidate, ats, patch_set = best
    block_lookup = {block["id"]: block for block in manifest["blocks"]}
    changes = []
    for edit in patch_set["edits"]:
        block = block_lookup[edit["block_id"]]
        changes.append({
            "operation": edit["operation"], "block_id": edit["block_id"],
            "section": block.get("section"), "before": block.get("text", ""),
            "after": _replacement_for_block(block, edit.get("text", ""))
            if edit["operation"] != "reorder" else "Reordered existing blocks",
            "jd_evidence_ids": edit.get("jd_evidence_ids", []),
        })
    report = {
        "status": "approved", "pipeline": "anchored_patch", "attempts": min(max_retries + 1, len(issues) + 1),
        "source_format": manifest["source_format"], "master_fingerprint": manifest["master_fingerprint"],
        "changed_block_count": len(patch_set["edits"]), "format_fidelity": "exact-structure" if manifest["source_format"] == "docx" else "best-effort",
        "master_ats_match": master_ats, "ats_match": ats,
        "unsupported_claim_warnings": patch_set.get("warnings", []),
        "unverified_claims_allowed": True, "refinement_issues": issues,
        "changes": changes, "job_requirements": requirements,
    }
    return candidate, report, patch_set, manifest


def create_tailored_artifacts(
    source_path: Path, text: str, patch_set: dict, manifest: dict, prefix_path: Path,
) -> dict:
    """Create per-job TXT, source-format, manifest and application PDF artifacts."""
    prefix_path.parent.mkdir(parents=True, exist_ok=True)
    text_path = prefix_path.with_suffix(".txt")
    text_path.write_text(text, encoding="utf-8")
    manifest_path = prefix_path.with_name(prefix_path.name + "_MANIFEST.json")
    patch_path = prefix_path.with_name(prefix_path.name + "_PATCHES.json")
    result: dict = {"text_path": text_path, "manifest_path": manifest_path, "patch_path": patch_path,
                    "docx_path": None, "pdf_path": None, "fidelity_warning": None,
                    "preservation_checks": {}}
    if source_path.suffix.casefold() == ".docx":
        docx_path = prefix_path.with_suffix(".docx")
        apply_docx_patch(source_path, manifest, patch_set, docx_path)
        result["preservation_checks"] = validate_docx_preservation(
            source_path, docx_path, manifest,
        )
        result["docx_path"] = docx_path
        # The document-derived extraction is authoritative for scoring/upload checks.
        rendered_text = extract_docx_text(docx_path)
        text_path.write_text(rendered_text, encoding="utf-8")
        try:
            result["pdf_path"] = render_docx_pdf(docx_path, prefix_path.with_suffix(".pdf"))
            with tempfile.TemporaryDirectory(prefix="selfapply-master-pages-") as folder:
                master_pdf = render_docx_pdf(source_path, Path(folder) / "master.pdf")
                master_pages = _pdf_page_count(master_pdf)
            output_pages = _pdf_page_count(result["pdf_path"])
            result["preservation_checks"].update({
                "master_pages": master_pages, "tailored_pages": output_pages,
                "page_growth_within_limit": output_pages <= master_pages + 1,
            })
            if output_pages > master_pages + 1:
                lookup = {block["id"]: block for block in manifest["blocks"]}
                compact_edits = [
                    edit for edit in patch_set["edits"]
                    if edit["operation"] == "replace_text"
                    and len(_replacement_for_block(lookup[edit["block_id"]], edit["text"]))
                    <= max(80, int(len(str(lookup[edit["block_id"]]["text"])) * 1.15))
                ]
                patch_set["edits"] = compact_edits
                apply_docx_patch(source_path, manifest, patch_set, docx_path)
                result["preservation_checks"] = validate_docx_preservation(
                    source_path, docx_path, manifest,
                )
                text_path.write_text(extract_docx_text(docx_path), encoding="utf-8")
                result["pdf_path"] = render_docx_pdf(
                    docx_path, prefix_path.with_suffix(".pdf"),
                )
                compact_pages = _pdf_page_count(result["pdf_path"])
                result["preservation_checks"].update({
                    "master_pages": master_pages, "tailored_pages": compact_pages,
                    "page_growth_within_limit": compact_pages <= master_pages + 1,
                    "edits_compacted_for_page_limit": True,
                })
                if compact_pages > master_pages + 1:
                    patch_set["edits"] = []
                    shutil.copy2(source_path, docx_path)
                    text_path.write_text(extract_docx_text(docx_path), encoding="utf-8")
                    result["pdf_path"] = render_docx_pdf(
                        docx_path, prefix_path.with_suffix(".pdf"),
                    )
                    result["preservation_checks"].update({
                        "tailored_pages": master_pages,
                        "page_growth_within_limit": True,
                        "fell_back_to_master_for_page_limit": True,
                    })
                    result["fidelity_warning"] = (
                        "Tailored edits exceeded the one-page growth limit, so the exact master layout was retained."
                    )
        except RuntimeError as exc:
            result["fidelity_warning"] = str(exc)
    elif source_path.suffix.casefold() == ".pdf":
        try:
            result["pdf_path"] = apply_pdf_patch(
                source_path, manifest, patch_set, prefix_path.with_suffix(".pdf"),
            )
        except RuntimeError as exc:
            # PDF is fixed-layout. Never expose or upload a damaged
            # reconstruction when bounded edits cannot be proven safe.
            safe_pdf = prefix_path.with_suffix(".pdf")
            shutil.copy2(source_path, safe_pdf)
            result["pdf_path"] = safe_pdf
            result["fidelity_warning"] = f"{exc} The unchanged master PDF was retained."
            patch_set["edits"] = []
            try:
                import fitz  # type: ignore
                document = fitz.open(source_path)
                master_text = "\n".join(page.get_text() for page in document).strip()
                document.close()
                if master_text:
                    text_path.write_text(master_text + "\n", encoding="utf-8")
            except (ImportError, RuntimeError, ValueError):
                pass
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    patch_path.write_text(json.dumps(patch_set, indent=2), encoding="utf-8")
    result["actual_edit_count"] = len(patch_set["edits"])
    return result
