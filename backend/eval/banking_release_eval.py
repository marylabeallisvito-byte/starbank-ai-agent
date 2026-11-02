"""Run frozen synthetic cases and actual regression tests; retain failed rows.

python -m backend.eval.banking_release_eval [--live-model]
Never edits the case set or touches the user's sandbox database.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import tempfile
import time
import unittest
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from fastapi import HTTPException
from backend import banking as bank, banking_evidence as evidence

ROOT = Path(__file__).resolve().parents[2]
CASES = Path(__file__).with_name("banking_holdout_v1.json")
REPORT = ROOT / "backend/eval/reports/banking_release_v1.json"


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def addSuccess(self, test):
        self.rows.append({"id": test.id(), "pass": True, "latency_ms": round((time.perf_counter() - self.started) * 1000, 2)})
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.rows.append({"id": test.id(), "pass": False, "error": self._exc_info_to_string(err, test)})
        super().addFailure(test, err)

    def addError(self, test, err):
        self.rows.append({"id": test.id(), "pass": False, "error": self._exc_info_to_string(err, test)})
        super().addError(test, err)


def run(live_model=False):
    corpus = json.loads(CASES.read_text(encoding="utf-8"))
    rows, comparison = [], []
    env = {"BANKING_LLM_ENABLED": "1" if live_model else "0", "BANKING_AUTH_MODE": "demo"}
    test_root = ROOT / "backend/data"
    test_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=test_root) as temp, patch.object(bank, "DB_PATH", Path(temp) / "eval.sqlite3"), patch.dict(os.environ, env):
        for case in corpus["cases"]:
            token = bank.create_session()["token"]  # Independent contexts, no test order leakage.
            start = time.perf_counter()
            actual = bank.interpret_intent(bank.IntentRequest(text=case["text"]), token)
            correct = actual["kind"] == case["expected"] and all(actual.get(key) == case[key] for key in ("recipient_id",) if key in case)
            if "amount" in case:
                correct = correct and actual.get("amount") is not None and Decimal(str(actual["amount"])) == Decimal(case["amount"])
            rows.append({**case, "actual": {k: actual.get(k) for k in ("kind", "amount", "recipient_id")}, "pass": correct,
                         "latency_ms": round((time.perf_counter() - start) * 1000, 2)})
        with bank._connect() as db:
            counts = {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in ("sandbox_transfers", "sandbox_wealth_orders", "sandbox_plans")}
            balances_unchanged = db.execute("SELECT COUNT(*) FROM sandbox_sessions WHERE balance_fen!=250000 OR reserved_fen!=0").fetchone()[0] == 0
        for scenario in ("normal", "shared", "layered"):
            token = bank.create_session()["token"]
            report = evidence.fixture(evidence.FixtureRequest(scenario=scenario, recipient_id="mom"), token)
            try:
                transfer = bank.prepare_transfer(bank.PrepareTransfer(recipient_id="mom", amount="80", note="ablation"), token)
                actual_gate = transfer["level"]
            except HTTPException as error:
                actual_gate = "hold" if error.status_code == 409 else f"error-{error.status_code}"
            comparison.append({"scenario": scenario, "amount": "80.00", "permission_only_reference": "yellow", "actual_graph_gate": actual_gate,
                               "expected": {"normal": "yellow", "shared": "red", "layered": "hold"}[scenario],
                               "source_ids": [i for f in report["findings"] for i in f["source_ids"]]})
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "backend.tests.test_banking_monthly",
        "backend.tests.test_banking_refinements",
        "backend.tests.test_banking_semantic_safety",
        "backend.tests.test_banking_lifecycle_matrix",
        "backend.tests.test_banking_feed_resilience",
        "backend.tests.test_banking_crash_recovery",
        "backend.tests.test_banking_release_bundle",
        "backend.tests.test_banking_goals",
        "backend.tests.test_banking_journeys",
        "backend.tests.test_banking_contracts",
        "backend.tests.test_banking_storage", "backend.tests.test_banking_identity", "backend.tests.test_banking_device_check", "backend.tests.test_banking_research",
        "backend.tests.test_banking_sandbox", "backend.tests.test_banking_routes",
        "backend.tests.test_banking_trust", "backend.tests.test_banking_evidence", "backend.tests.test_banking_external", "backend.tests.test_banking_money_tools", "backend.tests.test_banking_readiness", "backend.tests.test_banking_planning", "backend.tests.test_banking_delivery", "backend.tests.test_banking_mission_evidence", "backend.tests.test_banking_recovery", "backend.tests.test_banking_entities"])
    tests = unittest.TextTestRunner(stream=io.StringIO(), resultclass=RecordedResult, verbosity=0).run(suite)
    latency = sorted(r["latency_ms"] for r in rows)
    from backend.banking_readiness import source_hash as current_source_hash
    source_hash = current_source_hash()
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "corpus_version": corpus["version"],
              "corpus_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(), "source_sha256": source_hash,
              "scope": corpus["scope"], "environment": {"python": platform.python_version(), "platform": platform.system(), "live_model": live_model,
              "model": os.getenv("BANKING_LLM_MODEL", os.getenv("LLM_MODEL")) if live_model else None},
              "intent": {"passed": sum(r["pass"] for r in rows), "total": len(rows), "p50_ms": latency[len(latency)//2], "p95_ms": latency[min(len(latency)-1, int(len(latency)*.95))], "cases": rows},
              "interpretation_side_effects": {"no_funds_moved": balances_unchanged and not any(counts.values()), "rows": counts},
              "regression": {"passed": sum(r["pass"] for r in tests.rows), "total": tests.testsRun, "cases": tests.rows},
              "comparison": {"scope": "Three hand-authored synthetic fixtures. Permission-only reference = new known payee, 80 CNY, daily spend zero. This is a policy illustration, not a fraud benchmark or external baseline.", "cases": comparison},
              "limitations": ["No real bank, hardware authenticator, external fraud dataset, independent assessor or user study was exercised.",
                               "Latencies cover direct Python calls on this host, not HTTP, model throughput or end-to-end UI latency.",
                               "Graph source is an explicit sandbox fixture; factual sentences compile from templates. Optional model claim ordering is fault-tested, not a free-text GraphRAG quality benchmark.",
                               "Synthetic v1 was authored in this repository; it is a frozen regression corpus, not a statistically independent holdout."]}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = REPORT.with_name("banking_release_v1_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    archive.write_bytes(temporary.read_bytes())
    temporary.replace(REPORT)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    result = run(args.live_model)
    print(json.dumps({"report": str(REPORT), "intent": f'{result["intent"]["passed"]}/{result["intent"]["total"]}', "regression": f'{result["regression"]["passed"]}/{result["regression"]["total"]}', "no_funds_moved": result["interpretation_side_effects"]["no_funds_moved"]}, ensure_ascii=False))
    raise SystemExit(0 if result["intent"]["passed"] == result["intent"]["total"] and result["regression"]["passed"] == result["regression"]["total"] and result["interpretation_side_effects"]["no_funds_moved"] and all(r["actual_graph_gate"] == r["expected"] for r in result["comparison"]["cases"]) else 1)
