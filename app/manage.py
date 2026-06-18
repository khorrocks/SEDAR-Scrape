"""Small management CLI for ops tasks that don't belong in the web API.

    python -m app.manage initdb
    python -m app.manage enumerate --type Company        # queue a catalog build
    python -m app.manage recheck-all                     # queue rechecks now

These only enqueue jobs / touch the DB; the running worker executes them.
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from .db import init_db, session_scope
from .models import Company
from . import queue as q
from sedar import profiles


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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="app.manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb").set_defaults(func=cmd_initdb)

    e = sub.add_parser("enumerate", help="Queue a catalog enumeration job")
    e.add_argument("--type", default="Company", choices=profiles.PROFILE_TYPES)
    e.add_argument("--max-pages", type=int, default=None)
    e.set_defaults(func=cmd_enumerate)

    sub.add_parser("recheck-all", help="Queue rechecks for all saved companies").set_defaults(
        func=cmd_recheck_all
    )

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
