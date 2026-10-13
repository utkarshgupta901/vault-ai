"""Text normalization used wherever we compare model output with document text (quote verification, refusal
detection, grounding checks). PDFs and models disagree on quotes, dashes, ligatures, hyphenation and whitespace;
normalizing both sides first makes the comparison about content, not typography."""

import html
import re
import unicodedata

_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
                         "–": "-", "—": "-", "‐": "-", "‑": "-", "−": "-", " ": " "})
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_WS = re.compile(r"\s+")
_LEADING_MD = re.compile(r"^[\s>*_#`\-]+")


def normalize(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_QUOTES)
    s = _HYPHEN_BREAK.sub(r"\1\2", s)
    s = _WS.sub(" ", s)
    return s.strip().casefold()


def normalize_reply_start(s: str | None) -> str:
    """Normalize and strip leading markdown, for 'does the reply start with the refusal phrase?' checks."""
    return _LEADING_MD.sub("", normalize(s))
