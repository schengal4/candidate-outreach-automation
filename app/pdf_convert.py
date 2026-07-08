"""Deterministic .docx → PDF conversion via installed Microsoft Word (COM).

No LLM involved — the PDF is a faithful render of the uploaded file. Word COM
was chosen over LibreOffice headless because Word is already installed on the
host machine and LibreOffice isn't; conversion stays fully local either way
(no third-party conversion API). Windows-only, matching the current deployment.
"""

from pathlib import Path

import pythoncom
import win32com.client

WD_FORMAT_PDF = 17  # WdSaveFormat.wdFormatPDF


def docx_to_pdf(docx_path: Path, pdf_path: Path) -> None:
    """Convert one .docx to PDF using Word. Blocking (a few seconds) — callers
    on the event loop should wrap this in asyncio.to_thread; COM is initialized
    per-call so it works from any thread.
    """
    pythoncom.CoInitialize()
    try:
        # DispatchEx starts a dedicated Word instance instead of attaching to
        # (and later quitting) one the user may have open.
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            doc = word.Documents.Open(str(docx_path.resolve()), ReadOnly=True)
            try:
                doc.SaveAs(str(pdf_path.resolve()), FileFormat=WD_FORMAT_PDF)
            finally:
                doc.Close(SaveChanges=False)
        finally:
            word.Quit()
    finally:
        pythoncom.CoUninitialize()
