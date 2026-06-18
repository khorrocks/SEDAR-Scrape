"""Enumerate reporting issuers / companies from the SEDAR+ Profiles search.

The Profiles tab lets you filter by profile type (Company, Investment fund,
Investment fund group, Industry participant, Third party filer) and paginate
through results. Each result row exposes: Name, Principal jurisdiction, Type,
Number, Actions.

Two things to know:
  * The result rows do NOT contain the opaque ``profile.html?id=<hash>`` URL as
    a plain href -- that link is produced by the per-row "Generate URL" action.
  * The CSV "Export" on this page is capped at ~2,030 rows, but the paginated
    HTML is not, so we page through the HTML instead.

The "Number" captured here (e.g. ``000003771``) is the stable issuer number and
is what you feed into the Documents tab's "Profile name or number" lookup, so
you usually do not need the opaque profile id at all.
"""

from __future__ import annotations

import re
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# A reachable Profiles search entry point. Any SEDAR+ profile page redirects
# into a session and exposes the "Profiles" nav tab.
SEARCH_ENTRY = "https://www.sedarplus.ca/csa-party/records/search.html"

PROFILE_TYPES = (
    "Company",
    "Investment fund",
    "Investment fund group",
    "Industry participant",
    "Third party filer",
)


def open_profiles_search(driver, settle: float = 8.0) -> None:
    driver.get(SEARCH_ENTRY)
    time.sleep(settle)
    # Make sure we are on the Profiles tab.
    tabs = driver.find_elements(By.XPATH, "//a[normalize-space(.)='Profiles']")
    if tabs:
        driver.execute_script("arguments[0].click();", tabs[0])
        time.sleep(settle)


def set_profile_type(driver, profile_type: str) -> None:
    """Select a value in the 'Profile type' dropdown (best-effort)."""
    from selenium.webdriver.support.ui import Select

    selects = driver.find_elements(By.XPATH, "//select[contains(@name,'ProfileType')]")
    if not selects:
        return
    Select(selects[0]).select_by_visible_text(profile_type)


def run_search(driver, settle: float = 9.0) -> None:
    btn = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, "//button[normalize-space(.)='Search']"))
    )
    driver.execute_script("arguments[0].click();", btn)
    time.sleep(settle)


def total_results(driver) -> int | None:
    body = driver.find_element(By.TAG_NAME, "body").text
    m = re.search(r"Displaying[\s\d,\-]+of\s+([\d,]+)\s+results", body)
    return int(m.group(1).replace(",", "")) if m else None


def scrape_page(driver) -> list[dict]:
    """Scrape the current Profiles results page into row dicts."""
    rows = driver.find_elements(By.XPATH, "//table//tr")
    out = []
    for r in rows:
        cells = r.find_elements(By.TAG_NAME, "td")
        if len(cells) >= 4:
            out.append(
                {
                    "name": cells[0].text.strip(),
                    "jurisdiction": cells[1].text.strip(),
                    "type": cells[2].text.strip(),
                    "number": cells[3].text.strip(),
                }
            )
    return out


def next_page(driver, settle: float = 8.0) -> bool:
    """Click 'Next »' if present and enabled. Returns False when no next page."""
    links = driver.find_elements(
        By.XPATH, "//a[contains(normalize-space(.), 'Next')]"
    )
    for link in links:
        if link.is_displayed() and link.is_enabled():
            driver.execute_script("arguments[0].click();", link)
            time.sleep(settle)
            return True
    return False


def enumerate_profiles(
    driver,
    profile_type: str = "Company",
    max_pages: int | None = None,
    page_pause: float = 1.0,
) -> list[dict]:
    """Page through the Profiles search and collect rows for a profile type.

    ``max_pages`` caps how many result pages to walk (None = all). A polite
    ``page_pause`` is added on top of the per-page settle time.
    """
    open_profiles_search(driver)
    set_profile_type(driver, profile_type)
    run_search(driver)

    collected: list[dict] = []
    seen: set[tuple] = set()
    page = 0
    while True:
        page += 1
        for row in scrape_page(driver):
            key = (row["name"], row["number"])
            if key not in seen:
                seen.add(key)
                collected.append(row)
        if max_pages and page >= max_pages:
            break
        time.sleep(page_pause)
        if not next_page(driver):
            break
    return collected
