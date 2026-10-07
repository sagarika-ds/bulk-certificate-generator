"""The single predefined certificate template (A4 landscape PDF, drawn with ReportLab).

Text is drawn with ``canvas.drawCentredString`` (not Paragraph), so recipient input
is never interpreted as markup. Long text shrinks to fit instead of overflowing.
"""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = landscape(A4)
NAVY = colors.HexColor("#1F3A5F")
GOLD = colors.HexColor("#B8892B")
GREY = colors.HexColor("#555555")


@dataclass(frozen=True)
class CertificateData:
    recipient_name: str
    event_name: str
    issued_by: str
    issue_date: date
    certificate_number: str
    remarks: str | None = None


def _check_font_support(*texts: str | None) -> None:
    """Built-in PDF fonts only cover Latin-1/WinAnsi. Fail loudly instead of printing garbage."""
    for text in texts:
        if text:
            try:
                text.encode("cp1252")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"text {text!r} has characters the template font cannot render "
                    f"({text[exc.start]!r})"
                ) from None


def _fit(c: canvas.Canvas, text: str, font: str, size: float, max_width: float, min_size: float = 12) -> tuple[str, float]:
    """Shrink the font until ``text`` fits; as a last resort truncate with '...'."""
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 1
    while len(text) > 4 and stringWidth(text, font, size) > max_width:
        text = text[:-4].rstrip() + "..."
    return text, size


def generate_certificate_pdf(data: CertificateData, dest: Path) -> None:
    """Render one certificate to ``dest`` (written atomically via a temp file)."""
    _check_font_support(data.recipient_name, data.event_name, data.issued_by, data.remarks)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    cx, max_w = PAGE_W / 2, PAGE_W - 160

    c = canvas.Canvas(str(tmp), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"Certificate - {data.recipient_name}")
    c.setAuthor(data.issued_by)

    # double border
    c.setStrokeColor(NAVY); c.setLineWidth(4); c.rect(25, 25, PAGE_W - 50, PAGE_H - 50)
    c.setStrokeColor(GOLD); c.setLineWidth(1.5); c.rect(37, 37, PAGE_W - 74, PAGE_H - 74)

    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 38)
    c.drawCentredString(cx, PAGE_H - 125, "CERTIFICATE OF COMPLETION")
    c.setStrokeColor(GOLD); c.setLineWidth(2); c.line(cx - 130, PAGE_H - 142, cx + 130, PAGE_H - 142)

    c.setFillColor(GREY); c.setFont("Helvetica", 15)
    c.drawCentredString(cx, PAGE_H - 190, "This is to certify that")

    name, size = _fit(c, data.recipient_name, "Helvetica-Bold", 42, max_w)
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", size)
    c.drawCentredString(cx, PAGE_H - 245, name)
    c.setStrokeColor(GREY); c.setLineWidth(0.7); c.line(cx - 220, PAGE_H - 262, cx + 220, PAGE_H - 262)

    c.setFillColor(GREY); c.setFont("Helvetica", 15)
    c.drawCentredString(cx, PAGE_H - 295, "has successfully completed")

    event, esize = _fit(c, data.event_name, "Helvetica-Bold", 26, max_w)
    c.setFillColor(GOLD); c.setFont("Helvetica-Bold", esize)
    c.drawCentredString(cx, PAGE_H - 335, event)

    if data.remarks:
        remarks, rsize = _fit(c, data.remarks, "Helvetica-Oblique", 14, max_w, 9)
        c.setFillColor(GREY); c.setFont("Helvetica-Oblique", rsize)
        c.drawCentredString(cx, PAGE_H - 365, remarks)

    # footer: date | issuer
    c.setFillColor(NAVY); c.setFont("Helvetica", 13)
    c.drawCentredString(190, 105, data.issue_date.strftime("%d %B %Y"))
    c.setStrokeColor(GREY); c.setLineWidth(0.7); c.line(100, 120, 280, 120)
    c.setFillColor(GREY); c.setFont("Helvetica", 10); c.drawCentredString(190, 90, "Date of issue")

    issuer, isize = _fit(c, data.issued_by, "Helvetica", 13, 240, 8)
    c.setFillColor(NAVY); c.setFont("Helvetica", isize)
    c.drawCentredString(PAGE_W - 190, 105, issuer)
    c.setStrokeColor(GREY); c.line(PAGE_W - 280, 120, PAGE_W - 100, 120)
    c.setFillColor(GREY); c.setFont("Helvetica", 10); c.drawCentredString(PAGE_W - 190, 90, "Issued by")

    c.setFont("Helvetica", 9)
    c.drawCentredString(cx, 55, f"Certificate No. {data.certificate_number}")
    c.showPage()
    c.save()
    os.replace(tmp, dest)
