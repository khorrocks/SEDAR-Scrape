"""Command-line entry point for the SEDAR+ scraper.

Examples:
  # 1. Enumerate every active company into a CSV
  python -m sedar.cli enumerate --type Company --out companies.csv

  # 2. Download all documents for one profile (by its profile.html id)
  python -m sedar.cli documents --profile-id 517042d52d6b1ddfa40ea23cc4c62739 \\
      --out-dir downloads/

On a headless server, prefix any command with xvfb so a real (non-headless)
Chrome can run -- Radware blocks headless Chrome:
  xvfb-run -a -s "-screen 0 1920x1400x24" python -m sedar.cli ...
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .browser import BrowserConfig, build_driver
from . import documents, profiles


def _common_browser_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--chrome-binary", default=None, help="Path to Chrome binary")
    p.add_argument("--chromedriver", default=None, help="Path to matching chromedriver")
    p.add_argument("--chrome-version", type=int, default=None, help="Chrome major version, e.g. 150")
    p.add_argument("--headless", action="store_true", help="Headless (will likely be blocked by Radware)")
    p.add_argument("--ignore-cert-errors", action="store_true", help="Accept MITM proxy certs (CI sandboxes only)")


def _make_driver(args, download_dir: Path):
    cfg = BrowserConfig(
        download_dir=download_dir,
        chrome_binary=args.chrome_binary,
        chromedriver_binary=args.chromedriver,
        version_main=args.chrome_version,
        headless=args.headless,
        ignore_cert_errors=args.ignore_cert_errors,
    )
    return build_driver(cfg)


def cmd_enumerate(args) -> int:
    driver = _make_driver(args, Path(args.out).parent if args.out else Path("."))
    try:
        rows = profiles.enumerate_profiles(
            driver, profile_type=args.type, max_pages=args.max_pages
        )
    finally:
        driver.quit()
    if not rows:
        print("No profiles scraped.", file=sys.stderr)
        return 1
    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "jurisdiction", "type", "number"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} profiles to {out}")
    return 0


def cmd_documents(args) -> int:
    out_dir = Path(args.out_dir)
    driver = _make_driver(args, out_dir)
    try:
        documents.open_profile_documents(driver, args.profile_id)
        documents.run_search(driver)
        print(documents.result_count(driver) or "(no result count)")
        pages = 0
        while True:
            pages += 1
            fname = documents.download_current_page(driver, out_dir, timeout=args.timeout)
            print(f"  page {pages}: {'downloaded ' + fname if fname else 'TIMED OUT'}")
            if args.max_pages and pages >= args.max_pages:
                break
            # documents.next_page lives in profiles; reuse the same control here
            if not profiles.next_page(driver):
                break
    finally:
        driver.quit()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sedar", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("enumerate", help="List profiles (companies/funds) to CSV")
    e.add_argument("--type", default="Company", choices=profiles.PROFILE_TYPES)
    e.add_argument("--max-pages", type=int, default=None)
    e.add_argument("--out", default="profiles.csv")
    _common_browser_args(e)
    e.set_defaults(func=cmd_enumerate)

    d = sub.add_parser("documents", help="Download documents for one profile")
    d.add_argument("--profile-id", required=True, help="The id from profile.html?id=...")
    d.add_argument("--out-dir", default="downloads")
    d.add_argument("--max-pages", type=int, default=None, help="Cap result pages to download")
    d.add_argument("--timeout", type=float, default=600.0, help="Per-page download timeout (s)")
    _common_browser_args(d)
    d.set_defaults(func=cmd_documents)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
