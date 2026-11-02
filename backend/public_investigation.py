"""Deterministic, source-bound investigation of the public filing snapshot.

This module deliberately does not infer fraud, beneficial ownership or cash
transactions from corporate disclosures. Every observation is a filed edge.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CASE_PATH = Path(__file__).resolve().parents[1] / "frontend" / "public" / "data" / "catl-disclosure.json"


def load_public_case() -> dict[str, Any]:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


def investigate_public_entity(entity_id: str) -> dict[str, Any]:
    case = load_public_case()
    nodes = {node["id"]: node for node in case["nodes"]}
    if entity_id not in nodes:
        raise ValueError("公开案例中不存在该主体")

    snapshot = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    related = [edge for edge in case["edges"] if entity_id in (edge["source"], edge["target"])]
    source_ids = {"SZSE": "SZSE-2025-AR", "CNINFO": "CNINFO-2026-H1"}
    observations = []
    for edge in related:
        props = edge["properties"]
        source_url = props["source_url"]
        page = int(props["source_page"])
        observations.append({
            "edge_id": edge["id"],
            "relation_type": edge["type"],
            "relation_label": edge["label"],
            "source_entity": {"id": edge["source"], "name": nodes[edge["source"]]["label"]},
            "target_entity": {"id": edge["target"], "name": nodes[edge["target"]]["label"]},
            "statement": props["evidence_text"],
            "as_of": props["as_of"],
            "source_id": source_ids[props["source_system"]],
            "source_title": props["source_title"],
            "source_page": page,
            "source_record_id": props["source_record_id"],
            "source_url": f"{source_url}#page={page}",
            "qualification": props.get("identity_note") or props.get("amount_scope") or props.get("ratio_scope"),
        })

    categories = {
        "holdings": sum(item["relation_type"] in {"CONTROLLING_SHAREHOLDER", "DISCLOSED_HOLDING"} for item in observations),
        "subsidiaries": sum(item["relation_type"] == "GROUP_SUBSIDIARY" for item in observations),
        "investments": sum(item["relation_type"] == "PARENT_INVESTMENT" for item in observations),
    }
    used_sources = [source for source in case["meta"]["sources"] if source["id"] in {item["source_id"] for item in observations}]
    return {
        "report_id": f"PUBLIC-{hashlib.sha256((snapshot_hash + entity_id).encode()).hexdigest()[:12].upper()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_sha256": snapshot_hash,
        "data_class": "public_disclosure",
        "entity": {"id": entity_id, "name": nodes[entity_id]["label"], "type": nodes[entity_id]["type"]},
        "case_as_of": case["meta"]["as_of"],
        "summary": f"在公开披露快照中，找到与{nodes[entity_id]['label']}直接相关的 {len(observations)} 条关系记录。",
        "categories": categories,
        "sources": used_sources,
        "observations": observations,
        "limitations": [
            "仅整理公开披露中有页码的直接关系；不同记录的披露时点以各条证据为准。",
            "持股比例与长期股权投资账面价值不代表银行资金流或实际控制结论。",
            "同名 LEI 查询结果未经人工身份核对，不作为图谱中的已确认关联。",
            "本报告不作欺诈、洗钱、授信或最终受益人认定。",
        ],
    }
