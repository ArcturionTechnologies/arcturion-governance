# ArcturionGovernance

Governance tools for a workspace of AI agents: a safe auto-fixer that only
makes changes it can undo, a drift sentinel, a weekly agent scorecard, a
decision journal, and a second-opinion advisor.

When several AI agents share a workspace, small messes pile up. Leftover
`.bak` copies, files dropped in the wrong folder, the same alarm logged every
day for a month, notes nobody has reviewed in a year. Detecting that is easy.
Closing it safely is the hard part, and so is knowing whether the agents are
actually getting better. These five tools cover that loop:

| Tool | What it does | Writes? |
| --- | --- | --- |
| `arcgov remediate` | Fixes only what it can undo. Every finding gets an owner and a deadline; past the deadline it is auto-closed, handed to the owning agent, or put in front of a human. Every action is ledgered with a diff and can be undone | Yes, policy-gated |
| `arcgov drift` | Runs read-only probes (your own checkers, or built-ins for leftovers, stale notes, missing frontmatter) and reports findings as proposals | Only its own report and backlog |
| `arcgov scorecard` | Reads agent session transcripts already on disk and scores each agent weekly | Its own database and report |
| `arcgov journal` | Logs a decision with the outcome you expect and a review date, then makes you write the retro | Its own log |
| `arcgov advisor` | Asks a second model for a structured opinion (choice, score or probability) before a decision. Advice only | Private receipts |

Python 3.10+, standard library only. The only network calls are the advisor and the optional webhook notifier.
Both are off until you configure them; everything else, including the
scorecard, works on local files.

> **Portfolio project.** This is an open-source sample of the tooling behind
> Arcturion's multi-agent setup. It is not a commercial product and makes no
> claims about revenue or customers. All bundled data is synthetic.

## Quickstart

```bash
git clone https://github.com/ArcturionTechnologies/arcturion-governance.git
cd arcturion-governance
pip install .                       # puts `arcgov` on PATH

cd examples/demo                    # three made-up agents
export ARC_ROOT="$PWD" ARC_GOVERNANCE_CONFIG="$PWD/governance.json"

arcgov remediate plan               # dry run, no writes
arcgov drift run                    # read-only probes
arcgov journal log "Move CI to the faster runner" --expect "builds under 5 min" --review 2026-11-01
```

The demo's [README](examples/demo/README.md) explains what each finding is.
No install? `PYTHONPATH=/path/to/arcturion-governance python3 -m arcturion_governance <tool> ...`
works the same.

## How safe-remediate decides

Every finding is fingerprinted, so seeing it again **ages** it instead of
logging it again. The clock starts from real evidence (a file's modification
time), not from the first run. Inside its SLA a finding is legitimately open.
Once the SLA passes, it takes exactly one route:

| Route | When | What happens |
| --- | --- | --- |
| auto-close | the fix is reversible and the target belongs to the governing agent | the action runs, and the diff goes to the ledger first |
| delegate | the target is in another agent's area | one dated line in that agent's `inbox.md`, updated in place on later runs |
| human | the fix is lossy, ambiguous, or needs judgment | listed as a decision for a person; it stays open until someone decides |

Age never promotes a finding past that verdict, and nothing on the
`never_auto` list (read-only prefixes, protected folders) is ever touched.
A finding that disappears on its own is closed as resolved. The report
includes the backlog's first derivative (open count, change, closure rate),
because a backlog that grows at a steady rate is not being closed whatever
its size.

Built-in classes:

- **drift-debris**: leftover copies (`*.bak*`, `*.old`, `*.orig`, `*.tmp`, `*~`) and
  dangling symlinks. The leftover is diffed against the live file, the diff is
  written to the ledger, then the leftover is really deleted. With no live file,
  the recorded diff is the full pre-image. Binary leftovers that differ are never
  auto-deleted.
- **stray-root**: a loose file at the governing agent's root is moved into the
  folder named by its own `domain:` and `type:` frontmatter. Folders are never
  created; a file that doesn't say where it belongs goes to a human.
- **alarm-dedup**: the same alarm logged day after day in the drift backlog or
  the governing inbox collapses into one line with first seen, last seen, age
  and sighting count. Numbers are blanked when matching, so "flags: 3" and
  "flags: 4" are the same alarm.

You can also wrap an engine you already trust as a **command class**: give the
policy a dry argv and an apply argv, and `{limit}` is filled in from the
policy. See [`examples/remediate_policy.json`](examples/remediate_policy.json).

```bash
arcgov remediate plan                 # what would happen
arcgov remediate apply                # do it (policy must be enabled)
arcgov remediate routes               # every open finding, its age and route
arcgov remediate undo <action_id>     # reverse one action
```

## drift-sentinel

Probes are configured under `"drift"` in `governance.json`. Each one is
fail-soft: a broken checker shows up as amber, it never stops the sweep.

```json
"drift": {"probes": [
  {"name": "leftovers", "kind": "leftovers", "section": "scan", "red_at": 50},
  {"name": "stale-notes", "kind": "stale-files", "section": "memory", "days": 120},
  {"name": "missing-created", "kind": "missing-frontmatter", "required": ["created"]},
  {"name": "link-check", "kind": "command", "argv": ["python3", "tools/links.py", "--json"],
   "count_keys": ["broken"]}
]}
```

`arcgov drift report --apply` writes a dated report and appends amber and red
findings to the drift backlog, in the format alarm-dedup collapses. The
sentinel's own source contains no delete, rename or replace call, and a test
checks that.

## Weekly scorecard

