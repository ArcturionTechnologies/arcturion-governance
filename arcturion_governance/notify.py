"""Pluggable notifier.

Every tool that wants to tell a person something calls ``get_notifier(cfg).send(...)``.
Which channel that reaches is configuration, not code:

  {"kind": "stdout"}                                   print it (default)
  {"kind": "none"}                                     drop it
  {"kind": "file", "path": "$ARC_ROOT/notices.log"}    append one JSON line per notice
  {"kind": "webhook", "url_env": "GOVERNANCE_WEBHOOK_URL",
   "format": "json" | "slack" | "text", "timeout": 10}  POST it

The webhook URL is read from the named environment variable at send time and
never stored in config or logs. ``GOVERNANCE_NOTIFY`` (stdout/none/file/webhook)
overrides the configured kind, which is handy for dry runs.

Notifiers never raise: a failed send returns ``False`` and the caller decides
what to do (usually: write a fallback file so nothing is lost).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import config as config_mod


class Notifier(Protocol):
    def send(self, text: str, *, title: str = "", priority: str = "normal") -> bool: ...


class StdoutNotifier:
    def __init__(self, stream=None):
        self.stream = stream

    def send(self, text: str, *, title: str = "", priority: str = "normal") -> bool:
        stream = self.stream or sys.stdout
        head = f"[{priority}] {title}\n" if title else ""
        stream.write(head + text.rstrip() + "\n")
        return True


class NullNotifier:
    def send(self, text: str, *, title: str = "", priority: str = "normal") -> bool:
        return True


class FileNotifier:
    def __init__(self, path: Path):
        self.path = path

    def send(self, text: str, *, title: str = "", priority: str = "normal") -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "title": title, "priority": priority, "text": text}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False


class WebhookNotifier:
    def __init__(self, url_env: str, fmt: str = "json", timeout: float = 10.0, opener=None):
        self.url_env = url_env
        self.fmt = fmt
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def payload(self, text: str, title: str, priority: str) -> tuple[bytes, str]:
        if self.fmt == "text":
            body = (f"{title}\n\n{text}" if title else text).encode("utf-8")
            return body, "text/plain; charset=utf-8"
        if self.fmt == "slack":
            msg = f"*{title}*\n{text}" if title else text
            return json.dumps({"text": msg}).encode("utf-8"), "application/json"
        return (json.dumps({"title": title, "text": text, "priority": priority}).encode("utf-8"),
                "application/json")

    def send(self, text: str, *, title: str = "", priority: str = "normal") -> bool:
        url = os.environ.get(self.url_env, "")
        if not url.startswith(("https://", "http://")):
            return False
        body, ctype = self.payload(text, title, priority)
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                return 200 <= getattr(resp, "status", 200) < 300
        except Exception:
            return False


def get_notifier(cfg: config_mod.Config | None = None) -> Notifier:
    cfg = cfg or config_mod.load()
    spec = dict(cfg.section("notify"))
    override = os.environ.get("GOVERNANCE_NOTIFY")
    if override:
        spec["kind"] = override
    kind = str(spec.get("kind") or "stdout").lower()
    if kind == "none":
        return NullNotifier()
    if kind == "file":
        path = config_mod.expand(spec.get("path"), cfg.base) or (cfg.state_dir / "notices.log")
        return FileNotifier(path)
    if kind == "webhook":
        return WebhookNotifier(str(spec.get("url_env") or "GOVERNANCE_WEBHOOK_URL"),
                               str(spec.get("format") or "json"), float(spec.get("timeout") or 10))
    return StdoutNotifier()
