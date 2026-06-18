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


def is_blocked(driver) -> bool:
    """True if the browser is on a Radware/ShieldSquare block or captcha page."""
    try:
        url = (driver.current_url or "").lower()
        title = (driver.title or "").lower()
    except Exception:
        return False
    return (
        "perfdrive.com" in url
        or "captcha" in title
        or ("block" in title and "page" in title)
    )


def _log(msg: str) -> None:
    print(f"[documents] {msg}", flush=True)


def _wait_for_download(driver, download_dir: Path, before: set[str], timeout: float) -> str | None:
    """Block until a new, complete (non-.crdownload) file appears. Bails early if
    a Radware block page appears (a re-challenge would never produce a file)."""
    deadline = time.time() + timeout
    saw_partial = False
    while time.time() < deadline:
        now = set(p.name for p in download_dir.iterdir())
        added = now - before
        new = [f for f in added if not f.endswith(".crdownload")]
        if new:
            return new[0]
        saw_partial = saw_partial or any(f.endswith(".crdownload") for f in added)
        # If nothing has even started after a grace period and we're blocked, stop.
        if not saw_partial and is_blocked(driver):
            raise RuntimeError("Radware re-challenge appeared during download")
        time.sleep(2)
    return None


def _close_popups(driver, main_handle: str) -> None:
    """Close any extra windows (the download fires via window.open(); those
    popups otherwise pile up and eventually wedge the driver) and return focus
    to the main window."""
    try:
        for h in list(driver.window_handles):
            if h != main_handle:
                driver.switch_to.window(h)
                driver.close()
        driver.switch_to.window(main_handle)
    except Exception:
        try:
            driver.switch_to.window(main_handle)
        except Exception:
            pass


def download_current_page(
    driver, download_dir: Path, timeout: float = 180.0
) -> str | None:
    """Select every document on the current results page and download the zip.

    Two-step action: the blue "Download documents" button opens a modal whose
    green "Download" button is the real trigger. Fails fast (rather than hanging)
    if the page is a Radware block, the controls are missing, or no file lands.
    The download fires a window.open() popup; we close it afterwards so popups
    don't accumulate and wedge the driver after a few batches.
    """
    main_handle = driver.current_window_handle
    _close_popups(driver, main_handle)  # clear any strays from a prior batch
    if is_blocked(driver):
        raise RuntimeError("Radware block page detected before download")
    if not _select_all_on_page(driver):
        raise RuntimeError("could not find the 'All documents listed on this page' checkbox")
    time.sleep(2)

    before = set(p.name for p in download_dir.iterdir()) if download_dir.exists() else set()

    triggers = driver.find_elements(
        By.XPATH, "//button[contains(normalize-space(.), 'Download documents')]"
    )
    if not triggers:
        raise RuntimeError(
            f"no 'Download documents' button (url={driver.current_url})"
        )
    # The modal's confirmation button is labelled exactly "Download" (the blue
    # opener is "Download documents", so an exact match excludes it). The modal
    # can be slow to render, so poll for it and re-click the opener once.
    def _find_confirm():
        return [
            b
            for b in driver.find_elements(By.XPATH, "//button|//a")
            if b.is_displayed() and b.text.strip() == "Download"
        ]

    confirm = []
    for attempt in range(2):
        _log(f"clicking 'Download documents' (attempt {attempt + 1})")
        _click(driver, triggers[0])
        deadline = time.time() + 15
        while time.time() < deadline:
            confirm = _find_confirm()
            if confirm:
                break
            if is_blocked(driver):
                raise RuntimeError("Radware re-challenge when opening download modal")
            time.sleep(1)
        if confirm:
            break
    if not confirm:
        raise RuntimeError(
            f"download confirmation modal did not appear (url={driver.current_url})"
        )
    # Native click (ActionChains) -- a scripted .click() doesn't always count as
    # the trusted user gesture Chrome wants before starting a download.
    _log("clicking modal 'Download', waiting for zip")
    ActionChains(driver).move_to_element(confirm[0]).pause(0.3).click(confirm[0]).perform()

    fname = _wait_for_download(driver, download_dir, before, timeout)
    _close_popups(driver, main_handle)  # tidy the download popup before next batch
    if fname is None:
        raise RuntimeError(
            f"download produced no file within {timeout:.0f}s "
            f"(url={driver.current_url}, title={driver.title!r})"
        )
    _log(f"downloaded {fname}")
    return fname
