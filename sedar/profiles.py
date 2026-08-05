"""Enumerate Canadian reporting issuers from SEDAR+.

Verified navigation (June 2026, via live probes from the deployed worker):

  * The bare homepage and ``records/search.html`` are NOT usable: the homepage
    triggers the Radware captcha and ``search.html`` 404s.
  * A ``profile.html?id=<hash>`` deep link DOES clear Radware and 302s into a
    session ``viewInstance/view.html`` ("View Issuer Profile"). We use a known
    profile as a session bootstrap.
  * From that profile, the nav link **"View reporting issuers list"** opens the
    consolidated **Reporting issuers list** -- already populated (no "Search"
    button), paginated, with columns:
        Name | Number | Reporting jurisdictions | Principal jurisdiction |
        Type | In default | Active cease trade order
    A "Filter by name or profile number" box and an "Export" (CSV, capped) also
    exist; we page the HTML instead.

Columns are matched by header text (not fixed index) so a leading checkbox
column can't throw the mapping off.
"""

from __future__ import annotations

import os
import re
import time
from difflib import SequenceMatcher

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# A known, stable profile used purely to bootstrap a Radware-cleared session.
# Override with SEDAR_BOOTSTRAP_PROFILE_ID if this profile ever goes away.
BOOTSTRAP_PROFILE_ID = os.getenv(
    "SEDAR_BOOTSTRAP_PROFILE_ID", "517042d52d6b1ddfa40ea23cc4c62739"
)
BOOTSTRAP_URL = (
    "https://www.sedarplus.ca/csa-party/records/profile.html?id={pid}"
)
REPORTING_ISSUERS_LINK = "View reporting issuers list"

# Kept for the API/CLI "profile type" choices. The reporting issuers list mixes
# all types; we expose Type per row and can filter on it.
PROFILE_TYPES = (
    "Company",
    "Investment fund",
    "Investment fund group",
    "Industry participant",
    "Third party filer",
)


def _click_by_text(driver, text: str) -> bool:
    return bool(
        driver.execute_script(
            """const t=arguments[0].toLowerCase();
               const el=[...document.querySelectorAll('a,button')]
                 .find(e=>(e.textContent||'').trim().toLowerCase().includes(t));
               if(el){el.scrollIntoView({block:'center'});el.click();return true;}
               return false;""",
            text,
        )
    )


def open_reporting_issuers(driver, settle: float = 10.0) -> None:
    """Bootstrap a session via a known profile, then open the issuers list."""
    driver.get(BOOTSTRAP_URL.format(pid=BOOTSTRAP_PROFILE_ID))
    time.sleep(settle)
    if not _click_by_text(driver, REPORTING_ISSUERS_LINK):
        raise RuntimeError("could not find 'View reporting issuers list' nav link")
    time.sleep(settle)


# Backwards-compatible alias (lookup.py / older callers).
def open_profiles_search(driver, settle: float = 10.0) -> None:
    open_reporting_issuers(driver, settle=settle)


def set_profile_type(driver, profile_type: str) -> None:  # no-op: list isn't typed
    return None


def run_search(driver, settle: float = 2.0) -> None:  # list needs no search click
    time.sleep(settle)


def total_count(driver) -> int | None:
    """Best-effort total issuer count from the 'Displaying X of N results' or
    'N results' text on the list page."""
    import re

    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return None
    m = re.search(r"of\s+([\d,]+)\s+result", body) or re.search(r"([\d,]+)\s+results?\b", body)
    return int(m.group(1).replace(",", "")) if m else None


def _column_index(driver) -> dict[str, int]:
    """Map our field names to <th> positions by header text."""
    ths = driver.find_elements(By.XPATH, "(//table)[1]//th")
    idx: dict[str, int] = {}
    for i, th in enumerate(ths):
        t = (th.text or "").strip().lower()
        if not t:
            continue
        if "name" in t and "name" not in idx:
            idx["name"] = i
        elif t.startswith("number"):
            idx["number"] = i
        elif "principal jurisdiction" in t:
            idx["jurisdiction"] = i
        elif t.startswith("type"):
            idx["type"] = i
        elif "in default" in t:
            idx["in_default"] = i
        elif "cease trade" in t:
            idx["cease_trade_order"] = i
    return idx


