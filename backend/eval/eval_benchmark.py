"""
backend/eval/eval_benchmark.py
==============================
内部合成场景冒烟测试与消融实验。

设计定位
--------
本模块仅检查三条由项目自身生成的合成场景，不是独立真实数据评测。
真实链上交易独立评测见 elliptic_benchmark.py；它也不验证企业信贷工作流。
本模块提供：

    1. 内部靶点集（BLIND_EVAL_SET 为兼容保留的旧变量名）：预置答案的查询集，含正向（资金回环 /
       自融穿透）与负向（正常公司）样本，标注期望决策与期望可疑节点 id；
    2. 四组对比配置（对应 §12.2「纯规则 Cypher / 纯 LLM / 完整系统 / 消融」）：
         - rule-only   ：确定性图算法（零 LLM 调用），与完整系统共享同一份检索证据；
         - llm-only    ：单次朴素 LLM 调用（无图谱、无多 Agent、无法条白名单）；
         - full-system ：完整系统（图谱 grounding + 多 Agent + 法条白名单）；
         - ablation    ：完整系统关闭图谱 grounding（grounding=False），量化图谱增益；
    3. 指标（compute_metrics，纯函数）：决策准确率 / 风险命中准确率 / 节点级
       Precision·Recall·F1 / 法条引用真实率。

运行方式
--------
    # 纯逻辑自检（无需 Neo4j / LLM，校验规则基线与指标计算）
    python backend/eval/eval_benchmark.py --selftest

    # 完整评测（需 Neo4j 已灌入沙盒数据 + .env 已配 LLM）
    python backend/eval/eval_benchmark.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 将 backend/ 加入 sys.path，便于独立运行本模块时导入 agents / rag 组件
sys.path.append(str(Path(__file__).resolve().parents[1]))

from rag.graph_extractor import build_llm  # noqa: E402
from rag.spatiotemporal_aml import run_deterministic_detection  # noqa: E402
from agents.workflow import run_risk_control  # noqa: E402


# ======================================================================
# 内部合成靶点集：答案依据同一生成器硬编码，不具独立性
# ======================================================================
# 每个样本：query（自然语言）、expected_decision（期望裁决）、
#           expected_nodes（期望被标记的可疑节点 id，负样本为空列表）。
# 靶点 id 来源：
#   - 模式A 资金回环：LOOP_A/B/C、LOOP_ACCT_A/B/C、TXN_LOOP_1/2/3
#   - 模式B 自融穿透：PERSON_BOSS、SHELL_MIDDLE、SHELL_X/Y、SHELL_ACCT_X/Y
#   - 负样本：COMP_0000（正常公司，随机流水，无注入风险拓扑）
BLIND_EVAL_SET: List[Dict[str, Any]] = [
    {
        "id": "case_loop",
        "query": "查一下 LOOP_A 的资金回环关系，并评估其风险",
        "expected_decision": "REJECT",
        "expected_nodes": [
            "LOOP_A", "LOOP_B", "LOOP_C",
            "LOOP_ACCT_A", "LOOP_ACCT_B", "LOOP_ACCT_C",
        ],
        "desc": "模式A：虚假贸易 / 资金回环",
    },
    {
        "id": "case_ubo",
        "query": "分析陈志强的自融风险",
        "expected_decision": "REJECT",
        "expected_nodes": [
            "PERSON_BOSS", "SHELL_MIDDLE", "SHELL_X", "SHELL_Y",
            "SHELL_ACCT_X", "SHELL_ACCT_Y",
        ],
        "desc": "模式B：影子实控人自融穿透",
    },
    {
        "id": "case_normal",
        "query": "审查 COMP_0000 的信贷风险",
        "expected_decision": "NEEDS_EVIDENCE",
        "expected_nodes": [],
        "desc": "负样本：正常公司（无注入风险拓扑）",
    },
]


# ======================================================================
# 统一预测结构
# ======================================================================
@dataclass
class Prediction:
    """四种配置产出统一的预测结果，便于横向对比与指标计算。"""
    source: str                       # 配置名：rule-only / llm-only / full-system / ablation
    decision: str = ""                # REJECT / FLAGGED / APPROVE / NEEDS_EVIDENCE
    suspicious_nodes: List[str] = field(default_factory=list)
    citations_valid: int = 0          # 合法法条引用数（仅 full-system / ablation 产出）
    citations_invalid: int = 0        # 非法法条引用数（幻觉引用）
    runtime_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "decision": self.decision,
            "suspicious_nodes": self.suspicious_nodes,
            "citations_valid": self.citations_valid,
            "citations_invalid": self.citations_invalid,
            "runtime_ms": round(self.runtime_ms, 1),
        }


# ======================================================================
# 配置一：纯规则（rule-only）—— 确定性图算法，零 LLM 调用
# ======================================================================
def run_rule_baseline(graph_json: Dict[str, Any]) -> Prediction:
    """
    纯规则基线：在给定子图上执行确定性图算法，识别风险拓扑。

    与完整系统共享同一份检索证据（graph_json），唯一变量是「决策层」：
    这里是确定性的图算法，全程零 LLM 调用，用于回答「规则能兜住多少」。

    检测逻辑复用 rag.spatiotemporal_aml.run_deterministic_detection（与完整系统的
    图谱 grounding 同源），保证「规则基线」与「完整系统」的确定性证据完全一致。
    """
    det = run_deterministic_detection(graph_json)
    suspicious = set(det["suspicious_nodes"])
    decision = "REJECT" if suspicious else "NEEDS_EVIDENCE"
    return Prediction(
        source="rule-only",
        decision=decision,
        suspicious_nodes=sorted(suspicious),
    )


# ======================================================================
# 配置二：纯 LLM（llm-only）—— 无图谱、无多 Agent、无法条白名单
# ======================================================================
LLM_ONLY_PROMPT = """你是一名银行风控审批官。请仅凭你的常识与专业知识，判断下面的融资申请是否存在洗钱、骗贷或自融风险。

