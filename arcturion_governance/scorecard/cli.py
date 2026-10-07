"""`arcgov scorecard ...`

  collect [--since YYYY-MM-DD] [--json]   mine transcripts into turn rows
  index [--week YYYY-MM-DD] [--json]      score a week (default: last full week)
  card [--week ...] [--send]              print the card, or send it through the notifier
  review plan [--week ...]                show the weekday rotation
  review run [--agent A] [--week ...] [--dry-run]   write today's reviews to inboxes
  weekly [--week ...]                     collect, index, then send the card
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from .. import config as config_mod
from . import card, collect, index, review, store


def _week(value: str | None) -> str:
    if not value:
        return store.last_full_week()
    return store.week_start(dt.date.fromisoformat(value))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="arcgov scorecard", description="Weekly agent scorecard.")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--since")
    c.add_argument("--json", action="store_true")
    i = sub.add_parser("index")
    i.add_argument("--week")
    i.add_argument("--json", action="store_true")
    k = sub.add_parser("card")
    k.add_argument("--week")
    k.add_argument("--send", action="store_true")
    r = sub.add_parser("review")
    r.add_argument("action", choices=["plan", "run"])
    r.add_argument("--agent")
    r.add_argument("--week")
    r.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("weekly")
    w.add_argument("--week")
    args = p.parse_args(argv)
    settings = store.Settings(config_mod.load(args.config))

    if args.cmd == "collect":
        since = dt.date.fromisoformat(args.since) if args.since else None
        out = collect.collect(settings, since=since)
        print(json.dumps(out, indent=2) if args.json else
              f"collected {out['turns']} turn(s) from {out['files']} file(s): {out['by_agent']}")
        return 0
    if args.cmd == "index":
        rows = index.compute_week(settings, _week(args.week))
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(f"{row['agent']:12} turns={row['turns']:<4} composite={row['composite']} "
                      f"status={row['status']} {'; '.join(row['reasons'])}")
        return 0
    if args.cmd == "card":
        week = _week(args.week)
        if args.send:
            out = card.send(settings, week)
            print("sent" if out["sent"] else f"send failed; saved to {out['fallback']}")
        else:
            print(card.render(index.load_week(settings, week), week), end="")
        return 0
    if args.cmd == "review":
        if args.action == "plan":
            for day, agents in review.plan(settings, store.week_start(dt.date.fromisoformat(args.week))
                                           if args.week else store.week_start(dt.date.today())).items():
                print(f"{day}: {', '.join(agents)}")
            return 0
        results = review.run(settings, agent=args.agent, week=_week(args.week) if args.week else None,
                             dry=args.dry_run)
        for res in results:
            print(f"{res['agent']}: {res['status']}, {len(res['mitigations'])} mitigation(s); {res['result']}")
        if not results:
            print("no reviews scheduled today")
        return 0
    week = _week(args.week)
    collect.collect(settings)
    index.compute_week(settings, week)
    out = card.send(settings, week)
    print("sent" if out["sent"] else f"send failed; saved to {out['fallback']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
