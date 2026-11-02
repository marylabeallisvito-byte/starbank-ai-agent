"""
backend/rag/graph_extractor.py
==============================
GraphRAG 核心组件 —— 图谱抽取器（Text2Cypher 变体）。

设计定位
--------
复用 LangChain GraphCypherQAChain 的「自然语言 → Cypher」思路，但做了关键变体：
我们【不需要】LLM 直接回答用户问题（直接作答会丢失结构化证据与可解释性），而是：

    1. 由 LLM 生成 Cypher 查询语句（Text2Cypher）；
    2. 在 Neo4j 中执行该语句，取回「子图」；
    3. 将子图序列化为统一的 {nodes, edges} JSON 结构。

该 JSON 是「穿透式风控」的证据底座：既可供下游多 Agent 决策消费，也可直接交给
前端（AntV G6）做图谱可视化。XAI 的可解释路径，正来源于这个结构化、可回溯的子图，
而非 LLM 的「黑盒结论」。

核心流程
--------
用户 Query → Text2Cypher(LLM) → Cypher → Neo4j 执行 → 结果序列化 → {nodes, edges}
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from neo4j.graph import Node, Path as NeoPath, Relationship

# 将 backend/ 加入 sys.path，便于独立运行本模块时导入 database 单例
sys.path.append(str(Path(__file__).resolve().parents[1]))

from database import Neo4jDatabase  # noqa: E402

# ======================================================================
# 图数据库 Schema 描述（硬编码，作为 Prompt 上下文 / 离线兜底）
# ======================================================================
GRAPH_SCHEMA = """
【节点】（`id` 是业务主键，如 LOOP_A / PERSON_BOSS / ACCT_0087，用于精确匹配；
        `name` 是中文展示名，仅 Company / Person 拥有；Account 用 account_no、Device 用 ip/mac 区分）
- Company(公司): id, name(公司名), industry(行业), registered_capital(注册资本/万元), established_date(成立日期), location(注册地), risk_flag(风险标记)
- Person(自然人): id, name(姓名), id_number(身份证号), phone(电话), address(地址), location(常驻地), risk_flag
- Account(账户): id, account_no(账号), bank(开户行), balance(余额/元), opened_date(开户日期), location(开户地)
- Device(设备): id, ip(IP地址), mac(MAC地址), device_type(设备类型), os(操作系统), location(归属地)

【关系】（TRANSFER 转账只发生在 Account 账户之间；公司/自然人通过 HAS_ACCOUNT 连接到账户）
- (:Person)-[:OWNS {ratio}]->(:Company)              自然人持股公司，ratio 为持股比例(0~1)
- (:Company)-[:OWNS {ratio}]->(:Company)             公司间持股
- (:Person)-[:LEGAL_REP]->(:Company)                 法人代表
- (:Account)-[:TRANSFER {amount, date, location}]->(:Account)  转账流水（仅账户之间），amount 金额(元)，date 交易时间戳，location 交易发生地
- (:Company|:Person)-[:HAS_ACCOUNT]->(:Account)      开户关系
- (:Person)-[:LOGGED_IN {date}]->(:Device)           登录关系

【已知高危拓扑（沙盒已注入，便于验证穿透查询）】
- 资金回环：LOOP_A/B/C 三家公司，各自 HAS_ACCOUNT 到账户 LOOP_ACCT_A/B/C，账户间 TRANSFER 构成 A→B→C→A 等额环形转账
- 影子实控人自融：PERSON_BOSS 经 SHELL_MIDDLE 双层 OWNS 持股 SHELL_X / SHELL_Y，二者账户 SHELL_ACCT_X/Y 之间双向大额 TRANSFER
"""

# Text2Cypher 系统提示词
TEXT2CYPHER_SYSTEM_PROMPT = """你是一名精通 Neo4j Cypher 的金融图谱查询专家。
你的任务：根据用户的自然语言问题，生成【一条】Cypher 查询语句，从下面的图数据库中检索出相关子图。

