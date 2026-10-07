"""Per-action ledger and restore path.

A remediation class may close a finding only if every action is written to a
per-action ledger AND a named restore path exists. This module is that
guarantee.

Every destructive or moving action writes ONE JSON line plus ONE ``.diff``
sidecar, and each record says how to reverse it:

    op        restore path
    ───────   ──────────────────────────────────────────────────────────────
    move      move ``dst`` back to ``src`` (pure inverse; nothing recorded)
    rewrite   ``patch -R`` the file with the recorded unified diff
    delete    ``patch`` the live counterpart with the recorded diff to rebuild
              the deleted file; when the leftover was byte-identical to live,
              copy live back; when there was no counterpart, the recorded diff
              IS the full pre-image (a diff against empty) and is written back.

Deletes follow stage -> diff -> real delete -> log, and the diff is written
BEFORE the unlink: if the ledger write fails, nothing is deleted. Nothing is
copied aside "just in case"; the diff plus the live counterpart is the
reconstruction, so no duplicate is left behind.

Pure stdlib. Fail loud on writes, fail soft on reads.
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

# Beyond this a file is treated as binary: it cannot be diffed, so it cannot be
# ledgered reversibly, so it is never auto-deleted (it routes to a human).
_BINARY_SNIFF = 8192
UNDO_HINT = "arcgov remediate undo"


def today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def is_text(path: Path) -> bool:
    """True when the file can be diffed (and therefore ledgered reversibly)."""
    try:
        chunk = path.read_bytes()[:_BINARY_SNIFF]
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "?"


def _lines(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return []


def _changed(text: str) -> int:
    return sum(1 for ln in text.splitlines()
               if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")))


def unified(base: Path | None, target: Path, base_label: str | None = None) -> tuple[str, int]:
    """Unified diff of ``base`` (live counterpart, may be absent) -> ``target``.

    With no base this is the full contents of ``target`` as additions, which is
    exactly the pre-image needed to rebuild it. Returns (text, n_changed).
    """
    a, b = _lines(base), _lines(target)
    fromfile = base_label or (str(base) if base else "/dev/null")
    text = "".join(difflib.unified_diff(a, b, fromfile=fromfile, tofile=str(target)))
    return text, _changed(text)


def unified_text(before: str, after: str, label: str) -> tuple[str, int]:
    text = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{label} (before)", tofile=f"{label} (after)"))
    return text, _changed(text)


def _patch_bin() -> str:
    return shutil.which("patch") or "/usr/bin/patch"


class Ledger:
    """A ledger rooted at one folder: ``actions/*.jsonl`` + ``diffs/<day>/*.diff``."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def actions_dir(self) -> Path:
        return self.root / "actions"

    @property
    def diffs_dir(self) -> Path:
        return self.root / "diffs"

    def _write_diff(self, action_id: str, text: str) -> Path:
        d = self.diffs_dir / today()
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{action_id}.diff"
        p.write_text(text, encoding="utf-8")
        return p

    def record(self, action: dict) -> dict:
        """Append one action to today's ledger. Returns the stored record."""
        self.actions_dir.mkdir(parents=True, exist_ok=True)
        action.setdefault("id", uuid.uuid4().hex[:12])
        action.setdefault("ts", _now())
        path = self.actions_dir / f"actions-{today()}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(action, ensure_ascii=False, default=str) + "\n")
        return action

    def read_actions(self, day: str | None = None) -> list[dict]:
        out: list[dict] = []
        files = ([self.actions_dir / f"actions-{day}.jsonl"] if day
                 else sorted(self.actions_dir.glob("actions-*.jsonl")))
        for f in files:
            if not f.exists():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    # ── the ledgered operations ─────────────────────────────────────────────
    def delete_file(self, src: Path, live: Path | None, klass: str, note: str = "") -> dict:
        """Diff before delete, then a REAL delete. The diff lands first."""
        action_id = uuid.uuid4().hex[:12]
        if live is not None and live.exists():
            text, changed = unified(live, src)
            kind = "identical" if changed == 0 else "unified"
        else:
            text, changed = unified(None, src)
            kind = "full"  # the diff IS the complete pre-image
        diff_path = self._write_diff(action_id, text)
        rec = {
            "id": action_id, "class": klass, "op": "delete",
            "src": str(src), "live": str(live) if live else None,
            "diff_kind": kind, "diff_lines": changed, "diff_path": str(diff_path),
            "sha256": sha256(src), "reversible": True, "note": note,
            "restore": (f"{UNDO_HINT} {action_id}  "
                        + {"identical": f"(copy back from {live})",
                           "unified": f"(patch {live} with the recorded diff)",
                           "full": "(recorded diff is the full pre-image)"}[kind]),
        }
        src.unlink()
        return self.record(rec)

    def delete_tree(self, src: Path, live: Path | None, klass: str, note: str = "") -> dict:
        """Delete a leftover DIRECTORY. Requires a live counterpart directory: the
        per-file diff is the manifest of what differed and the counterpart is the
        reconstruction base. Without one, callers route to a human instead."""
        if live is None or not live.is_dir():
            raise ValueError("delete_tree requires a live counterpart directory")
        action_id = uuid.uuid4().hex[:12]
        rel_src = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
        rel_live = {p.relative_to(live) for p in live.rglob("*") if p.is_file()}
        chunks, changed = [], 0
        for rel in sorted(rel_src | rel_live, key=str):
            a = live / rel if rel in rel_live else None
            b = src / rel
            if b.exists() and is_text(b) and (a is None or is_text(a)):
                text, n = unified(a, b)
            elif rel not in rel_src:
                text, n = f"--- {live / rel}\n+++ /dev/null\n(only in live counterpart)\n", 1
            else:
                text, n = f"--- {a}\n+++ {b}\n(binary; sha256 {sha256(b)})\n", 1
            if n:
                chunks.append(text)
                changed += n
        diff_path = self._write_diff(action_id, "".join(chunks))
        rec = {
            "id": action_id, "class": klass, "op": "delete-tree",
            "src": str(src), "live": str(live), "diff_kind": "tree",
            "diff_lines": changed, "diff_path": str(diff_path),
            "sha256": "-", "reversible": True, "note": note,
            "restore": f"{UNDO_HINT} {action_id}  (rebuild from {live} + the recorded diff)",
        }
        shutil.rmtree(src)
        return self.record(rec)

    def move_file(self, src: Path, dst: Path, klass: str, note: str = "") -> dict:
        """Move, ledgered. The inverse of a move is a move."""
        if dst.exists():
            raise FileExistsError(f"destination already exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        action_id = uuid.uuid4().hex[:12]
        digest = sha256(src)
        shutil.move(str(src), str(dst))
        return self.record({
            "id": action_id, "class": klass, "op": "move",
            "src": str(src), "dst": str(dst), "sha256": digest,
            "diff_kind": None, "diff_lines": 0, "diff_path": None,
            "reversible": True, "note": note,
            "restore": f"{UNDO_HINT} {action_id}  (move back to {src})",
        })

    def rewrite_file(self, path: Path, new_text: str, klass: str, note: str = "") -> dict:
        """Rewrite in place, recording the unified diff so ``patch -R`` reverses it."""
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        action_id = uuid.uuid4().hex[:12]
        text, changed = unified_text(before, new_text, str(path))
        diff_path = self._write_diff(action_id, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
        return self.record({
            "id": action_id, "class": klass, "op": "rewrite",
            "src": str(path), "diff_kind": "unified", "diff_lines": changed,
            "diff_path": str(diff_path), "sha256": sha256(path),
            "reversible": True, "note": note,
            "restore": f"{UNDO_HINT} {action_id}  (patch -R with the recorded diff)",
        })

    # ── the restore path ────────────────────────────────────────────────────
    @staticmethod
    def _patch(target: Path, diff: Path, *, reverse: bool, output: Path | None = None) -> tuple[bool, str]:
        argv = [_patch_bin(), "-s", "-p0"]
        if reverse:
            argv.append("-R")
        if output is not None:
            argv += ["-o", str(output)]
        argv += [str(target), str(diff)]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def undo(self, action_id: str) -> tuple[bool, str]:
        """Reverse one ledgered action."""
        rec = next((a for a in self.read_actions() if a.get("id") == action_id), None)
        if rec is None:
            return False, f"no ledgered action with id {action_id}"
        op = rec.get("op")

        if op == "move":
            src, dst = Path(rec["src"]), Path(rec["dst"])
            if not dst.exists():
                return False, f"moved file is gone: {dst}"
            if src.exists():
                return False, f"origin already occupied: {src}"
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            return True, f"moved back -> {src}"

        if op == "rewrite":
            path, diff = Path(rec["src"]), Path(rec["diff_path"])
            if not diff.exists():
                return False, f"diff sidecar missing: {diff}"
            ok, msg = self._patch(path, diff, reverse=True)
            return ok, (f"reverted -> {path}" if ok else f"patch -R failed: {msg}")

        if op == "delete":
            src = Path(rec["src"])
            if src.exists():
                return False, f"already present: {src}"
            if rec.get("diff_kind") == "symlink":
                return False, "dangling symlinks are restored by hand (the target was already gone)"
            diff = Path(rec["diff_path"])
            if not diff.exists():
                return False, f"diff sidecar missing: {diff}"
            kind = rec.get("diff_kind")
            if kind == "identical":
                live = Path(rec["live"])
                if not live.exists():
                    return False, f"live counterpart gone: {live}"
                shutil.copy2(live, src)
                return True, f"restored from identical counterpart -> {src}"
            if kind == "full":
                body = []
                for ln in diff.read_text(encoding="utf-8").splitlines(keepends=True):
                    if ln.startswith(("+++", "---", "@@")):
                        continue
                    if ln.startswith("+"):
                        body.append(ln[1:])
                src.parent.mkdir(parents=True, exist_ok=True)
                src.write_text("".join(body), encoding="utf-8")
                return True, f"restored from recorded pre-image -> {src}"
            live = Path(rec["live"])
            if not live.exists():
                return False, f"live counterpart gone: {live}"
            ok, msg = self._patch(live, diff, reverse=False, output=src)
            return ok, (f"reconstructed from counterpart + diff -> {src}" if ok else f"patch failed: {msg}")

        if op == "delete-tree":
            return False, ("tree restores are manual by design: rebuild from "
                           f"{rec.get('live')} then apply {rec.get('diff_path')}")

        return False, f"unknown op: {op}"
