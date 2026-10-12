"""Bounded document extraction. Extracted entries are drafts for human verification."""
import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from .engine import BUREAUS, parse_letter, validate_report

MAX_FILE = 10 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
SUPPORTED = {".pdf", ".docx", ".txt", ".csv", ".json"}
BUREAU_NAMES = {"transunion": "TU", "experian": "EX", "equifax": "EQ", "tu": "TU", "ex": "EX", "eq": "EQ"}


def bureau(value):
    return BUREAU_NAMES.get(value.strip().lower(), "")


def document_date(value):
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return value  # Invalid/ambiguous values remain visible for correction, never guessed.


def read_text(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError("Supported files: PDF, DOCX, TXT, CSV, and JSON. Convert legacy DOC files to DOCX.")
    if not content or len(content) > MAX_FILE:
        raise ValueError("Upload a nonempty file no larger than 10 MB")
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("The uploaded file is not a valid PDF")
        from pypdf import PdfReader
        from pypdf import filters
        # pypdf exposes supported decompression limits; never raise its defaults.
        for setting in ("MAX_DECLARED_STREAM_LENGTH", "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
                        "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH", "ZLIB_MAX_OUTPUT_LENGTH"):
            setattr(filters, setting, min(getattr(filters, setting), MAX_TEXT))
        try:
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise ValueError("Unlock the PDF before uploading")
            if len(reader.pages) > 100:
                raise ValueError("PDF exceeds 100 pages; split it before uploading")
            pages = []
            for page in reader.pages:
                # Guard decompression before handing content to the text extractor.
                streams = page.get("/Contents")
                if streams:
                    streams = streams.get_object()
                    for stream in (streams if isinstance(streams, list) else [streams]):
                        stream = stream.get_object()
                        if len(getattr(stream, "_data", b"")) > MAX_TEXT:
                            raise ValueError("PDF page content exceeds extraction limits")
                pages.append(page.extract_text() or "")
                if sum(len(x) for x in pages) > MAX_TEXT:
                    raise ValueError("Extracted document exceeds 2 MB")
            missing_pages = [i + 1 for i, text in enumerate(pages) if not text.strip()]
            return "\n\n".join(pages), missing_pages
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("PDF could not be safely read; upload a valid text-based PDF") from error
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if len(archive.infolist()) > 1000 or sum(i.file_size for i in archive.infolist()) > 20 * MAX_FILE:
                    raise ValueError("DOCX archive exceeds extraction limits")
                # Include headers/footers and tables, not just top-level paragraphs.
                names = [n for n in archive.namelist() if re.fullmatch(r"word/(document|header\d+|footer\d+)\.xml", n)]
                if "word/document.xml" not in names:
                    raise ValueError("DOCX has no document body")
                parts = []
                body_text = []
                for name in names:
                    if archive.getinfo(name).file_size > MAX_TEXT:
                        raise ValueError("DOCX text exceeds extraction limits")
                    raw = archive.read(name)
                    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                        raise ValueError("Unsupported XML declarations")
                    root = ElementTree.fromstring(raw)
                    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                    for paragraph in root.iter(ns + "p"):
                        paragraph_text = "".join(el.text or "" for el in paragraph.iter(ns + "t"))
                        parts.append(paragraph_text)
                        if name == "word/document.xml":
                            body_text.append(paragraph_text)
                text = "\n".join(parts)
                return text, [] if "\n".join(body_text).strip() else [1]
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as error:
            raise ValueError("The uploaded file is not a valid DOCX") from error
    try:
        return content.decode("utf-8-sig"), []
    except UnicodeDecodeError as error:
        raise ValueError("Text files must use UTF-8 encoding") from error


def drafts(text, kind):
    report = {"inquiries": [], "accounts": []}
    notes = []
    stripped = text.strip()
    if stripped.startswith("{") and kind == "source":
        try:
            return validate_report(json.loads(stripped)), notes
        except (ValueError, TypeError, AttributeError) as error:
            notes.append(f"JSON needs correction: {error}")
    if stripped.startswith("bureau,name,date"):
        try:
            entries = parse_letter(stripped)
            report["inquiries"] = [{"id": f"i{n}", **entry} for n, entry in enumerate(entries, 1)]
            if kind == "source":
                notes.append("This file contains inquiries only. Add and verify all open/positive accounts from the complete report.")
            return report, notes
        except ValueError as error:
            notes.append(str(error))
    if stripped.startswith("row_type,") and kind == "source":
        for n, row in enumerate(csv.DictReader(io.StringIO(stripped)), 1):
            if row.get("row_type") == "inquiry":
                report["inquiries"].append({"id": row.get("id") or f"i{n}", "bureau": bureau(row.get("bureau", "")),
                    "name": row.get("name", ""), "date": document_date(row.get("date", ""))})
            elif row.get("row_type") == "account":
                report["accounts"].append({"id": row.get("id") or f"a{n}", "bureau": bureau(row.get("bureau", "")),
                    "name": row.get("name", ""), "opened": document_date(row.get("opened", "")), "status": row.get("status", ""),
                    "payment_history": row.get("payment_history", ""), "product": row.get("product", "")})
            else:
                notes.append(f"Unrecognized CSV row {n}; review it manually")
        return report, notes
    context = ""
    date_pattern = r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        heading = bureau(line.rstrip(":"))
        if heading:
            context = heading
            continue
        parts = [p.strip() for p in line.split("|")]
        if parts[0].upper() == "ACCOUNT" and len(parts) == 7 and kind == "source":
            report["accounts"].append({"id": f"a{len(report['accounts'])+1}", "bureau": bureau(parts[1]), "name": parts[2],
                "opened": document_date(parts[3]), "status": parts[4].lower(), "payment_history": parts[5].lower(), "product": parts[6]})
            continue
        if parts[0].upper() == "INQUIRY" and len(parts) == 4:
            report["inquiries"].append({"id": f"i{len(report['inquiries'])+1}", "bureau": bureau(parts[1]), "name": parts[2], "date": document_date(parts[3]), "original_line": line})
            continue
        # Only explicit inquiry-shaped lines, not arbitrary names or letter dates.
        match = re.fullmatch(r"(?:(TU|EX|EQ|TransUnion|Experian|Equifax)\s*[|:—–-]\s*)?(?:\d+[.)]\s*)?(.+?)\s*(?:\(\s*Inquiry\s+(?:date[ds]?\s*:?\s*)|[|—–]\s*)" + date_pattern + r"\)?", line, re.I)
        if match:
            report["inquiries"].append({"id": f"i{len(report['inquiries'])+1}", "bureau": bureau(match[1]) if match[1] else context,
                "name": match[2].strip(), "date": document_date(match[3]), "original_line": line})
        elif re.search(r"inquir|account|\d{1,2}/\d{1,2}/\d{4}", line, re.I):
            notes.append(f"Review unparsed line: {line[:200]}")
    notes.insert(0, "Extracted entries are drafts. Verify names, bureaus, dates, completeness, and account data against the original document.")
    if kind == "source" and not report["accounts"]:
        notes.append("No structured accounts detected. Add every open/positive account before screening.")
    if not report["inquiries"]:
        notes.append("No inquiry list detected. Enter the document's inquiries manually; no matches have been invented.")
    if len(report["inquiries"]) > 1000 or len(report["accounts"]) > 1000:
        raise ValueError("Document exceeds 1000 entries per collection")
    return report, notes


def extract_upload(payload, actor):
    filename = payload.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Document filename required")
    kind = payload.get("kind")
    if kind not in ("source", "letter"):
        raise ValueError("Document kind must be source or letter")
    try:
        content = base64.b64decode(payload.get("content_base64", ""), validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid file encoding") from error
    text, unreadable_pages = read_text(filename, content)
    if len(text.encode()) > MAX_TEXT:
        raise ValueError("Extracted document exceeds 2 MB")
    draft, notes = drafts(text, kind)
    if unreadable_pages:
        notes.insert(0, f"Text extraction missing on pages {unreadable_pages}. Run OCR externally and upload a fully readable document; approval is blocked.")
    digest = hashlib.sha256(content).hexdigest()
    return {"id": f"{kind}-{digest}", "kind": kind, "filename": Path(filename).name,
            "source_hash": digest, "text": text, "draft": draft, "notes": notes,
            "unreadable_pages": unreadable_pages, "uploaded_by": actor,
            "content_base64": base64.b64encode(content).decode()}
