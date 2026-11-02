"""
backend/agents/workflow.py
==========================
多 Agent 风控决策工作流（LangGraph 编排）——本项目「大脑」。

拓扑结构
--------
        START
          │
     retrieve_graph    (GraphRAG 抽取子图)
          │
   ┌──────┴──────┐     (Fan-out 并行分发)
   ▼             ▼
 risk_agent   compliance_agent
   │             │
   └──────┬──────┘     (Fan-in 汇聚)
          ▼
      decision
          │
         END

三个 Agent 各司其职：
- 风控 Agent  ：关注供应链集中度、空壳欺诈、资金链断裂等信用/经营风险；
- 合规 Agent  ：关注 AML 反洗钱、自融穿透(UBO)、虚假回环、设备聚集等合规风险；
- 决策 Agent  ：汇总两者意见，输出最终裁决（REJECT/FLAGGED/APPROVE），并在末尾
               附带结构化 JSON（decision + suspicious_nodes + suspicious_edges_types），
               供前端对可疑节点/边高亮，实现可解释性 XAI。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

# 将 backend/ 加入 sys.path，便于独立运行本模块时导入 database 与 rag 组件
sys.path.append(str(Path(__file__).resolve().parents[1]))

from agents.prompts import (  # noqa: E402
    COMPLIANCE_AGENT_PROMPT,
    DECISION_AGENT_PROMPT,
    RISK_AGENT_PROMPT,
)
from rag.graph_extractor import (  # noqa: E402
    GraphExtractor,
    build_llm,
    sanitize_enabled,
    sanitize_subgraph,
)
from rag.legal_registry import (  # noqa: E402
    detect_risk_labels,
    retrieve_clauses,
    verify_citations,
)
from rag.spatiotemporal_aml import (  # noqa: E402
    format_deterministic_summary,
    run_deterministic_detection,
)
from rag.risk_scoring import RiskScoringEngine, format_risk_scores  # noqa: E402


# ======================================================================
# 全局状态定义
# ======================================================================
class RiskControlState(TypedDict, total=False):
    """
    贯穿全流程的共享状态（total=False 允许各节点做局部更新，LangGraph 自动合并）。

    字段说明：
        query                  : 用户自然语言查询
        cypher                 : retrieve 阶段生成的 Cypher（用于 XAI 追溯与审计）
        graph_json             : 抽取出的子图 {nodes, edges}
        risk_report            : 风控 Agent 审查报告
        compliance_report      : 合规 Agent 审查报告
        final_decision         : 决策 Agent 输出的完整文本（含 Markdown 摘要 + JSON）
        decision               : 解析出的最终裁决（REJECT / FLAGGED / APPROVE）
        suspicious_nodes       : 解析出的高危节点 id 列表，供前端高亮
        suspicious_edges_types : 解析出的高危关系类型列表，供前端高亮
    """
    query: str
    grounding: bool          # 是否启用图谱 grounding（消融实验置 False 以量化图谱增益）
    cypher: str
    graph_json: Dict[str, Any]
    risk_report: str
    compliance_report: str
    final_decision: str
    decision: str
    risk_score: Optional[float]    # 综合风险评分 0~100（越高越危险），供前端分级展示；解析失败为 None
    confidence: Optional[float]    # 决策置信度 0~1（决策 Agent 自评），供前端分级展示；解析失败为 None
    suspicious_nodes: List[str]
    suspicious_edges_types: List[str]
    compliance_citations: Dict[str, Any]       # 合规法条引用校验结果 {valid, invalid}（创新点一）
    compliance_evidence: List[Dict[str, Any]]  # 合规结论的证据列表（edge_id/金额/模式，已回填真实值）
    compliance_risk_labels: str                # 合规结论的风险定性文本
    aml_findings: List[Dict[str, Any]]         # 确定性时空聚集检测结果（创新点二，非 LLM）
    deterministic_nodes: List[str]             # 统一确定性检测（risk_flag+回环+自融+时空聚集）命中的高危节点
    deterministic_summary: str                 # 确定性检测的文本锚点（供决策 Agent 作为权威证据）
    risk_scores: Dict[str, float]              # 初步风险评分（GraphSAGE 模拟 · 指令1）{node_id: 0~100}


# ======================================================================
# 模块级懒加载单例：复用抽取器与 LLM，避免每次调用重复建连
# ======================================================================
_extractor: GraphExtractor | None = None
_llm = None


def _get_llm():
    """惰性获取 LLM 客户端（风控/合规/决策三 Agent 复用同一实例，线程安全）。"""
    global _llm
    if _llm is None:
        _llm = build_llm()
    return _llm


def _get_extractor() -> GraphExtractor:
    """惰性获取图谱抽取器，并注入共享 LLM 以复用连接。"""
    global _extractor
    if _extractor is None:
        _extractor = GraphExtractor(llm=_get_llm())
    return _extractor


_scorer: RiskScoringEngine | None = None


def _get_scorer() -> RiskScoringEngine:
    """惰性获取风险评分引擎（GraphSAGE 模拟，纯函数式、线程安全，可并发复用）。"""
    global _scorer
    if _scorer is None:
        _scorer = RiskScoringEngine()
    return _scorer


def _invoke_llm(system_prompt: str, user_content: str) -> str:
    """以「系统提示词 + 用户内容」调用 LLM，返回纯文本输出。"""
    resp = _get_llm().invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ])
    return resp.content if isinstance(resp.content, str) else str(resp.content)


def _format_graph(graph_json: Dict[str, Any], max_chars: int = 8000) -> str:
    """将子图序列化为 JSON 字符串供 LLM 阅读；送 LLM 前先做字段级脱敏，超长则截断。"""
    if sanitize_enabled():
        graph_json = sanitize_subgraph(graph_json)
    text = json.dumps(graph_json, ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(截断，子图过大)"
    return text


def _find_json_object(text: str, key: str = '"decision"') -> str | None:
    """在文本中定位首个包含指定 key 的 JSON 对象（平衡花括号扫描，容忍换行）。"""
    idx = text.find(key)
    if idx == -1:
        return None
    start = text.rfind("{", 0, idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_decision_json(decision_text: str) -> Dict[str, Any]:
    """
    从决策 Agent 输出末尾的 JSON 代码块中解析结构化字段。

    预期结构：
        {"decision": "REJECT",
         "risk_score": 92,
         "confidence": 0.95,
         "frontend_highlight": {"suspicious_nodes": [...], "suspicious_edges_types": [...]}}

    优先匹配 ```json ... ``` 代码块；若无代码块，则回退到平衡花括号扫描。
    """
    empty = {
        "decision": "NEEDS_EVIDENCE",
        "risk_score": None,
        "confidence": None,
        "suspicious_nodes": [],
        "suspicious_edges_types": [],
    }

    m = re.search(r"```json\s*(.*?)\s*```", decision_text, re.DOTALL)
    raw = m.group(1) if m else _find_json_object(decision_text)
    if not raw:
        return empty

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return empty
    if not isinstance(data, dict):
        return empty

    highlight = data.get("frontend_highlight", {}) or {}
    if not isinstance(highlight, dict):
        highlight = {}
    decision = str(data.get("decision", "")).strip().upper()
    if decision not in {"REJECT", "FLAGGED", "APPROVE", "NEEDS_EVIDENCE"}:
        return empty
    nodes = highlight.get("suspicious_nodes", [])
    edge_types = highlight.get("suspicious_edges_types", [])
    return {
        "decision": decision,
        "risk_score": None,
        "confidence": None,
        "suspicious_nodes": [str(node) for node in nodes if isinstance(node, str)] if isinstance(nodes, list) else [],
        "suspicious_edges_types": [str(edge) for edge in edge_types if isinstance(edge, str)] if isinstance(edge_types, list) else [],
    }


def _format_legal_clauses(clauses: List[Dict[str, str]]) -> str:
    """把候选法条格式化为可注入 prompt 的文本清单（条款号 + 原文）。"""
    if not clauses:
        return "（无候选法条）"
    return "\n".join(f"- {c['clause_id']}：{c['text']}" for c in clauses)


def _parse_compliance_json(report_text: str) -> Dict[str, Any]:
    """
    从合规 Agent 输出末尾解析结构化 JSON（风险定性 / 证据 / 法条引用）。

    优先匹配 ```json ... ``` 代码块；若无，则回退到按「风险定性」key 的平衡花括号扫描。
    解析失败返回空 dict（上层据此做空引用校验，不会中断流程）。
    """
    m = re.search(r"```json\s*(.*?)\s*```", report_text, re.DOTALL)
    raw = m.group(1) if m else _find_json_object(report_text, '"风险定性"')
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _backfill_evidence(evidence: List[Dict[str, Any]], graph_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    证据金额回填：用 edge_id 在【原始未脱敏】子图中回查真实金额 / 关系类型 / 时间。

    合规 Agent 收到的图谱已做字段级脱敏（金额为 ***），无法读到真实金额；但它能定位
    到具体 edge_id 与风险模式。这里由系统在本地用 edge_id 回填真实金额，使证据
    「可回溯到金额」，而敏感值始终不离开本地、不进入 LLM。
    """
    edges_by_id = {e.get("id"): e for e in (graph_json or {}).get("edges", [])}
    out: List[Dict[str, Any]] = []
    for ev in evidence or []:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("edge_id") or ev.get("边ID") or ev.get("id") or ""
        edge = edges_by_id.get(eid)
        if edge:
            props = edge.get("properties", {}) or {}
            out.append({
                "edge_id": eid,
                "type": edge.get("type", ""),
                "amount": props.get("amount"),
                "date": props.get("date"),
                "模式": ev.get("模式") or ev.get("pattern") or "",
            })
        else:
            out.append(ev)
    return out


