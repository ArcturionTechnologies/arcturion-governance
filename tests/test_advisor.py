"""Second-opinion advisor: validation, privacy, caching, receipts. No network."""
import io
import json
import os
import stat
import unittest
from contextlib import redirect_stdout

from arcturion_governance import advisor as A
from arcturion_governance import config as config_mod
from tests._helpers import Sandbox

KEY = "placeholder" + "x" * 20  # synthetic, built at runtime


def request(**over):
    req = {
        "agent": "builder", "context": "business", "decision_id": "queue-review-001",
        "evidence": {"goal": "Restore dependable service before cosmetic work",
                     "options": {"repair": "A health check fails; recovery is reversible",
                                 "cosmetic": "Rename dashboard labels; no impact"}},
        "questions": {
            "priority": {"type": "choice", "instructions": "Which option first?",
                         "criteria": {"repair": "Investigate the failing service", "cosmetic": "Rename labels"}},
            "urgency": {"type": "score", "instructions": "How urgent is the repair?",
                        "criteria": ["Routine", "Degraded but usable", "Current failure"]},
            "gap": {"type": "noul", "instructions": "Is important evidence missing?"},
        },
    }
    req.update(over)
    return req


def good_response(model="judge-1"):
    return {"model": model, "usage": {"input_tokens": 1200, "output_tokens": 40},
            "extra_field": "dropped",
            "answers": {
                "priority": {"type": "choice", "choice": "repair",
                             "probabilities": {"repair": 0.9, "cosmetic": 0.1}, "confidence": 0.8},
                "urgency": {"type": "score", "score": 1.8, "probabilities": {"0": 0.05, "1": 0.1, "2": 0.85},
                            "confidence": 0.7, "legend": {"0": "a", "1": "b", "2": "c"}},
                "gap": {"type": "noul", "noul": 0.2},
            }}


class FakeTransport:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or good_response()

    def __call__(self, payload, secret):
        self.calls.append((payload, secret))
        return self.response


class _A(Sandbox):
    extra = {"advisor": {"enabled": True, "endpoint": "https://judgment.example.invalid/v1/evaluate",
                         "model": "judge-1", "cache_seconds": 300, "input_cost_per_million": 0.05}}

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport()
        self.now = [1_000_000.0]

    def advisor(self, transport=None, secret=KEY):
        return A.Advisor(config_mod.load(), transport=transport or self.transport,
                         secret_getter=lambda: secret, clock=lambda: self.now[0])


class TestValidation(_A):
    def evaluate(self, req):
        return self.advisor().evaluate(req)

    def test_unknown_agent_rejected_before_any_call(self):
        r = self.evaluate(request(agent="stranger"))
        self.assertEqual((r["status"], r["error"], r["receipt_persisted"]), ("error", "unknown_agent", False))
        self.assertEqual(self.transport.calls, [])

    def test_missing_evidence(self):
        self.assertEqual(self.evaluate(request(evidence={"facts": []}))["error"], "missing_evidence")

    def test_credential_shaped_text_is_refused(self):
        r = self.evaluate(request(evidence={"facts": ["api_key = abc123secretvalue"]}))
        self.assertEqual(r["error"], "suspicious_credentials")
        r = self.evaluate(request(evidence={"password": "x"}))
        self.assertEqual(r["error"], "suspicious_credentials")
        self.assertEqual(self.transport.calls, [])

    def test_bad_question_shapes(self):
        bad_choice = request(questions={"q": {"type": "choice", "instructions": "x", "criteria": {"only": "one"}}})
        self.assertEqual(self.evaluate(bad_choice)["error"], "invalid_choice_criteria")
        bad_type = request(questions={"q": {"type": "essay", "instructions": "x"}})
        self.assertEqual(self.evaluate(bad_type)["error"], "invalid_question_type")
        bad_id = request(decision_id="has spaces")
        self.assertEqual(self.evaluate(bad_id)["error"], "invalid_decision_id")

    def test_context_must_be_configured(self):
        self.assertEqual(self.evaluate(request(context="family"))["error"], "invalid_context")