【融资申请】
{query}

【输出要求】严格只输出两行，不要输出任何解释：
决策: <REJECT / FLAGGED / APPROVE / NEEDS_EVIDENCE 四选一>
可疑主体: <逗号分隔的主体标识；若不确定或无则写 无>
"""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = build_llm()
    return _llm


def run_llm_only(query: str) -> Prediction:
    """
    纯 LLM 基线：单次朴素 LLM 调用，不提供图谱证据，也不走多 Agent 与法条白名单。

    这是「最弱」的对照：模型只能凭参数记忆猜测，既无法锚定真实节点 id，
    也可能编造法条 / 主体，用于凸显图谱 grounding 与反幻觉机制的价值。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    t0 = time.perf_counter()
    resp = _get_llm().invoke([
        SystemMessage(content="你是严谨的银行风控审批官。"),
        HumanMessage(content=LLM_ONLY_PROMPT.format(query=query)),
    ])
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    runtime_ms = (time.perf_counter() - t0) * 1000

    decision = ""
    m = re.search(r"决策[:：]\s*(REJECT|FLAGGED|APPROVE|NEEDS_EVIDENCE)", text)
    if m:
        decision = m.group(1)

    nodes: List[str] = []
    m2 = re.search(r"可疑主体[:：]\s*(.+)", text)
    if m2 and "无" not in m2.group(1):
        # 提取形如 LOOP_A / PERSON_BOSS / SHELL_X / COMP_0000 的主体标识
        nodes = re.findall(r"[A-Z][A-Z0-9_]{2,}", m2.group(1))

    return Prediction(
        source="llm-only",
        decision=decision,
        suspicious_nodes=nodes,
        runtime_ms=runtime_ms,
    )


# ======================================================================
# 配置三 / 四：完整系统 vs 消融（grounding 开 / 关）
# ======================================================================
def _from_workflow_result(result: Dict[str, Any], source: str, runtime_ms: float) -> Prediction:
    """把 run_risk_control 的返回状态映射为统一 Prediction。"""
    citations = result.get("compliance_citations") or {}
    return Prediction(
        source=source,
        decision=result.get("decision", ""),
        suspicious_nodes=list(result.get("suspicious_nodes", []) or []),
        citations_valid=len(citations.get("valid", []) or []),
        citations_invalid=len(citations.get("invalid", []) or []),
        runtime_ms=runtime_ms,
    )


def run_full_system(query: str) -> Prediction:
    """完整系统：图谱 grounding + 多 Agent + 法条白名单。"""
    t0 = time.perf_counter()
    result = run_risk_control(query, grounding=True)
    return _from_workflow_result(result, "full-system", (time.perf_counter() - t0) * 1000)


def run_ablation(query: str) -> Prediction:
    """消融：完整系统关闭图谱 grounding（grounding=False），量化图谱增益。"""
    t0 = time.perf_counter()
    result = run_risk_control(query, grounding=False)
    return _from_workflow_result(result, "ablation", (time.perf_counter() - t0) * 1000)