# ======================================================================
# 四个节点函数
# ======================================================================
def without_training_labels(graph_json: Dict[str, Any]) -> Dict[str, Any]:
    """Exclude synthetic answer labels before scoring or sending graph data to agents."""
    return {
        **graph_json,
        "nodes": [
            {**node, "properties": {key: value for key, value in (node.get("properties") or {}).items() if key != "risk_flag"}}
            for node in graph_json.get("nodes", [])
        ],
    }


def retrieve_graph_node(state: RiskControlState) -> Dict[str, Any]:
    """
    节点1：调用 Phase 2 的图谱抽取器，将自然语言查询转为子图 JSON。

    当 state 传入 grounding=False（消融实验）时，跳过图谱检索、返回空子图，
    使下游 Agent 失去图谱锚定，用于量化「图谱 grounding 的增益」。
    """
    query = state["query"]
    if state.get("grounding") is False:
        return {"cypher": "", "graph_json": {"nodes": [], "edges": []}}
    result = _get_extractor().extract_subgraph(query)
    return {
        "cypher": result["cypher"],
        "graph_json": without_training_labels(result["subgraph"]),
    }


def risk_analysis_node(state: RiskControlState) -> Dict[str, Any]:
    """节点2a：风控 Agent —— 供应链集中度 / 空壳欺诈 / 资金链断裂。"""
    graph_text = _format_graph(state.get("graph_json", {}))
    user_content = f"当前审查任务：{state.get('query', '')}\n请基于以下图谱数据进行分析：\n{graph_text}"
    report = _invoke_llm(RISK_AGENT_PROMPT, user_content)
    return {"risk_report": report}


