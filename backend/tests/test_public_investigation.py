"""Provenance and scope checks for the public filing investigation path."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.public_investigation import investigate_public_entity, load_public_case


def test_central_report_covers_snapshot_without_simulated_flows() -> None:
    case = load_public_case()
    report = investigate_public_entity("CATL-300750")
    assert report["data_class"] == "public_disclosure"
    assert len(report["observations"]) == len(case["edges"]) == 19
    assert report["categories"] == {"holdings": 10, "subsidiaries": 5, "investments": 4}
    assert len(report["sources"]) == 2
    assert len(report["snapshot_sha256"]) == 64
    assert report["report_id"] == investigate_public_entity("CATL-300750")["report_id"]
    edge_ids = {edge["id"] for edge in case["edges"]}
    assert {row["edge_id"] for row in report["observations"]} == edge_ids
    for row in report["observations"]:
        assert row["source_record_id"] and row["source_page"] > 0
        assert row["source_url"].startswith("https://") and "#page=" in row["source_url"]
        assert "TRANSFER" != row["relation_type"]


def test_named_entity_only_reports_direct_relations() -> None:
    report = investigate_public_entity("CATL-HOLDER-07")
    assert len(report["observations"]) == 1
    assert report["observations"][0]["source_entity"]["name"] == "UBS AG"
    assert report["categories"]["holdings"] == 1
    assert len(report["sources"]) == 1


def test_unknown_entity_is_rejected_without_generated_claims() -> None:
    response = TestClient(app).get("/api/public-case/investigate", params={"entity_id": "NOT-IN-CASE"})
    assert response.status_code == 404
    assert "observations" not in response.json()


def test_production_review_endpoints_require_a_configured_token() -> None:
    client = TestClient(app)
    with patch.dict(os.environ, {"APP_ENV": "production", "REVIEW_API_TOKEN": ""}):
        assert client.get("/api/review/audit").status_code == 503
    with patch.dict(os.environ, {"APP_ENV": "production", "REVIEW_API_TOKEN": "test-secret"}):
        assert client.get("/api/review/audit").status_code == 401
        assert client.get("/api/review/audit", headers={"X-Review-Token": "wrong"}).status_code == 401
        assert client.get("/api/review/audit", headers={"X-Review-Token": "test-secret"}).status_code == 200
