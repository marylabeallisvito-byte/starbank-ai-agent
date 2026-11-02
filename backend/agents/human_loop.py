"""
backend/agents/human_loop.py
============================
人在回路闭环（创新点四 · P2）—— 信贷员确认/驳回回写 + 审计日志。

设计定位
--------
计划书定位「金融风控 Copilot（副驾驶）」，但代码若只有 AI 单向输出、无人工介入
回路，则「Copilot」名不副实。本模块补齐闭环三要素：

    1. 审批回写图谱   ：信贷员确认 AI 的 REJECT 后，把 suspicious_nodes 的
                        risk_flag=true 写回 Neo4j（强化证据、供后续查询复用）；
                        驳回则标记 human_review=rejected（记录 AI 误报）。
    2. 回写评测集     ：人工确认的结论沉淀为带标签样本（human_labels.jsonl），
                        持续扩充盲评集，形成「上线 → 纠错 → 再评测」的校准闭环。
    3. 操作日志       ：每次人工介入都以 JSONL 追加（可追溯，但文件本身不具备防篡改保证），
                        记录 query / AI 决策 / 人工决策 / 审核人 / 理由 / 时间戳。

设计原则：写回采用参数化 Cypher（杜绝注入）、审计日志 append-only（可追溯），
且核心函数对「图数据库不可用」容错——离线/演示模式下仍能落审计与标签文件。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 数据目录：审计日志与人工标签沉淀于此（backend/data/，已 gitignore 的运行时产物）
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
AUDIT_LOG_PATH = DATA_DIR / "audit_log.jsonl"
HUMAN_LABELS_PATH = DATA_DIR / "human_labels.jsonl"

# 人工决策枚举
HUMAN_CONFIRM = "confirm"   # 信贷员确认 AI 的风险结论
HUMAN_REJECT = "reject"     # 信贷员驳回 AI 的结论（记为误报）


# ======================================================================
# 反馈记录结构
# ======================================================================
@dataclass
class FeedbackRecord:
    """
    一次人工介入的完整记录。

    字段：
        query            用户自然语言查询；
        ai_decision      AI 裁决（REJECT / FLAGGED / APPROVE）；
        suspicious_nodes AI 标记的可疑节点 id 列表；
        human_decision   人工决策：confirm（确认）/ reject（驳回）；
        reviewer         审核人标识（如「信贷员-张」）；
        rationale        审核理由（自由文本，供审计与后续校准）；
        timestamp        介入时间（ISO 格式，缺省取当前本地时间）。
    """
    query: str
    ai_decision: str
    suspicious_nodes: List[str]
    human_decision: str
    reviewer: str = "system"
    rationale: str = ""
    timestamp: str = ""
    investigation_id: str = ""
    source: str = "unknown"

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ======================================================================
# 基础工具
# ======================================================================
def _ensure_data_dir() -> None:
    """确保 backend/data/ 存在（审计日志与人工标签的落盘位置）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    """以 UTF-8 + 追加模式写入一行 JSON（append-only，不覆盖历史记录）。"""
    _ensure_data_dir()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取 JSONL 的全部记录（最多 limit 条，默认倒序返回最近的在后）。"""
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 跳过损坏行，保证审计读取健壮
    return rows[-limit:] if limit else rows


# ======================================================================
# 图回写（参数化 Cypher，杜绝注入）
# ======================================================================
def write_back_to_graph(db: Any, record: FeedbackRecord) -> Dict[str, Any]:
    """
    把人工决策写回 Neo4j 图谱。

    - confirm：对 suspicious_nodes 逐节点 SET risk_flag=true + human_review=confirmed，
               强化「已确认高危」证据，供后续穿透查询复用；
    - reject ：仅 SET human_review=rejected，不改动原 risk_flag（误报记录在案，
               但不过度覆盖既有风险标记）。

    Args:
        db:     Neo4jDatabase 单例（离线/演示可传 None，此时仅记录、不写图）。
        record: 本次反馈记录。

    Returns:
        {"written": bool, "nodes": int, "human_review": str} 或含 reason 的失败说明。
    """
    nodes = [n for n in (record.suspicious_nodes or []) if n]
    if not nodes:
        return {"written": False, "reason": "无可写回的节点"}
    if db is None:
        return {"written": False, "reason": "图数据库未注入（离线/演示模式）"}

    confirm = record.human_decision == HUMAN_CONFIRM
    props = {
        "human_review": "confirmed" if confirm else "rejected",
        "human_reviewer": record.reviewer,
        "human_reviewed_at": record.timestamp,
    }
    if confirm and record.ai_decision in {"REJECT", "FLAGGED"}:
        props["risk_flag"] = True  # 确认后强化风险标记，回写进图谱

    try:
        # 参数化：节点 id 与属性均以参数传入，UNWIND 批量写，无 Cypher 注入面
        rows = db.execute_query(
            "UNWIND $ids AS id MATCH (n {id: id}) SET n += $props RETURN count(n) AS updated",
            {"ids": nodes, "props": props},
        )
        updated = int(rows[0]["updated"]) if rows else 0
        return {"written": updated == len(nodes), "nodes": updated,
                "human_review": props["human_review"],
                **({"reason": "部分节点已不存在"} if updated != len(nodes) else {})}
    except Exception as exc:  # noqa: BLE001 —— 写图失败不应阻断审计落盘
        return {"written": False, "reason": str(exc)}


# ======================================================================
# 记录反馈（审计 + 评测集回写 + 图回写 三合一）
# ======================================================================
def record_feedback(
    record: FeedbackRecord,
    db: Any = None,
    audit_path: Optional[Path] = None,
    labels_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    处理一次人工反馈：① 追加审计日志；② 沉淀人工标签样本；③ 回写图谱。

    Args:
        record:      反馈记录。
        db:          可选 Neo4j 单例（缺省只落文件不写图）。
        audit_path:  审计日志路径（缺省 AUDIT_LOG_PATH，测试可注入临时路径）。
        labels_path: 人工标签路径（缺省 HUMAN_LABELS_PATH，测试可注入临时路径）。

    Returns:
        处理结果汇总，含 audit / label / graph 三个子结果。
    """
    audit_path = audit_path or AUDIT_LOG_PATH
    labels_path = labels_path or HUMAN_LABELS_PATH

    # ① 审计日志（append-only）
    _append_jsonl(audit_path, record.to_dict())

    # ② 模拟案例绝不进入可供再评测的人工标签池；驳回也不能沿用 AI 的错误节点。
    label_appended = record.source == "neo4j"
    if label_appended:
        _append_jsonl(labels_path, {
            "investigation_id": record.investigation_id,
            "source": record.source,
            "query": record.query,
            "ai_decision": record.ai_decision,
            "expected_decision": record.ai_decision if record.human_decision == HUMAN_CONFIRM else None,
            "expected_nodes": list(record.suspicious_nodes or []) if record.human_decision == HUMAN_CONFIRM else [],
            "human_decision": record.human_decision,
            "reviewer": record.reviewer,
            "timestamp": record.timestamp,
        })

    # ③ 回写图谱
    graph_result = write_back_to_graph(db, record)

    return {
        "record": record.to_dict(),
        "audit": {"appended": True},
        "label": {"appended": label_appended},
        "graph": graph_result,
    }


