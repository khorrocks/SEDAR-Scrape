"""Minimal, KNOWN-GOOD reference that downloaded a real zip from SEDAR+.

This is the exact shape of script that succeeded in the previous session (it
landed `requested_documents.zip` containing a real filing PDF). It hardcodes the
sandbox's Chrome/chromedriver paths and cert/Xvfb workarounds — treat it as a
reference for the working sequence, not as the production entry point (use
`python -m sedar.cli` for that). Run under Xvfb:

    xvfb-run -a -s "-screen 0 1920x1400x24" python reference/verified_download.py
"""

import os, sys, time, shutil
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains


def log(*a):
    print(*a, flush=True)


# Adjust these to your environment (or let Selenium Manager / uc resolve them).
CHROME = "/root/.cache/selenium/chrome/linux64/150.0.7871.24/chrome"
DRIVER = "/root/.cache/selenium/chromedriver/linux64/150.0.7871.24/chromedriver"
DLDIR = os.path.abspath("./downloads")
PROFILE = "https://www.sedarplus.ca/csa-party/records/profile.html?id=517042d52d6b1ddfa40ea23cc4c62739"

os.makedirs(DLDIR, exist_ok=True)
patched = "/tmp/uc_ref_chromedriver"
shutil.copy(DRIVER, patched)
os.chmod(patched, 0o755)

opts = uc.ChromeOptions()
for a in [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--ignore-certificate-errors",   # sandbox TLS-MITM only; drop on a normal machine
    "--window-size=1920,1400",
    "--disable-popup-blocking",      # CRITICAL: download fires via window.open()
]:
    opts.add_argument(a)
opts.binary_location = CHROME
opts.set_capability("acceptInsecureCerts", True)
opts.add_experimental_option(
    "prefs",
    {
        "download.default_directory": DLDIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.popups": 1,
    },
)

d = uc.Chrome(
    options=opts,
    driver_executable_path=patched,
    browser_executable_path=CHROME,
    headless=False,
    use_subprocess=True,
    version_main=150,
)
# Browser-wide so the popup target's download is captured.
d.execute_cdp_cmd(
    "Browser.setDownloadBehavior",
    {"behavior": "allow", "downloadPath": DLDIR, "eventsEnabled": True},
)
J = d.execute_script
try:
    d.get(PROFILE)
    time.sleep(8)
    J("arguments[0].click();", d.find_element(
        By.XPATH,
        "//*[contains(.,'Search and download documents for this profile')][self::a or self::button]"))
    time.sleep(8)
    J("arguments[0].click();", d.find_element(By.XPATH, "//button[normalize-space(.)='Search']"))
    time.sleep(9)
    # tick exactly one row checkbox for a small, fast test
    J("const rs=document.querySelectorAll('table tr');"
      "for(const r of rs){const c=r.querySelector('input[type=checkbox]');if(c){c.click();break;}}")
    time.sleep(2)
    J("arguments[0].click();", d.find_element(
        By.XPATH, "//button[contains(normalize-space(.),'Download documents')]"))
    time.sleep(4)
    before = set(os.listdir(DLDIR))
    btn = [b for b in d.find_elements(By.XPATH, "//button|//a")
           if b.is_displayed() and b.text.strip() == "Download"][0]
    ActionChains(d).move_to_element(btn).pause(0.3).click(btn).perform()
    log("clicked; window handles:", len(d.window_handles))
    for i in range(45):
        files = os.listdir(DLDIR)
        done = [f for f in files if not f.endswith(".crdownload") and f not in before]
        if done:
            log("SUCCESS:", done)
            break
        time.sleep(3)
    log("FINAL:", os.listdir(DLDIR))
finally:
    try:
        d.quit()
    except Exception:
        pass
