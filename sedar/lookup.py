"""Open a company's document search from its issuer Number.

Verified navigation (June 2026, live probes):
  profile.html?id=<bootstrap>            (clears Radware, starts a session)
    -> click the nav link whose href contains 'searchDocuments'
    -> "Search and download documents" page (title "Search"), which has:
         * an input placeholder "Profile name or number"  (autocompletes)
         * a "Search" button
         * results table: Profile(s) | Document | Submitted date |
                          Principal jurisdiction | File size | Actions
         * "All documents listed on this page" select-all + "Download documents"

We type the Number, pick the autocomplete suggestion to scope the search to that
issuer, then Search. The download mechanics live in ``documents.py``.
"""

from __future__ import annotations

import os
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BOOTSTRAP_PROFILE_ID = os.getenv(
    "SEDAR_BOOTSTRAP_PROFILE_ID", "517042d52d6b1ddfa40ea23cc4c62739"
)
BOOTSTRAP_URL = "https://www.sedarplus.ca/csa-party/records/profile.html?id={pid}"


def _click(driver, element) -> None:
    driver.execute_script("arguments[0].click();", element)


def _click_href(driver, substring: str) -> bool:
    return bool(
        driver.execute_script(
            """const s=arguments[0];
               const el=[...document.querySelectorAll('a')]
                 .find(a=>(a.getAttribute('href')||'').includes(s));
               if(el){el.scrollIntoView({block:'center'});el.click();return true;}
               return false;""",
            substring,
        )
    )


def open_documents_search(driver, settle: float = 10.0) -> None:
    """Bootstrap a session and open the 'Search and download documents' page."""
    driver.get(BOOTSTRAP_URL.format(pid=BOOTSTRAP_PROFILE_ID))
    time.sleep(settle)
    if not _click_href(driver, "searchDocuments"):
        raise RuntimeError("could not find the 'searchDocuments' nav link")
    time.sleep(settle)


def add_profile_by_number(driver, number: str, settle: float = 6.0) -> bool:
    """Type an issuer Number into 'Profile name or number' and pick the
    suggestion so the search is scoped to that one issuer."""
    boxes = driver.find_elements(
        By.XPATH,
        "//input[contains(@placeholder,'Profile name or number') or "
        "contains(@aria-label,'Profile name or number')]",
    )
    if not boxes:
        return False
    box = boxes[0]
    box.clear()
    box.send_keys(number)
    time.sleep(settle)

    # Pick a visible autocomplete suggestion; fall back to Enter.
    options = [
        o
        for o in driver.find_elements(
            By.XPATH,
            "//li[@role='option']|//ul[contains(@class,'autocomplete')]//li"
            "|//*[contains(@class,'suggestion')]|//*[contains(@class,'typeahead')]//li",
        )
        if o.is_displayed()
    ]
    if options:
        _click(driver, options[0])
    else:
        box.send_keys(Keys.ENTER)
    time.sleep(settle)
    return True


def open_documents_by_number(driver, number: str, settle: float = 9.0) -> bool:
    """Open the documents search scoped to ``number`` and submit it. Leaves the
    driver on a results page (equivalent to the verified profile-id flow)."""
    open_documents_search(driver, settle=settle)
    if not add_profile_by_number(driver, number, settle=min(settle, 6.0)):
        return False
    btn = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(.)='Search']"))
    )
    _click(driver, btn)
    time.sleep(settle)
    return True