`arcgov scorecard` reads session transcripts the agent harness already writes
(Claude Code's JSON Lines in `~/.claude/projects` by default), so it adds no
hooks and no latency. It stores numbers and labels only, never prompt or
response text.

Each human message plus everything the agent did before the next one is a
**turn**. Rework is judged by what the person says next, not by what the agent
claims: the next message is classified as a new request, a clarification, a
correction ("that's not what I asked", "still broken") or a repeat of the same
request. Only the last two count as rework.

Four faculties are scored 0 to 100 per agent per week:

| Faculty | Measured as |
| --- | --- |
| Economy | 1 minus the rework rate |
| Precision | 1 minus the share of tool calls that failed |
| Speed | target first-response time / median first-response time, capped at 100 |
| Recall | share of turns that consulted a knowledge or memory tool (`knowledge_tools` regex) |

A missing measurement is shown as `n/a`, never 0. The composite is the mean of
the measured faculties, and it is withheld, with the reason, when a week has
too few turns or too few measurable faculties. The card leads with each
agent's change against its previous four weeks. Two judged faculties from the
original design (did it say the right thing, did it sound human) need an LLM
judge reading text, so this version does not score them.

```bash
arcgov scorecard collect                 # mine transcripts (idempotent)
arcgov scorecard index --week 2026-07-06 # score a week
arcgov scorecard card --send             # send last week's card via the notifier
arcgov scorecard review run              # today's agents get an evidence-backed review in their inbox
```

Transcript folders are matched to agents by roster name (the most specific
path segment wins), by `retired` names, or by an explicit `project_map`.
Unmatched sessions are reported as UNKNOWN, never dropped.

## Decision journal

```bash
arcgov journal log "Retire the nightly export" --expect "no user impact" --review 2026-11-15
arcgov journal due --notify       # one notice when retros are due
arcgov journal retro dec_1a2b3c4d --actual "two users asked for it back" --verdict bad-call
arcgov journal stats              # good-call rate across every retro
```

The log is append-only JSON Lines. A retro is a new row; the decision row is
never rewritten.

## Second-opinion advisor

Before a real decision an agent can send a small, sanitized summary of the
facts and up to 32 questions to a typed-judgment API: **choice** (pick one of
the options), **score** (a position on 2 to 10 ordered levels) or **noul** (the
probability that a statement is true). The client:

- refuses anything that looks like a credential before it leaves the machine,
  and refuses evidence that contains the API key itself;
- makes exactly one HTTPS request, with no retries, no redirects, a 15 second
  deadline and a 1 MiB response cap;
- validates every answer (a "choice" must be the most probable option, a score
  must agree with its probabilities) and drops fields it doesn't know;
- keeps a private receipt (files 0600, folders 0700) with hashes, answers and
  token usage, never the evidence;
- reuses an identical answer for up to 5 minutes at no new cost.

The answer is advice. It never authorizes an action, and "unavailable" is not
a veto. Configure it under `"advisor"` with an `https://` endpoint and a model
id; the key is read from `ADVISOR_API_KEY` (or the variable you name).

## Configuration

One `governance.json` describes the workspace: who governs, who the agents are,
where state lives, and how to notify. Paths may use `$ARC_ROOT`, `$HOME` or `~`.
See [`examples/governance.example.json`](examples/governance.example.json).

| Setting | Meaning |
| --- | --- |
| `self` | The governing agent. Only its findings can be auto-closed |
| `human` | Label for the human decision route |
| `agents` | Name -> `home` folder (and optional `inbox`, `aliases`). Delegation writes to `<home>/inbox.md` |
| `retired` | Old agent name -> the agent that inherited its work |
| `state_dir`, `ledger_dir`, `drift_queue` | Where state, the undo ledger and the backlog live |
| `notify` | `stdout` (default), `none`, `file`, or `webhook` |
| `remediate`, `drift`, `scorecard`, `journal`, `advisor` | Each tool's own settings |

Environment variables: `ARC_ROOT`, `ARC_GOVERNANCE_CONFIG`,
`ARC_REMEDIATE_POLICY`, `ARC_DECISION_LOG`, `GOVERNANCE_NOTIFY` (override the
notifier kind), `ADVISOR_ENDPOINT`, `ADVISOR_API_KEY`. A webhook URL is read
from the variable named in `notify.url_env` (default `GOVERNANCE_WEBHOOK_URL`).
Secrets come from the environment only; nothing reads a credential store.

### Notifications

```json
"notify": {"kind": "webhook", "url_env": "GOVERNANCE_WEBHOOK_URL", "format": "slack"}
```

`format` is `json` (`{"title", "text", "priority"}`), `slack` (`{"text"}`, which
most chat incoming-webhooks accept) or `text`. A failed send never raises.

## Scheduling (macOS)

[`examples/launchd/`](examples/launchd) has templates for a weekly remediate
apply, a daily drift report, the Monday journal nudge, and the scorecard jobs.
Replace each `__PLACEHOLDER__`, copy to `~/Library/LaunchAgents/`, and
`launchctl bootstrap gui/$UID <file>`. On Linux, the same commands work from cron
or a systemd timer.

## Project layout

```
arcturion_governance/config.py       governance.json, roster, paths
arcturion_governance/notify.py       stdout / file / webhook / none
arcturion_governance/remediate/      ledger (undo), sla (routes), hygiene classes, engine
arcturion_governance/drift/          drift sentinel and built-in probes
arcturion_governance/scorecard/      weekly agent scorecard
arcturion_governance/journal.py      decision journal
arcturion_governance/advisor.py      second-opinion advisor
arcturion_governance/cli.py          `arcgov` dispatcher
examples/                            demo workspace, policy, config, launchd templates
tests/                               169 tests, stdlib unittest
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Every test runs in a temporary folder with synthetic data and no network. The
destructive paths (delete, move, rewrite) are tested together with their undo.

## License

MIT. See [LICENSE](LICENSE).

Implementation is AI-assisted; architecture, requirements, and testing directed by Robert Lingoes.
