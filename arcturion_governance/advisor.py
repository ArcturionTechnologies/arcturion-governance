"""Second-opinion advisor: a bounded, advisory client for a typed-judgment API.

Before a substantive decision (which of two fixes first, how urgent is this,
is evidence missing) an agent can ask a second model for a structured opinion.
This client keeps that consultation honest and cheap:

  * the request is validated and screened for credential-shaped text before
    anything leaves the machine; evidence must be a small sanitized summary;
  * exactly one HTTPS POST to the configured endpoint, no retries, no
    redirects, a 15 s deadline and a 1 MiB response cap;
  * the response is validated question by question (choice / score / noul),
    and anything unexpected is dropped or rejected;
  * every call leaves a private receipt (0600 files in 0700 folders) holding
    hashes, answers and usage, never the evidence itself;
  * identical requests within ``cache_seconds`` (max 300) reuse the answer
    with no new charge;
  * the result is ADVICE. It never authorizes an action, and an unavailable
    advisor is not a veto.

Question types:
  choice  pick one of 2-255 labelled options      -> choice, probabilities, confidence
  score   a position on 2-10 ordered levels         -> fractional score, probabilities, confidence
  noul    probability that a statement is true      -> noul (a probability, not a confidence)

Configuration (``"advisor"`` section of governance.json):

  {"enabled": true, "endpoint": "https://judgment.example.com/v1/evaluate",
   "model": "judge-1", "api_key_env": "ADVISOR_API_KEY", "cache_seconds": 300,
   "contexts": ["business", "personal"], "allowed_agents": ["BUILDER", "STEWARD"],
   "input_cost_per_million": null}

``ADVISOR_ENDPOINT`` overrides the endpoint. The API key is read only from the
named environment variable. ``allowed_agents`` defaults to the roster.

    arcgov advisor --status
    arcgov advisor --request-json '{"agent": "builder", "context": "business", ...}'
    arcgov advisor --record-choice <receipt_id> --agent builder --context business --choice "repair first"
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import io
import json
import math
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import config as config_mod

TIMEOUT = 15
MAX_BYTES = 128_000
MAX_RESPONSE = 1_048_576
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SECRET = re.compile(
    r"(?i)(?:bearer\s+\S+|apikey_[a-z0-9_-]{16,}|(?:sk|ts)[_-][a-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?:api[_ -]?key|password|secret|access[_ -]?token)\s*[:=]\s*\S+)")
SENSITIVE_KEYS = re.compile(
    r"(?i)^(?:password|credential|secret|api[_-]?key|authorization|access[_-]?token|private[_-]?key)$")


class AdvisorError(Exception):
    """Only fixed, safe codes are exposed; never provider error text."""

    def __init__(self, code: str, status: str = "error"):
        super().__init__(code)
        self.code, self.status = code, status


def encode(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise AdvisorError("invalid_json") from None


def digest(value) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def number(value, low, high) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def rounded_score_bounds(probabilities: dict) -> tuple[float, float]:
    """Providers often round probabilities and the score to two decimals. Bound
    the possible mean while keeping total probability at one."""
    levels = sorted((int(k), p) for k, p in probabilities.items())
    lower = {level: max(0.0, p - 0.005) for level, p in levels}
    upper = {level: min(1.0, p + 0.005) for level, p in levels}
    if sum(lower.values()) > 1.0 + 1e-9 or sum(upper.values()) < 1.0 - 1e-9:
        raise AdvisorError("invalid_probabilities")
    bounds = []
    for order in (levels, list(reversed(levels))):
        mass = dict(lower)
        remaining = max(0.0, 1.0 - sum(mass.values()))
        for level, _ in order:
            addition = min(remaining, upper[level] - mass[level])
            mass[level] += addition
            remaining -= addition
        bounds.append(sum(level * p for level, p in mass.items()))
    return bounds[0] - 0.005 - 1e-9, bounds[1] + 0.005 + 1e-9


def safe_content(value, depth: int = 0) -> None:
    if depth > 12:
        raise AdvisorError("input_too_deep")
    if isinstance(value, str):
        if SECRET.search(value) or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise AdvisorError("suspicious_credentials")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE_KEYS.match(key):
                raise AdvisorError("suspicious_credentials")
            safe_content(key, depth + 1)
            safe_content(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            safe_content(item, depth + 1)
    elif value is not None and type(value) not in (bool, int, float):
        raise AdvisorError("invalid_json")
    encode(value)


def _description(value) -> bool:
    return isinstance(value, (str, dict, list)) and bool(value)


class Settings:
    def __init__(self, cfg: config_mod.Config):
        sec = cfg.section("advisor")
        self.enabled = bool(sec.get("enabled", False))
        self.endpoint = os.environ.get("ADVISOR_ENDPOINT") or str(sec.get("endpoint") or "")
        self.model = str(sec.get("model") or "")
        self.api_key_env = str(sec.get("api_key_env") or "ADVISOR_API_KEY")
        self.cache_seconds = int(sec.get("cache_seconds", 300))
        self.contexts = tuple(sec.get("contexts") or ("business", "personal"))
        allowed = sec.get("allowed_agents")
        self.allowed_agents = tuple(a.upper() for a in allowed) if allowed else tuple(cfg.agent_names())
        rate = sec.get("input_cost_per_million")
        self.input_cost_per_million = float(rate) if rate is not None else None
        self.state_root = cfg.state_dir / "advisor"

    def check(self) -> None:
        if not 0 <= self.cache_seconds <= 300:
            raise AdvisorError("invalid_config")
        if not self.model or not ID.fullmatch(self.model):
            raise AdvisorError("invalid_config")
        parts = urlsplit(self.endpoint)
        if parts.scheme != "https" or not parts.hostname:
            raise AdvisorError("endpoint_not_configured", "unavailable")


def validate_request(request, settings: Settings) -> dict:
    if not isinstance(request, dict) or set(request) != {"agent", "context", "decision_id", "evidence", "questions"}:
        raise AdvisorError("invalid_request")
    agent = request["agent"]
    if not isinstance(agent, str) or agent.upper() not in settings.allowed_agents:
        raise AdvisorError("unknown_agent")
    if request["context"] not in settings.contexts:
        raise AdvisorError("invalid_context")
    if not isinstance(request["decision_id"], str) or not ID.fullmatch(request["decision_id"]):
        raise AdvisorError("invalid_decision_id")
    evidence = request["evidence"]
    if not isinstance(evidence, dict) or not any(v not in (None, "", [], {}) for v in evidence.values()):
        raise AdvisorError("missing_evidence")
    questions = request["questions"]
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 32:
        raise AdvisorError("invalid_questions")
    for key, q in questions.items():
        if not isinstance(key, str) or not ID.fullmatch(key) or not isinstance(q, dict):
            raise AdvisorError("invalid_question")
        if set(q) - {"type", "instructions", "criteria"} or not _description(q.get("instructions")):
            raise AdvisorError("invalid_question")
        kind, criteria = q.get("type"), q.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 255:
                raise AdvisorError("invalid_choice_criteria")
            if any(not isinstance(k, str) or not ID.fullmatch(k) or (v is not None and not _description(v))
                   for k, v in criteria.items()):
                raise AdvisorError("invalid_choice_criteria")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or not all(_description(v) for v in criteria):
                raise AdvisorError("invalid_score_criteria")
        elif kind == "noul":
            if criteria is not None and (not isinstance(criteria, dict) or set(criteria) - {"true", "false"}
                                         or not all(_description(v) for v in criteria.values())):
                raise AdvisorError("invalid_noul_criteria")
        else:
            raise AdvisorError("invalid_question_type")
    safe_content(request)
    normalized = dict(request, agent=agent.upper())
    if len(encode(normalized)) > MAX_BYTES:
        raise AdvisorError("input_too_large")
    return normalized


def validate_response(response, questions: dict, model: str) -> dict:
    if not isinstance(response, dict) or response.get("model") != model:
        raise AdvisorError("invalid_response_model")
    raw = response.get("answers")
    if not isinstance(raw, dict) or set(raw) != set(questions):
        raise AdvisorError("missing_or_extra_answers")
    answers, uncertainty = {}, {}
    for key, q in questions.items():
        answer, kind = raw[key], q["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise AdvisorError("invalid_answer_type")
        clean = {"type": kind}
        if kind == "noul":
            if not number(answer.get("noul"), 0, 1) or "confidence" in answer:
                raise AdvisorError("invalid_noul")
            clean["noul"] = answer["noul"]
            uncertainty[key] = {"yes_probability": clean["noul"], "interpretation": "probability_not_confidence"}
        else:
            probs = answer.get("probabilities")
            expected = set(q["criteria"]) if kind == "choice" else {str(i) for i in range(len(q["criteria"]))}
            if not isinstance(probs, dict) or set(probs) != expected or not all(number(p, 0, 1) for p in probs.values()):
                raise AdvisorError("invalid_probabilities")
            if (sum(max(0.0, p - 0.005) for p in probs.values()) > 1.0 + 1e-9
                    or sum(min(1.0, p + 0.005) for p in probs.values()) < 1.0 - 1e-9):
                raise AdvisorError("invalid_probabilities")
            if not number(answer.get("confidence"), 0, 1):
                raise AdvisorError("invalid_confidence")
            clean.update(probabilities=probs, confidence=answer["confidence"])
            uncertainty[key] = {"confidence": answer["confidence"], "probabilities": probs}
            if kind == "choice":
                choice = answer.get("choice")
                if not isinstance(choice, str) or choice not in expected or probs[choice] + 0.001 < max(probs.values()):
                    raise AdvisorError("invalid_choice")
                clean["choice"] = choice
            else:
                if not number(answer.get("score"), 0, len(expected) - 1):
                    raise AdvisorError("invalid_score")
                lo, hi = rounded_score_bounds(probs)
                if not lo <= answer["score"] <= hi:
                    raise AdvisorError("inconsistent_score")
                legend = answer.get("legend")
                if not isinstance(legend, dict) or set(legend) != expected:
                    raise AdvisorError("invalid_score_legend")
                clean.update(score=answer["score"], level_count=len(expected))
        answers[key] = clean
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                          for k in ("input_tokens", "output_tokens")):
        raise AdvisorError("invalid_usage")
    return {"model": model, "answers": answers, "uncertainty": uncertainty,
            "usage": {k: usage[k] for k in ("input_tokens", "output_tokens")}}


def https_post(endpoint: str, payload: dict, secret: str) -> dict:
    """One HTTPS attempt to a fixed host and path; no proxy, redirect or retry."""
    parts = urlsplit(endpoint)
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=TIMEOUT)
    deadline = time.monotonic() + TIMEOUT
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        connection.request("POST", path, body=encode(payload),
                           headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            code = {401: "authentication_failed", 403: "authentication_failed",
                    429: "rate_limited"}.get(response.status, "provider_unavailable")
            raise AdvisorError(code, "unavailable")
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdvisorError("timeout", "unavailable")
            if connection.sock:
                connection.sock.settimeout(remaining)
            part = response.read1(min(65536, MAX_RESPONSE + 1 - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
            if size > MAX_RESPONSE:
                raise AdvisorError("response_too_large")
        try:
            return json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            raise AdvisorError("malformed_response") from None
    except (TimeoutError, socket.timeout):
        raise AdvisorError("timeout", "unavailable") from None
    except (OSError, http.client.HTTPException):
        raise AdvisorError("transport_unavailable", "unavailable") from None
    finally:
        connection.close()


def env_secret(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise AdvisorError("credential_unavailable", "unavailable")
    return value


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise AdvisorError("unsafe_state_path")
    if not path.exists():
        private_dir(path.parent)
        path.mkdir(mode=0o700)
    if not path.is_dir():
        raise AdvisorError("unsafe_state_path")


def write_private(path: Path, value) -> None:
    private_dir(path.parent)
    if path.is_symlink():
        raise AdvisorError("unsafe_state_path")
    fd, tmp = tempfile.mkstemp(prefix=".advisor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encode(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Advisor:
    def __init__(self, cfg: config_mod.Config | None = None, *, transport=None, secret_getter=None,
                 clock=time.time):
        self.cfg = cfg or config_mod.load()
        self.settings = Settings(self.cfg)
        self.transport = transport or (lambda payload, secret: https_post(self.settings.endpoint, payload, secret))
        self.secret_getter = secret_getter or (lambda: env_secret(self.settings.api_key_env))
        self.clock = clock

    def _scope(self, agent, context) -> Path | None:
        s = self.settings
        if isinstance(agent, str) and agent.upper() in s.allowed_agents and context in s.contexts:
            return s.state_root / context / agent.upper()
        return None

    def _prepare(self, scope: Path) -> None:
        for path in (self.settings.state_root, scope.parent, scope):
            private_dir(path)
            os.chmod(path, 0o700)

    def status(self) -> dict:
        s = self.settings
        try:
            s.check()
            return {"status": "ok", "enabled": s.enabled, "model": s.model, "endpoint_host": urlsplit(s.endpoint).hostname,
                    "cache_seconds": s.cache_seconds, "allowed_agents": list(s.allowed_agents),
                    "api_key_present": bool(os.environ.get(s.api_key_env)), "provider_checked": False}
        except AdvisorError as exc:
            return {"status": exc.status, "error": exc.code, "enabled": s.enabled}

    def evaluate(self, request) -> dict:
        s = self.settings
        receipt = {"schema_version": 1, "receipt_id": str(uuid.uuid4()), "created_at": self.clock(),
                   "status": "error", "model": s.model, "provider_called": False,
                   "estimated_cost_usd": 0.0, "cost_status": "no_provider_call"}
        scope = self._scope(request.get("agent") if isinstance(request, dict) else None,
                            request.get("context") if isinstance(request, dict) else None)
        try:
            normalized = validate_request(request, s)
            receipt.update(agent=normalized["agent"], context=normalized["context"],
                           decision_id=normalized["decision_id"], request_hash=digest(normalized),
                           evidence_hash=digest(normalized["evidence"]))
            if not s.enabled:
                raise AdvisorError("disabled", "unavailable")
            s.check()
            self._prepare(scope)
            private_dir(scope / "receipts")
            private_dir(scope / "cache")
            cache_key = digest({"request": normalized, "model": s.model, "endpoint": s.endpoint})
            cache_path = scope / "cache" / (cache_key + ".json")
            cached = None
            if s.cache_seconds and cache_path.exists() and not cache_path.is_symlink():
                try:
                    entry = json.loads(cache_path.read_text())
                    if entry["cache_key"] == cache_key and 0 <= self.clock() - entry["created_at"] < s.cache_seconds:
                        raw = json.loads(json.dumps(entry["result"]))
                        for a in raw["answers"].values():
                            if a["type"] == "score":
                                if type(a.get("level_count")) is not int or not 2 <= a["level_count"] <= 10:
                                    raise ValueError("bad cached level count")
                                a["legend"] = {str(i): "" for i in range(a["level_count"])}
                        cached = validate_response(raw, normalized["questions"], s.model)
                        receipt["source_receipt_id"] = entry["receipt_id"]
                except (ValueError, KeyError, TypeError, AttributeError, AdvisorError):
                    cached = None
            if cached is not None:
                result = cached
                receipt.update(cache_hit=True, estimated_cost_usd=0.0, cost_status="cache_hit_no_new_charge")
            else:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    secret = self.secret_getter()
                if (not isinstance(secret, str) or not 16 <= len(secret) <= 512
                        or any(c.isspace() for c in secret) or not secret.isascii()):
                    raise AdvisorError("credential_unavailable", "unavailable")
                if secret in encode(normalized).decode():
                    raise AdvisorError("suspicious_credentials")
                payload = {"model": s.model, "state": normalized["evidence"], "questions": normalized["questions"]}
                receipt.update(provider_called=True, estimated_cost_usd=None, cost_status="unknown_provider_attempt")
                raw_result = self.transport(payload, secret)
                usage = raw_result.get("usage") if isinstance(raw_result, dict) else None
                if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                                   for k in ("input_tokens", "output_tokens")):
                    receipt["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens")}
                    if s.input_cost_per_million is not None:
                        receipt.update(estimated_cost_usd=usage["input_tokens"] * s.input_cost_per_million / 1_000_000,
                                       cost_status="estimated_from_reported_usage")
                    else:
                        receipt["cost_status"] = "no_rate_configured"
                result = validate_response(raw_result, normalized["questions"], s.model)
                receipt["cache_hit"] = False
            receipt.update(result, status="ok", advisory_only=True)
            write_private(scope / "receipts" / (receipt["receipt_id"] + ".json"), receipt)
            if cached is None and s.cache_seconds:
                write_private(cache_path, {"created_at": self.clock(), "cache_key": cache_key,
                                           "receipt_id": receipt["receipt_id"], "result": result})
            return receipt
        except AdvisorError as exc:
            receipt.update(status=exc.status, error=exc.code)
        except Exception:  # noqa: BLE001
            receipt.update(status="unavailable", error="local_unavailable")
        if scope:
            receipt.update(agent=scope.name, context=scope.parent.name)
            try:
                self._prepare(scope)
                write_private(scope / "receipts" / (receipt["receipt_id"] + ".json"), receipt)
            except Exception:  # noqa: BLE001
                receipt["receipt_persisted"] = False
        else:
            receipt["receipt_persisted"] = False
        return receipt

    def record_choice(self, receipt_id: str, choice: str, agent: str, context: str) -> dict:
        """Write the agent's final choice next to the receipt. Never calls the provider."""
        scope = self._scope(agent, context)
        try:
            try:
                canonical = str(uuid.UUID(str(receipt_id)))
            except ValueError:
                canonical = None
            if not scope or canonical != receipt_id:
                raise AdvisorError("invalid_receipt_scope")
            if not isinstance(choice, str) or not choice.strip() or len(choice) > 1000:
                raise AdvisorError("invalid_final_choice")
            safe_content(choice)
            self._prepare(scope)
            source = scope / "receipts" / (receipt_id + ".json")
            if source.is_symlink():
                raise AdvisorError("unsafe_state_path")
            receipt = json.loads(source.read_text())
            if (receipt.get("receipt_id") != receipt_id or receipt.get("agent") != agent.upper()
                    or receipt.get("context") != context or receipt.get("status") != "ok"):
                raise AdvisorError("invalid_receipt_scope")
            result = {"schema_version": 1, "receipt_id": receipt_id, "agent": agent.upper(), "context": context,
                      "final_choice": choice, "recorded_at": self.clock(), "provider_called": False}
            write_private(scope / "choices" / (receipt_id + ".json"), result)
            return dict(result, status="ok")
        except AdvisorError as exc:
            return {"status": exc.status, "error": exc.code}
        except (OSError, ValueError):
            return {"status": "error", "error": "receipt_unavailable"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="arcgov advisor", description="Bounded, advisory second opinion.")
    parser.add_argument("--config")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--input", type=Path, help="request JSON file")
    inputs.add_argument("--request-json", help="short sanitized request JSON; never put secrets in arguments")
    parser.add_argument("--status", action="store_true", help="read local settings; no provider call")
    parser.add_argument("--record-choice", metavar="RECEIPT_ID")
    parser.add_argument("--choice")
    parser.add_argument("--agent")
    parser.add_argument("--context")
    args = parser.parse_args(argv)
    advisor = Advisor(config_mod.load(args.config))
    if args.status:
        result = advisor.status()
    elif args.record_choice:
        result = advisor.record_choice(args.record_choice, args.choice, args.agent or "", args.context or "")
    else:
        try:
            if args.request_json is not None:
                raw = args.request_json.encode("utf-8")
            elif args.input:
                with args.input.open("rb") as fh:
                    raw = fh.read(MAX_BYTES + 1)
            else:
                raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise AdvisorError("input_too_large")
            result = advisor.evaluate(json.loads(raw))
        except AdvisorError as exc:
            result = {"status": exc.status, "error": exc.code}
        except ValueError:
            result = {"status": "error", "error": "invalid_input"}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
