"""Bounded, session-scoped graph retrieval for the synthetic banking ledger.

Reuses the investigation graph schema and deterministic detectors. No generated
Cypher, external identities, fraud probabilities, or model decisions enter the gate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException
from pydantic import BaseModel

from backend import banking as bank
from backend.rag.spatiotemporal_aml import detect_device_clustering, detect_fast_in_fast_out


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def make_graph(recipient: str, scenario: str, timestamp: str) -> dict:
    """Explicitly synthetic fixture; shared-device alone is insufficient to block."""
    nodes = [dict(id=recipient, labels=["Person"], properties={"name": "模拟收款人"}),
             dict(id="account", labels=["Account"], properties={"name": "收款账户"}),
             dict(id="peer", labels=["Person"], properties={"name": "关联模拟用户"}),
             dict(id="device", labels=["Device"], properties={"name": "设备 D01"}),
             dict(id="source", labels=["Account"], properties={"name": "上游账户"}),
             dict(id="destination", labels=["Account"], properties={"name": "下游账户"})]
    edges = [dict(id="own-01", source=recipient, target="account", type="OWNS", properties={"date": timestamp}),
             dict(id="login-01", source=recipient, target="device", type="LOGGED_IN", properties={"date": timestamp})]
    if scenario in ("shared", "layered"):
        edges.append(dict(id="login-02", source="peer", target="device", type="LOGGED_IN", properties={"date": timestamp}))
    if scenario == "layered":
        earlier = (datetime.fromisoformat(timestamp) - timedelta(minutes=8)).isoformat()
        edges.extend([dict(id="flow-01", source="source", target="account", type="TRANSFER", properties={"amount": 3000, "date": earlier}),
                      dict(id="flow-02", source="account", target="destination", type="TRANSFER", properties={"amount": 2900, "date": timestamp})])
    active = {e[key] for e in edges for key in ("source", "target")}
    return {"nodes": [n for n in nodes if n["id"] in active], "edges": edges}


def assess(graph: dict) -> list:
    # Banking sandbox configuration, not thresholds calibrated for real AML.
    device = detect_device_clustering(graph, window_hours=24, min_persons=2)
    flow = detect_fast_in_fast_out(graph, window_hours=1, min_amount=1000, fast_ratio=.9)
    findings = []
    if device:
        findings.append({"code": "SHARED_DEVICE", "title": "两位模拟用户共用设备", "source_ids": [e["id"] for e in graph["edges"] if e["type"] == "LOGGED_IN"],
                         "explanation": "登录记录显示设备复用；家庭共用设备也可能产生这一信号，单独出现时仅升级验证。"})
    if flow:
        findings.append({"code": "RAPID_FLOW", "title": "八分钟内转出大部分入账", "source_ids": [flow[0]["evidence"][k] for k in ("in_edge", "out_edge")],
                         "explanation": "同一模拟账户入账 3,000 元、随后转出 2,900 元。与设备复用同时出现时，本沙箱停止转账。"})
    return findings


def retrieve(db, sid: str, recipient: str) -> dict:
    row = db.execute("SELECT * FROM sandbox_graph_sources WHERE session_id=? AND recipient_id=?", (sid, recipient)).fetchone()
    try:
        graph = json.loads(row["graph_json"]) if row else {"nodes": [], "edges": []}
    except (TypeError, ValueError):
        raise HTTPException(503, "图谱来源无法解析，转账暂不可执行") from None
    if row and digest(graph) != row["graph_hash"]:
        raise HTTPException(503, "图谱来源完整性校验失败，转账暂不可执行")
    fresh = not row or bank._now().isoformat() <= row["expires_at"]
    # Historical observations remain inspectable. Freshness controls authorization,
    # not whether a user can read what the source contained.
    findings = assess(graph)
    decision = "unavailable" if not fresh else "hold" if len(findings) >= 2 else "verify" if findings else "standard"
    revision = digest({"recipient": recipient, "graph": graph, "version": row["revision"] if row else 0})
    return {"recipient_id": recipient, "revision": revision, "source": "synthetic_banking_graph", "coverage": "fixture" if row else "none",
            "observed_at": row["created_at"] if row else None, "expires_at": row["expires_at"] if row else None,
            "decision": decision, "graph": graph, "findings": findings, "sandbox": True,
            "source_status": "missing" if not row else "fresh" if fresh else "expired",
            "scenario": row["scenario"] if row else None,
            "authorization_usable": bool(row) and fresh,
            "generation": "evidence_bound_template", "policy_version": "bank-graph-v1",
            "summary": {"hold": "两类关联信号同时出现，当前转账停止；设备验证不能覆盖这一拦截。", "verify": "发现设备共用，转账升级为红色设备验证；该信号不代表欺诈。", "standard": "未发现本规则覆盖的组合异常。未接入的外部风险不在本次判断范围内。", "unavailable": "来源已过期，无法完成图谱核验；请刷新模拟来源后重新预览。"}[decision]}


class FixtureRequest(BaseModel):
    scenario: str
    recipient_id: str = "li"


class ExplainRequest(BaseModel):
    revision: str
    focus: Literal["evidence", "authorization", "coverage"] = "evidence"


@bank.router.post("/evidence/{recipient_id}/explain")
def explain_evidence(recipient_id: str, req: ExplainRequest, x_banking_session: str | None = Header(default=None)):
    from backend.banking_graph_narrative import explain
    report = evidence(recipient_id, x_banking_session)
    if report["revision"] != req.revision:
        raise HTTPException(409, "来源已变化，请重新读取证据")
    result = explain(report, req.focus)  # No write lock or tool access during a model call.
    with bank._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = bank._session(db, x_banking_session)
        current = retrieve(db, user["id"], recipient_id)
        if current["revision"] != req.revision or current["source_status"] != report["source_status"]:
            raise HTTPException(409, "讲解期间来源已变化，请重新读取证据")
        bank._audit(db, user["id"], None, "GRAPH_EXPLANATION", result)
    return result


@bank.router.get("/evaluation/report")
def evaluation_report(x_banking_session: str | None = Header(default=None)):
    with bank._connect() as db:
        bank._session(db, x_banking_session)
    path = Path(__file__).resolve().parent / "eval/reports/banking_release_v1.json"
    if not path.is_file():
        raise HTTPException(404, "尚未生成评测报告，请先运行隔离评测命令")
    return json.loads(path.read_text(encoding="utf-8"))


@bank.router.get("/evidence/{recipient_id}")
def evidence(recipient_id: str, x_banking_session: str | None = Header(default=None)):
    if recipient_id not in {c["id"] for c in bank.CONTACTS}:
        raise HTTPException(404, "联系人不存在")
    with bank._connect() as db:
        user = bank._session(db, x_banking_session)
        return retrieve(db, user["id"], recipient_id)


@bank.router.post("/evidence-fixtures")
def fixture(req: FixtureRequest, x_banking_session: str | None = Header(default=None)):
    if req.scenario not in ("normal", "shared", "layered", "clear") or req.recipient_id not in {c["id"] for c in bank.CONTACTS}:
        raise HTTPException(422, "未知的模拟情境或联系人")
    with bank._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        user = bank._session(db, x_banking_session)
        if req.scenario == "clear":
            db.execute("DELETE FROM sandbox_graph_sources WHERE session_id=? AND recipient_id=?", (user["id"], req.recipient_id))
        else:
            now = bank._now()
            graph = make_graph(req.recipient_id, req.scenario, now.isoformat())
            db.execute("INSERT INTO sandbox_graph_sources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,recipient_id) DO UPDATE SET graph_json=excluded.graph_json,graph_hash=excluded.graph_hash,created_at=excluded.created_at,expires_at=excluded.expires_at,revision=revision+1,scenario=excluded.scenario",
                       (user["id"], req.recipient_id, json.dumps(graph), digest(graph), now.isoformat(), (now + timedelta(minutes=15)).isoformat(), 1, req.scenario))
        bank._audit(db, user["id"], None, "GRAPH_FIXTURE_CHANGED", req.model_dump())
        return retrieve(db, user["id"], req.recipient_id)
