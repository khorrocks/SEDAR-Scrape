"""Turn what we scraped into something a downstream ingest can join on.

The archives in R2 hold PDFs and nothing else, so a system reading R2 alone has
no filing date to attach to a document. The date *is* captured -- it is on the
SEDAR+ results row and lands in ``Document.submitted`` -- it just never travels
with the file. These helpers extract the two things needed to bind them back
together: the filing id (which is part of the path inside each archive) and a
machine-readable date (the scraped cell is rendered for humans, not parsers).
"""

from __future__ import annotations

import re

# "06468830 EXEMPT_ISSUER_BID_FILINGS" -- a filing folder inside the archive.
# The company's own profile number leads the path in the same shape, so the LAST
# match before the filename is the filing, not the issuer.
_ID_SEGMENT = re.compile(r"\s*(\d{6,10})\s+\S")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# First line of the cell: "22 Jul 2026 14:59 EDT"
_SHORT = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", re.I
)
# Second line: "July 22 2026 at 14:59:43 Eastern Daylight Time"
_LONG = re.compile(
    r"\b([A-Za-z]{3})[a-z]*\s+(\d{1,2})[,\s]+(\d{4})\s+at\s+(\d{1,2}):(\d{2}):(\d{2})", re.I
)


def filing_id_from_member(member: str | None) -> str | None:
    """The SEDAR filing id from an archive member path, or None.

    Matches the ``filing_id`` the downstream ``files`` table already stores, so
    it is the join key that needs no name or encoding agreement.
    """
    if not member:
        return None
    found = None
    for seg in member.split("/")[:-1]:  # never the filename itself
        m = _ID_SEGMENT.match(seg)
        if m:
            found = m.group(1)  # last wins
    return found


def parse_submitted(raw: str | None) -> tuple[str | None, str | None]:
    """``Document.submitted`` -> (ISO date, ISO-ish timestamp).

    The cell renders as two lines, e.g.::

        22 Jul 2026 14:59 EDT
        July 22 2026 at 14:59:43 Eastern Daylight Time

    Returns ("2026-07-22", "2026-07-22T14:59:43") -- date first because that is
    what a filing is usually keyed on, and the timestamp separately for anyone
    who wants ordering within a day. Timezone is deliberately dropped rather
    than guessed: the abbreviation is all SEDAR gives, and a wrong offset is
    worse than none. Both are None when nothing parses.
    """
    if not raw:
        return None, None
    text = str(raw)

    hh = mm = ss = None
    y = mo = d = None

    m = _LONG.search(text)
    if m:
        mon, day, year, hh, mm, ss = m.groups()
        mo = _MONTHS.get(mon[:3].lower())
        y, d = int(year), int(day)
    else:
        m = _SHORT.search(text)
        if not m:
            return None, None
        day, mon, year, hh, mm = m.groups()
        mo = _MONTHS.get(mon[:3].lower())
        y, d = int(year), int(day)

    if not mo or not (1 <= d <= 31):
        return None, None
    date = f"{y:04d}-{mo:02d}-{d:02d}"
    if hh is None:
        return date, None
    stamp = f"{date}T{int(hh):02d}:{int(mm):02d}:{int(ss or 0):02d}"
    return date, stamp
