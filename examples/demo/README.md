# Demo workspace

A tiny synthetic workspace with three agents (`steward` governs, `builder` and
`research` are peers). Everything here is made up.

```bash
cd examples/demo
export ARC_ROOT="$PWD" ARC_GOVERNANCE_CONFIG="$PWD/governance.json"

arcgov remediate plan      # dry run: what would be closed, delegated, or held
arcgov drift run           # read-only probes
arcgov drift report        # preview of the report + backlog lines
```

What you should see:

- `04 Governance/Runbooks/deploy-checklist.md.orig` is a leftover copy of a live file (drift-debris).
- `weekly-sweep.md` sits at the steward root but declares `domain: "04 Governance"`
  and `type: ledger-entry`, so stray-root would file it into `04 Governance/Ledger/`.
- `builder/build.sh.old` is in another agent's area, so it is delegated, never touched.
- `governance/drift-backlog.md` and `steward/messages.md` (the steward's inbox) contain the same alarm logged
  several times; alarm-dedup collapses each into one aging line.

A fresh clone gives every file today's date, so most findings show as inside
their SLA ("in-sla"). Nothing is closed until a finding has been open longer
than its `sla_days`. `apply` writes a ledger and every action can be undone with
`arcgov remediate undo <id>`.