def compliance_analysis_node(state: RiskControlState) -> Dict[str, Any]:
    """
    节点2b：合规 Agent —— AML / 自融穿透(UBO) / 虚假回环 / 设备聚集。

    创新点一（可验证合规推理）：先按子图特征定向检索候选法条，随图谱注入 prompt
    形成「引用白名单」；Agent 输出结构化结论后，再用 verify_citations 校验法条引用、
    回填权威原文、剔除非法引用，并对证据做金额回填，从机制上杜绝「引用幻觉」。

    创新点二（时空聚集反洗钱 + 确定性检测）：先用确定性图算法（非 LLM）统一检出
    「等额短时回环 / 自融穿透 / 设备聚集 / 快进快出 / 分散转集」等
    高危账户簇，随图谱一并注入 prompt；合规 Agent 只负责解读「为什么可疑」，从而把
    XAI 链路中「子图 → 结论」的自由文本段替换为确定性证据链。
    """
    graph_json = state.get("graph_json", {})
    graph_text = _format_graph(graph_json)
    query = state.get("query", "")

    # 1) 确定性检索：按 query + 子图特征推断风险标签，检索相关法条（第二个 RAG）
    risk_labels = detect_risk_labels(query, graph_json)
    clauses = retrieve_clauses(risk_labels)
    clause_text = _format_legal_clauses(clauses)

    # 2) 确定性检测（创新点二）：等额短时回环 + 自融穿透 + 时空聚集，均非 LLM
    det = run_deterministic_detection(graph_json)
    aml_findings = det["findings"]  # 时空聚集 findings（前端「时空聚集检测」展示）
    det_summary = format_deterministic_summary(det)

    # 2.5) 初步风险评分（指令1 · GraphSAGE 模拟）：确定性图消息传播，作为 metadata 供 Agent 参考
    risk_scores = _get_scorer().score(graph_json)
    scores_text = format_risk_scores(risk_scores)

    # 3) 组装输入：候选法条清单（引用白名单）+ 确定性检测结果 + 风险评分 随图谱一并交给合规 Agent
    user_content = (
        f"当前审查任务：{query}\n"
        f"请基于以下图谱数据进行分析：\n{graph_text}\n\n"
        f"【候选法条清单】（你的法条引用只能从以下条款中选择，且只写条款号、不要写原文）：\n{clause_text}\n\n"
        f"【确定性检测结果】（图算法检出，供你解读「为什么可疑」，不必重复该检测；未命中时请如实说明无可疑拓扑）：\n{det_summary}\n\n"
        f"【图消息传播启发式排序】（只在当前子图内相对排序，不能视为绝对风险概率；未上榜也不表示安全）：\n{scores_text}"
    )
    report = _invoke_llm(COMPLIANCE_AGENT_PROMPT, user_content)

    # 4) 解析结构化结论 + 校验法条引用（原文以权威库回填，剔除幻觉引用）
    parsed = _parse_compliance_json(report)
    citations = verify_citations(parsed.get("法条", []))

    # 5) 证据金额回填：用 edge_id 在原始子图中回查真实金额，证据可回溯但敏感值不出本地
    evidence = _backfill_evidence(parsed.get("证据", []), graph_json)

    return {
        "compliance_report": report,
        "compliance_citations": citations,
        "compliance_evidence": evidence,
        "compliance_risk_labels": parsed.get("风险定性", ""),
        "aml_findings": aml_findings,
        "deterministic_nodes": det["suspicious_nodes"],
        "deterministic_summary": det_summary,
        "risk_scores": risk_scores,
    }