## 图数据库 Schema
{schema}

## 输出要求（必须严格遵守）
1. 只输出一条 Cypher 查询语句，不要输出任何解释、注释、推理过程或多余文字。
2. 不要使用 Markdown 代码块（不要用 ``` 包裹，不要写 "cypher" 前缀）。
3. 查询应尽量返回「节点、关系或路径变量」，以便前端可视化为子图，例如 RETURN p、RETURN n, r, m、RETURN nodes(p), relationships(p)。
4. 属性名、标签名、关系名必须与上面 Schema 完全一致。
5. 如果问题涉及「回环 / 环形 / 穿透 / 持股链 / 资金流向」，优先使用可变长度路径匹配，例如 (a)-[:TRANSFER*1..6]->(b) 或 (a)-[:OWNS*1..3]->(b)。
6. WHERE 条件【优先匹配 id 属性】（业务主键，如 'LOOP_A'、'PERSON_BOSS'、'ACCT_0087'）；只有当用户给出的是中文名称（如「深圳市恒源贸易」「陈志强」）时，才用 name 属性 + CONTAINS 模糊匹配（Account/Device 没有 name 属性）。
7. TRANSFER 只存在于 Account 之间；要追踪某公司/自然人的资金流向，必须先经 HAS_ACCOUNT 到达账户，再沿 TRANSFER 匹配。仅当问题明确提到「回环 / 环形转账」时才需用 OPTIONAL MATCH 沿 HAS_ACCOUNT 反查账户归属公司（照抄参考示例）；「审查某公司信贷风险」这类常规审查只聚焦目标主体的直接账户与直接交易对手，不要额外展开 OWNS 股权链或多跳转账。
8. 查询必须限定范围、避免全图扫描：可变长度路径 *1..n 的 n 上限取 4；起始节点必须有属性锚定（例如 id='LOOP_A'）；若用户问题宽泛（如「分析全部风险」），聚焦已知高危拓扑并附加 LIMIT。

## 参考示例（严格照着这种写法生成）
问：查一下 LOOP_A 的资金回环
答：MATCH p=(c:Company {{id:'LOOP_A'}})-[:HAS_ACCOUNT]->(a:Account)-[:TRANSFER*1..4]->(b:Account)
    OPTIONAL MATCH (owner:Company)-[:HAS_ACCOUNT]->(b)
    RETURN p, owner

问：审查 COMP_0000 的信贷风险
答：MATCH p=(c:Company {{id:'COMP_0000'}})-[:HAS_ACCOUNT]->(a:Account)
    OPTIONAL MATCH (a)-[r:TRANSFER]-(b:Account)
    OPTIONAL MATCH (owner:Company|Person)-[:HAS_ACCOUNT]->(b)
    RETURN p, r, b, owner

问：分析陈志强的自融风险
答：MATCH p=(boss:Person {{name:'陈志强'}})-[:OWNS*1..3]->(shell:Company)
    MATCH q=(shell)-[:HAS_ACCOUNT]->(acct:Account)-[:TRANSFER*1..2]->(acct)
    RETURN p, q
"""


# ======================================================================
# LLM 构建
# ======================================================================
def build_llm() -> ChatOpenAI:
    """
    根据环境变量构建 LLM 客户端（OpenAI 兼容协议）。

    通过 LLM_API_BASE 可无缝切换 OpenAI / DeepSeek / 通义 / 月之暗面 等
    OpenAI 兼容服务商，无需改动代码，仅需调整 .env。
    """
    return ChatOpenAI(
        model=os.getenv("LLM_MODEL", "gpt-4o"),
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_API_BASE", "https://api.openai.com/v1"),
        temperature=0,  # 查询生成需要确定性输出，关闭随机性
    )


def _strip_cypher_fence(text: str) -> str:
    """
    清洗 LLM 输出：去掉可能包裹的 Markdown 代码块围栏与 "cypher" 前缀，
    确保返回纯 Cypher 字符串（LLM 常会「画蛇添足」加上 ```cypher ... ```）。
    """
    text = text.strip()
    text = re.sub(r"^```(?:cypher|sql)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^cypher\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


