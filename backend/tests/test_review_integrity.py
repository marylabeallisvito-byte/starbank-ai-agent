"""Review IDs bind writes to server-owned investigation results."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main
from backend.agents.workflow import _parse_decision_json
from backend.agents.human_loop import FeedbackRecord, record_feedback
from backend.review_store import create_investigation, pending_investigation


class ReviewIntegrityTests(unittest.TestCase):
    def test_demo_decision_is_marked_and_has_no_uncalibrated_numbers(self) -> None:
        with patch.object(main, "create_investigation", return_value="a" * 32):
            events = list(main._stream_demo_events("调查资金回环"))
        decision = next(json.loads(event["data"]) for event in events if event["event"] == "decision")
        self.assertEqual(decision["analysis_source"], "demo")
        self.assertEqual(decision["investigation_id"], "a" * 32)
        self.assertIsNone(decision["risk_score"])
        self.assertIsNone(decision["confidence"])

    def test_unstructured_or_invalid_model_decision_never_becomes_an_approval(self) -> None:
        self.assertEqual(_parse_decision_json("没有结构化结果")["decision"], "NEEDS_EVIDENCE")
        self.assertEqual(_parse_decision_json('```json\n{"decision":"SAFE","frontend_highlight":{}}\n```')["decision"], "NEEDS_EVIDENCE")
        parsed = _parse_decision_json('```json\n{"decision":"FLAGGED","risk_score":99,"confidence":1,"frontend_highlight":{"suspicious_nodes":["N1"],"suspicious_edges_types":["TRANSFER"]}}\n```')
        self.assertIsNone(parsed["risk_score"])
        self.assertIsNone(parsed["confidence"])

    def test_review_uses_server_snapshot_and_cannot_be_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "investigations.sqlite3"
            investigation_id = create_investigation({
                "query": "原始调查", "ai_decision": "FLAGGED",
                "suspicious_nodes": ["KNOWN_NODE"], "suspicious_edge_ids": ["KNOWN_EDGE"],
                "source": "demo",
            }, path)
            captured: list[FeedbackRecord] = []

            def fake_record(record: FeedbackRecord, db=None):
                captured.append(record)
                return {"record": record.to_dict(), "audit": {"appended": True},
                        "label": {"appended": False}, "graph": {"written": False}}

            with patch.dict("os.environ", {"APP_ENV": "production", "REVIEW_API_TOKEN": "test-secret"}), \
                 patch.object(main, "pending_investigation", side_effect=lambda key: pending_investigation(key, path)), \
                 patch.object(main, "record_feedback", side_effect=fake_record):
                client = TestClient(main.app)
                body = {"investigation_id": investigation_id, "human_decision": "confirm",
                        "rationale": "已核对交易路径", "query": "伪造调查",
                        "ai_decision": "APPROVE", "suspicious_nodes": ["ATTACKER_NODE"]}
                response = client.post("/api/review", json=body, headers={"X-Review-Token": "test-secret"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(captured[0].query, "原始调查")
                self.assertEqual(captured[0].ai_decision, "FLAGGED")
                self.assertEqual(captured[0].suspicious_nodes, ["KNOWN_NODE"])
                replay = client.post("/api/review", json=body, headers={"X-Review-Token": "test-secret"})
                self.assertEqual(replay.status_code, 404)

    def test_demo_feedback_never_enters_labels_and_rejected_nodes_are_not_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.jsonl"
            labels = Path(directory) / "labels.jsonl"
            demo = FeedbackRecord("case", "FLAGGED", ["DEMO_NODE"], "confirm", source="demo")
            result = record_feedback(demo, audit_path=audit, labels_path=labels)
            self.assertFalse(result["label"]["appended"])
            self.assertFalse(labels.exists())
            rejected = FeedbackRecord("case", "FLAGGED", ["WRONG_NODE"], "reject", source="neo4j")
            record_feedback(rejected, audit_path=audit, labels_path=labels)
            label = json.loads(labels.read_text(encoding="utf-8").splitlines()[0])
            self.assertIsNone(label["expected_decision"])
            self.assertEqual(label["expected_nodes"], [])


if __name__ == "__main__":
    unittest.main()
