"""`arcgov <tool> ...`: one entry point for every governance tool.

    arcgov remediate plan|apply|status|routes|undo ...
    arcgov drift run|report|probes ...
    arcgov scorecard <subcommand> ...
    arcgov journal log|due|retro|stats ...
    arcgov advisor --status | --request-json ... | --record-choice ...
"""
from __future__ import annotations

import importlib
import sys

TOOLS = {
    "remediate": ("arcturion_governance.remediate.engine", "safe, reversible auto-remediation"),
    "drift": ("arcturion_governance.drift.sentinel", "drift sentinel (proposals only)"),
    "scorecard": ("arcturion_governance.scorecard.cli", "weekly agent scorecard"),
    "journal": ("arcturion_governance.journal", "decision journal with retros"),
    "advisor": ("arcturion_governance.advisor", "second-opinion advisor"),
}


def usage() -> str:
    lines = ["usage: arcgov <tool> [args...]", "", "tools:"]
    lines += [f"  {name:10} {desc}" for name, (_mod, desc) in TOOLS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    tool, rest = args[0], args[1:]
    if tool not in TOOLS:
        print(f"unknown tool: {tool}\n\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(TOOLS[tool][0])
    return int(module.main(rest) or 0)


if __name__ == "__main__":
    sys.exit(main())