class TestEvaluate(_A):
    def test_success_receipt_and_privacy(self):
        r = self.advisor().evaluate(request())
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(r["answers"]["priority"]["choice"], "repair")
        self.assertEqual(r["answers"]["urgency"]["level_count"], 3)
        self.assertNotIn("extra_field", json.dumps(r))
        self.assertTrue(r["advisory_only"])
        self.assertAlmostEqual(r["estimated_cost_usd"], 1200 * 0.05 / 1_000_000)
        payload, secret = self.transport.calls[0]
        self.assertEqual(secret, KEY)
        self.assertEqual(payload["model"], "judge-1")
        receipt = self.cfg.state_dir / "advisor" / "business" / "BUILDER" / "receipts" / f"{r['receipt_id']}.json"
        self.assertTrue(receipt.exists())
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(receipt.parent.stat().st_mode), 0o700)
        stored = receipt.read_text()
        self.assertNotIn("health check fails", stored, "evidence is hashed, never stored")
        self.assertNotIn(KEY, stored)

    def test_cache_hit_makes_no_second_call(self):
        adv = self.advisor()
        first = adv.evaluate(request())
        self.now[0] += 10
        second = adv.evaluate(request())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["source_receipt_id"], first["receipt_id"])
        self.assertEqual(second["estimated_cost_usd"], 0.0)

    def test_cache_expires(self):
        adv = self.advisor()
        adv.evaluate(request())
        self.now[0] += 301
        adv.evaluate(request())
        self.assertEqual(len(self.transport.calls), 2)

    def test_disabled_makes_no_call(self):
        raw = json.loads(self.cfg_path.read_text())
        raw["advisor"]["enabled"] = False
        self.cfg_path.write_text(json.dumps(raw))
        r = self.advisor().evaluate(request())
        self.assertEqual((r["status"], r["error"]), ("unavailable", "disabled"))
        self.assertEqual(self.transport.calls, [])

    def test_missing_key_is_unavailable(self):
        adv = A.Advisor(config_mod.load(), transport=self.transport, clock=lambda: self.now[0])
        r = adv.evaluate(request())
        self.assertEqual((r["status"], r["error"]), ("unavailable", "credential_unavailable"))
        self.assertEqual(self.transport.calls, [])

    def test_key_echoed_in_evidence_is_refused(self):
        r = self.advisor(secret="plainlongtokenvalue1234").evaluate(
            request(evidence={"facts": ["value plainlongtokenvalue1234 appears here"]}))
        self.assertEqual(r["error"], "suspicious_credentials")
        self.assertEqual(self.transport.calls, [])

    def test_endpoint_must_be_https(self):
        os.environ["ADVISOR_ENDPOINT"] = "http://insecure.example.invalid/x"
        r = self.advisor().evaluate(request())
        self.assertEqual(r["error"], "endpoint_not_configured")

    def test_model_mismatch_and_bad_answers_rejected(self):
        r = self.advisor(FakeTransport(good_response(model="other"))).evaluate(request())
        self.assertEqual(r["error"], "invalid_response_model")
        resp = good_response()
        resp["answers"]["priority"]["choice"] = "cosmetic"  # not the most probable
        self.assertEqual(self.advisor(FakeTransport(resp)).evaluate(request())["error"], "invalid_choice")
        resp = good_response()
        resp["answers"]["urgency"]["score"] = 0.2  # inconsistent with probabilities
        self.assertEqual(self.advisor(FakeTransport(resp)).evaluate(request())["error"], "inconsistent_score")
        resp = good_response()
        resp["answers"]["gap"]["confidence"] = 0.9  # noul carries no confidence
        self.assertEqual(self.advisor(FakeTransport(resp)).evaluate(request())["error"], "invalid_noul")
        resp = good_response()
        del resp["answers"]["gap"]
        self.assertEqual(self.advisor(FakeTransport(resp)).evaluate(request())["error"], "missing_or_extra_answers")

    def test_transport_failure_is_unavailable_not_raised(self):
        def boom(payload, secret):
            raise A.AdvisorError("timeout", "unavailable")
        r = self.advisor(boom).evaluate(request())
        self.assertEqual((r["status"], r["error"], r["provider_called"]), ("unavailable", "timeout", True))


class TestRecordChoice(_A):
    def test_record_choice_needs_a_matching_ok_receipt(self):
        adv = self.advisor()
        r = adv.evaluate(request())
        ok = adv.record_choice(r["receipt_id"], "repair first; labels next week", "builder", "business")
        self.assertEqual(ok["status"], "ok")
        self.assertFalse(ok["provider_called"])
        wrong_scope = adv.record_choice(r["receipt_id"], "x", "steward", "business")
        self.assertEqual(wrong_scope["status"], "error")
        self.assertEqual(adv.record_choice("not-a-uuid", "x", "builder", "business")["error"], "invalid_receipt_scope")


class TestStatusCli(_A):
    def test_status_reports_without_calling_anything(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = A.main(["--status"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(out["endpoint_host"], "judgment.example.invalid")
        self.assertFalse(out["api_key_present"])
        self.assertFalse(out["provider_checked"])
        self.assertIn("BUILDER", out["allowed_agents"])

    def test_cli_rejects_bad_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = A.main(["--request-json", "{nope"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(buf.getvalue())["error"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
