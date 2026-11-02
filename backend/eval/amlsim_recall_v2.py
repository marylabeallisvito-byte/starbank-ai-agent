"""Development-only parameter selection, then frozen scenario evaluation.

V1 exposed test results remain a historical diagnostic, never a new blind test.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.eval.amlsim_external_eval import ROOT, read_archive, features, cohort, metrics, graph_predictions
from backend.rag.temporal_patterns import detect_temporal_patterns
from backend.rag.graph_features import extract, NAMES

DIRECTORY = ROOT / "backend/eval/reports"
SELECTION = DIRECTORY / "amlsim_recall_v2_selection.json"
REPORT = DIRECTORY / "amlsim_recall_v2.json"
PROTOCOL = {"version": "amlsim-recall-v2", "cut": 90, "step_hours": 24, "max_development_fpr": .10,
            "candidates": [{"min_amount": amount, "window_hours": hours, "min_sources": count, "tolerance": .2} for amount in (150, 200, 300) for hours in (72, 168) for count in (3, 4, 5)] + [{"min_amount": amount, "window_hours": 336, "min_sources": 99, "tolerance": tolerance, "max_cycle_length": 10} for amount in (50, 150, 200) for tolerance in (.3, 1.0)],
            "selection": "Max development F2 subject to FPR <= 0.10; fallback maximum F1 if none feasible. First candidate wins ties.",
            "supervised": {"train_end_step": 60, "validation_steps": [61, 90], "threshold_candidates": [.05, .1, .2, .3, .4, .5, .6, .7, .8, .9], "trees": 120, "max_depth": 8, "min_samples_leaf": 10, "seed": 20261027},
            "scope": "New scenario holdouts from the same external synthetic generator; shared background/IDs are possible. Not real fraud or independent seed validation."}
SOURCE = ROOT / "data/external/AMLSim"
MIXED = SOURCE / "20K_fanin200cycle200.tgz"
MODEL = ROOT / "backend/data/amlsim_graph_forest_v2.joblib"


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def code_hash(): return hashlib.sha256(Path(__file__).read_bytes() + (ROOT / "backend/rag/temporal_patterns.py").read_bytes() + (ROOT / "backend/rag/graph_features.py").read_bytes()).hexdigest()


def predict(graph, config):
    result = detect_temporal_patterns(graph, **config)
    if not result["complete"]: raise RuntimeError("Traversal budget exhausted; refuse incomplete benchmark")
    return {n for f in result["findings"] for n in f["node_ids"]}, result


def develop():
    nodes, edges = read_archive(MIXED)
    development = [e for e in edges if e["step"] <= PROTOCOL["cut"]]
    labels = cohort(nodes, development, 0, PROTOCOL["cut"])
    graph = features(development, PROTOCOL["step_hours"])
    trials = []
    for config in PROTOCOL["candidates"]:
        try:
            prediction, result = predict(graph, config)
        except RuntimeError:
            trials.append({"config": config, "complete": False, "metrics": {"false_positive_rate": 1, "f1": 0, "f2": 0}})
            continue
        score = metrics(labels, prediction)
        score["f2"] = round(5 * score["tp"] / (5 * score["tp"] + 4 * score["fn"] + score["fp"]), 6) if score["tp"] else 0
        trials.append({"config": config, "metrics": score, "complete": result["complete"]})
    feasible = [r for r in trials if r["complete"] and r["metrics"]["false_positive_rate"] <= PROTOCOL["max_development_fpr"]]
    selected = max(feasible or [r for r in trials if r["complete"]], key=lambda r: r["metrics"]["f2" if feasible else "f1"])
    import joblib
    import sklearn
    from sklearn.ensemble import RandomForestClassifier
    train = [e for e in edges if e["step"] <= 60]
    validation = [e for e in edges if 60 < e["step"] <= 90]
    train_labels, validation_labels = cohort(nodes, train, 0, 60), cohort(nodes, validation, 61, 90)
    train_ids, train_x = extract(features(train, 24)); validation_ids, validation_x = extract(features(validation, 24))
    keep = [i for i, n in enumerate(train_ids) if n in train_labels]
    model = RandomForestClassifier(n_estimators=120, max_depth=8, min_samples_leaf=10, class_weight="balanced_subsample", random_state=20261027, n_jobs=2)
    model.fit(train_x[keep], [train_labels[train_ids[i]] for i in keep])
    amount_model = RandomForestClassifier(n_estimators=120, max_depth=8, min_samples_leaf=10, class_weight="balanced_subsample", random_state=20261027, n_jobs=2)
    amount_model.fit(train_x[keep, 4:12], [train_labels[train_ids[i]] for i in keep])
    probabilities = model.predict_proba(validation_x)[:, 1]
    validation_trials = []
    for threshold in PROTOCOL["supervised"]["threshold_candidates"]:
        score = metrics(validation_labels, {n for n, probability in zip(validation_ids, probabilities) if probability >= threshold})
        score["f2"] = 5 * score["tp"] / max(1, 5 * score["tp"] + 4 * score["fn"] + score["fp"])
        validation_trials.append({"threshold": threshold, "metrics": score})
    feasible_ml = [r for r in validation_trials if r["metrics"]["false_positive_rate"] <= .1]
    ml_selection = max(feasible_ml or validation_trials, key=lambda r: r["metrics"]["f2"])
    amount_trials = []
    amount_probabilities = amount_model.predict_proba(validation_x[:, 4:12])[:, 1]
    for threshold in PROTOCOL["supervised"]["threshold_candidates"]:
        score = metrics(validation_labels, {n for n, probability in zip(validation_ids, amount_probabilities) if probability >= threshold})
        score["f2"] = 5 * score["tp"] / max(1, 5 * score["tp"] + 4 * score["fn"] + score["fp"])
        amount_trials.append({"threshold": threshold, "metrics": score})
    feasible_amount = [r for r in amount_trials if r["metrics"]["false_positive_rate"] <= .1]
    amount_selection = max(feasible_amount or amount_trials, key=lambda r: r["metrics"]["f2"])
    MODEL.parent.mkdir(exist_ok=True); joblib.dump({"graph": model, "amount": amount_model}, MODEL)
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "protocol": PROTOCOL, "code_sha256": code_hash(), "development_sha256": sha(MIXED), "trials": trials, "selected": selected,
              "scenario_files_sha256": {name: sha(SOURCE / name) for name in ("20K_cycle200.tgz", "20K_fanin200.tgz")},
              "amount_ablation": {"features": NAMES[4:12], "selected": amount_selection, "trials": amount_trials},
              "supervised_model": {"kind": "RandomForest over graph topology/flow features (not GNN)", "sklearn": sklearn.__version__, "model_sha256": sha(MODEL), "train_accounts": len(train_labels), "train_positives": sum(train_labels.values()), "validation_accounts": len(validation_labels), "validation_positives": sum(validation_labels.values()), "selected": ml_selection, "trials": validation_trials, "feature_importances": sorted([{"name": name, "importance": round(float(value), 6)} for name, value in zip(NAMES, model.feature_importances_)], key=lambda r: r["importance"], reverse=True)}}
    DIRECTORY.mkdir(exist_ok=True)
    SELECTION.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"selected_on_development_only": selected, "supervised_validation_selection": ml_selection, "selection": str(SELECTION)}))


def evaluate():
    selection = json.loads(SELECTION.read_text())
    if selection["code_sha256"] != code_hash() or selection["development_sha256"] != sha(MIXED): raise ValueError("Selection/source changed; create a new evaluation version")
    import joblib
    if sha(MODEL) != selection["supervised_model"]["model_sha256"]: raise ValueError("Local trained model checksum changed")
    models = joblib.load(MODEL)  # Only the locally trained, checksum-bound artifact; no user-uploaded models.
    model = models["graph"]
    scenarios = []
    for name, scope in [("20K_fanin200cycle200.tgz", "historical_exposed_diagnostic"), ("20K_cycle200.tgz", "new_scenario_holdout"), ("20K_fanin200.tgz", "new_scenario_holdout")]:
        path = SOURCE / name
        if name in selection["scenario_files_sha256"] and sha(path) != selection["scenario_files_sha256"][name]: raise ValueError("Scenario checksum mismatch")
        nodes, edges = read_archive(path)
        end = max(e["step"] for e in edges)
        test = [e for e in edges if e["step"] > PROTOCOL["cut"]]
        labels = cohort(nodes, test, PROTOCOL["cut"] + 1, end)
        graph = features(test, PROTOCOL["step_hours"])
        prediction, result = predict(graph, selection["selected"]["config"])
        old, _ = graph_predictions(graph, {"min_amount": 200, "window_hours": 72, "min_sources": 3, "fast_ratio": .9})
        model_ids, matrix = extract(graph)
        probabilities = model.predict_proba(matrix)[:, 1]
        supervised_prediction = {n for n, probability in zip(model_ids, probabilities) if probability >= selection["supervised_model"]["selected"]["threshold"]}
        amount_prediction = {n for n, probability in zip(model_ids, models["amount"].predict_proba(matrix[:, 4:12])[:, 1]) if probability >= selection["amount_ablation"]["selected"]["threshold"]}
        scenarios.append({"name": name, "scope": scope, "archive_sha256": sha(path), "accounts": len(labels), "positives": sum(labels.values()), "transactions": len(test),
                          "v1": metrics(labels, old), "v2": metrics(labels, prediction), "graph_feature_model": metrics(labels, supervised_prediction), "amount_feature_model": metrics(labels, amount_prediction), "complete": result["complete"], "finding_count": len(result["findings"])})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "protocol": PROTOCOL, "selection": selection, "scenarios": scenarios,
              "limitations": ["V1 historical test was already exposed; new algorithm comparisons there are retrospective.", "Other scenarios may share generator background, account IDs and seeds; their independence is not established.", "Generator-specific amount artifacts may explain strong supervised performance; amount-only supervised ablation is included.", "All results concern synthetic data, not bank fraud rates. No labels enter graph features."]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    REPORT.write_text(text, encoding="utf-8")
    REPORT.with_name("amlsim_recall_v2_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json").write_text(text, encoding="utf-8")
    print(json.dumps(scenarios, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("phase", choices=["develop", "evaluate"])
    args = parser.parse_args(); develop() if args.phase == "develop" else evaluate()