# ======================================================================
# 指标计算（纯函数，可独立单测）
# ======================================================================
def _is_risky(decision: str) -> bool:
    """REJECT / FLAGGED 均视为「命中风险」，APPROVE 视为「未命中」。"""
    return decision in ("REJECT", "FLAGGED")


def compute_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    由一批 {expected_decision, expected_nodes, prediction} 计算综合指标。

    指标说明：
    - decision_accuracy   : 裁决精确匹配率（REJECT/FLAGGED/APPROVE 完全一致）；
    - risk_accuracy       : 二分类「有风险 / 无风险」命中率（更稳健，容忍 REJECT↔FLAGGED）；
    - node_precision/recall/f1 : 节点级微平均，衡量「可疑节点定位」质量；
    - citation_truth_rate : 法条引用真实率 = valid / (valid + invalid)；
    """
    n = len(results)
    if n == 0:
        return {}

    exact = 0
    tp = tn = fp = fn = 0
    tp_nodes = fp_nodes = fn_nodes = 0
    valid_c = invalid_c = 0

    for r in results:
        pred: Prediction = r["prediction"]
        exp_dec = r["expected_decision"]
        exp_nodes = set(r["expected_nodes"] or [])
        pred_nodes = set(pred.suspicious_nodes or [])

        if pred.decision == exp_dec:
            exact += 1

        exp_risky = _is_risky(exp_dec)
        pred_risky = _is_risky(pred.decision)
        if exp_risky and pred_risky:
            tp += 1
        elif exp_risky and not pred_risky:
            fn += 1
        elif not exp_risky and pred_risky:
            fp += 1
        else:
            tn += 1

        # 节点级（负样本误标可疑节点会拉低 precision，正是要暴露的「过度告警」）
        tp_nodes += len(pred_nodes & exp_nodes)
        fp_nodes += len(pred_nodes - exp_nodes)
        fn_nodes += len(exp_nodes - pred_nodes)

        valid_c += pred.citations_valid
        invalid_c += pred.citations_invalid

    node_precision = tp_nodes / (tp_nodes + fp_nodes) if (tp_nodes + fp_nodes) else 0.0
    node_recall = tp_nodes / (tp_nodes + fn_nodes) if (tp_nodes + fn_nodes) else 0.0
    node_f1 = (2 * node_precision * node_recall / (node_precision + node_recall)
               if (node_precision + node_recall) else 0.0)

    total_c = valid_c + invalid_c
    citation_truth_rate = (valid_c / total_c) if total_c else None

    return {
        "n": n,
        "decision_accuracy": exact / n,
        "risk_accuracy": (tp + tn) / n,
        "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
        "node_precision": node_precision,
        "node_recall": node_recall,
        "node_f1": node_f1,
        "citation_truth_rate": citation_truth_rate,
    }


# ======================================================================
# 自检（--selftest）：无需 Neo4j / LLM，校验纯逻辑
# ======================================================================
def _synthetic_graph() -> Dict[str, Any]:
    """构造覆盖模式 A（回环）与模式 B（自融）的最小合成子图。"""
    def node(nid, labels, **props):
        return {"id": nid, "labels": labels, "properties": props}

    def edge(s, t, etype, **props):
        return {"id": f"{s}->{t}", "type": etype, "source": s, "target": t, "properties": props}

    return {
        "nodes": [
            node("LOOP_A", ["Company"], risk_flag=True),
            node("LOOP_B", ["Company"], risk_flag=True),
            node("LOOP_C", ["Company"], risk_flag=True),
            node("LOOP_ACCT_A", ["Account"]),
            node("LOOP_ACCT_B", ["Account"]),
            node("LOOP_ACCT_C", ["Account"]),
            node("PERSON_BOSS", ["Person"], risk_flag=True),
            node("SHELL_MIDDLE", ["Company"], risk_flag=True),
            node("SHELL_X", ["Company"], risk_flag=True),
            node("SHELL_Y", ["Company"], risk_flag=True),
            node("SHELL_ACCT_X", ["Account"]),
            node("SHELL_ACCT_Y", ["Account"]),
        ],
        "edges": [
            edge("LOOP_A", "LOOP_ACCT_A", "HAS_ACCOUNT"),
            edge("LOOP_B", "LOOP_ACCT_B", "HAS_ACCOUNT"),
            edge("LOOP_C", "LOOP_ACCT_C", "HAS_ACCOUNT"),
            edge("LOOP_ACCT_A", "LOOP_ACCT_B", "TRANSFER", amount=5_000_000, date="2024-05-10T10:00:00"),
            edge("LOOP_ACCT_B", "LOOP_ACCT_C", "TRANSFER", amount=4_980_000, date="2024-05-10T13:30:00"),
            edge("LOOP_ACCT_C", "LOOP_ACCT_A", "TRANSFER", amount=5_020_000, date="2024-05-10T17:00:00"),
            edge("PERSON_BOSS", "SHELL_MIDDLE", "OWNS"),
            edge("SHELL_MIDDLE", "SHELL_X", "OWNS"),
            edge("SHELL_MIDDLE", "SHELL_Y", "OWNS"),
            edge("SHELL_X", "SHELL_ACCT_X", "HAS_ACCOUNT"),
            edge("SHELL_Y", "SHELL_ACCT_Y", "HAS_ACCOUNT"),
            edge("SHELL_ACCT_X", "SHELL_ACCT_Y", "TRANSFER", amount=20_000_000, date="2024-05-10T11:00:00"),
            edge("SHELL_ACCT_Y", "SHELL_ACCT_X", "TRANSFER", amount=18_000_000, date="2024-05-10T11:05:00"),
        ],
    }


def selftest() -> bool:
    """校验规则基线 + 指标计算的纯逻辑；返回是否全部通过。"""
    ok = True
    g = _synthetic_graph()

    # 1) 规则基线应命中回环三账户 + 自融实控人链路
    pred = run_rule_baseline(g)
    expected_loop = {"LOOP_A", "LOOP_B", "LOOP_C", "LOOP_ACCT_A", "LOOP_ACCT_B", "LOOP_ACCT_C"}
    expected_ubo = {"PERSON_BOSS", "SHELL_MIDDLE", "SHELL_X", "SHELL_Y", "SHELL_ACCT_X", "SHELL_ACCT_Y"}
    got = set(pred.suspicious_nodes)
    loop_ok = expected_loop <= got
    ubo_ok = expected_ubo <= got
    ok &= loop_ok and ubo_ok
    print(f"[selftest] 规则基线 命中回环: {loop_ok}  命中自融: {ubo_ok}  决策: {pred.decision}")

    # 2) 指标计算：完美预测 → risk_accuracy=1、node_f1=1
    perfect = [
        {
            "expected_decision": "REJECT",
            "expected_nodes": list(expected_loop | expected_ubo),
            "prediction": Prediction(source="x", decision="REJECT",
                                     suspicious_nodes=list(expected_loop | expected_ubo)),
        },
        {
            "expected_decision": "APPROVE",
            "expected_nodes": [],
            "prediction": Prediction(source="x", decision="APPROVE", suspicious_nodes=[]),
        },
    ]
    m = compute_metrics(perfect)
    perf_ok = (m["risk_accuracy"] == 1.0 and m["decision_accuracy"] == 1.0
               and m["node_f1"] == 1.0)
    ok &= perf_ok
    print(f"[selftest] 完美预测指标: risk_acc={m['risk_accuracy']}  node_f1={m['node_f1']}  -> {perf_ok}")

    # 3) 幻觉引用 → citation_truth_rate 正确下降
    m2 = compute_metrics([
        {
            "expected_decision": "REJECT",
            "expected_nodes": ["LOOP_A"],
            "prediction": Prediction(source="x", decision="REJECT", suspicious_nodes=["LOOP_A"],
                                     citations_valid=2, citations_invalid=1),
        },
    ])
    cite_ok = abs(m2["citation_truth_rate"] - 2 / 3) < 1e-9
    ok &= cite_ok
    print(f"[selftest] 引用真实率 2/(2+1)={m2['citation_truth_rate']}  -> {cite_ok}")

    return ok


# ======================================================================
# 主流程
# ======================================================================
def _print_table(rows: List[Dict[str, Any]]) -> None:
    """打印四配置 × 三样本的决策对照表。"""
    header = f"{'样本':<14}{'配置':<14}{'裁决':<10}{'可疑节点数':<8}{'命中靶点':<8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        case = r["case"]
        pred: Prediction = r["prediction"]
        exp_nodes = set(r["expected_nodes"] or [])
        hit = len(exp_nodes & set(pred.suspicious_nodes))
        total = len(exp_nodes)
        print(f"{case:<14}{pred.source:<14}{pred.decision:<10}"
              f"{len(pred.suspicious_nodes):<8}{hit}/{total}")


def main() -> None:
    parser = argparse.ArgumentParser(description="内部合成场景冒烟测试与消融实验，非独立验证")
    parser.add_argument("--selftest", action="store_true", help="纯逻辑自检（无需 Neo4j / LLM）")
    parser.add_argument("--case", default=None, help="仅运行指定样本（case_loop / case_ubo / case_normal）")
    args = parser.parse_args()

    if args.selftest:
        ok = selftest()
        print("\n" + ("✅ 自检全部通过" if ok else "❌ 自检存在失败项"))
        sys.exit(0 if ok else 1)

    cases = BLIND_EVAL_SET
    if args.case:
        cases = [c for c in BLIND_EVAL_SET if c["id"] == args.case]
        if not cases:
            print(f"未知样本：{args.case}（可选 {[c['id'] for c in BLIND_EVAL_SET]}）")
            sys.exit(2)

    print("== 三条内部合成场景冒烟测试；结果不得作为真实场景准确率 ==")
    print("配置：rule-only（纯规则）/ llm-only（纯LLM）/ full-system（完整）/ ablation（去图谱）\n")

    # 每个样本跑四组配置；full-system 只跑一次，其 graph_json 同时作为规则基线的共享证据。
    per_source: Dict[str, List[Dict[str, Any]]] = {
        "rule-only": [], "llm-only": [], "full-system": [], "ablation": [],
    }

    def _append(source: str, case: Dict[str, Any], pred: Prediction) -> None:
        per_source[source].append({
            "case": case["id"],
            "expected_decision": case["expected_decision"],
            "expected_nodes": case["expected_nodes"],
            "prediction": pred,
        })

    for case in cases:
        cid, query = case["id"], case["query"]
        print(f"\n▶ 样本 [{cid}]：{case['desc']}")

        # 1) 完整系统（grounding=True），graph_json 供规则基线复用
        t0 = time.perf_counter()
        try:
            full_result = run_risk_control(query, grounding=True)
            shared_graph = full_result.get("graph_json", {"nodes": [], "edges": []})
            full_pred = _from_workflow_result(full_result, "full-system", (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            shared_graph = {"nodes": [], "edges": []}
            full_pred = Prediction(source="full-system", decision="")
            print(f"  ⚠ full-system 执行失败：{e}")
        _append("full-system", case, full_pred)

        # 2) 纯规则（与完整系统共享同一证据，零 LLM）
        t0 = time.perf_counter()
        rule_pred = run_rule_baseline(shared_graph)
        rule_pred.runtime_ms = (time.perf_counter() - t0) * 1000
        _append("rule-only", case, rule_pred)

        # 3) 纯 LLM（无图谱、无多 Agent）
        try:
            llm_pred = run_llm_only(query)
        except Exception as e:  # noqa: BLE001
            llm_pred = Prediction(source="llm-only", decision="")
            print(f"  ⚠ llm-only 执行失败：{e}")
        _append("llm-only", case, llm_pred)

        # 4) 消融（完整系统关闭图谱 grounding）
        t0 = time.perf_counter()
        try:
            ablation_result = run_risk_control(query, grounding=False)
            ablation_pred = _from_workflow_result(ablation_result, "ablation", (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            ablation_pred = Prediction(source="ablation", decision="")
            print(f"  ⚠ ablation 执行失败：{e}")
        _append("ablation", case, ablation_pred)

        # 逐样本即时打印
        _print_table([
            {"case": cid, "expected_nodes": case["expected_nodes"], "prediction": full_pred},
            {"case": cid, "expected_nodes": case["expected_nodes"], "prediction": rule_pred},
            {"case": cid, "expected_nodes": case["expected_nodes"], "prediction": llm_pred},
            {"case": cid, "expected_nodes": case["expected_nodes"], "prediction": ablation_pred},
        ])

    # 各配置的指标
    print("\n== 指标汇总 ==")
    for name in ("rule-only", "llm-only", "full-system", "ablation"):
        results = per_source[name]
        if not results:
            continue
        m = compute_metrics(results)
        cite = m["citation_truth_rate"]
        cite_s = f"{cite:.3f}" if cite is not None else "N/A"
        print(
            f"[{name:<12}] 决策准确率={m['decision_accuracy']:.3f}  "
            f"风险命中率={m['risk_accuracy']:.3f}  "
            f"节点 P/R/F1={m['node_precision']:.2f}/{m['node_recall']:.2f}/{m['node_f1']:.2f}  "
            f"引用真实率={cite_s}"
        )


if __name__ == "__main__":
    main()
