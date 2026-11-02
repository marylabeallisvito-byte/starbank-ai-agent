"""Frozen-model sensitivity diagnostics, never a new independent fraud benchmark.

No fitting, threshold search or selection occurs here. Scaling is a counterfactual
stress test with an explicit assumption that original labels remain applicable.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.eval import amlsim_recall_v2 as frozen
from backend.eval.amlsim_external_eval import read_archive, features, cohort, metrics
from backend.rag.graph_features import extract

REPORT = frozen.DIRECTORY / "amlsim_robustness_v1.json"
PROTOCOL = {"version": "amlsim-robustness-v1", "amount_scales": [0.5, 1.0, 2.0], "cut": 90,
            "selection": "All predeclared scales are reported; no model fitting or threshold adjustment.",
            "label_assumption": "Original synthetic labels retained under uniform amount scaling; not newly verified counterfactual fraud labels."}


def overlap(left, right):
    common = len(left & right)
    return {"shared": common, "left": len(left), "right": len(right), "share_of_smaller": round(common / min(len(left), len(right)), 6) if left and right else 0}


def run():
    import joblib
    selection = json.loads(frozen.SELECTION.read_text(encoding="utf-8"))
    if selection["code_sha256"] != frozen.code_hash() or frozen.sha(frozen.MODEL) != selection["supervised_model"]["model_sha256"]:
        raise ValueError("Frozen source/model checksum changed; refuse sensitivity report")
    models = joblib.load(frozen.MODEL)
    names = ["20K_fanin200cycle200.tgz", "20K_cycle200.tgz", "20K_fanin200.tgz"]
    all_ids, transactions, scenarios = {}, {}, []
    for name in names:
        path = frozen.SOURCE / name
        expected = selection["development_sha256"] if name == names[0] else selection["scenario_files_sha256"][name]
        if frozen.sha(path) != expected: raise ValueError("Source archive checksum changed")
        nodes, edges = read_archive(path)
        all_ids[name] = set(nodes)
        transactions[name] = {(e["source"], e["target"], e["value"], e["step"]) for e in edges}
        test = [e for e in edges if e["step"] > PROTOCOL["cut"]]
        labels = cohort(nodes, test, PROTOCOL["cut"] + 1, max(e["step"] for e in edges))
        rows = []
        reference = {}
        for scale in PROTOCOL["amount_scales"]:
            scaled = [{**e, "value": e["value"] * scale} for e in test]
            ids, matrix = extract(features(scaled, 24))
            results, predictions = {}, {}
            for key, threshold_key, data in [("graph", "supervised_model", matrix), ("amount", "amount_ablation", matrix[:, 4:12])]:
                probabilities = models[key].predict_proba(data)[:, 1]
                predicted = {n for n, probability in zip(ids, probabilities) if probability >= selection[threshold_key]["selected"]["threshold"]}
                predictions[key] = predicted
                results[key] = metrics(labels, predicted)
            if scale == 1.0: reference = predictions
            rows.append({"scale": scale, "metrics": results, "predictions": predictions})
        for row in rows:
            row["changed_decision_fraction"] = {key: round(len((pred ^ reference[key]) & labels.keys()) / len(labels), 6) for key, pred in row.pop("predictions").items()}
        scenarios.append({"name": name, "archive_sha256": frozen.sha(path), "accounts": len(labels), "scales": rows})
    comparisons = [{"left": left, "right": right, "account_ids": overlap(all_ids[left], all_ids[right]), "exact_transaction_tuples": overlap(transactions[left], transactions[right])} for i, left in enumerate(names) for right in names[i+1:]]
    code = Path(__file__).read_bytes()
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": PROTOCOL,
              "protocol_sha256": hashlib.sha256(json.dumps(PROTOCOL, sort_keys=True).encode()).hexdigest(), "audit_code_sha256": hashlib.sha256(code).hexdigest(),
              "model_sha256": frozen.sha(frozen.MODEL), "scenarios": scenarios, "overlap": comparisons,
              "limitations": ["Account ID overlap alone does not establish the same real entity; exact transaction tuple overlap is stronger evidence of shared synthetic background.",
                              "These are diagnostics on already available archives, not new independent tests. No real-bank accuracy is estimated.",
                              "Uniform amount scaling preserves topology and ratios but assumes original labels remain applicable; stress metrics are not validated real-world performance.",
                              "Zero observed false positives in finite synthetic cohorts does not establish zero production risk."]}
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(REPORT), "scenarios": [{"name": s["name"], "scales": [{"scale": r["scale"], "graph_recall": r["metrics"]["graph"]["recall"], "amount_recall": r["metrics"]["amount"]["recall"]} for r in s["scales"]]} for s in scenarios], "overlap": comparisons}))


if __name__ == "__main__": run()
