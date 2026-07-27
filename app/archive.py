"""Read what is actually inside a downloaded batch archive.

The document index used to be built entirely from the SEDAR+ results *table*, so
a wrong or truncated zip still produced a full set of Document rows: the database
claimed files that were never obtained (observed live -- a 91KB archive indexed as
30 filings, where a real 30-document batch is several MB). Opening the archive and
recording its true manifest is what makes possession verifiable, and lets a
"download only what's new" pass trust its own bookkeeping.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

_CHUNK = 1 << 20


def inspect_zip(path: Path) -> dict:
    """Return the archive's real contents.

    ``{"ok": bool, "members": [{"name","size","sha256"}], "member_count": int,
       "total_bytes": int, "zip_bytes": int, "error": str|None}``

    Never raises: a corrupt/partial download is reported as ``ok=False`` so the
    caller can retry the page rather than crash the whole company download.
    """
    out: dict = {
        "ok": False,
        "members": [],
        "member_count": 0,
        "total_bytes": 0,
        "zip_bytes": 0,
        "error": None,
    }
    try:
        out["zip_bytes"] = path.stat().st_size
    except OSError:
        pass

    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                digest = hashlib.sha256()
                with zf.open(info) as fh:
                    while True:
                        chunk = fh.read(_CHUNK)
                        if not chunk:
                            break
                        digest.update(chunk)
                out["members"].append(
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "sha256": digest.hexdigest(),
                    }
                )
        out["member_count"] = len(out["members"])
        out["total_bytes"] = sum(m["size"] for m in out["members"])
        out["ok"] = True
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _norm(name: str) -> str:
    """Normalise a document/member name for matching: drop any directory part,
    lowercase, and strip a de-duplicating suffix such as "(1)" that the server
    adds when a batch contains two identically named filings."""
    base = name.rsplit("/", 1)[-1].strip().lower()
    base = re.sub(r"\s*\(\d+\)(?=\.[a-z0-9]+$|$)", "", base)
    return re.sub(r"\s+", " ", base)


def match_members(titles: list[str], members: list[dict]) -> list[dict | None]:
    """Best-effort pairing of results-table titles to archive members.

    Returns a list positionally aligned with ``titles`` (``None`` where no member
    could be claimed). Each member is consumed at most once, so duplicate titles
    map to distinct files. Matching is by normalised name, then by stem prefix --
    the archive generally names entries exactly as the Document column does.
    """
    pool: list[tuple[str, dict]] = [(_norm(m["name"]), m) for m in members]
    used: set[int] = set()
    result: list[dict | None] = []

    for title in titles:
        want = _norm(title)
        found = None
        for i, (name, member) in enumerate(pool):
            if i in used:
                continue
            if name == want:
                found, _ = member, used.add(i)
                break
        if found is None:  # fall back to a prefix match on the filename stem
            stem = want.rsplit(".", 1)[0]
            for i, (name, member) in enumerate(pool):
                if i in used:
                    continue
                if stem and (name.startswith(stem) or name.rsplit(".", 1)[0] == stem):
                    found, _ = member, used.add(i)
                    break
        result.append(found)

    # Positional fallback: when the archive holds at least as many files as the
    # page listed, every row IS accounted for, so a name that simply didn't match
    # (server renaming, odd characters) should still be linked rather than left
    # unverified. Hand the remaining rows the remaining members in order.
    if len(members) >= len(titles) and any(r is None for r in result):
        leftovers = [m for i, (_n, m) in enumerate(pool) if i not in used]
        it = iter(leftovers)
        for idx, item in enumerate(result):
            if item is None:
                result[idx] = next(it, None)
    return result