def decision_node(state: RiskControlState) -> Dict[str, Any]:
    """节点3：决策 Agent —— 汇总 Query + 风控报告 + 合规报告，输出最终裁决。"""
    user_content = (
        f"【用户的初始申请（Query）】\n{state.get('query', '')}\n\n"
        f"【风控 Agent 审查报告】\n{state.get('risk_report', '')}\n\n"
        f"【合规 Agent 审查报告】\n{state.get('compliance_report', '')}\n\n"
        f"【确定性检测证据锚点】（图算法检出，非 LLM 推断，是判断的权威依据）\n"
        f"{state.get('deterministic_summary', '')}\n\n"
        f"【图消息传播启发式排序】（仅供当前子图交叉印证，不能作为绝对风险分）\n"
        f"{format_risk_scores(state.get('risk_scores', {}))}\n\n"
        f"【真实法条】（合规 Agent 引用、并经权威法条库校验通过的条款，仅供你在「监管条文依据」小节引用，"
        f"只写条款号、严禁编造）：\n"
        f"{_format_legal_clauses((state.get('compliance_citations') or {}).get('valid', []))}\n\n"
        "请综合以上资料，做出最终审批决策。"
    )
    decision = _invoke_llm(DECISION_AGENT_PROMPT, user_content)
    parsed = _parse_decision_json(decision)
    return {
        "final_decision": decision,
        "decision": parsed["decision"],
        "risk_score": parsed["risk_score"],
        "confidence": parsed["confidence"],
        "suspicious_nodes": parsed["suspicious_nodes"],
        "suspicious_edges_types": parsed["suspicious_edges_types"],
    }


# ======================================================================
# 组装 StateGraph
# ======================================================================
def _build_graph():
    """构建并编译多 Agent 工作流计算图。"""
    graph = StateGraph(RiskControlState)

    graph.add_node("retrieve", retrieve_graph_node)
    graph.add_node("risk", risk_analysis_node)
    graph.add_node("compliance", compliance_analysis_node)
    graph.add_node("decision", decision_node)

    # 顺序：START → retrieve
    graph.add_edge(START, "retrieve")
    # Fan-out：retrieve 完成后并行分发到 risk 与 compliance
    graph.add_edge("retrieve", "risk")
    graph.add_edge("retrieve", "compliance")
    # Fan-in：risk 与 compliance 均完成后汇聚到 decision
    graph.add_edge("risk", "decision")
    graph.add_edge("compliance", "decision")
    # 结束：decision → END
    graph.add_edge("decision", END)

    return graph.compile()


# 模块加载时编译一次，后续复用（编译后的图是线程安全的，可供 FastAPI 并发调用）
app = _build_graph()


def run_risk_control(query: str, grounding: bool = True) -> Dict[str, Any]:
    """
    对外统一入口：给定自然语言查询，驱动多 Agent 完成「检索 → 分析 → 决策」全流程。

    Args:
        query:     用户自然语言查询，如「查一下公司A的资金回环关系」。
        grounding: 是否启用图谱检索锚定（True 默认；False 为消融实验，Agent 无图谱可依）。

    Returns:
        最终状态字典，含 graph_json / cypher / risk_report / compliance_report /
        final_decision / decision / risk_score / confidence /
        suspicious_nodes / suspicious_edges_types 等字段。
    """
    return app.invoke({"query": query, "grounding": grounding})