def scrape_page(driver, col: dict[str, int] | None = None) -> list[dict]:
    """Scrape the current issuers page. Retries locally on a
    StaleElementReferenceException (the table re-renders as you paginate) so a
    transient stale ref doesn't escalate to a full browser rebuild + re-walk."""
    from selenium.common.exceptions import StaleElementReferenceException

    col = col or _column_index(driver)
    if "name" not in col or "number" not in col:
        return []
    for _ in range(4):
        out = []
        try:
            rows = driver.find_elements(By.XPATH, "(//table)[1]//tbody//tr")
            for r in rows:
                cells = r.find_elements(By.TAG_NAME, "td")
                need = max(col["name"], col["number"])
                if len(cells) <= need:
                    continue
                name = cells[col["name"]].text.strip()
                number = cells[col["number"]].text.strip()
                if not name or not number:
                    continue
                def _cell(key: str, cells=cells) -> str:
                    i = col.get(key)
                    return cells[i].text.strip() if i is not None and len(cells) > i else ""

                out.append(
                    {
                        "name": name,
                        "number": number,
                        "jurisdiction": _cell("jurisdiction"),
                        "type": _cell("type"),
                        "in_default": _cell("in_default"),
                        "cease_trade_order": _cell("cease_trade_order"),
                    }
                )
            return out
        except StaleElementReferenceException:
            time.sleep(1)
    return out


def query_variants(name: str) -> list[str]:
    """Progressively shorter search terms for one company name.

    The filter box is a substring match, so a name the source truncated can
    never match: "Alkane Resources Ltd" does not occur inside "Alkane Resources
    Limited". Dropping trailing words turns the truncated tail into a prefix
    that does match, and the caller still scores candidates against the full
    original name, so a looser query cannot loosen the match test.
    """
    base = english_legal_name(name)
    # Trailing single-letter markers ("Airboss of America J") are share-class
    # noise from the source export, never part of the legal name.
    base = re.sub(r"\s+[A-Z]$", "", base).strip()
    words = [w for w in re.split(r"\s+", base) if w]
    out: list[str] = []
    for take in (len(words), 3, 2, 1):
        if take < 1 or take > len(words):
            continue
        q = " ".join(words[:take]).strip(" ,.-&")
        # A one-word query only helps when the word is distinctive enough that
        # the results stay scannable.
        if len(q) < (5 if take == 1 else 4):
            continue
        if q and q not in out:
            out.append(q)
    return out or ([base] if base else [])


def _filter_box(driver):
    """The "Filter by name or profile number" input, or None."""
    for el in driver.find_elements(By.TAG_NAME, "input"):
        try:
            if not el.is_displayed():
                continue
            hint = " ".join([
                el.get_attribute("placeholder") or "",
                el.get_attribute("aria-label") or "",
                el.get_attribute("name") or "",
            ]).lower()
        except Exception:
            continue
        if "filter" in hint and ("name" in hint or "profile" in hint):
            return el
    return None


def _table_signature(driver) -> str:
    """Cheap fingerprint of the results table: row count + first row's text.

    Used to tell whether typing into the filter actually changed anything, which
    a fixed sleep cannot.
    """
    try:
        return driver.execute_script(
            """const rs = document.querySelectorAll('table tbody tr');
               if (!rs.length) return '0|';
               return rs.length + '|' + (rs[0].innerText || '').slice(0, 120);"""
        ) or ""
    except Exception:
        return ""


def english_legal_name(raw: str) -> str:
    """The "Full legal company name in English" out of a SEDAR name cell.

    SEDAR renders that column as "<English legal name> / <French legal name>",
    which is why catalog entries look doubled -- "Quebec Innovative Materials
    Corp. / Quebec Innovative Materials Corp." is one company, not two. Some
    rows also carry a trailing "Operating name: ..." on either side.
    """
    s = (raw or "").split("/")[0]
    s = re.split(r"operating\s+name\s*:", s, maxsplit=1, flags=re.IGNORECASE)[0]
    return re.sub(r"\s+", " ", s).strip()


def _norm_for_match(s: str) -> str:
    """Normalise a name for comparison: English side only, no punctuation, no
    corporate suffix. "ACME INC" and "Acme Inc. / Acme Inc." both reduce to
    "acme"."""
    s = english_legal_name(s).lower()
    s = re.sub(r"[.,'\"]", " ", s)
    s = re.sub(
        r"\b(inc|ltd|ltee|limited|corp|corporation|co|company|plc|sa|lp|llc|"
        r"holdings|group|the)\b",
        " ", s,
    )
    return re.sub(r"\s+", " ", s).strip()


