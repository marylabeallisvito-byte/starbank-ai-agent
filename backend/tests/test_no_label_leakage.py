"""Synthetic answer labels must not enter the inference path."""
from __future__ import annotations

from backend.agents.workflow import without_training_labels
from backend.rag.risk_scoring import RiskScoringEngine
from backend.rag.spatiotemporal_aml import run_deterministic_detection


def test_synthetic_risk_flag_does_not_drive_inference() -> None:
    graph = {"nodes": [
        {"id": "flagged", "labels": ["Company"], "properties": {"risk_flag": True, "name": "测试企业"}},
        {"id": "other", "labels": ["Company"], "properties": {"risk_flag": False}},
    ], "edges": []}
    analysis_graph = without_training_labels(graph)
    assert all("risk_flag" not in node["properties"] for node in analysis_graph["nodes"])
    assert graph["nodes"][0]["properties"]["risk_flag"] is True
    assert RiskScoringEngine().score(graph)["flagged"] == 0.0
    assert run_deterministic_detection(graph)["suspicious_nodes"] == []
