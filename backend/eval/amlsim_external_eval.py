"""Evaluate existing graph detectors against pinned, externally authored AMLSim data."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.rag.spatiotemporal_aml import detect_fast_in_fast_out, detect_dispersed_in_concentrated_out

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = Path(__file__).with_name("amlsim_protocol_v1.json")
REPORT = ROOT / "backend/eval/reports/amlsim_external_v1.json"


def read_archive(path):
    result = {}
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            name = Path(member.name).name
            if member.isfile() and name in ("nodes.csv", "transactions.csv"):
                if name in result or member.size > 30_000_000:
                    raise ValueError("duplicate or oversize dataset member")
                result[name] = list(csv.DictReader(io.TextIOWrapper(archive.extractfile(member), encoding="utf-8-sig")))
    nodes = {r["nodeid"]: {"positive": r["isFraud"] == "1", "fraud_step": int(r["fraudStep"])} for r in result["nodes.csv"]}
    if len(nodes) != len(result["nodes.csv"]):
        raise ValueError("duplicate account ids")
    edges = []
    for i, row in enumerate(result["transactions.csv"]):
        value, step = float(row["value"]), int(row["time"])
        if not math.isfinite(value) or value <= 0 or step < 0 or row["sourceNodeId"] not in nodes or row["targetNodeId"] not in nodes:
            raise ValueError("invalid transaction")
        edges.append({"id": f"AMLSIM-TX-{i+1}", "source": row["sourceNodeId"], "target": row["targetNodeId"], "value": value, "step": step})
    return nodes, edges


def features(edges, step_hours):
    # Labels, isFraud, fraudStep, initial balances and metadata are deliberately absent.
    epoch = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ids = {e[k] for e in edges for k in ("source", "target")}
    return {"nodes": [{"id": n, "labels": ["Account"], "properties": {}} for n in sorted(ids)],
            "edges": [{"id": e["id"], "source": e["source"], "target": e["target"], "type": "TRANSFER",
                       "properties": {"amount": e["value"], "date": (epoch + timedelta(hours=e["step"] * step_hours)).isoformat()}} for e in edges]}


def cohort(nodes, edges, start, end):
    active = {e[k] for e in edges for k in ("source", "target")}
    return {n: int(nodes[n]["positive"]) for n in active if not nodes[n]["positive"] or start <= nodes[n]["fraud_step"] <= end}


def metrics(labels, predicted):
    tp = sum(y == 1 and n in predicted for n, y in labels.items())
    fp = sum(y == 0 and n in predicted for n, y in labels.items())
    fn = sum(y == 1 and n not in predicted for n, y in labels.items())
    tn = sum(y == 0 and n not in predicted for n, y in labels.items())
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": round(precision, 6), "recall": round(recall, 6),
            "f1": round(2 * tp / (2 * tp + fp + fn), 6) if 2 * tp + fp + fn else 0,
            "false_positive_rate": round(fp / (fp + tn), 6) if fp + tn else 0}


def graph_predictions(graph, config):
    rapid = detect_fast_in_fast_out(graph, window_hours=config["window_hours"], min_amount=config["min_amount"], fast_ratio=config["fast_ratio"])
    fan = detect_dispersed_in_concentrated_out(graph, window_hours=config["window_hours"], min_amount=config["min_amount"], min_sources=config["min_sources"])
    findings = rapid + fan
    return {r["evidence"]["account_id"] for r in findings}, findings


def baseline(edges, threshold):
    return {e["source"] for e in edges if e["value"] >= threshold}


def run(path):
    started = time.perf_counter()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    archive_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if archive_hash != protocol["archive_sha256"]:
        raise ValueError("Archive checksum does not match pinned protocol; use a new protocol for a different dataset.")
    nodes, edges = read_archive(path)
    cut = protocol["development_end_step"]
    end = max(e["step"] for e in edges)
    development = [e for e in edges if e["step"] <= cut]
    test = [e for e in edges if e["step"] > cut]
    dev_labels = cohort(nodes, development, 0, cut)
    test_labels = cohort(nodes, test, cut + 1, end)
    dev_graph, test_graph = features(development, protocol["simulation_step_hours"]), features(test, protocol["simulation_step_hours"])
    dev_trials = []
    for candidate in protocol["baseline_amount_candidates"]:
        dev_trials.append({"family": "amount_only", "config": candidate, "metrics": metrics(dev_labels, baseline(development, candidate))})
    for candidate in protocol["graph_candidates"]:
        predicted, _ = graph_predictions(dev_graph, candidate)
        dev_trials.append({"family": "graph_patterns", "config": candidate, "metrics": metrics(dev_labels, predicted)})
    selected = {family: max((r for r in dev_trials if r["family"] == family), key=lambda r: r["metrics"]["f1"]) for family in ("amount_only", "graph_patterns")}
    graph_pred, findings = graph_predictions(test_graph, selected["graph_patterns"]["config"])
    base_pred = baseline(test, selected["amount_only"]["config"])
    examples = []
    for finding in findings[:10]:
        account = finding["evidence"]["account_id"]
        sources = [edge["id"] for edge in test_graph["edges"] if account in (edge["source"], edge["target"])][:12]
        examples.append({"account_id": account, "pattern": finding["pattern"], "source_ids": sources, "label_in_test_cohort": test_labels.get(account), "source_scope": "incident edges for inspection; not all are causal"})
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": protocol, "protocol_sha256": hashlib.sha256(PROTOCOL.read_bytes()).hexdigest(),
              "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "detector_sha256": hashlib.sha256((ROOT / "backend/rag/spatiotemporal_aml.py").read_bytes()).hexdigest(),
              "data_kind": "external_author_synthetic", "source_commit": protocol["source_commit"], "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "source_url": protocol["source_url"], "license": "Apache-2.0 (repository)", "nodes": len(nodes), "transactions": len(edges),
              "positive_nodes_in_csv": sum(n["positive"] for n in nodes.values()), "positive_nodes_without_known_time": sum(n["positive"] and n["fraud_step"] < 0 for n in nodes.values()),
              "development": {"steps": [0, cut], "transactions": len(development), "accounts": len(dev_labels), "positives": sum(dev_labels.values()), "trials": dev_trials},
              "test": {"steps": [cut + 1, end], "transactions": len(test), "accounts": len(test_labels), "positives": sum(test_labels.values()),
                       "excluded_positive_accounts": len({e[k] for e in test for k in ("source", "target")} - set(test_labels)),
                       "amount_only": {"config": selected["amount_only"]["config"], **metrics(test_labels, base_pred)},
                       "graph_patterns": {"config": selected["graph_patterns"]["config"], **metrics(test_labels, graph_pred)}},
              "examples": examples, "runtime_seconds": round(time.perf_counter() - started, 3),
              "limitations": ["External author's synthetic sample, not real bank data; one archive, no independent seed replicates.", "Temporal transductive split: accounts can recur; all feature edges stay inside their time interval.", "Labels and fraudStep are used only for cohort construction and evaluation, not detector inputs.", "Unknown fraud timestamps and pre-existing positives are excluded from test, not treated as normal.", "Simulation steps are mapped to days by convention; values retain dataset units and are not CNY.", "Evaluates existing rapid-flow and fan-in/out rules, not device signals or the full transfer permission policy.", "Upstream metadata says 1803 positive nodes; actual CSV contains 1804. This report uses CSV labels without changing them."]}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text(output, encoding="utf-8")
    REPORT.with_name("amlsim_external_v1_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json").write_text(output, encoding="utf-8")
    tmp.replace(REPORT)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=ROOT / "data/external/AMLSim/20K_fanin200cycle200.tgz")
    args = parser.parse_args()
    report = run(args.archive)
    print(json.dumps({"nodes": report["nodes"], "transactions": report["transactions"], "test": report["test"], "runtime_seconds": report["runtime_seconds"]}, ensure_ascii=False))
