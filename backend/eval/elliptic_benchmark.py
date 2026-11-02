"""Independent, retrospective Bitcoin transaction benchmark.

This is an out-of-domain research check, not a validation of corporate credit
decisions. The licensed Elliptic archive stays in data/external (gitignored).
Only aggregate metrics are written to data/processed (also gitignored).

Run: python -X utf8 backend/eval/elliptic_benchmark.py
Requires the separate requirements-eval.txt environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE = ROOT / "data/external/elliptic/elliptic-data-set.zip"
DEFAULT_REPORT = ROOT / "data/processed/elliptic-benchmark.json"
PREFIX = "elliptic_bitcoin_dataset/"
SOURCE_URL = "https://www.kaggle.com/datasets/ellipticco/elliptic-data-set"
EXPECTED_SHA256 = "70ed052c52002392720615a03261039b62d1ad594f6081ad021cb174a8ad5717"
SPLITS = {"train": (1, 34), "validation": (35, 41), "test": (42, 49)}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def choose_threshold(y_true: np.ndarray, probabilities: np.ndarray) -> float:
    """Choose on validation only; never inspect the held-out test labels."""
    precision, recall, thresholds = precision_recall_curve(y_true, probabilities)
    if not len(thresholds):
        return 0.5
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-12)
    return float(thresholds[int(np.argmax(f1))])


def evaluate(y_true: np.ndarray, probabilities: np.ndarray, threshold: float) -> dict:
    predicted = (probabilities >= threshold).astype(np.int8)
    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()
    return {
        "n": int(len(y_true)),
        "positives": int(y_true.sum()),
        "prevalence": float(y_true.mean()),
        "pr_auc": float(average_precision_score(y_true, probabilities)),
        "roc_auc": float(roc_auc_score(y_true, probabilities)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "confusion": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def load_archive(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if not path.is_file():
        raise FileNotFoundError(f"缺少 Elliptic 原始压缩包：{path}\n官方入口：{SOURCE_URL}")
    with ZipFile(path) as archive:
        required = {PREFIX + name for name in (
            "elliptic_txs_features.csv", "elliptic_txs_classes.csv", "elliptic_txs_edgelist.csv"
        )}
        if not required.issubset(set(archive.namelist())):
            raise ValueError("压缩包缺少 Elliptic 官方三个 CSV 文件")
        # The original features file has no header. Float32 cuts RAM use without
        # changing the published source. Labels are read separately and never joined
        # to the predictor matrix.
        with archive.open(PREFIX + "elliptic_txs_features.csv") as handle:
            features = pd.read_csv(handle, header=None, dtype={0: "int64", 1: "int8", **{i: "float32" for i in range(2, 167)}})
        with archive.open(PREFIX + "elliptic_txs_classes.csv") as handle:
            labels = pd.read_csv(handle, dtype={"txId": "int64", "class": "string"})
        with archive.open(PREFIX + "elliptic_txs_edgelist.csv") as handle:
            edges = pd.read_csv(handle, dtype="int64")
    if features.shape[1] != 167 or features[0].duplicated().any():
        raise ValueError("特征列数或交易 ID 不符合官方数据结构")
    if labels.txId.duplicated().any() or not set(labels["class"].dropna().unique()).issubset({"1", "2", "unknown"}):
        raise ValueError("标签文件包含重复或未知类别")
    return features, labels, edges


def graph_features(transaction_ids: np.ndarray, edges: pd.DataFrame) -> np.ndarray:
    """Five topology signals from the whole observed graph, without labels."""
    index = pd.Index(transaction_ids)
    src = index.get_indexer(edges.txId1.to_numpy())
    dst = index.get_indexer(edges.txId2.to_numpy())
    if (src < 0).any() or (dst < 0).any():
        raise ValueError("边文件引用了不存在的交易 ID")
    n = len(transaction_ids)
    incoming = np.bincount(dst, minlength=n).astype(np.float32)
    outgoing = np.bincount(src, minlength=n).astype(np.float32)
    adjacent_total = np.zeros(n, dtype=np.float32)
    adjacent_count = incoming + outgoing
    degree = adjacent_count
    np.add.at(adjacent_total, src, degree[dst])
    np.add.at(adjacent_total, dst, degree[src])
    neighbor_mean = adjacent_total / np.maximum(adjacent_count, 1)
    return np.column_stack((np.log1p(incoming), np.log1p(outgoing), np.log1p(degree),
                            np.log1p(neighbor_mean), (incoming > 0) & (outgoing > 0))).astype(np.float32)


def split_masks(time_step: np.ndarray, label: np.ndarray) -> dict[str, np.ndarray]:
    known = (label == 0) | (label == 1)
    masks = {name: known & (time_step >= start) & (time_step <= end)
             for name, (start, end) in SPLITS.items()}
    if any(not mask.any() for mask in masks.values()):
        raise ValueError("训练、验证或测试时间窗没有有标签交易")
    if np.any(sum(mask.astype(np.int8) for mask in masks.values()) > 1):
        raise ValueError("时间窗发生交叉")
    if any(len(np.unique(label[mask])) != 2 for mask in masks.values()):
        raise ValueError("每个时间窗必须包含合法和非法两类")
    return masks


def run(archive: Path, output: Path) -> dict:
    if not archive.is_file():
        raise FileNotFoundError(f"缺少 Elliptic 官方压缩包：{archive}\n下载入口：{SOURCE_URL}")
    archive_sha256 = sha256_file(archive)
    if archive_sha256 != EXPECTED_SHA256:
        raise ValueError("原始压缩包哈希与已核验的 Elliptic v1 不符；请重新下载官方版本并复核许可")
    features, labels, edges = load_archive(archive)
    ids = features[0].to_numpy()
    time_step = features[1].to_numpy()
    aligned = labels.set_index("txId")["class"].reindex(ids)
    if aligned.isna().any():
        raise ValueError("特征表与标签表交易 ID 不一致")
    label = np.full(len(ids), -1, dtype=np.int8)
    label[aligned.eq("1").fillna(False).to_numpy(dtype=bool)] = 1
    label[aligned.eq("2").fillna(False).to_numpy(dtype=bool)] = 0
    masks = split_masks(time_step, label)
    numeric = features.iloc[:, 2:].to_numpy(dtype=np.float32)
    topology = graph_features(ids, edges)
    matrices = {"numeric_features": numeric, "graph_topology": topology,
                "combined": np.column_stack((numeric, topology))}
    results = {}
    for name, matrix in matrices.items():
        model = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                              LogisticRegression(class_weight="balanced", max_iter=400, random_state=42))
        model.fit(matrix[masks["train"]], label[masks["train"]])
        val_prob = model.predict_proba(matrix[masks["validation"]])[:, 1]
        threshold = choose_threshold(label[masks["validation"]], val_prob)
        test_prob = model.predict_proba(matrix[masks["test"]])[:, 1]
        results[name] = {"threshold_from_validation": threshold,
                         "validation": evaluate(label[masks["validation"]], val_prob, threshold),
                         "test": evaluate(label[masks["test"]], test_prob, threshold)}
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"name": "Elliptic Data Set", "source_url": SOURCE_URL,
                    "license": "CC BY-NC-ND 4.0", "archive_sha256": archive_sha256,
                    "transaction_count": int(len(ids)), "edge_count": int(len(edges)),
                    "label_counts": {"illicit": int((label == 1).sum()),
                                     "licit": int((label == 0).sum()), "unknown": int((label == -1).sum())}},
        "protocol": {"type": "retrospective temporal holdout", "time_step_windows": SPLITS,
                     "known_labels_only": True, "threshold_selected_on": "validation",
                     "test_tuning": False, "fixed_random_state": 42,
                     "scope": "Bitcoin on-chain transaction classification; not bank transfers or corporate credit decisions",
                     "comparability": "Separate research baseline; the production GraphRAG workflow is not evaluated by this dataset"},
        "splits": {name: {"n": int(mask.sum()), "positives": int(label[mask].sum())}
                   for name, mask in masks.items()},
        "models": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    result = run(args.archive, args.output)
    print(json.dumps({"dataset": result["dataset"], "splits": result["splits"],
                      "test": {key: value["test"] for key, value in result["models"].items()}},
                     ensure_ascii=False, indent=2))