# ======================================================================
# 查询接口
# ======================================================================
def load_audit_log(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取审计日志（最近 limit 条）。"""
    return _read_jsonl(AUDIT_LOG_PATH, limit)


def load_human_labels(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """读取人工标签样本（可并入盲评集做持续校准）。"""
    return _read_jsonl(HUMAN_LABELS_PATH, limit)


# ======================================================================
# 自检（无需 Neo4j / LLM，用临时文件 + FakeDB 校验逻辑）
# ======================================================================
class _FakeDB:
    """记录 execute_query 调用，供自检验证参数化写回。"""
    def __init__(self):
        self.calls: List[tuple] = []

    def execute_query(self, cypher: str, parameters: Optional[Dict] = None):
        self.calls.append((cypher, parameters or {}))
        return [{"updated": len((parameters or {}).get("ids", []))}]


def selftest() -> bool:
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        audit_p = Path(tmp) / "audit.jsonl"
        labels_p = Path(tmp) / "labels.jsonl"

        rec = FeedbackRecord(
            query="查一下 LOOP_A 的资金回环关系",
            ai_decision="REJECT",
            suspicious_nodes=["LOOP_A", "LOOP_ACCT_A"],
            human_decision=HUMAN_CONFIRM,
            reviewer="信贷员-张",
            rationale="确认为资金回环骗贷",
            source="neo4j",
        )
        fake_db = _FakeDB()
        result = record_feedback(rec, db=fake_db, audit_path=audit_p, labels_path=labels_p)

        # ① 审计与标签各追加一条
        audit_rows = _read_jsonl(audit_p)
        label_rows = _read_jsonl(labels_p)
        ok &= len(audit_rows) == 1 and len(label_rows) == 1
        ok &= audit_rows[0]["human_decision"] == HUMAN_CONFIRM
        ok &= label_rows[0]["expected_nodes"] == ["LOOP_A", "LOOP_ACCT_A"]
        print(f"[selftest] 审计/标签落盘: audit={len(audit_rows)} label={len(label_rows)}")

        # ② 图回写：参数化 + confirm 时强化 risk_flag
        ok &= result["graph"]["written"] is True
        cypher, params = fake_db.calls[0]
        ok &= "UNWIND $ids" in cypher and params["props"].get("risk_flag") is True
        ok &= params["ids"] == ["LOOP_A", "LOOP_ACCT_A"]
        print(f"[selftest] 图回写: {result['graph']}  params.risk_flag={params['props'].get('risk_flag')}")

        # ③ 驳回不强化 risk_flag
        rec2 = FeedbackRecord(
            query="审查 COMP_0000 的信贷风险", ai_decision="FLAGGED",
            suspicious_nodes=["COMP_0000"], human_decision=HUMAN_REJECT,
            source="neo4j",
        )
        fake_db2 = _FakeDB()
        record_feedback(rec2, db=fake_db2, audit_path=audit_p, labels_path=labels_p)
        _, params2 = fake_db2.calls[0]
        ok &= params2["props"].get("human_review") == "rejected"
        ok &= "risk_flag" not in params2["props"]
        print(f"[selftest] 驳回不强化: human_review={params2['props'].get('human_review')} risk_flag={'risk_flag' in params2['props']}")

    print("[selftest] 人在回路闭环 -> " + ("通过" if ok else "失败"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
