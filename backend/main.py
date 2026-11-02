"""
backend/main.py
===============
FastAPI 服务入口 —— 将 Phase 3 的多 Agent 风控决策工作流「服务化」。

Phase 4 交付内容
----------------
1. CORS 中间件（开发环境放开所有来源）；
2. GET  /api/graph/schema —— 返回节点/边类型元数据，供前端渲染图例；
3. GET  /api/graph/sample  —— 返回全局/部分全景图数据（AntV G6 {nodes, edges} 格式）；
4. POST /api/analyze      —— 核心审批流接口，以 SSE 实时推送 LangGraph 各节点的执行
                             进度与最终决策（含前端高亮 JSON），实现「思维流 + 图谱高亮」联动。

启动方式（在项目根目录执行）：
    uvicorn backend.main:app --reload

设计说明
--------
- 本模块通过 `sys.path` 扁平导入 database / agents / rag，与既有代码风格保持一致；
- LangGraph 编译图、Neo4j Driver、LLM 客户端均为「惰性初始化」，模块导入阶段
  不会真正连库或发起 LLM 请求，因此 uvicorn 启动本身不依赖 Neo4j / LLM 就绪；
- SSE 采用 sse-starlette，yield 的 `{event, data}` 字典会被自动编码为 SSE 事件。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterator, List
import time
import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

# 将 backend/ 加入 sys.path，便于扁平导入 database / agents / rag 等模块
sys.path.append(str(Path(__file__).resolve().parent))

from agents.human_loop import (  # noqa: E402
    HUMAN_CONFIRM,
    HUMAN_REJECT,
    FeedbackRecord,
    load_audit_log,
    record_feedback,
)
from agents.workflow import app as risk_workflow  # noqa: E402  Phase 3 编译好的 LangGraph
from database import Neo4jDatabase  # noqa: E402
from demo_graph import expand_demo_graph, get_demo_case, get_demo_graph  # noqa: E402
from public_investigation import investigate_public_entity, load_public_case  # noqa: E402
from review_store import create_investigation, pending_investigation  # noqa: E402
from rag.graph_extractor import serialize_graph_result  # noqa: E402
from backend.banking import router as banking_router  # noqa: E402
from backend.banking_plans import background_plan_worker, router as banking_plan_router  # noqa: E402
from backend.banking_wealth import router as banking_wealth_router  # noqa: E402
from backend.banking_auth import router as banking_auth_router  # noqa: E402
from backend.banking_missions import router as banking_mission_router  # noqa: E402


# ======================================================================
# 图例元数据（供前端渲染节点/边的图例与配色）
# ======================================================================
NODE_TYPE_META: List[Dict[str, Any]] = [
    {"label": "Company", "name": "公司", "color": "#5B8FF9",
     "properties": ["id", "name", "industry", "registered_capital", "established_date", "risk_flag"]},
    {"label": "Person", "name": "自然人", "color": "#61DDAA",
     "properties": ["id", "name", "id_number", "phone", "address", "risk_flag"]},
    {"label": "Account", "name": "账户", "color": "#F6BD16",
     "properties": ["id", "account_no", "bank", "balance", "opened_date"]},
    {"label": "Device", "name": "设备", "color": "#9270CA",
     "properties": ["id", "ip", "mac", "device_type", "os"]},
]

EDGE_TYPE_META: List[Dict[str, Any]] = [
    {"type": "OWNS", "name": "持股", "color": "#5B8FF9", "properties": ["ratio"]},
    {"type": "CONTROLLING_SHAREHOLDER", "name": "控股股东", "color": "#81C995", "properties": ["ratio", "share_count"]},
    {"type": "DISCLOSED_HOLDING", "name": "报告期末持股", "color": "#81C995", "properties": ["ratio", "share_count"]},
    {"type": "GROUP_SUBSIDIARY", "name": "集团子公司", "color": "#8AB4F8", "properties": ["ratio"]},
    {"type": "PARENT_INVESTMENT", "name": "母公司投资账面值", "color": "#C58AF9", "properties": ["book_value_thousand_cny"]},
    {"type": "LEGAL_REP", "name": "法人代表", "color": "#61DDAA", "properties": []},
    {"type": "TRANSFER", "name": "转账流水", "color": "#F6BD16", "properties": ["amount", "date"]},
    {"type": "HAS_ACCOUNT", "name": "开户关系", "color": "#9270CA", "properties": []},
    {"type": "LOGGED_IN", "name": "登录关系", "color": "#FF9845", "properties": ["date"]},
]


# ======================================================================
# FastAPI 应用与 CORS
# ======================================================================
app = FastAPI(
    title="穿透式金融风控与决策引擎",
    version="0.4.0",
    description="基于多智能体协同与 GraphRAG 的穿透式金融风控系统 · Phase 4 服务化接口",
)
from backend import banking_statements, banking_fx  # noqa: E402,F401
from backend import banking_external, banking_plaid  # noqa: E402,F401
from backend import banking_evidence  # noqa: E402,F401; register bounded graph routes
from backend import banking_readiness  # noqa: E402,F401; local diagnostics before router inclusion
from backend import banking_mission_evidence  # noqa: E402,F401; task-scoped provenance routes
from backend import banking_replanning  # noqa: E402,F401; source-change preview and explicit recovery
from backend import banking_entities  # noqa: E402,F401; public exact-LEI registry lookup
from backend import banking_identity  # noqa: E402,F401; named workforce identities
from backend import banking_research  # noqa: E402,F401; consented usability pilot
from backend import banking_execution  # noqa: E402,F401; read-only recovery and reconciliation
from backend import banking_goal_planner  # noqa: E402,F401; bounded goal-to-constraint compilation

app.include_router(banking_router)
app.include_router(banking_plan_router)
app.include_router(banking_wealth_router)
app.include_router(banking_auth_router)
app.include_router(banking_mission_router)


_plan_worker_task = None


async def _start_plan_worker() -> None:
    global _plan_worker_task
    import asyncio
    _plan_worker_task = asyncio.create_task(background_plan_worker())


async def _stop_plan_worker() -> None:
    if _plan_worker_task is not None:
        _plan_worker_task.cancel()
        try:
            await _plan_worker_task
        except __import__("asyncio").CancelledError:
            pass


app.router.add_event_handler("startup", _start_plan_worker)
app.router.add_event_handler("shutdown", _stop_plan_worker)

# Local development origins; same-origin production hosting needs no CORS.
cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],       # 允许任意 HTTP 方法
    allow_headers=["*"],       # 允许任意请求头
)


@app.get("/api/graph/public-case")
def get_public_case() -> Dict[str, Any]:
    """Versioned, source-linked public filing excerpt; never mixed with sandbox transactions."""
    return load_public_case()


@app.get("/api/public-case/investigate")
def get_public_investigation(entity_id: str) -> Dict[str, Any]:
    """Return a reproducible report with one original source pointer per fact."""
    try:
        return investigate_public_entity(entity_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/benchmarks/elliptic")
def get_elliptic_benchmark() -> Dict[str, Any]:
    """Aggregate research metrics only; never expose licensed transaction rows."""
    project_root = Path(__file__).resolve().parent.parent
    local_report = project_root / "data/processed/elliptic-benchmark.json"
    saved_summary = project_root / "backend/eval/reports/elliptic-v1-summary.json"
    report_path = local_report if local_report.is_file() else saved_summary
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="独立链上交易评测报告不可用")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1 or not isinstance(report.get("models"), dict):
        raise HTTPException(status_code=503, detail="独立评测报告格式无效")
    return report


_lei_cache: Dict[tuple[str, int], tuple[float, Dict[str, Any]]] = {}
_lei_cache_lock = Lock()


@app.get("/api/sources/gleif/search")
def search_gleif_entities(q: str, limit: int = 8) -> Dict[str, Any]:
    """Query GLEIF's public LEI registry through a fixed, bounded server-side API."""
    query = " ".join(q.strip().split())
    if len(query) < 2 or len(query) > 120:
        raise HTTPException(status_code=422, detail="请输入 2–120 个字符的机构名称")
    limit = max(1, min(limit, 10))
    key = (query.casefold(), limit)
    now = time.monotonic()
    with _lei_cache_lock:
        cached = _lei_cache.get(key)
        if cached and cached[0] > now:
            return {**cached[1], "cached": True}

    try:
        response = httpx.get(
            "https://api.gleif.org/api/v1/lei-records",
            params={"filter[entity.legalName]": query, "page[size]": limit},
            headers={"Accept": "application/vnd.api+json", "User-Agent": "FinRiskGraphRAG/0.5 (public LEI registry research)"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="GLEIF 官方接口暂时不可用，请稍后重试") from exc

    records = []
    for item in payload.get("data", []):
        attributes = item.get("attributes") or {}
        entity = attributes.get("entity") or {}
        registration = attributes.get("registration") or {}
        records.append({
            "lei": attributes.get("lei") or item.get("id"),
            "legal_name": (entity.get("legalName") or {}).get("name", ""),
            "jurisdiction": entity.get("jurisdiction"),
            "country": (entity.get("legalAddress") or {}).get("country"),
            "entity_status": entity.get("status"),
            "registration_status": registration.get("status"),
            "last_updated": registration.get("lastUpdateDate"),
            "source_url": (item.get("links") or {}).get("self"),
        })
    records.sort(key=lambda item: (item["legal_name"].casefold() != query.casefold(), item["legal_name"].casefold()))
    result = {
        "query": query,
        "source": "GLEIF API",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "golden_copy_at": ((payload.get("meta") or {}).get("goldenCopy") or {}).get("publishDate"),
        "total": ((payload.get("meta") or {}).get("pagination") or {}).get("total", len(records)),
        "records": records,
        "cached": False,
    }
    with _lei_cache_lock:
        _lei_cache[key] = (time.monotonic() + 600, result)
    return result


# ======================================================================
# 工具函数
# ======================================================================
def _mask_account_no(account_no: str) -> str:
    """Mask account identifiers before placing them in any browser response."""
    digits = "".join(ch for ch in account_no if ch.isdigit())
    if len(digits) >= 9:
        return f"{digits[:4]}***{digits[-4:]}"
    if len(digits) > 4:
        return f"***{digits[-4:]}"
    return "***"


def _to_g6_graph(subgraph: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 `serialize_graph_result` 的通用子图结构，转换为前端 AntV G6 友好的格式。

    输入节点形如 {id, labels, properties}，边形如 {id, type, source, target, properties}；
    输出节点 {id, label, type, properties}、边 {id, source, target, label, type, properties}，
    其中 label 取业务展示名（公司名/账号等），type 为节点标签或关系类型，便于图例与样式映射。
    """
    nodes = []
    for n in subgraph.get("nodes", []):
        props = dict(n.get("properties", {}) or {})
        labels = n.get("labels") or []
        # 聚合节点（如 device_cluster）用 properties.cluster_type 作为前端类型标识，
        # 普通节点沿用 Neo4j 标签首项（Company/Person/Account/Device）。
        node_type = props.get("cluster_type") or (labels[0] if labels else "Unknown")
        # 账户节点：对长账号预脱敏（6222***0001），其余节点沿用业务展示名
        if node_type == "Account" and props.get("account_no"):
            display = _mask_account_no(str(props.get("account_no")))
            props["account_no"] = display
        else:
            display = props.get("name") or props.get("account_no") or n.get("id")
        nodes.append({
            "id": n["id"],
            "label": str(display),
            "type": node_type,
            "properties": props,
        })

    edges = []
    for e in subgraph.get("edges", []):
        edges.append({
            "id": e["id"],
            "source": e["source"],
            "target": e["target"],
            "label": e.get("type", ""),
            "type": e.get("type", ""),
            "properties": e.get("properties", {}),
        })
    return {"nodes": nodes, "edges": edges}


def _graph_response(subgraph: Dict[str, Any], source: str, message: str = "") -> Dict[str, Any]:
    """统一图谱响应，并明确告知前端当前数据来源。"""
    graph = _to_g6_graph(subgraph)
    graph["meta"] = {
        "source": source,
        "degraded": source == "demo",
        "message": message,
    }
    return graph


def _demo_fallback_enabled() -> bool:
    """开发环境默认可运行；生产可通过环境变量关闭静默降级。"""
    return os.getenv("GRAPH_FALLBACK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_suspicious_edge_ids(
    graph_json: Dict[str, Any],
    suspicious_nodes: List[str],
    suspicious_edge_types: List[str],
) -> List[str]:
    """把模型给出的节点/关系类型约束解析成精确边 ID，避免前端自行猜测证据路径。"""
    node_ids = {str(node_id) for node_id in suspicious_nodes}
    edge_types = {str(edge_type) for edge_type in suspicious_edge_types}
    if not node_ids or not edge_types:
        return []

    return [
        str(edge["id"])
        for edge in graph_json.get("edges", [])
        if str(edge.get("type", "")) in edge_types
        and str(edge.get("source", "")) in node_ids
        and str(edge.get("target", "")) in node_ids
        and edge.get("id") is not None
    ]


def _merge_subgraphs(*subgraphs: Dict[str, Any]) -> Dict[str, Any]:
    """按 id 去重合并多个 {nodes, edges} 子图（节点以业务 id、边以 element_id 为键）。"""
    nodes: Dict[str, Any] = {}
    edges: Dict[str, Any] = {}
    for sg in subgraphs:
        for n in sg.get("nodes", []):
            nodes[n["id"]] = n
        for e in sg.get("edges", []):
            edges[e["id"]] = e
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _collapse_device_clusters(subgraph: Dict[str, Any]) -> Dict[str, Any]:
    """
    设备节点聚合（懒加载时降噪）。

    将「单一父节点独占的私有设备」折叠为 1 个聚合节点（type='device_cluster'），
    属性携带 {count: N}，前端据此渲染成「N 台设备」的聚合簇。

    共享设备（同一台设备被多名自然人登录，即设备复用）本身就是可疑风险信号，
    保持独立节点、不参与聚合，避免折叠后丢失「设备复用」这条穿透线索。
    仅对 >=2 台私有设备的父节点做聚合；单台私有设备保持原样。
    """
    nodes = {n["id"]: n for n in subgraph.get("nodes", [])}
    edges = list(subgraph.get("edges", []))
    device_ids = {i for i, n in nodes.items() if n.get("labels") and n["labels"][0] == "Device"}
    if not device_ids:
        return subgraph

    # 建立「设备 id → 其全部父节点 id」映射（沿 LOGGED_IN 边），并记录相关边 id
    parents_of: Dict[str, List[str]] = defaultdict(list)
    login_edge_ids: Dict[str, List[str]] = defaultdict(list)
    for e in edges:
        if e.get("type") != "LOGGED_IN":
            continue
        src, dst = e["source"], e["target"]
        if src in device_ids and dst not in device_ids:
            parents_of[src].append(dst)
            login_edge_ids[src].append(e["id"])
        elif dst in device_ids and src not in device_ids:
            parents_of[dst].append(src)
            login_edge_ids[dst].append(e["id"])

    # 仅「独占设备」（父节点唯一）可安全聚合；共享设备保留原样
    groups: Dict[str, List[str]] = defaultdict(list)
    for dev_id, parents in parents_of.items():
        if len(parents) == 1:
            groups[parents[0]].append(dev_id)

    removed_nodes: set = set()
    removed_edges: set = set()
    extra_nodes: List[Dict[str, Any]] = []
    extra_edges: List[Dict[str, Any]] = []

    for parent, dev_ids in groups.items():
        if len(dev_ids) < 2:
            continue  # 单台私有设备不聚合
        removed_nodes.update(dev_ids)
        for d in dev_ids:
            removed_edges.update(login_edge_ids[d])
        cluster_id = f"DEVICE_CLUSTER_{parent}"
        extra_nodes.append({
            "id": cluster_id,
            "labels": ["Device"],
            "properties": {"count": len(dev_ids), "cluster": True, "cluster_type": "device_cluster"},
        })
        extra_edges.append({
            "id": f"{parent}->{cluster_id}",
            "type": "LOGGED_IN",
            "source": parent,
            "target": cluster_id,
            "properties": {"clustered": True, "count": len(dev_ids)},
        })

    nodes_out = [n for i, n in nodes.items() if i not in removed_nodes] + extra_nodes
    edges_out = [e for e in edges if e["id"] not in removed_edges] + extra_edges
    return {"nodes": nodes_out, "edges": edges_out}


def _filter_degree1_accounts(subgraph: Dict[str, Any]) -> Dict[str, Any]:
    """
    首屏降噪：过滤「度数 ≤ 1 的边缘账户」。

    初始全景图里存在大量只参与单笔流水、或只与公司有一条开户关系的叶子账户，
    对风险穿透无增益却制造视觉噪声。这里在合并后的子图内统计每个 Account 节点的
    真实度数（任意类型的邻接边），剔除度数 ≤ 1 的普通 Account 节点及其唯一关联边，
    让初始大盘保持清爽。

    这些边缘账户只在「下钻展开」(/api/graph/expand) 时按需补充，故仅在此处（首屏）
    过滤，不作用于 expand 与 SSE 分析子图。
    """
    nodes = {n["id"]: n for n in subgraph.get("nodes", [])}
    edges = list(subgraph.get("edges", []))

    # 迭代剪枝：反复剔除「度数 ≤1 的普通 Account 叶子」，直到不再产生新叶子。
    # 单次剔除后，原本与其相邻的账户可能沦为新的叶子（级联效应），故需循环至收敛，
    # 保证初始大盘「绝对清爽」；这些边缘账户仅在下钻展开（/api/graph/expand）时按需补充。
    while True:
        degree: Dict[str, int] = defaultdict(int)
        for e in edges:
            degree[e["source"]] += 1
            degree[e["target"]] += 1

        # 仅剔除「普通 Account」叶子（聚合设备簇 device_cluster 不在其列）
        drop = {
            nid
            for nid, n in nodes.items()
            if n.get("labels") and n["labels"][0] == "Account" and degree[nid] <= 1
        }
        if not drop:
            break
        nodes = {i: n for i, n in nodes.items() if i not in drop}
        edges = [e for e in edges if e["source"] not in drop and e["target"] not in drop]

    return {"nodes": list(nodes.values()), "edges": edges}


def _sse_event(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """构造一条 SSE 事件（sse-starlette 接受 {event, data} 字典并自动编码）。"""
    return {"event": event, "data": json.dumps(payload, ensure_ascii=False)}


# ======================================================================
# 接口 1：根路由（健康检查）
# ======================================================================
@app.get("/")
def root() -> Dict[str, Any]:
    """轻量健康检查，便于确认服务已启动。"""
    return {"service": "穿透式金融风控与决策引擎", "status": "ok", "phase": 4}


@app.get("/api/health")
def health() -> Dict[str, Any]:
    """同时报告 API 与 Neo4j 的真实状态，前端不再把“进程在线”误报成“实时图谱”。"""
    neo4j = {"status": "offline", "nodes": 0, "relationships": 0}
    try:
        records = Neo4jDatabase().execute_query(
            "MATCH (n) WITH count(n) AS nodes "
            "OPTIONAL MATCH ()-[r]->() RETURN nodes, count(r) AS relationships"
        )
        if records:
            neo4j = {
                "status": "online",
                "nodes": int(records[0]["nodes"]),
                "relationships": int(records[0]["relationships"]),
            }
    except Exception as exc:
        neo4j["reason"] = type(exc).__name__
    return {
        "status": "ok",
        "graph_source": "neo4j" if neo4j["status"] == "online" and neo4j["nodes"] > 0 else "demo",
        "dependencies": {"neo4j": neo4j},
        "graph_fallback": "ready" if _demo_fallback_enabled() else "disabled",
        "version": app.version,
    }


@app.get("/api/graph/stats")
def get_graph_stats() -> Dict[str, Any]:
    """返回工作区全量规模；与首屏抽样规模分离，供状态区展示覆盖范围。"""
    state = health()
    neo4j = state["dependencies"]["neo4j"]
    if neo4j["status"] == "online" and neo4j["nodes"] > 0:
        return {"source": "neo4j", **neo4j}
    demo = get_demo_graph()
    return {"source": "demo", "status": "ready", "nodes": len(demo["nodes"]), "relationships": len(demo["edges"])}


# ======================================================================
# 接口 2：图谱 Schema（图例）
# ======================================================================
@app.get("/api/graph/schema")
def get_graph_schema() -> Dict[str, Any]:
    """返回可用的节点类型与边类型，供前端渲染图例与样式映射。"""
    return {"node_types": NODE_TYPE_META, "edge_types": EDGE_TYPE_META}


# ======================================================================
# 接口 3：图谱样例（首屏全景图）
# ======================================================================
@app.get("/api/graph/sample")
def get_graph_sample(top_n: int = 20) -> Dict[str, Any]:
    """
    返回前端首次加载的「核心骨干网络」（AntV G6 {nodes, edges} 格式）。

    为保持首屏克制优雅，不再一次性推送全量节点，而是仅提取：
      1. 风险标记公司（risk_flag=true，含模式A资金回环 / 模式B影子实控人的靶点，恒在）；
      2. 交易频次最高的 Top-N 公司（按参与 TRANSFER 的条数排序）；
      3. 这些公司的关键法人（LEGAL_REP）与「每家公司转账频次最高的 1 个关键账户」，
         并抽取核心账户之间的转账（过滤掉所有 Device 与单次流水账户）；
      4. 风险节点间的股权穿透链（OWNS），保证影子实控人拓扑可见。

    节点规模与 top_n 的实测关系（约）：
        top_n=20 → ~77 节点（落在 50~80 目标区间，推荐默认值）
        top_n=25 → ~96 节点
        top_n=30 → ~114 节点（超出 50~80 目标）
    由于「Top-N 公司 + 关键法人」本身已贡献 ~45 节点，再叠加账户后，top_n=30 必然
    超出 80，故默认取 20 以命中目标区间；风险靶点走 UNION 恒在，调小 top_n 不丢靶点。
    """
    db = Neo4jDatabase()
    try:
        # 1) 核心公司主键：风险标记公司 ∪ 交易频次 Top-N 公司（UNION 自动去重）
        seed_records = db.execute_query(
            """
            MATCH (c:Company) WHERE c.risk_flag = true RETURN c.id AS id
            UNION
            MATCH (c:Company)-[:HAS_ACCOUNT]->(:Account)-[:TRANSFER]-()
            WITH c, count(*) AS flow ORDER BY flow DESC LIMIT $top_n
            RETURN c.id AS id
            """,
            {"top_n": top_n},
        )
        seed_ids = [r["id"] for r in seed_records]
        if not seed_ids:
            if not _demo_fallback_enabled():
                return {"nodes": [], "edges": [], "meta": {"source": "neo4j", "degraded": False}}
            return _graph_response(
                get_demo_graph(),
                "demo",
                "Neo4j 已连接但尚无图数据，当前展示内置演示案例。",
            )

        # 2) 核心公司 + 关键法人（LEGAL_REP 边）
        company_recs = db.execute_query(
            """
            MATCH (c:Company)
            WHERE c.id IN $ids
            OPTIONAL MATCH (p:Person)-[r:LEGAL_REP]->(c)
            RETURN c, p, r
            """,
            {"ids": seed_ids},
        )

        # 3) 关键账户（每家公司仅取转账频次最高的 1 个账户）+ 开户关系 + 账户间转账
        #    先过滤单次流水账户（>=2 笔），再按频次排序取 top-1，控制账户规模、只留核心结算账户。
        account_recs = db.execute_query(
            """
            MATCH (c:Company)-[h:HAS_ACCOUNT]->(a:Account)
            WHERE c.id IN $ids AND size([(a)-[:TRANSFER]-() | 1]) >= 2
            WITH c, a, h
            ORDER BY size([(a)-[:TRANSFER]-() | 1]) DESC
            WITH c, collect(a)[0] AS a, collect(h)[0] AS h
            MATCH (a)-[t:TRANSFER]->(b:Account)
            MATCH (cb:Company)-[:HAS_ACCOUNT]->(b)
            WHERE cb.id IN $ids AND size([(b)-[:TRANSFER]-() | 1]) >= 2
            RETURN c, a, b, h, t
            """,
            {"ids": seed_ids},
        )

        # 4) 风险节点间的股权穿透链（OWNS），保证影子实控人拓扑可见
        owns_recs = db.execute_query(
            """
            MATCH (n)-[o:OWNS]->(m)
            WHERE n.risk_flag = true OR m.risk_flag = true
            RETURN n, m, o
            """
        )
    except Exception as exc:
        if not _demo_fallback_enabled():
            raise HTTPException(status_code=503, detail="图数据库暂时不可用") from exc
        print(f"[graph/sample] Neo4j 不可用，切换到内置演示图谱：{exc!r}", file=sys.stderr)
        return _graph_response(
            get_demo_graph(),
            "demo",
            "Neo4j 暂未连接，当前展示内置演示案例；数据库恢复后刷新即可自动切回实时数据。",
        )

    merged = _merge_subgraphs(
        serialize_graph_result(company_recs),
        serialize_graph_result(account_recs),
        serialize_graph_result(owns_recs),
    )
    # 首屏降噪：过滤度数 ≤1 的边缘账户（只在「下钻展开」时按需补充）
    merged = _filter_degree1_accounts(merged)
    return _graph_response(merged, "neo4j")


# ======================================================================
# 接口 3.5：图谱按需扩展（懒加载）
# ======================================================================
@app.get("/api/graph/expand")
def get_graph_expand(node_id: str, depth: int = 1) -> Dict[str, Any]:
    """
    按需加载：以指定节点为中心，向外扩展 1~2 度关系。

    用户在前端点击某公司节点（或 AI 排查需深入）时调用，避免首屏一次性推送
    全量数据。结果会对海量 Device 叶子节点做聚合（device_cluster），控制节点规模。

    Args:
        node_id: 业务主键（如 LOOP_A / PERSON_BOSS / ACCT_0087）。
        depth:   扩展度数（1 或 2，自动收敛到 [1, 2]）。
    """
    depth = max(1, min(2, depth))
    db = Neo4jDatabase()
    try:
        anchor_recs = db.execute_query("MATCH (n {id: $id}) RETURN n", {"id": node_id})
        if not anchor_recs:
            raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")
        # Neo4j 不允许把变量长度路径的上下界参数化（`[*1..$depth]` 会报
        # "Parameter maps cannot be used in MATCH patterns"）。此处 depth 已收敛为
        # 1 或 2 的整数（非用户可控），故用 f-string 安全地内插为字面量，其余仍参数化。
        neigh_recs = db.execute_query(
            f"MATCH path = (n {{id: $id}})-[*1..{depth}]-(m) RETURN path LIMIT $limit",
            {"id": node_id, "limit": 300},
        )
    except HTTPException:
        raise
    except Exception as exc:
        if not _demo_fallback_enabled():
            raise HTTPException(status_code=503, detail="图数据库暂时不可用") from exc
        print(f"[graph/expand] Neo4j 不可用，使用演示邻域：{exc!r}", file=sys.stderr)
        try:
            return _graph_response(
                expand_demo_graph(node_id, depth),
                "demo",
                "当前为内置演示案例的关联邻域。",
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在") from None

    merged = _merge_subgraphs(
        serialize_graph_result(anchor_recs),
        serialize_graph_result(neigh_recs),
    )
    merged = _collapse_device_clusters(merged)
    return _graph_response(merged, "neo4j")


# ======================================================================
# 接口 4：核心审批流（SSE）
# ======================================================================
class AnalyzeRequest(BaseModel):
    """审批流请求体。"""
    query: str  # 用户自然语言查询，如「查一下公司A的资金回环关系」


def _stream_demo_events(query: str) -> Iterator[Dict[str, Any]]:
    """外部图数据库或 LLM 不可用时，保持完整可演示的 SSE 协议。"""
    graph = _graph_response(
        get_demo_graph(),
        "demo",
        "外部分析服务暂不可用，本次使用内置确定性演示案例。",
    )
    case = get_demo_case(query)
    citations = ([{"clause_id": case["clause"][0], "text": case["clause"][1]}]
                 if case["clause"] else [])
    edge_ids = _resolve_suspicious_edge_ids(graph, case["nodes"], case["edge_types"])

    yield _sse_event("status", {
        "node": "retrieve",
        "message": "证据快照已完成，启动风险与合规双轨审查",
    })
    yield _sse_event("graph", {"cypher": "DEMO_FALLBACK", "data": graph})
    yield _sse_event("status", {"node": "risk", "message": "风险规则检查完毕"})
    yield _sse_event("report", {
        "agent": "risk",
        "content": case["risk_summary"],
    })
    yield _sse_event("status", {"node": "compliance", "message": "合规依据检查完毕"})
    yield _sse_event("report", {
        "agent": "compliance",
        "content": case["compliance_summary"],
        "risk_labels": "待补充案件证据" if not edge_ids else "资金关系异常 / 需人工复核",
        "citations": {"valid": citations, "invalid": []},
        "evidence": [{"edge_id": edge_id} for edge_id in edge_ids],
        "aml_findings": [{
            "pattern": "demo_deterministic_check",
            "node_ids": case["nodes"],
            "evidence": {"edge_ids": edge_ids},
            "description": case["compliance_summary"],
        }],
    })
    yield _sse_event("status", {"node": "decision", "message": "确定性演示报告生成完毕"})
    decision = "NEEDS_EVIDENCE" if not edge_ids else "FLAGGED"
    investigation_id = create_investigation({
        "query": query,
        "ai_decision": decision,
        "suspicious_nodes": case["nodes"],
        "suspicious_edge_ids": edge_ids,
        "source": "demo",
    }) if edge_ids else None
    yield _sse_event("decision", {
        "decision": decision,
        "investigation_id": investigation_id,
        "analysis_source": "demo",
        "risk_score": None,
        "confidence": None,
        "final_report": case["decision_summary"],
        "highlight": {
            "suspicious_nodes": case["nodes"],
            "suspicious_edges_types": case["edge_types"],
            "suspicious_edge_ids": edge_ids,
        },
    })
    yield _sse_event("done", {"message": "演示审批流程结束"})


def _stream_events(query: str) -> Iterator[Dict[str, Any]]:
    """
    SSE 事件生成器（同步）：驱动 Phase 3 的 LangGraph 工作流，逐节点向前端推送状态。

    事件类型约定（前端据此分发）：
        status   —— 流程进度提示（含 node 字段标识当前节点）；
        graph    —— 抽取到的子图（G6 格式），供图谱渲染与后续高亮；
        report   —— 风控/合规 Agent 的审查报告（左侧「思维流」展示）；
        decision —— 最终决策 + 前端高亮 JSON（suspicious_nodes / suspicious_edges_types）；
        error    —— 流程异常信息；
        done     —— 流程结束。
    """
    yield _sse_event("status", {"node": "start", "message": "任务已受理，正在提取图数据..."})

    finished: set = set()  # 记录已完成的并行审查节点（risk / compliance）
    current_graph_json: Dict[str, Any] = {}
    try:
        # stream_mode="updates"：每完成一个节点即 yield 一次 {节点名: 状态更新}
        for chunk in risk_workflow.stream({"query": query}, stream_mode="updates"):
            for node_name, update in chunk.items():
                if node_name == "retrieve":
                    current_graph_json = update.get("graph_json", {})
                    yield _sse_event("status", {
                        "node": "retrieve",
                        "message": "图数据提取完成，风控与合规 Agent 开始并行审查",
                    })
                    yield _sse_event("graph", {
                        "cypher": update.get("cypher", ""),
                        "data": _to_g6_graph(update.get("graph_json", {})),
                    })

                elif node_name in ("risk", "compliance"):
                    finished.add(node_name)
                    message = "风控审查完毕" if node_name == "risk" else "合规审查完毕"
                    yield _sse_event("status", {"node": node_name, "message": message})
                    field = "risk_report" if node_name == "risk" else "compliance_report"
                    payload = {"agent": node_name, "content": update.get(field, "")}
                    # 合规报告额外携带「法条引用校验结果」「证据」（创新点一·可验证合规推理）
                    # 与「确定性时空聚集检测结果」（创新点二·非 LLM）
                    if node_name == "compliance":
                        payload["citations"] = update.get("compliance_citations", {})
                        payload["evidence"] = update.get("compliance_evidence", [])
                        payload["risk_labels"] = update.get("compliance_risk_labels", "")
                        payload["aml_findings"] = update.get("aml_findings", [])
                    yield _sse_event("report", payload)
                    # 风控 + 合规均完成后，决策 Agent 才被调度，据此推送「生成报告」进度
                    if len(finished) == 2:
                        yield _sse_event("status", {"node": "decision", "message": "正在生成最终报告..."})

                elif node_name == "decision":
                    yield _sse_event("status", {"node": "decision", "message": "最终报告生成完毕"})
                    # 最终报告 + 供前端高亮/分级展示的 JSON 一并推送（XAI 可解释性落点）
                    suspicious_nodes = update.get("suspicious_nodes", [])
                    suspicious_edge_types = update.get("suspicious_edges_types", [])
                    suspicious_edge_ids = _resolve_suspicious_edge_ids(
                        current_graph_json,
                        suspicious_nodes,
                        suspicious_edge_types,
                    )
                    evidence_ids = set(suspicious_edge_ids)
                    evidence_node_ids = {
                        str(edge[endpoint])
                        for edge in current_graph_json.get("edges", [])
                        if str(edge.get("id")) in evidence_ids
                        for endpoint in ("source", "target")
                    }
                    suspicious_nodes = [node for node in suspicious_nodes if str(node) in evidence_node_ids]
                    decision_value = update.get("decision", "")
                    if decision_value not in {"REJECT", "FLAGGED"}:
                        suspicious_nodes = []
                        suspicious_edge_ids = []
                        suspicious_edge_types = []
                    investigation_id = create_investigation({
                        "query": query,
                        "ai_decision": decision_value,
                        "suspicious_nodes": suspicious_nodes,
                        "suspicious_edge_ids": suspicious_edge_ids,
                        "source": "neo4j",
                    }) if decision_value in {"REJECT", "FLAGGED"} and suspicious_edge_ids else None
                    yield _sse_event("decision", {
                        "decision": decision_value,
                        "investigation_id": investigation_id,
                        "analysis_source": "neo4j",
                        "risk_score": None,
                        "confidence": None,
                        "final_report": update.get("final_decision", ""),
                        "highlight": {
                            "suspicious_nodes": suspicious_nodes,
                            "suspicious_edges_types": suspicious_edge_types,
                            "suspicious_edge_ids": suspicious_edge_ids,
                        },
                    })

        yield _sse_event("done", {"message": "审批流程结束"})

    except Exception as exc:  # 捕获 LLM / Neo4j 等运行时异常：脱敏后返回，杜绝内部细节泄漏
        print(f"[analyze] 审批流执行异常，切换本地演示流程：{exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        if _demo_fallback_enabled():
            yield from _stream_demo_events(query)
        else:
            yield _sse_event("error", {"message": "当前审批流程执行受阻，请检查数据库或模型服务"})


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> EventSourceResponse:
    """
    核心审批流接口：以 SSE 实时驱动多 Agent 风控决策全流程。

    ping=10 表示每 10 秒发送一次心跳注释，避免长耗时 LLM 调用期间连接被代理/浏览器判超时。
    """
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="查询内容不能为空")
    return EventSourceResponse(_stream_events(query), ping=10)


# ======================================================================
# 接口 5：人在回路审批回写（创新点四 · 闭环）
# ======================================================================
class ReviewRequest(BaseModel):
    """
    信贷员对 AI 决策的确认/驳回请求体。

    human_decision 取值：confirm（确认 AI 结论）/ reject（驳回 AI 结论）。
    """
    investigation_id: str = Field(min_length=32, max_length=32)  # 服务端签发的调查结果标识
    human_decision: str                 # confirm / reject
    rationale: str = Field(min_length=5, max_length=2000)  # 审核理由（供审计）


def require_review_access(request: Request) -> None:
    """Protect mutating and audit endpoints when running a public deployment."""
    configured = os.getenv("REVIEW_API_TOKEN", "")
    if not configured:
        if os.getenv("APP_ENV", "development").casefold() == "production":
            raise HTTPException(status_code=503, detail="生产环境未配置复核口令")
        return
    supplied = request.headers.get("X-Review-Token", "")
    if not secrets.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="复核口令无效")


@app.post("/api/review")
def submit_review(req: ReviewRequest, request: Request) -> Dict[str, Any]:
    """
    提交人工审批结果：确认/驳回 AI 结论，回写图谱 + 沉淀标签 + 追加审计日志。

    这是「Copilot 闭环」的落点——AI 出结论，信贷员确认或纠错，反馈反向回流：
      ① risk_flag 回写 Neo4j（强化证据）；
      ② 带标签样本沉淀（持续扩充盲评集，供再评测校准）；
      ③ append-only 审计日志（可追溯「谁在何时确认/驳回了什么」）。
    """
    require_review_access(request)
    if req.human_decision not in (HUMAN_CONFIRM, HUMAN_REJECT):
        raise HTTPException(
            status_code=422,
            detail=f"human_decision 必须为 '{HUMAN_CONFIRM}' 或 '{HUMAN_REJECT}'",
        )
    with pending_investigation(req.investigation_id) as investigation:
        if investigation is None:
            raise HTTPException(status_code=404, detail="调查结果不存在或已完成复核")
        record = FeedbackRecord(
            query=investigation["query"],
            ai_decision=investigation["ai_decision"],
            suspicious_nodes=investigation["suspicious_nodes"],
            human_decision=req.human_decision,
            reviewer="授权复核者",
            rationale=req.rationale,
            investigation_id=req.investigation_id,
            source=investigation["source"],
        )
        return record_feedback(record, db=Neo4jDatabase() if investigation["source"] == "neo4j" else None)


@app.get("/api/review/audit")
def get_review_audit(request: Request, limit: int = 50) -> Dict[str, Any]:
    """返回最近的人工审批审计日志（最近 limit 条，默认 50）。"""
    require_review_access(request)
    return {"audit_log": load_audit_log(limit=limit)}
