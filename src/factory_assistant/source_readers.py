"""Bounded file readers. Reading a format does not imply understanding its contents."""

import hashlib
import io
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from factory_assistant.models import SourceSnapshot


def read_source(root: Path, entry: dict, allowed: Path | None = None) -> SourceSnapshot:
    """Read a registered UTF-8 file or selectable-text PDF, retaining PDF page locations.

    Registered files live under data/; a file added in the app lives in its own folder
    (allowed), at the location its entry records.
    """
    if allowed is None:
        path, allowed = (root / entry["path"]).resolve(), root / "data"
    else:
        path = Path(entry["location"]).resolve()
    if not path.is_relative_to(allowed.resolve()):
        raise ValueError("Source must remain within data.")
    is_pdf = path.suffix.lower() == ".pdf"
    if not is_pdf and path.suffix.lower() not in {".txt", ".md", ".csv", ".log"}:
        raise ValueError("Unsupported source format.")
    limit = 2_000_000 if is_pdf else 100_000
    if path.stat().st_size > limit:
        raise ValueError(f"Source exceeds the {limit:,}-byte limit.")
    if datetime.fromisoformat(entry["recorded_at"]).utcoffset() is None:
        raise ValueError("Source registration needs a timezone.")
    data = path.read_bytes()
    pages = []
    if is_pdf:
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 20:
                raise ValueError("Encrypted PDFs and PDFs over 20 pages need another reader.")
            lines = []
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if not text.strip():
                    raise ValueError(f"Page {number} has no readable text; OCR review is required.")
                page_lines = text.strip("\r\n").splitlines()
                lines.extend(page_lines)
                pages.extend([number] * len(page_lines))
            text = "\n".join(lines)
        except PyPdfError as exc:
            raise ValueError("PDF could not be read; review the source file.") from exc
    else:
        text = data.decode("utf-8")
    if not text.strip():
        raise ValueError("The source is empty.")
    if len(text) > 100_000:
        raise ValueError("Extracted text exceeds 100,000 characters; split the document first.")
    return SourceSnapshot(
        source_id=entry["id"],
        path=entry["path"],
        sha256=hashlib.sha256(data).hexdigest(),
        text=text,
        registered_at=entry["recorded_at"],
        page_numbers=pages,
    )