# ======================================================================
# 子图序列化
# ======================================================================
def serialize_graph_result(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    将 Neo4j 查询结果序列化为统一的 {nodes, edges} 子图 JSON 结构。

    Args:
        records: database.execute_query 返回的记录列表（每行为 dict）。

    Returns:
        {"nodes": [...], "edges": [...]}，其中：
        - node:  {id, labels, properties}
        - edge:  {id, type, source, target, properties}
    """
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}
    for record in records:
        for value in record.values():
            _collect_graph_value(value, nodes, edges)
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def _collect_graph_value(value: Any, nodes: Dict, edges: Dict) -> None:
    """
    递归收集结果中的 Node / Relationship / Path，去重后写入 nodes / edges。
    节点以业务 id 为键去重，边以 element_id 为键去重，保证子图不冗余。
    """
    if value is None:
        return

    if isinstance(value, Node):
        node_id = value.get("id") or value.element_id  # 优先业务主键，回退内部 id
        nodes[node_id] = {
            "id": node_id,
            "labels": list(value.labels),
            "properties": dict(value),
        }

    elif isinstance(value, Relationship):
        # 先收录两端点节点，保证子图「节点—边」闭合一致，便于前端直接渲染
        _collect_graph_value(value.start_node, nodes, edges)
        _collect_graph_value(value.end_node, nodes, edges)
        rel_id = value.element_id
        edges[rel_id] = {
            "id": rel_id,
            "type": value.type,
            "source": value.start_node.get("id") or value.start_node.element_id,
            "target": value.end_node.get("id") or value.end_node.element_id,
            "properties": dict(value),
        }

    elif isinstance(value, NeoPath):
        for node in value.nodes:
            _collect_graph_value(node, nodes, edges)
        for rel in value.relationships:
            _collect_graph_value(rel, nodes, edges)

    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_graph_value(item, nodes, edges)

    elif isinstance(value, dict):
        for v in value.values():
            _collect_graph_value(v, nodes, edges)


# ======================================================================
# 字段级脱敏（数据隐私边界：送外部 LLM 前脱敏敏感字段）
# ======================================================================
# 敏感字段清单：身份证号、手机号、住址、账号、设备标识、余额、金额等。
# 这些字段属于个人隐私 / 资金隐私，送外部 LLM 前必须脱敏，仅保留业务 id、
# 结构拓扑（labels / type / source / target）与风险分析所必需的非敏感字段
# （risk_flag / ratio / date / name 等），从机制上落实计划书「仅传结构拓扑
# 与必要标识」的隐私承诺（对应 HANDOFF §12.1 #5 的整改）。
SENSITIVE_FIELDS = {
    "id_number",          # 身份证号
    "phone",              # 手机号
    "address",            # 住址
    "account_no",         # 银行账号
    "ip",                 # 登录 IP
    "mac",                # 设备 MAC
    "balance",            # 账户余额（元）
    "amount",             # 转账金额（元）
}

# 脱敏占位符：保留键名但替换值，前端 / 审计可清晰看出该字段已被脱敏处理
SENSITIVE_MASK = "***"


def sanitize_enabled() -> bool:
    """
    是否启用「送 LLM 前字段级脱敏」。

    默认开启（隐私优先）；可通过环境变量 SANITIZE_GRAPH_DATA=0 临时关闭，
    便于本地调试时查看真实金额 / 身份信息。开关的存在使脱敏策略可审计、可回退。
    """
    return os.getenv("SANITIZE_GRAPH_DATA", "1").strip().lower() not in ("0", "false", "no", "off")


def _sanitize_properties(props: Dict[str, Any]) -> Dict[str, Any]:
    """对单组属性做字段级脱敏：命中敏感字段的值替换为掩码，其余字段原样保留。"""
    return {
        key: (SENSITIVE_MASK if key in SENSITIVE_FIELDS else value)
        for key, value in props.items()
    }


def sanitize_subgraph(subgraph: Dict[str, Any]) -> Dict[str, Any]:
    """
    返回脱敏后的子图副本（不修改原对象，避免污染前端可视化所用的原始数据）。

    仅对 properties 里的敏感字段做掩码；节点 id / labels、边 id / type /
    source / target 等结构信息原样保留，保证多 Agent 仍可基于拓扑完成穿透分析。
    """
    return {
        "nodes": [
            {**node, "properties": _sanitize_properties(node.get("properties", {}) or {})}
            for node in subgraph.get("nodes", [])
        ],
        "edges": [
            {**edge, "properties": _sanitize_properties(edge.get("properties", {}) or {})}
            for edge in subgraph.get("edges", [])
        ],
    }


# ======================================================================
# 图谱抽取器
# ======================================================================
class GraphExtractor:
    """
    Text2Cypher + 子图抽取的统一入口。

    使用方式：
        extractor = GraphExtractor()
        result = extractor.extract_subgraph("查一下公司A的资金回环关系")
        # result = {"question": ..., "cypher": ..., "subgraph": {"nodes": [...], "edges": [...]}}
    """

    def __init__(
        self,
        db: Optional[Neo4jDatabase] = None,
        llm: Optional[ChatOpenAI] = None,
        schema: Optional[str] = None,
    ) -> None:
        """
        Args:
            db:     Neo4j 单例（默认自动获取，可注入 Mock 便于测试）。
            llm:    LLM 客户端（默认按 .env 构建，可注入 Mock）。
            schema: 图 Schema 描述（默认硬编码 GRAPH_SCHEMA，避免启动时强行连库）。
        """
        self.db = db or Neo4jDatabase()
        self.llm = llm or build_llm()
        self.schema = schema or GRAPH_SCHEMA
        self.last_cypher: str = ""  # 记录最近一次生成的 Cypher，便于调试 / 可解释性审计
        self._chain = self._build_chain()

    def _build_chain(self):
        """构建「Prompt → LLM → 字符串解析」的 Text2Cypher 链。"""
        prompt = ChatPromptTemplate.from_messages([
            ("system", TEXT2CYPHER_SYSTEM_PROMPT),
            ("human", "{question}"),
        ])
        return prompt | self.llm | StrOutputParser()

    def text_to_cypher(self, question: str) -> str:
        """自然语言 → Cypher。"""
        raw = self._chain.invoke({"schema": self.schema, "question": question})
        self.last_cypher = _strip_cypher_fence(raw)
        return self.last_cypher

    def extract_subgraph(self, question: str) -> Dict[str, Any]:
        """
        全链路：自然语言 → Cypher → Neo4j 执行 → 子图 JSON。

        Returns:
            {"question", "cypher", "subgraph"}，其中 subgraph = {nodes, edges}。
        """
        cypher = self.text_to_cypher(question)
        records = self.db.execute_query(cypher)
        subgraph = serialize_graph_result(records)
        return {"question": question, "cypher": cypher, "subgraph": subgraph}

    def refresh_schema(self) -> str:
        """（可选）在线动态读取 Neo4j 的标签与关系类型，更新 schema。需数据库已启动。"""
        try:
            labels = [r["label"] for r in self.db.execute_query("CALL db.labels() YIELD label RETURN label")]
            rel_types = [r["relationshipType"] for r in self.db.execute_query("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
            self.schema = f"节点标签: {labels}\n关系类型: {rel_types}\n" + GRAPH_SCHEMA
        except Exception:
            self.schema = GRAPH_SCHEMA  # 连库失败回退硬编码
        return self.schema
