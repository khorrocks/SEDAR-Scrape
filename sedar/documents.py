"""Document search + download for a single SEDAR+ profile.

Flow (all observed against the live site):
  profile.html?id=<hash>  ->  redirects into a session viewInstance
  click "Search and download documents for this profile"
  click "Search"          ->  paginated results table
  per page: tick "All documents listed on this page"  ->  "Download documents"
  a modal appears ("You are downloading N documents X MB")  ->  click "Download"
  a zip is prepared server-side and downloaded.

SEDAR+ is a stateful, token-driven server app (opaque session ids, slow CDN),
so everything goes through the browser; there is no clean JSON API to call.
"""

from __future__ import annotations

import time
from pathlib import Path

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


PROFILE_URL = "https://www.sedarplus.ca/csa-party/records/profile.html?id={profile_id}"


def _click(driver, element) -> None:
    driver.execute_script("arguments[0].click();", element)


def open_profile_documents(driver, profile_id: str, settle: float = 8.0) -> None:
    """Open a profile and navigate to its document search page."""
    driver.get(PROFILE_URL.format(profile_id=profile_id))
    time.sleep(settle)
    link = WebDriverWait(driver, 30).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//*[contains(., 'Search and download documents for this profile')]"
                "[self::a or self::button]",
            )
        )
    )
    _click(driver, link)
    time.sleep(settle)


def run_search(driver, settle: float = 9.0) -> None:
    """Submit the document search form (empty criteria = all documents)."""
    btn = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(.)='Search']"))
    )
    _click(driver, btn)
    time.sleep(settle)


def result_count(driver) -> str:
    """Return the 'Displaying 1-30 of N results' line, or '' if absent."""
    import re

    body = driver.find_element(By.TAG_NAME, "body").text
    m = re.search(r"Displaying[^\n]+results", body)
    return m.group(0) if m else ""


def _results_table(driver):
    """Return the documents results table (the one with Document + Submitted
    headers), not the date-picker calendar table that shares the page."""
    for t in driver.find_elements(By.XPATH, "//table"):
        heads = [th.text.strip().lower() for th in t.find_elements(By.XPATH, ".//th")]
        if any("document" in h for h in heads) and any("submitted" in h for h in heads):
            idx = {}
            for i, h in enumerate(heads):
                if h.startswith("profile"):
                    idx["profile"] = i
                elif h.startswith("document"):
                    idx["document"] = i
                elif "submitted" in h:
                    idx["submitted"] = i
                elif "principal jurisdiction" in h:
                    idx["jurisdiction"] = i
                elif "file size" in h:
                    idx["file_size"] = i
            return t, idx
    return None, {}


def list_page_rows(driver) -> list[dict]:
    """Scrape the documents results table into row dicts, mapping columns by
    header so a leading checkbox / trailing Actions column can't misalign."""
    table, idx = _results_table(driver)
    if not table or "document" not in idx:
        return []
    out = []
    for r in table.find_elements(By.XPATH, ".//tbody//tr"):
        cells = r.find_elements(By.TAG_NAME, "td")

        def cell(key: str) -> str:
            i = idx.get(key)
            return cells[i].text.strip() if i is not None and i < len(cells) else ""

        doc = cell("document")
        if not doc:
            continue
        out.append(
            {
                "profile": cell("profile"),
                "document": doc,
                "submitted": cell("submitted"),
                "jurisdiction": cell("jurisdiction"),
                "file_size": cell("file_size"),
            }
        )
    return out


def _select_all_on_page(driver) -> bool:
    """Tick the 'All documents listed on this page' checkbox."""
    return bool(
        driver.execute_script(
            """
            const cbs=[...document.querySelectorAll('input[type=checkbox]')];
            for(const c of cbs){
              const lab=(c.closest('label')||c.parentElement);
              const t=(lab&&lab.textContent)||'';
              if(t.includes('All documents listed on this page')){c.click(); return true;}
            }
            return false;
            """
        )
    )


def _wait_for_download(download_dir: Path, before: set[str], timeout: float) -> str | None:
    """Block until a new, complete (non-.crdownload) file appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = set(p.name for p in download_dir.iterdir())
        new = [f for f in now - before if not f.endswith(".crdownload")]
        if new:
            return new[0]
        time.sleep(2)
    return None


def download_current_page(
    driver, download_dir: Path, timeout: float = 600.0
) -> str | None:
    """Select every document on the current results page and download the zip.

    Returns the downloaded filename, or None on timeout. The download is a
    two-step action: the blue "Download documents" button opens a confirmation
    modal whose green "Download" button is the real trigger.
    """
    if not _select_all_on_page(driver):
        raise RuntimeError("could not find the 'All documents listed on this page' checkbox")
    time.sleep(2)

    before = set(p.name for p in download_dir.iterdir()) if download_dir.exists() else set()

    trigger = driver.find_element(
        By.XPATH, "//button[contains(normalize-space(.), 'Download documents')]"
    )
    _click(driver, trigger)
    time.sleep(4)  # let the modal render

    # The modal's confirmation button is labelled exactly "Download". Use a
    # *native* click (ActionChains) -- a scripted .click() does not always count
    # as the trusted user gesture Chrome wants before starting a download.
    confirm = [
        b
        for b in driver.find_elements(By.XPATH, "//button|//a")
        if b.is_displayed() and b.text.strip() == "Download"
    ]
    if not confirm:
        raise RuntimeError("download confirmation modal did not appear")
    ActionChains(driver).move_to_element(confirm[0]).pause(0.3).click(confirm[0]).perform()

    return _wait_for_download(download_dir, before, timeout)
