"""Graph gate, isolation, TOCTOU and rollback tests on fresh synthetic databases."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from backend import banking as b, banking_evidence as e, banking_graph_narrative as narrative


class BankingEvidenceTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / "data"
        test_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=test_root)
        self.path = patch.object(b, "DB_PATH", Path(self.tmp.name) / "test.sqlite3")
        self.path.start()
        self.env = patch.dict("os.environ", {"BANKING_AUTH_MODE": "demo", "BANKING_LLM_ENABLED": "0"})
        self.env.start()
        self.token = b.create_session()["token"]

    def tearDown(self):
        self.env.stop()
        self.path.stop()
        self.tmp.cleanup()

    def scenario(self, name, token=None):
        return e.fixture(e.FixtureRequest(scenario=name, recipient_id="mom"), token or self.token)

    def prepare(self):
        return b.prepare_transfer(b.PrepareTransfer(recipient_id="mom", amount="80", note="验证"), self.token)

    def test_graph_ablation_same_amount_different_evidence(self):
        self.scenario("normal")
        self.assertEqual(self.prepare()["level"], "yellow")
        self.scenario("shared")
        self.assertEqual(self.prepare()["level"], "red")
        self.scenario("layered")
        with self.assertRaises(HTTPException) as error: self.prepare()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_session_isolation_and_exact_source_grounding(self):
        report = self.scenario("layered")
        source_ids = {edge["id"] for edge in report["graph"]["edges"]}
        self.assertTrue(all(set(f["source_ids"]) <= source_ids for f in report["findings"]))
        other = b.create_session()["token"]
        self.assertEqual(e.evidence("mom", other)["coverage"], "none")

    def test_evidence_change_after_confirmation_rejects_commit(self):
        self.scenario("normal")
        transfer = self.prepare()
        b.confirm_transfer(transfer["id"], b.ConfirmTransfer(digest=transfer["digest"]), self.token)
        self.scenario("normal")  # Even a benign new snapshot requires a new authorization.
        with self.assertRaises(HTTPException):
            b.execute_transfer(transfer["id"], b.ExecuteTransfer(idempotency_key="graph-change-test"), self.token)
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_expired_source_fails_closed(self):
        self.scenario("normal")
        with b._connect() as db: db.execute("UPDATE sandbox_graph_sources SET expires_at='2000-01-01'")
        self.assertEqual(e.evidence("mom", self.token)["decision"], "unavailable")
        with self.assertRaises(HTTPException): self.prepare()

    def test_expired_snapshot_preserves_observations_and_read_only_explanation(self):
        original = self.scenario("layered")
        with b._connect() as db: db.execute("UPDATE sandbox_graph_sources SET expires_at='2000-01-01'")
        expired = e.evidence("mom", self.token)
        self.assertEqual(expired["findings"], original["findings"])
        self.assertEqual(expired["source_status"], "expired")
        self.assertFalse(expired["authorization_usable"])
        explained = e.explain_evidence("mom", e.ExplainRequest(revision=expired["revision"], focus="authorization"), self.token)
        self.assertTrue(explained["historical_only"])
        self.assertIn("不构成当前授权", explained["paragraphs"][0]["text"])
        with self.assertRaises(HTTPException): self.prepare()
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")

    def test_read_does_not_refresh_expiry_and_regeneration_requires_reauthorization(self):
        original = self.scenario("normal")
        with b._connect() as db: db.execute("UPDATE sandbox_graph_sources SET expires_at='2000-01-01'")
        self.assertEqual(e.evidence("mom", self.token)["expires_at"], '2000-01-01')
        refreshed = self.scenario("normal")
        self.assertEqual(refreshed["source_status"], "fresh")
        self.assertNotEqual(refreshed["revision"], original["revision"])

    def test_explanation_cannot_silently_switch_snapshots(self):
        report = self.scenario("normal")
        def changed(snapshot, focus):
            self.scenario("shared")
            return {"paragraphs": []}
        with patch.object(narrative, "explain", side_effect=changed):
            with self.assertRaises(HTTPException) as error:
                e.explain_evidence("mom", e.ExplainRequest(revision=report["revision"]), self.token)
        self.assertEqual(error.exception.status_code, 409)

    def test_tampered_graph_fails_closed(self):
        self.scenario("layered")
        with b._connect() as db: db.execute("UPDATE sandbox_graph_sources SET graph_json=?", (json.dumps({"nodes": [], "edges": []}),))
        with self.assertRaises(HTTPException) as error: self.prepare()
        self.assertEqual(error.exception.status_code, 503)

    def test_failure_before_commit_rolls_back_money_and_receipt(self):
        transfer = self.prepare()
        b.confirm_transfer(transfer["id"], b.ConfirmTransfer(digest=transfer["digest"]), self.token)
        original = b._audit
        def fault(db, sid, tid, event, detail):
            if event == "COMMITTED": raise RuntimeError("injected storage failure")
            return original(db, sid, tid, event, detail)
        with patch.object(b, "_audit", fault):
            with self.assertRaises(RuntimeError):
                b.execute_transfer(transfer["id"], b.ExecuteTransfer(idempotency_key="rollback-fault-test"), self.token)
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")
        retry = b.execute_transfer(transfer["id"], b.ExecuteTransfer(idempotency_key="rollback-fault-test"), self.token)
        self.assertEqual(retry["status"], "COMMITTED")
        self.assertEqual(b.overview(self.token)["balance"], "2420.00")

    def test_no_graph_coverage_never_claims_fraud_clearance(self):
        report = e.evidence("mom", self.token)
        self.assertEqual(report["coverage"], "none")
        self.assertEqual(report["source_status"], "missing")
        self.assertFalse(report["authorization_usable"])
        self.assertEqual(report["graph"]["edges"], [])
        self.assertIn("不在本次判断", report["summary"])

    def test_receipt_retains_original_evidence_snapshot(self):
        self.scenario("normal")
        transfer = self.prepare()
        b.confirm_transfer(transfer["id"], b.ConfirmTransfer(digest=transfer["digest"]), self.token)
        b.execute_transfer(transfer["id"], b.ExecuteTransfer(idempotency_key="immutable-receipt-test"), self.token)
        self.scenario("layered")
        historical = b.get_transfer(transfer["id"], self.token)
        self.assertEqual(historical["risk"]["graph"]["revision"], transfer["risk"]["graph"]["revision"])
        self.assertEqual(historical["risk"]["graph"]["decision"], "standard")

    def test_model_duplicate_or_omitted_claims_fall_back(self):
        report = self.scenario("layered")
        invalid = narrative.NarrativePlan(ordered_codes=["SHARED_DEVICE", "SHARED_DEVICE"], focus="evidence")
        with patch.object(narrative, "model_plan", return_value=invalid):
            result = narrative.explain(report, "evidence")
        self.assertEqual(result["mode"], "deterministic_fallback")
        allowed = {edge["id"] for edge in report["graph"]["edges"]}
        self.assertTrue(all(set(p["source_ids"]) <= allowed for p in result["paragraphs"]))
        self.assertEqual(result["decision"], "hold")

    def test_model_failure_has_grounded_fallback_without_money_effect(self):
        report = self.scenario("layered")
        with patch.object(narrative, "model_plan", return_value=None):
            result = e.explain_evidence("mom", e.ExplainRequest(revision=report["revision"], focus="authorization"), self.token)
        self.assertEqual(result["mode"], "deterministic_fallback")
        self.assertEqual(result["paragraphs"][0]["text"], report["summary"])
        self.assertEqual(b.overview(self.token)["balance"], "2500.00")


if __name__ == "__main__": unittest.main()
