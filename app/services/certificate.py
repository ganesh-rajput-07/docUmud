"""Completion certificate: a one-page-plus PDF summarising a signing and its audit trail."""
import re
from datetime import timezone

import pymupdf

from app.config import settings
from app.models import SignatureEvent, SignatureRequest, aware

MARGIN, WIDTH = 50, 495
INK, MUTED, BRAND = (0.06, 0.09, 0.16), (0.39, 0.45, 0.55), (0.31, 0.27, 0.9)


class _Writer:
    def __init__(self) -> None:
        self.doc = pymupdf.open()
        self.page = None
        self.y = 0.0
        self._new_page()

    def _new_page(self) -> None:
        self.page = self.doc.new_page(width=595, height=842)
        self.y = 56.0

    def ensure(self, height: float) -> None:
        if self.y + height > 790:
            self._new_page()

    def _box(self, x0: float, width: float, text: str, size: float, font: str, color) -> float:
        """Draw text in a box that grows until it fits (insert_textbox draws nothing when it doesn't).
        Returns the height used."""
        lines = max(1, int(pymupdf.get_text_length(text, font, size) // width) + 1 + text.count("\n"))
        height = lines * size * 1.45 + 4
        self.ensure(height)
        for _ in range(6):
            rc = self.page.insert_textbox(pymupdf.Rect(x0, self.y, x0 + width, self.y + height), text,
                                          fontsize=size, fontname=font, color=color)
            if rc >= 0:
                break
            height += max(-rc, size) + 2
        return height

    def text(self, text: str, size: float = 10, bold: bool = False, color=INK, x: float = MARGIN,
             width: float = WIDTH, gap: float = 4) -> None:
        self.y += self._box(x, width, text, size, "hebo" if bold else "helv", color) + gap

    def row(self, label: str, value: str) -> None:
        self.ensure(18)
        self.page.insert_text((MARGIN, self.y + 9.5), label, fontsize=9, fontname="helv", color=MUTED)
        self.y += self._box(165, 380, value or "-", 9.5, "helv", INK) - 2

    def rule(self) -> None:
        self.ensure(10)
        self.page.draw_line((MARGIN, self.y), (MARGIN + WIDTH, self.y), color=(0.89, 0.91, 0.94), width=0.8)
        self.y += 10


def _fmt(dt) -> str:
    return aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _detail(events: list[SignatureEvent], kind: str, key: str) -> str:
    for e in reversed(events):
        if e.event_type == kind:
            m = re.search(rf"{key}=([^;]*)", e.details)
            if m:
                return m.group(1).strip()
    return ""


def build_certificate(*, req: SignatureRequest, document_name: str, sender: str,
                      events: list[SignatureEvent], chain_valid: bool) -> bytes:
    w = _Writer()
    w.text(settings.app_name, 11, True, BRAND, gap=2)
    w.text("Certificate of Completion", 22, True, gap=6)
    w.text("This certificate records the electronic signing of the document below.", 10, color=MUTED, gap=12)
    w.rule()

    identity = _detail(events, "signed", "identity")
    method = {"email_code": "Email address verified with a one-time code before signing",
              "link_only": "Secure single-use link (email address not separately verified)"}.get(
        identity, "Secure single-use link")
    if req.mode == "in_person":
        method = "Signed in person on the sender's device"
    for label, value in [
        ("Document", document_name),
        ("Request ID", req.id),
        ("Sent by", sender),
        ("Signer", f"{req.recipient_name} <{req.recipient_email}>" if req.recipient_email else req.recipient_name),
        ("Signing method", "In person" if req.mode == "in_person" else "Email"),
        ("Identity check", method),
        ("Signed at", _fmt(req.signed_at) if req.signed_at else "-"),
        ("Signer IP address", _detail(events, "signed", "ip")),
        ("Original SHA-256", req.document_hash),
        ("Signed SHA-256", req.signed_file_hash or "-"),
        ("Audit trail", "Verified: no tampering detected" if chain_valid else "FAILED verification"),
    ]:
        w.row(label, value)

    w.y += 6
    w.rule()
    w.text("Activity", 13, True, gap=6)
    for e in events:
        detail = re.sub(r"signed_hash=\S+", "", e.details).strip(" ;")
        w.ensure(26)
        w.page.insert_text((MARGIN, w.y + 9), _fmt(e.created_at), fontsize=8.5, fontname="helv", color=MUTED)
        w.page.insert_text((200, w.y + 9), e.event_type.replace("_", " ").capitalize(), fontsize=9, fontname="hebo", color=INK)
        w.y += 13
        if detail:
            w.text(detail, 8, color=MUTED, x=200, width=345, gap=3)
    w.y += 8
    w.rule()
    w.text("This is a record of electronic signature events produced by DocUMud. It documents who signed what "
           "and when, and lets anyone compare file hashes to confirm the document has not changed. It is not a "
           "qualified electronic signature or a certificate-backed digital signature.", 8, color=MUTED)
    out = w.doc.tobytes(garbage=3, deflate=True)
    w.doc.close()
    return out
