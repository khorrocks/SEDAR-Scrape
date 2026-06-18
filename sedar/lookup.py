"""Bridge the enumerated issuer *Number* to the Documents search, and capture a
company's stable ``profile.html?id=`` URL via the per-row "Generate URL" action.

Why this module exists
----------------------
``documents.open_profile_documents`` (verified end-to-end) starts from an opaque
``profile.html?id=<hash>``. But enumeration only gives us the stable issuer
**Number** (e.g. ``000003771``), not the hash. SEDAR+ offers two ways across:

  1. The Documents tab's "Profile name or number" lookup ("Add another"), which
     scopes a document search to that issuer using the Number directly.
  2. The Profiles tab per-row "Generate URL" action, which reveals the
     ``profile.html?id=`` link we can then store and reuse forever.

Both were observed in the live UI but, per the handoff, not yet automated. The
selectors below follow the documented DOM/text. They are best-effort and
text-based, so expect to adjust them if the SEDAR+ UI shifts. ``profile_id`` is
preferred once captured (it is the verified path); the number lookup is the
fallback when we have never resolved a company before.
"""

from __future__ import annotations

import re
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

DOCUMENTS_ENTRY = "https://www.sedarplus.ca/csa-party/records/search.html"
PROFILE_ID_RE = re.compile(r"profile\.html\?id=([0-9a-fA-F]+)")


def _click(driver, element) -> None:
    driver.execute_script("arguments[0].click();", element)


def open_documents_tab(driver, settle: float = 8.0) -> None:
    """Land on the search page and select the Documents tab."""
    driver.get(DOCUMENTS_ENTRY)
    time.sleep(settle)
    tabs = driver.find_elements(By.XPATH, "//a[normalize-space(.)='Documents']")
    if tabs:
        _click(driver, tabs[0])
        time.sleep(settle)


def add_profile_by_number(driver, number: str, settle: float = 6.0) -> bool:
    """Type an issuer Number into the 'Profile name or number' lookup and pick
    the suggestion. Returns True if a suggestion was selected.

    The field autocompletes; we type the number, wait for the dropdown, and
    click the first suggestion (or press Enter as a fallback).
    """
    inputs = driver.find_elements(
        By.XPATH,
        "//input[contains(@placeholder,'Profile name or number') or "
        "contains(@aria-label,'Profile name or number') or "
        "contains(@name,'profile')]",
    )
    if not inputs:
        return False
    box = inputs[0]
    box.clear()
    box.send_keys(number)
    time.sleep(settle)

    options = driver.find_elements(
        By.XPATH,
        "//li[contains(@class,'suggestion') or contains(@role,'option')]"
        "|//ul[contains(@class,'autocomplete')]//li",
    )
    visible = [o for o in options if o.is_displayed()]
    if visible:
        _click(driver, visible[0])
    else:
        box.send_keys(Keys.ENTER)
    time.sleep(settle)
    return True


def open_documents_by_number(driver, number: str, settle: float = 8.0) -> bool:
    """Open the Documents tab, scope it to the given issuer Number, and submit.

    Leaves the driver on a document results page equivalent to the verified
    ``open_profile_documents`` + ``run_search`` flow. Returns False if the
    number lookup field could not be used.
    """
    open_documents_tab(driver, settle=settle)
    if not add_profile_by_number(driver, number, settle=settle):
        return False
    btn = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(.)='Search']"))
    )
    _click(driver, btn)
    time.sleep(settle)
    return True


def capture_profile_id_for_number(driver, number: str, settle: float = 7.0) -> str | None:
    """On the Profiles tab, search the issuer Number and use the row's
    "Generate URL" action to capture the ``profile.html?id=`` hash.

    Returns the hash (e.g. ``517042d52d6b1ddfa40ea23cc4c62739``) or None.
    """
    from . import profiles

    profiles.open_profiles_search(driver, settle=settle)

    # Reuse the keyword/number box on the Profiles search if present.
    boxes = driver.find_elements(
        By.XPATH,
        "//input[contains(@placeholder,'name or number') or "
        "contains(@aria-label,'name or number') or @type='search']",
    )
    if boxes:
        boxes[0].clear()
        boxes[0].send_keys(number)
        time.sleep(1)
    profiles.run_search(driver, settle=settle)

    # Find the row whose Number matches and click its "Generate URL" action.
    rows = driver.find_elements(By.XPATH, "//table//tr")
    for r in rows:
        if number in r.text:
            actions = r.find_elements(
                By.XPATH, ".//a[contains(.,'Generate URL')]|.//button[contains(.,'Generate URL')]"
            )
            if actions:
                _click(driver, actions[0])
                time.sleep(settle)
                break

    # The generated URL appears in a modal/field or is copied to clipboard. Scan
    # the DOM (inputs + body text) for the profile.html?id= pattern.
    candidates = []
    for inp in driver.find_elements(By.XPATH, "//input|//textarea"):
        val = inp.get_attribute("value") or ""
        candidates.append(val)
    candidates.append(driver.find_element(By.TAG_NAME, "body").text)
    for text in candidates:
        m = PROFILE_ID_RE.search(text or "")
        if m:
            return m.group(1)
    return None
