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

import shutil
import time
from pathlib import Path

from selenium.common.exceptions import StaleElementReferenceException
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
    """Return the 'Displaying 1-30 of N results' line, or '' if absent.

    Reads innerText via execute_script (bounded by the script timeout) rather
    than element ``.text``, which has no timeout and can hang the worker.
    """
    import re

    body = driver.execute_script(
        "return (document.body && document.body.innerText) || '';"
    ) or ""
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


# Extract the whole results table in ONE call. Columns are mapped by header
# text, so a leading checkbox / trailing Actions column can't misalign them.
_ROWS_JS = r"""
const tables = [...document.querySelectorAll('table')];
for (const t of tables) {
  const ths = [...t.querySelectorAll('th')].map(th => (th.textContent||'').trim().toLowerCase());
  if (!ths.some(h => h.includes('document')) || !ths.some(h => h.includes('submitted'))) continue;
  const idx = {};
  ths.forEach((h, i) => {
    if (h.startsWith('profile')) idx.profile = i;
    else if (h.startsWith('document')) idx.document = i;
    else if (h.includes('submitted')) idx.submitted = i;
    else if (h.includes('principal jurisdiction')) idx.jurisdiction = i;
    else if (h.includes('file size')) idx.file_size = i;
  });
  if (idx.document === undefined) continue;
  const out = [];
  const trs = [...t.querySelectorAll('tbody tr')];
  trs.forEach((tr, ri) => {
    const tds = tr.querySelectorAll('td');
    const cell = k => (idx[k] !== undefined && tds[idx[k]])
      ? ((tds[idx[k]].innerText || '').trim()) : '';
    const doc = cell('document');
    if (!doc) return;
    // row_index is the position in tbody, so a caller can tick exactly this
    // row's download checkbox even though blank rows are skipped here.
    out.push({row_index: ri, profile: cell('profile'), document: doc,
              submitted: cell('submitted'), jurisdiction: cell('jurisdiction'),
              file_size: cell('file_size')});
  });
  return out;
}
return [];
"""


# Tick ONLY the given rows' download checkboxes (clearing any prior selection),
# so a recheck fetches just the handful of new filings instead of re-downloading
# the whole 30-document page. Row indices come from list_page_rows' row_index.
_SELECT_ROWS_JS = r"""
const want = new Set(arguments[0]);
const tables = [...document.querySelectorAll('table')];
for (const t of tables) {
  const ths = [...t.querySelectorAll('th')].map(th => (th.textContent||'').trim().toLowerCase());
  if (!ths.some(h => h.includes('document')) || !ths.some(h => h.includes('submitted'))) continue;
  // Clear everything first: the page-level "all documents" box and every row.
  for (const cb of document.querySelectorAll('input[type=checkbox]')) {
    const lab = (cb.closest('label') || cb.parentElement);
    const txt = (lab && lab.textContent) || '';
    if (cb.checked && (txt.includes('All documents listed on this page') || cb.closest('table') === t)) {
      cb.click();
    }
  }
  let picked = 0;
  const trs = [...t.querySelectorAll('tbody tr')];
  trs.forEach((tr, ri) => {
    if (!want.has(ri)) return;
    const cb = tr.querySelector('input[type=checkbox]');
    if (cb) { if (!cb.checked) cb.click(); picked++; }
  });
  return {picked: picked, requested: want.size};
}
return null;
"""


def list_page_rows(driver) -> list[dict]:
    """Scrape the documents results table into row dicts.

    Done as a SINGLE ``execute_script`` on purpose. The previous element-by-element
    version issued hundreds of ``find_elements``/``.text`` round trips per page --
    and those commands have no timeout, so one wedged call froze the worker
    forever with no exception. ``execute_script`` is bounded by chromedriver's
    script timeout, so a bad page raises (and the worker's recovery resumes)
    instead of hanging. It is also far faster and immune to stale-element races,
    since the DOM is read in one shot inside the page.
    """
    rows = driver.execute_script(_ROWS_JS) or []
    out = []
    for r in rows:
        doc = (r.get("document") or "").strip()
        if not doc:
            continue
        out.append(
            {
                "row_index": r.get("row_index"),
                "profile": (r.get("profile") or "").strip(),
                "document": doc,
                "submitted": (r.get("submitted") or "").strip(),
                "jurisdiction": (r.get("jurisdiction") or "").strip(),
                "file_size": (r.get("file_size") or "").strip(),
            }
        )
    return out


def select_rows_on_page(driver, row_indices: list[int]) -> int:
    """Tick only the given rows' checkboxes. Returns how many were selected."""
    info = driver.execute_script(_SELECT_ROWS_JS, list(row_indices))
    if not info:
        raise RuntimeError("could not find the documents results table to select rows")
    picked = int(info.get("picked") or 0)
    if picked != len(row_indices):
        raise RuntimeError(
            f"selected {picked} of {len(row_indices)} intended document(s)"
        )
    return picked


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


