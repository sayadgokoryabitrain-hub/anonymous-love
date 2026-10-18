from __future__ import annotations

import re
from dataclasses import dataclass

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None)

_LINK = re.compile(
    r"(https?://|hxxps?://|tg://|www\.|t\.me/|telegram\.me/|telegram\.dog/)",
    re.IGNORECASE,
)
_URL = re.compile(r"(https?://[^\s<>]+|tg://[^\s<>]+)", re.IGNORECASE)
_USERNAME = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z][A-Za-z0-9_]{4,31}")
_SCHEME = re.compile(r"\b(javascript|data|file|vbscript)\s*:", re.IGNORECASE)
_SHORTENERS = (
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "cutt.ly",
    "is.gd",
    "ow.ly",
)
_SUSPICIOUS = (
    "grabify.link",
    "iplogger.org",
    "iplogger.com",
    "iplogger.ru",
    "2no.co",
    "yip.su",
    "bmwforum.co",
    "ipgrabber.ru",
    "discord-nitro.com",
)
# Obvious minor-sexual terms. Messages that match are not relayed and are not stored in full.
_MINOR_SEXUAL = re.compile(
    r"(child\s*porn|csam|\bcp\b|\bloli\b|детск\w*\s*порн|цп\b|порно\w*\s*несовершенн|"
    r"underage\s*(sex|nude|porn)|sex\s*with\s*(a\s*)?(child|minor|kid))",
    re.IGNORECASE,
)
_SCAM = re.compile(
    r"(send\s+money|bank\s+card|seed\s+phrase|крипт\w*|перевод(и|ите)\s+денег|номер\s+карт)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PolicyResult:
    allowed: bool
    code: str = ""
    scam_hint: bool = False


def deobfuscate(text: str) -> str:
    cleaned = text.translate(_ZERO_WIDTH)
    cleaned = cleaned.replace("[.]", ".").replace("(.)", ".").replace("{.}", ".")
    cleaned = cleaned.replace("hxxp", "http").replace("hXXp", "http")
    return cleaned


def looks_like_link(text: str) -> bool:
    folded = deobfuscate(text)
    compact = re.sub(r"\s+", "", folded)
    return bool(_LINK.search(folded) or _LINK.search(compact) or _URL.search(folded) or _SCHEME.search(folded))


def username_mentioned(text: str) -> bool:
    return bool(_USERNAME.search(deobfuscate(text)))


def _bad_url(text: str) -> str | None:
    folded = deobfuscate(text).lower()
    if _SCHEME.search(folded):
        return "dangerous_scheme"
    for host in _SUSPICIOUS:
        if host in folded:
            return "suspicious_domain"
    for host in _SHORTENERS:
        if host in folded:
            return "shortener"
    return None


def evaluate_text(text: str, *, sender_verified: bool, recipient_verified: bool, username_allowed: bool) -> PolicyResult:
    if _MINOR_SEXUAL.search(deobfuscate(text)):
        return PolicyResult(False, "child_safety")
    both = sender_verified and recipient_verified
    if looks_like_link(text):
        if not both:
            return PolicyResult(False, "verification_required")
        problem = _bad_url(text)
        if problem:
            return PolicyResult(False, problem)
    if username_mentioned(text) and not (both and username_allowed):
        return PolicyResult(False, "verification_required")
    return PolicyResult(True, scam_hint=bool(_SCAM.search(text)))


def evaluate_media(kind: str, *, sender_verified: bool, recipient_verified: bool) -> PolicyResult:
    open_kinds = {"text", "sticker", "emoji", "reaction"}
    closed_kinds = {"photo", "video", "animation", "document", "voice", "video_note", "audio"}
    blocked_always = {"contact", "location", "venue"}
    if kind in blocked_always:
        return PolicyResult(False, "forbidden_media")
    if kind in open_kinds:
        return PolicyResult(True)
    if kind in closed_kinds:
        if sender_verified and recipient_verified:
            return PolicyResult(True)
        return PolicyResult(False, "verification_required")
    return PolicyResult(False, "unsupported")
