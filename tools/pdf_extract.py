"""PDF extraction helpers shared by research adapters."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 5000) -> str:
    """Extract text from PDF bytes using PyMuPDF first, then pdftotext."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        fitz_cmd = (
            "import fitz; "
            f"doc=fitz.open(r'{tmp_path}'); "
            "print('\\n'.join(page.get_text() for page in doc[:3]))"
        )
        result = subprocess.run(
            ["python3", "-c", fitz_cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout[:max_chars]

        result = subprocess.run(
            ["pdftotext", str(tmp_path), "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return result.stdout[:max_chars]
        return ""
    finally:
        tmp_path.unlink(missing_ok=True)

