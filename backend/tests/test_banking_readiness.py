"""Diagnostics distinguish missing, expired and invalid without remote traffic."""
import json
import unittest
from unittest.mock import patch
from backend.tests import test_banking_evidence as fixtures
from backend import banking as b, banking_readiness as r


class ReadinessTests(unittest.TestCase):
    setUp = fixtures.BankingEvidenceTests.setUp
    tearDown = fixtures.BankingEvidenceTests.tearDown
    scenario = fixtures.BankingEvidenceTests.scenario
    def test_readiness_is_session_scoped_and_does_not_refresh(self):
        self.scenario("layered")
        with b._connect() as db: db.execute("UPDATE sandbox_graph_sources SET expires_at='2000-01-01'")
        with patch("httpx.Client", side_effect=AssertionError("diagnostics must remain local")):
            result = r.readiness(self.token)
        self.assertEqual(next(g for g in result["graphs"] if g["recipient_id"] == 'mom')["status"], "expired")
        self.assertEqual(result["plaid"]["status"], "disconnected")
        other = b.create_session()["token"]
        self.assertTrue(all(g["status"] == "missing" for g in r.readiness(other)["graphs"]))

    def test_corrupt_graph_is_identified_without_hiding_other_sources(self):
        self.scenario("layered")
        with b._connect() as db: db.execute("UPDATE sandbox_graph_sources SET graph_json=?", (json.dumps({"nodes": [], "edges": []}),))
        result = r.readiness(self.token)
        self.assertEqual(next(g for g in result["graphs"] if g["recipient_id"] == 'mom')["status"], "invalid")
        self.assertIn("fx", result)

    def test_runtime_change_is_explicit(self):
        with patch.object(r, "LOADED_SOURCE", "older-build"):
            result = r.readiness(self.token)
        self.assertEqual(result["runtime"]["status"], "restart_required")
