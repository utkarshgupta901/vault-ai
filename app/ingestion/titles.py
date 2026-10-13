"""The filename-derived document title. Kept in its own tiny module because the API (upload) and the worker
(chunk headers) both need it, and the API should not import the chunker (tiktoken + text splitters) just for this."""

from pathlib import PurePath


def filename_title(filename: str) -> str:
    """'Larkspur_Handbook-2026.pdf' -> 'Larkspur Handbook 2026'.

    Two uses, one rule: the initial documents.title at upload, and the title in every chunk header. The chunk
    header must not use documents.title, because insights replace it with the model's title; the header is part of
    embed_hash, so a changed title would make a reprocess re-embed (and pay for) every chunk. The filename never
    changes, so the same file always gets the same headers and a reprocess reuses every vector."""
    return PurePath(filename).stem.replace("_", " ").replace("-", " ").strip() or filename