def _wait_for_download(download_dir: Path, before: set[str], timeout: float) -> str | None:
    """Block until a new, complete (non-.crdownload) file appears, or timeout.

    Pure filesystem polling on purpose: calling into WebDriver here can block
    forever when Chrome is busy on a download, which would defeat the timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = [
            f
            for f in (set(p.name for p in download_dir.iterdir()) - before)
            if not f.endswith(".crdownload")
        ]
        if new:
            return new[0]
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


def neutralize_dialogs(driver) -> None:
    """Clear anything that blocks JavaScript execution.

    SEDAR+ raises a ``beforeunload`` ("Leave site? Changes may not be saved")
    dialog when navigating/paginating. A blocking JS dialog freezes every
    ``execute_script`` call -> Selenium raises ``TimeoutException: script
    timeout`` and the scrape stalls (undetected-chromedriver doesn't honour the
    ``unhandledPromptBehavior`` capability, so it isn't auto-dismissed). We:
      1. accept any dialog already up (the alert API is NOT blocked by it), then
      2. null future beforeunload/unload handlers so navigation can't re-trigger
         a blocking prompt.
    """
    for _ in range(3):
        try:
            driver.switch_to.alert.accept()
        except Exception:
            break
    try:
        driver.execute_script(
            "try{window.onbeforeunload=null;window.onunload=null;"
            "window.addEventListener('beforeunload',"
            "function(e){e.stopImmediatePropagation();},true);}catch(e){}"
        )
    except Exception:
        pass


def purge_stale_downloads(download_dir: Path) -> None:
    """Delete leftover files sitting directly in the download dir and report free
    space.

    Completed batches are moved/uploaded out of this directory, so anything left
    at the top level is debris from a failed or aborted download (including
    ``.crdownload`` partials). That debris accumulates on the mounted volume, and
    a FULL volume makes Chrome abort downloads *silently* -- the modal closes and
    no file ever lands, which is indistinguishable from a hang. Sweeping first
    keeps the volume healthy and logs the free space so a real disk problem is
    visible instead of looking like a stall.
    """
    try:
        removed = 0
        for p in download_dir.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
        usage = shutil.disk_usage(download_dir)
        free_mb = usage.free // (1024 * 1024)
        if removed or free_mb < 200:
            _log(f"download dir: purged {removed} stale file(s), {free_mb} MB free")
        if free_mb < 25:
            raise RuntimeError(
                f"only {free_mb} MB free on the data volume -- Chrome will fail "
                "downloads silently; free space or enlarge the volume"
            )
    except RuntimeError:
        raise
    except Exception:
        pass


def ensure_single_window(driver) -> None:
    """Collapse to a single browser window, focused on the primary one.

    The document download fires a window.open() popup; if one lingers (or a
    pagination click spawns one), the driver can end up operating on the wrong
    window and later WebDriver commands hang indefinitely (no clean timeout).
    Closing strays and re-focusing the first handle before each page's work
    keeps the driver on the real results window."""
    try:
        handles = driver.window_handles
    except Exception:
        return
    if len(handles) <= 1:
        # Still make sure focus is valid.
        try:
            driver.switch_to.window(handles[0])
        except Exception:
            pass
        return
    keep = handles[0]
    for h in handles[1:]:
        try:
            driver.switch_to.window(h)
            driver.close()
        except Exception:
            pass
    try:
        driver.switch_to.window(keep)
    except Exception:
        pass


# Pick the download MODAL's confirm button. Verified against the live DOM:
#   DIV.ui-dialog > DIV.appDialogPopup > DIV.appDialogPopupChildren
#     > DIV.appBox...-buttons > BUTTON.appButton...-buttons-ok.appOk   ("Download")
# (sibling is "Cancel"). So the reliable anchor is `.appOk` inside `.ui-dialog` /
# `.appDialogPopup`; we fall back to a text match only if the markup changes.
# Visibility is checked strictly -- a laid-out-but-hidden dialog still returns
# client rects, which would otherwise let us "click" an invisible button.
_CONFIRM_JS = r"""
const vis = el => {
  const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && !!el.offsetParent &&
         cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
};
document.querySelectorAll('[data-sedar-confirm]')
  .forEach(e => e.removeAttribute('data-sedar-confirm'));
const dlgSel = '.ui-dialog,.appDialogPopup,[role=dialog],[aria-modal=true]';
const openDlg = [...document.querySelectorAll(dlgSel)].filter(vis);
let pick = null, how = '';
for (const d of openDlg) {           // preferred: the dialog's OK button
  const b = [...d.querySelectorAll('button.appOk,button')]
    .find(e => vis(e) && (e.classList.contains('appOk') ||
                          (e.textContent || '').trim() === 'Download'));
  if (b) { pick = b; how = 'dialog-ok'; break; }
}
if (!pick) {                          // fallback: exact-text button anywhere visible
  pick = [...document.querySelectorAll('button,a')]
    .find(e => (e.textContent || '').trim() === 'Download' && vis(e)) || null;
  how = pick ? 'text-fallback' : '';
}
if (!pick) return null;
pick.setAttribute('data-sedar-confirm', '1');
return {how: how, dialogs_open: openDlg.length,
        in_dialog: !!pick.closest(dlgSel), tag: pick.tagName,
        cls: (pick.className || '').toString().slice(0, 60)};
"""


def download_current_page(
    driver, download_dir: Path, timeout: float = 180.0,
    row_indices: list[int] | None = None,
) -> str | None:
    """Download the current results page's documents as a zip.

    ``row_indices`` restricts the download to specific rows (from
    ``list_page_rows``' ``row_index``). A recheck that finds 3 new filings on a
    30-row page then fetches only those 3 instead of re-downloading all 30.
    Passing None selects the whole page via the "All documents listed on this
    page" checkbox, which is the right thing for a first full download.

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
    if row_indices is None:
        if not _select_all_on_page(driver):
            raise RuntimeError(
                "could not find the 'All documents listed on this page' checkbox"
            )
    else:
        n = select_rows_on_page(driver, row_indices)
        _log(f"selected {n} specific document(s) on this page")
    time.sleep(2)

    # Clear debris from earlier failed downloads first: it fills the volume, and
    # a full volume makes Chrome drop downloads silently.
    if download_dir.exists():
        purge_stale_downloads(download_dir)
    before = set(p.name for p in download_dir.iterdir()) if download_dir.exists() else set()

    def _find_trigger():
        els = driver.find_elements(
            By.XPATH, "//button[contains(normalize-space(.), 'Download documents')]"
        )
        return els[0] if els else None

    if _find_trigger() is None:
        raise RuntimeError(
            f"no 'Download documents' button (url={driver.current_url})"
        )
    def _find_confirm():
        """Locate the MODAL's confirm button and return (element, info) or (None, None).

        Must not match the per-row "Download" links in the results table's Actions
        column: those appear BEFORE the modal in document order, so taking the
        first match downloaded a single filing (a ~91KB file instead of a ~5MB
        30-doc batch) or opened a viewer and produced no file at all -- which is
        what stalled the whole download. We therefore prefer a candidate inside a
        dialog/modal container, then any candidate outside a <table>, and mark the
        winner with a data attribute so Selenium clicks exactly that element.
        """
        info = driver.execute_script(_CONFIRM_JS)
        if not info:
            return None, None
        try:
            el = driver.find_element(By.CSS_SELECTOR, "[data-sedar-confirm='1']")
        except Exception:
            return None, None
        return el, info

    # Open the modal with a NATIVE click. A scripted element.click() does not
    # count as the trusted user gesture SEDAR+/Chrome want here: the browser
    # reaches the results page and select-all, but the scripted click on
    # "Download documents" silently fails to open the modal / fire the download
    # (confirmed live: a real manual click works, a scripted one doesn't). Use
    # ActionChains (like the confirm button) and re-find the trigger each try so
    # a stale ref after a re-render can't wedge it. Try a few times.
    confirm, cinfo = None, None
    for attempt in range(4):
        trig = _find_trigger()
        if trig is None:
            time.sleep(1)
            continue
        _log(f"clicking 'Download documents' natively (attempt {attempt + 1})")
        try:
            ActionChains(driver).move_to_element(trig).pause(0.2).click(trig).perform()
        except Exception:
            _click(driver, trig)  # fallback to scripted if native click can't run
        # The modal is SLOW: measured live it was absent at 4s and present by
        # ~12s, so a 12s budget sat right on the edge and intermittently missed
        # it. Give it 30s per attempt.
        deadline = time.time() + 30
        while time.time() < deadline:
            confirm, cinfo = _find_confirm()
            if confirm is not None:
                break
            if is_blocked(driver):
                raise RuntimeError("Radware re-challenge when opening download modal")
            time.sleep(1)
        if confirm is not None:
            break
    if confirm is None:
        raise RuntimeError(
            f"download confirmation modal did not appear (url={driver.current_url})"
        )
    # Native click (ActionChains) -- a scripted .click() doesn't always count as
    # the trusted user gesture Chrome wants before starting a download. Re-find
    # the confirm button immediately before clicking so a re-render between the
    # poll and the click can't hand us a stale element.
    _log(
        f"clicking modal 'Download' (how={cinfo.get('how')}, "
        f"dialogs_open={cinfo.get('dialogs_open')}, cls={cinfo.get('cls')}), waiting for zip"
    )
    fresh, finfo = _find_confirm()
    target = fresh if fresh is not None else confirm
    ActionChains(driver).move_to_element(target).pause(0.3).click(target).perform()

    fname = _wait_for_download(download_dir, before, timeout)
    _close_popups(driver, main_handle)  # tidy the download popup before next batch
    if fname is None:
        raise RuntimeError(
            f"download produced no file within {timeout:.0f}s "
            f"(url={driver.current_url}, title={driver.title!r})"
        )
    try:
        _size = (download_dir / fname).stat().st_size
        _log(f"downloaded {fname} ({_size:,} bytes)")
    except Exception:
        _log(f"downloaded {fname}")
    return fname
