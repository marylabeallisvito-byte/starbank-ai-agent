"""内置演示图谱。

用于本地首次启动、Neo4j 暂时不可用或尚未导入沙盒数据时的可运行降级。
数据结构与 ``serialize_graph_result`` 的输出一致，因而不会在前端形成第二套协议。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


_NODES = [
    {"id": "LOOP_A", "labels": ["Company"], "properties": {"name": "辰星贸易", "industry": "批发业", "registered_capital": 1200, "risk_flag": True}},
    {"id": "LOOP_B", "labels": ["Company"], "properties": {"name": "远航供应链", "industry": "供应链服务", "registered_capital": 800, "risk_flag": True}},
    {"id": "LOOP_C", "labels": ["Company"], "properties": {"name": "嘉禾实业", "industry": "商务服务", "registered_capital": 600, "risk_flag": True}},
    {"id": "PERSON_A", "labels": ["Person"], "properties": {"name": "陈明", "risk_flag": False}},
    {"id": "PERSON_B", "labels": ["Person"], "properties": {"name": "林涛", "risk_flag": False}},
    {"id": "PERSON_C", "labels": ["Person"], "properties": {"name": "周岚", "risk_flag": False}},
    {"id": "ACCT_LOOP_A", "labels": ["Account"], "properties": {"account_no": "6222020000001001", "bank": "华夏商业银行", "balance": 312000}},
    {"id": "ACCT_LOOP_B", "labels": ["Account"], "properties": {"account_no": "6222020000001002", "bank": "东海商业银行", "balance": 286000}},
    {"id": "ACCT_LOOP_C", "labels": ["Account"], "properties": {"account_no": "6222020000001003", "bank": "华夏商业银行", "balance": 341000}},
    {"id": "PERSON_BOSS", "labels": ["Person"], "properties": {"name": "高远", "risk_flag": True}},
    {"id": "HOLDING_X", "labels": ["Company"], "properties": {"name": "启明控股", "industry": "投资管理", "risk_flag": True}},
    {"id": "SHELL_Y", "labels": ["Company"], "properties": {"name": "瀚海咨询", "industry": "咨询服务", "risk_flag": True}},
    {"id": "TARGET_Z", "labels": ["Company"], "properties": {"name": "融达科技", "industry": "软件服务", "risk_flag": True}},
    {"id": "DEVICE_SHARED", "labels": ["Device"], "properties": {"name": "共享登录设备", "ip": "10.24.18.76", "device_type": "Desktop", "risk_flag": True}},
]

_EDGES = [
    {"id": "legal-a", "type": "LEGAL_REP", "source": "PERSON_A", "target": "LOOP_A", "properties": {}},
    {"id": "legal-b", "type": "LEGAL_REP", "source": "PERSON_B", "target": "LOOP_B", "properties": {}},
    {"id": "legal-c", "type": "LEGAL_REP", "source": "PERSON_C", "target": "LOOP_C", "properties": {}},
    {"id": "account-a", "type": "HAS_ACCOUNT", "source": "LOOP_A", "target": "ACCT_LOOP_A", "properties": {}},
    {"id": "account-b", "type": "HAS_ACCOUNT", "source": "LOOP_B", "target": "ACCT_LOOP_B", "properties": {}},
    {"id": "account-c", "type": "HAS_ACCOUNT", "source": "LOOP_C", "target": "ACCT_LOOP_C", "properties": {}},
    {"id": "transfer-a-b", "type": "TRANSFER", "source": "ACCT_LOOP_A", "target": "ACCT_LOOP_B", "properties": {"amount": 1850000, "date": "2026-08-18T09:12:00+08:00"}},
    {"id": "transfer-b-c", "type": "TRANSFER", "source": "ACCT_LOOP_B", "target": "ACCT_LOOP_C", "properties": {"amount": 1815000, "date": "2026-08-18T12:47:00+08:00"}},
    {"id": "transfer-c-a", "type": "TRANSFER", "source": "ACCT_LOOP_C", "target": "ACCT_LOOP_A", "properties": {"amount": 1798000, "date": "2026-08-18T16:31:00+08:00"}},
    {"id": "owns-boss-holding", "type": "OWNS", "source": "PERSON_BOSS", "target": "HOLDING_X", "properties": {"ratio": 0.82}},
    {"id": "owns-holding-shell", "type": "OWNS", "source": "HOLDING_X", "target": "SHELL_Y", "properties": {"ratio": 0.67}},
    {"id": "owns-shell-target", "type": "OWNS", "source": "SHELL_Y", "target": "TARGET_Z", "properties": {"ratio": 0.74}},
    {"id": "login-boss", "type": "LOGGED_IN", "source": "PERSON_BOSS", "target": "DEVICE_SHARED", "properties": {"date": "2026-08-17T21:08:00+08:00"}},
    {"id": "login-a", "type": "LOGGED_IN", "source": "PERSON_A", "target": "DEVICE_SHARED", "properties": {"date": "2026-08-18T08:56:00+08:00"}},
]


def _extend_demo_graph() -> None:
    """构造接近真实调查密度的确定性网络，断网时也能验证大图交互。"""
    company_names = ["北辰数科", "海岳物流", "锦川实业", "云岭商贸", "澄海智造", "华曜资本", "凌峰供应链", "新桥医药", "星港能源", "柏舟咨询", "远景材料", "开源数据", "融信服务", "衡泰工程", "蓝湾贸易", "安澜科技", "卓越投资", "启航设备"]
    person_names = ["许安", "沈悦", "韩哲", "顾宁", "唐可", "魏然", "宋扬", "陆清", "白川", "叶蓁", "江临", "罗嘉", "程野", "苏禾", "杜衡", "夏一", "方舟", "余森"]
    banks = ["华夏商业银行", "东海商业银行", "城市联合银行", "中州商业银行"]
    for index, (company_name, person_name) in enumerate(zip(company_names, person_names), start=1):
        company_id = f"DEMO_COMPANY_{index:02d}"
        person_id = f"DEMO_PERSON_{index:02d}"
        account_id = f"DEMO_ACCOUNT_{index:02d}"
        device_id = f"DEMO_DEVICE_{(index - 1) % 4 + 1:02d}"
        next_account = f"DEMO_ACCOUNT_{index % len(company_names) + 1:02d}"
        risk = index in {3, 7, 11, 15}
        _NODES.extend([
            {"id": company_id, "labels": ["Company"], "properties": {"name": company_name, "industry": ["科技服务", "供应链", "先进制造"][index % 3], "registered_capital": 500 + index * 125, "risk_flag": risk, "display_priority": "context"}},
            {"id": person_id, "labels": ["Person"], "properties": {"name": person_name, "risk_flag": risk and index % 2 == 1, "display_priority": "context"}},
            {"id": account_id, "labels": ["Account"], "properties": {"account_no": f"6222028800{index:06d}", "bank": banks[index % len(banks)], "balance": 86000 + index * 17300, "display_priority": "context"}},
        ])
        _EDGES.extend([
            {"id": f"demo-legal-{index:02d}", "type": "LEGAL_REP", "source": person_id, "target": company_id, "properties": {}},
            {"id": f"demo-account-{index:02d}", "type": "HAS_ACCOUNT", "source": company_id, "target": account_id, "properties": {}},
            {"id": f"demo-transfer-{index:02d}", "type": "TRANSFER", "source": account_id, "target": next_account, "properties": {"amount": 98000 + index * 27100, "date": f"2026-08-{(index % 18) + 1:02d}T{8 + index % 10:02d}:24:00+08:00"}},
            {"id": f"demo-login-{index:02d}", "type": "LOGGED_IN", "source": person_id, "target": device_id, "properties": {"date": f"2026-08-{(index % 18) + 1:02d}T21:08:00+08:00"}},
        ])
        if index < len(company_names):
            _EDGES.append({"id": f"demo-owns-{index:02d}", "type": "OWNS", "source": company_id, "target": f"DEMO_COMPANY_{index + 1:02d}", "properties": {"ratio": round(0.18 + (index % 5) * 0.09, 2)}})

    for index in range(1, 5):
        _NODES.append({"id": f"DEMO_DEVICE_{index:02d}", "labels": ["Device"], "properties": {"name": f"关联终端 {index:02d}", "ip": f"10.38.{index}.17", "device_type": "Desktop", "risk_flag": index in {2, 4}, "display_priority": "context"}})

    _EDGES.extend([
        {"id": "demo-bridge-core-a", "type": "OWNS", "source": "LOOP_C", "target": "DEMO_COMPANY_01", "properties": {"ratio": 0.24}},
        {"id": "demo-bridge-core-b", "type": "OWNS", "source": "TARGET_Z", "target": "DEMO_COMPANY_10", "properties": {"ratio": 0.31}},
        {"id": "demo-transfer-core", "type": "TRANSFER", "source": "ACCT_LOOP_C", "target": "DEMO_ACCOUNT_06", "properties": {"amount": 720000, "date": "2026-08-18T18:42:00+08:00"}},
    ])


_extend_demo_graph()


def get_demo_graph() -> Dict[str, Any]:
    """返回独立副本，避免请求间共享对象被修改。"""
    return {"nodes": deepcopy(_NODES), "edges": deepcopy(_EDGES)}


def expand_demo_graph(node_id: str, depth: int = 1) -> Dict[str, Any]:
    """按无向邻接关系返回演示图谱的 1~2 度子图。"""
    graph = get_demo_graph()
    node_ids = {node["id"] for node in graph["nodes"]}
    if node_id not in node_ids:
        raise KeyError(node_id)

    selected = {node_id}
    frontier = {node_id}
    for _ in range(max(1, min(2, depth))):
        neighbours = set()
        for edge in graph["edges"]:
            if edge["source"] in frontier:
                neighbours.add(edge["target"])
            if edge["target"] in frontier:
                neighbours.add(edge["source"])
        frontier = neighbours - selected
        selected.update(neighbours)

    return {
        "nodes": [node for node in graph["nodes"] if node["id"] in selected],
        "edges": [edge for edge in graph["edges"] if edge["source"] in selected and edge["target"] in selected],
    }


def get_demo_case(query: str) -> Dict[str, Any]:
    """根据查询选择可解释的离线演示案例。"""
    normalized = query.lower()
    if any(keyword in normalized for keyword in ("实控", "ubo", "股权", "自融")):
        nodes = ["PERSON_BOSS", "HOLDING_X", "SHELL_Y", "TARGET_Z"]
        edge_types = ["OWNS"]
        risk_summary = "四层持股关系最终汇聚至高远，启明控股与瀚海咨询形成控制权传导，关联授信存在集中暴露。"
        compliance_summary = "复杂所有权结构触发受益所有人穿透识别要求，需补充最终自然人控制证明并开展强化尽调。"
        decision_summary = "暂缓自动授信审批；在受益所有人、关联交易与资金用途资料核验完成前，转入人工增强审查。"
        score = 86
        clause = ("反洗钱法·第十七条", "金融机构应当采取合理措施识别受益所有人，了解实际控制客户或交易的自然人，穿透复杂所有权结构。")
    elif any(keyword in normalized for keyword in ("资金", "回环", "转账", "交易", "快进快出", "洗钱")):
        nodes = ["ACCT_LOOP_A", "ACCT_LOOP_B", "ACCT_LOOP_C"]
        edge_types = ["TRANSFER"]
        risk_summary = "三账户在 7 小时 19 分内完成 546.3 万元循环转账，回流金额保留率达 97.2%，显著偏离正常贸易结算节奏。"
        compliance_summary = "短期大额资金快进快出并闭环回流，命中可疑交易识别特征，需核验交易背景、发票与最终受益人。"
        decision_summary = "标记为高风险并暂缓自动审批；补齐资金用途凭证和交易对手关系说明后，由信贷员完成最终裁决。"
        score = 92
        clause = ("3号令·第十二条", "可疑交易特征包括短期内资金快进快出，以及与客户身份、经营规模明显不符的交易。")
    else:
        nodes = []
        edge_types = []
        risk_summary = "当前演示图谱未找到与该问题匹配的案件证据；不能据此判断主体风险。"
        compliance_summary = "缺少可定位的交易、主体关系及原始记录，需先确认调查对象与补充授权数据。"
        decision_summary = "待补证：请提供明确的企业或账户线索，并核对原始交易与主体资料后重新调查。"
        score = None
        clause = None
    return {
        "nodes": nodes,
        "edge_types": edge_types,
        "summary": risk_summary,
        "risk_summary": risk_summary,
        "compliance_summary": compliance_summary,
        "decision_summary": decision_summary,
        "score": score,
        "clause": clause,
    }
