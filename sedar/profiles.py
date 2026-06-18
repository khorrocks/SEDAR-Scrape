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
import time

from selenium.webdriver.common.by import By

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
    return idx


def scrape_page(driver, col: dict[str, int] | None = None) -> list[dict]:
    col = col or _column_index(driver)
    if "name" not in col or "number" not in col:
        return []
    out = []
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
        out.append(
            {
                "name": name,
                "number": number,
                "jurisdiction": cells[col["jurisdiction"]].text.strip()
                if "jurisdiction" in col and len(cells) > col["jurisdiction"] else "",
                "type": cells[col["type"]].text.strip()
                if "type" in col and len(cells) > col["type"] else "",
            }
        )
    return out


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
