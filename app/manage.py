"""Small management CLI for ops tasks that don't belong in the web API.

    python -m app.manage initdb
    python -m app.manage enumerate --type Company        # queue a catalog build
    python -m app.manage recheck-all                     # queue rechecks now
    python -m app.manage smoke                            # live one-company test

The first three only enqueue jobs / touch the DB; the running worker executes
them. ``smoke`` instead drives a real browser itself (no worker/queue) so you
can shake out the live SEDAR+ selectors in one short run.
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from .config import settings
from .db import init_db, session_scope
from .models import Company
from . import queue as q

# Mirror of sedar.profiles.PROFILE_TYPES, kept local so the DB-only commands
# (and --help) don't import the selenium-backed sedar package.
PROFILE_TYPES = (
    "Company",
    "Investment fund",
    "Investment fund group",
    "Industry participant",
    "Third party filer",
)


def cmd_initdb(_args) -> int:
    init_db()
    print("database initialised")
    return 0


def cmd_enumerate(args) -> int:
    init_db()
    with session_scope() as db:
        job = q.enqueue_enumerate(db, profile_type=args.type, max_pages=args.max_pages)
        print(f"queued enumerate job #{job.id} (type={args.type}, max_pages={args.max_pages})")
    return 0


def cmd_recheck_all(_args) -> int:
    init_db()
    with session_scope() as db:
        companies = list(db.scalars(select(Company).where(Company.saved.is_(True))))
        for c in companies:
            q.enqueue_recheck(db, c)
        print(f"queued rechecks for {len(companies)} saved companies")
    return 0


def cmd_smoke(args) -> int:
    """End-to-end live test for ONE company, run synchronously in this process.

    Builds a real browser, resolves the company (preferring a given
    ``--profile-id`` over the Number bridge), runs the document search, and
    downloads up to ``--max-batches`` zip(s). Prints each step so you can see
    exactly where a selector needs tuning. On a headless server run it under
    Xvfb:  xvfb-run -a -s "-screen 0 1920x1400x24" python -m app.manage smoke
    """
    # Imported here so the lighter DB-only commands don't require Chrome deps.
    from . import scraper

    init_db()
    with session_scope() as db:
        company = db.scalar(select(Company).where(Company.number == args.number))
        if company is None:
            company = Company(number=args.number, name=args.name, type="Company")
            db.add(company)
            db.flush()
        if args.profile_id:
            company.profile_id = args.profile_id
            from sedar import documents as _docs
            company.profile_url = _docs.PROFILE_URL.format(profile_id=args.profile_id)
        company.saved = True
        db.flush()
        company_id = company.id
        label = f"{company.name} (#{company.number})"

    print(f"[smoke] building browser (headless={settings.headless}) …")
    driver = scraper.make_driver(settings.download_dir)
    try:
        def progress(batches, done, total, msg):
            print(f"[smoke]   progress: batch {batches}, {done} new doc(s), "
                  f"site reports {total} total — {msg}")

        print(f"[smoke] resolving + downloading {label} "
              f"(max_batches={args.max_batches}) …")
        with session_scope() as db:
            company = db.get(Company, company_id)
            result = scraper.download_company(
                db, driver, company,
                only_new=False, max_batches=args.max_batches, progress=progress,
            )
        print(f"[smoke] DONE: {result}")
        company_dir = settings.download_dir / args.number
        files = sorted(p.name for p in company_dir.glob("*")) if company_dir.exists() else []
        print(f"[smoke] files in {company_dir}: {files or '(none — check selectors/popups)'}")
        return 0 if result["batches"] else 2
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="app.manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb").set_defaults(func=cmd_initdb)

    e = sub.add_parser("enumerate", help="Queue a catalog enumeration job")
    e.add_argument("--type", default="Company", choices=PROFILE_TYPES)
    e.add_argument("--max-pages", type=int, default=None)
    e.set_defaults(func=cmd_enumerate)

    sub.add_parser("recheck-all", help="Queue rechecks for all saved companies").set_defaults(
        func=cmd_recheck_all
    )

    s = sub.add_parser("smoke", help="Live one-company download test (no worker)")
    s.add_argument("--number", default="000003771", help="Issuer number (folder/DB key)")
    s.add_argument("--name", default="Homerun Resources Inc.", help="Company name (if new)")
    # Default to the verified Homerun profile id so the default run exercises the
    # known-good download path. Pass --profile-id '' to test the Number bridge.
    s.add_argument("--profile-id", default="517042d52d6b1ddfa40ea23cc4c62739",
                   help="profile.html id (verified path). Use '' to test the Number bridge instead")
    s.add_argument("--max-batches", type=int, default=1,
                   help="Cap zip batches to download (default 1; None-equivalent: large number)")
    s.set_defaults(func=cmd_smoke)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
