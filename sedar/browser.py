"""Stealth Chrome driver for SEDAR+.

SEDAR+ sits behind Radware / ShieldSquare bot detection, which blocks plain
headless Selenium (you get redirected to a ``validate.perfdrive.com`` block
page). Getting through it reliably means driving a *real* (non-headless)
Chrome via ``undetected-chromedriver``. On a headless server that means
running it under a virtual display (Xvfb).

This module just builds and configures that driver. Politeness and the actual
SEDAR+ navigation live in the other modules.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import undetected_chromedriver as uc


@dataclass
class BrowserConfig:
    download_dir: Path
    # Path to a Chrome / Chrome-for-Testing binary. If None, undetected
    # chromedriver tries to locate an installed Chrome.
    chrome_binary: str | None = None
    # Path to a matching chromedriver. If None, uc downloads one.
    chromedriver_binary: str | None = None
    # Pin the Chrome major version uc patches the driver for (e.g. 150).
    version_main: int | None = None
    # Run a *real* headed browser. Strongly recommended against Radware.
    # On a server, pair headless=False with an Xvfb display (see run_under_xvfb).
    headless: bool = False
    # Accept the egress proxy's MITM certificate. Needed in environments that
    # intercept TLS (e.g. some CI sandboxes); leave False on a normal machine.
    ignore_cert_errors: bool = False
    window_size: tuple[int, int] = (1920, 1400)
    extra_args: list[str] = field(default_factory=list)


def build_driver(cfg: BrowserConfig) -> uc.Chrome:
    """Construct a configured undetected-chromedriver Chrome instance."""
    cfg.download_dir.mkdir(parents=True, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--window-size={cfg.window_size[0]},{cfg.window_size[1]}")
    # SEDAR+ triggers the document download via a window.open() popup; with the
    # popup blocker on, the green "Download" button silently does nothing.
    options.add_argument("--disable-popup-blocking")
    if cfg.ignore_cert_errors:
        options.add_argument("--ignore-certificate-errors")
        options.set_capability("acceptInsecureCerts", True)
    for arg in cfg.extra_args:
        options.add_argument(arg)
    if cfg.chrome_binary:
        options.binary_location = cfg.chrome_binary

    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(cfg.download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            # Save PDFs instead of opening them in the viewer.
            "plugins.always_open_pdf_externally": True,
            # Allow popups (1 = allow) so the download window.open() succeeds.
            "profile.default_content_setting_values.popups": 1,
        },
    )

    # uc patches the chromedriver binary in place, so hand it a writable copy.
    driver_path = None
    if cfg.chromedriver_binary:
        tmp = Path(tempfile.gettempdir()) / "uc_sedar_chromedriver"
        shutil.copy(cfg.chromedriver_binary, tmp)
        os.chmod(tmp, 0o755)
        driver_path = str(tmp)

    driver = uc.Chrome(
        options=options,
        headless=cfg.headless,
        use_subprocess=True,
        driver_executable_path=driver_path,
        browser_executable_path=cfg.chrome_binary,
        version_main=cfg.version_main,
    )

    # Make sure CDP allows downloads to our directory (covers headed Chrome
    # which can otherwise ignore the prefs download path). Browser.* is
    # browser-wide and also covers any new tab/target a download may open;
    # fall back to the page-scoped command on older Chrome.
    try:
        driver.execute_cdp_cmd(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(cfg.download_dir),
                "eventsEnabled": True,
            },
        )
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(cfg.download_dir)},
        )
    except Exception:
        pass

    return driver
