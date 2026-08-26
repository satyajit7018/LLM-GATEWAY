"""File text extraction for attachments.

Text/code files are read client-side; PDFs are sent as base64 and their text is
extracted here with pypdf (pure-python, no system deps). Scanned/image-only PDFs
have no embedded text and would need OCR — out of scope; we flag them instead.
"""
import base64
import io


def extract_pdf_text(b64: str, max_chars: int = 100_000) -> str:
    from pypdf import PdfReader

    raw = base64.b64decode(b64.split(",")[-1])  # tolerate a data: URL prefix
    reader = PdfReader(io.BytesIO(raw))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        return ("[No extractable text — this looks like a scanned/image-only PDF; "
                "OCR would be needed to read it.]")
    return text[:max_chars]