def score_name(query: str, candidate: str) -> float:
    """0..1 resemblance between a stored name and a SEDAR row's name.

    Sequence ratio, with a floor for the containment case: a truncated import
    ("Aclara Res Inc. J") or a shortened form ("QUEBEC INNOVATIVE MATERIALS")
    should still rank its full legal name highly.
    """
    a, b = _norm_for_match(query), _norm_for_match(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        ratio = max(ratio, 0.75 + 0.25 * (len(shorter) / len(longer)))
    return ratio


def find_number_by_name(driver, name: str, settle: float = 6.0,
                        want: str | None = None) -> dict | None:
    """Look up an issuer's SEDAR profile number from its name.

    Uses the Reporting Issuers List's "Filter by name or profile number" box and
    returns the best-resembling row, scored, with the English legal name
    extracted. The filter already narrows to near-matches, so the top row is
    usually right -- but the score and rank come back with it so the caller can
    decide, and so a weak match can be flagged rather than trusted silently.
    """
    before = _table_signature(driver)

    box = _filter_box(driver)
    if box is None:
        return None
    # Real keystrokes, not el.value = x. Assigning the property updates the DOM
    # without driving the framework binding behind the filter, so the table
    # silently kept showing the unfiltered first page -- and every lookup then
    # "matched" against whatever issuer happened to sort first.
    try:
        box.clear()
        box.send_keys(name)
        box.send_keys(Keys.ENTER)
    except Exception:
        return None

    # Wait for the table to actually change rather than sleeping blind.
    deadline = time.time() + settle
    changed = False
    while time.time() < deadline:
        time.sleep(0.5)
        if _table_signature(driver) != before:
            changed = True
            break

    col = _column_index(driver)
    rows = scrape_page(driver, col)
    if not rows:
        return None

    # If the table never moved, the filter did not take. Returning these rows
    # would be worse than returning nothing: they are the unfiltered list, and a
    # confident-looking wrong number files one company's documents under
    # another. The exception is a genuinely strong hit, which cannot be a
    # coincidence of ordering.
    # Score against the caller's real company name, which may differ from the
    # (possibly shortened) term actually typed into the box.
    target = want or name
    if not changed and max(score_name(target, r.get("name", "")) for r in rows) < 0.9:
        return {"filter_failed": True, "number": "", "score": 0.0,
                "candidates": len(rows)}

    # Rank every candidate; ties keep SEDAR's own ordering, so an equal-scoring
    # first result wins -- that is the one the filter box considered best.
    best_i, best_score = 0, -1.0
    for i, r in enumerate(rows):
        s = score_name(target, r.get("name", ""))
        if s > best_score:
            best_i, best_score = i, s
    best = rows[best_i]
    return {
        **best,
        "english_name": english_legal_name(best.get("name", "")),
        "score": round(best_score, 3),
        "match": "exact" if best_score >= 0.999 else "close",
        "rank": best_i + 1,
        "candidates": len(rows),
        "query": name,
    }


def next_page(driver, settle: float = 8.0) -> bool:
    """Click a 'Next' pagination control if present and enabled."""
    clicked = driver.execute_script(
        """const els=[...document.querySelectorAll('a,button')];
           const el=els.find(e=>{
             const t=(e.textContent||'').trim().toLowerCase();
             const ok=t==='next'||t.includes('next')||t.includes('»');
             return ok && !e.disabled && e.offsetParent!==null
                    && !(e.getAttribute('aria-disabled')==='true');
           });
           if(el){el.scrollIntoView({block:'center'});el.click();return true;}
           return false;""",
    )
    if clicked:
        time.sleep(settle)
    return bool(clicked)


def enumerate_profiles(
    driver,
    profile_type: str | None = "Company",
    max_pages: int | None = None,
    page_pause: float = 1.0,
) -> list[dict]:
    """Page through the Reporting issuers list and collect rows.

    ``profile_type`` filters on the row Type when set (case-insensitive
    substring); pass None to keep every type.
    """
    open_reporting_issuers(driver)
    col = _column_index(driver)

    collected: list[dict] = []
    seen: set[str] = set()
    page = 0
    while True:
        page += 1
        for row in scrape_page(driver, col):
            if profile_type and profile_type.lower() not in (row["type"] or "").lower():
                continue
            if row["number"] in seen:
                continue
            seen.add(row["number"])
            collected.append(row)
        if max_pages and page >= max_pages:
            break
        time.sleep(page_pause)
        if not next_page(driver):
            break
    return collected
