"""
backend/rag/spatiotemporal_aml.py
=================================
时空聚集反洗钱检测（创新点二 · P1）—— 确定性图算法，零 LLM 调用。

设计定位
--------
传统反洗钱的「时空聚集」判断依赖合规 Agent 的 LLM prompt 去「猜」，结论不可复现、
不可审计，且可能遗漏确定性可判定的高危模式。本模块用**确定性图算法**（非 LLM）
直接从子图拓扑 + 时间戳中检出三类典型洗钱特征：

    1. 设备 / IP 聚集   —— 同一设备或同一 IP 被多个自然人登录（设备复用 = 代账团伙/黑产）；
    2. 快进快出         —— 账户在短时间窗口内「进多少、出多少」，资金过账不留痕；
    3. 分散转入集中转出 —— 短期内多账户资金汇入单一账户后集中转出（3号令·第十二条）。

算法只负责**确定性检出**（可复现、可解释），至于「为什么可疑」的语义解读仍交给
LLM 合规 Agent——从而把 XAI 链路中「子图 → 结论」的自由文本段，替换为
「确定性检出 → LLM 解释」的强证据链。数据模型无需改动（复用 Schema 已有的
Device + LOGGED_IN(date) + TRANSFER(amount,date)）。

所有检测函数均为纯函数：输入统一的 {nodes, edges} 子图 JSON，输出结构化的
findings 列表，便于单测与审计。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

# ======================================================================
# 检测配置（默认阈值，可通过参数覆盖；金额单位为元）
# ======================================================================
DEFAULT_CONFIG: Dict[str, Any] = {
    "device_window_hours": 24 * 30,   # 设备/IP 聚集的时间跨度（登录日期最大差）
    "min_shared_persons": 2,          # 判定「设备/IP 复用」的最少关联自然人
    "flow_window_hours": 72,          # 快进快出 / 分散转集中的时间窗口
    "min_amount": 1_000_000,          # 仅关注 ≥100 万元的大额资金（过滤随机小流水噪声）
    "fast_ratio": 0.8,                # 快进快出：窗口内 出/入 比例阈值
    "min_dispersed_sources": 3,       # 分散转入集中转出：最少独立转入源账户数
    "max_findings": 20,               # 单次返回的 findings 上限（防刷屏）
}


# ======================================================================
# 基础工具
# ======================================================================
def _parse_dt(value: Any) -> Optional[datetime]:
    """解析 ISO 时间戳（如 2024-05-10T10:00:00）；失败返回 None（该边不参与时间判定）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _edges_of_type(graph_json: Dict[str, Any], etype: str) -> List[Dict[str, Any]]:
    """取子图中指定类型的边列表。"""
    return [e for e in graph_json.get("edges", []) if e.get("type") == etype]


def _node_labels(graph_json: Dict[str, Any]) -> Dict[str, set]:
    """node_id -> 标签集合（用于区分 Person / Device / Account 等）。"""
    return {n["id"]: set(n.get("labels", []) or []) for n in graph_json.get("nodes", [])}


def _hours_between(a: datetime, b: datetime) -> float:
    """两时间戳的间隔小时数（绝对值）。"""
    return abs((a - b).total_seconds()) / 3600.0


# ======================================================================
# 检测一：设备 / IP 聚集（设备复用 = 代账团伙 / 黑产）
# ======================================================================
def detect_device_clustering(
    graph_json: Dict[str, Any],
    window_hours: Optional[float] = None,
    min_persons: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    检出「同一设备 / 同一 IP 被多个自然人登录」的聚集簇。

    依据：正常账户的登录设备应是私有的；同一台设备 / 同一 IP 若同时关联多个
    自然人，则高度疑似代账团伙集中操控或黑产（设备复用是反洗钱的高危信号）。

    Returns:
        findings，每项：
          {pattern:"设备聚集", node_ids:[...], evidence:{device_id, ip, persons, dates},
           description:"..."}
    """
    window_hours = window_hours if window_hours is not None else DEFAULT_CONFIG["device_window_hours"]
    min_persons = min_persons if min_persons is not None else DEFAULT_CONFIG["min_shared_persons"]

    nodes = {n["id"]: n for n in graph_json.get("nodes", [])}
    labels = _node_labels(graph_json)

    # 设备节点的 ip；以及每个设备被哪些自然人登录（沿 LOGGED_IN）
    device_ip: Dict[str, str] = {}
    persons_of_device: Dict[str, set] = defaultdict(set)
    dates_of_device: Dict[str, List[datetime]] = defaultdict(list)

    for e in _edges_of_type(graph_json, "LOGGED_IN"):
        s, t = e["source"], e["target"]
        # LOGGED_IN 方向为 Person -> Device（防御性：任一端是 Device 均可）
        person, device = (s, t) if "Device" in labels.get(t, set()) else (t, s)
        if "Device" not in labels.get(device, set()):
            continue
        persons_of_device[device].add(person)
        device_ip[device] = (nodes.get(device, {}).get("properties", {}) or {}).get("ip", "")
        dt = _parse_dt((e.get("properties") or {}).get("date"))
        if dt:
            dates_of_device[device].append(dt)

    findings: List[Dict[str, Any]] = []
    for device, persons in persons_of_device.items():
        if len(persons) < min_persons:
            continue
        # 时间跨度过滤（可选）：登录日期跨度超过窗口则不算「聚集」
        dts = dates_of_device.get(device, [])
        if dts and window_hours is not None:
            span = max(dts) - min(dts)
            if span.total_seconds() / 3600.0 > window_hours:
                continue
        findings.append({
            "pattern": "设备聚集",
            "node_ids": sorted(persons) + [device],
            "evidence": {
                "device_id": device,
                "ip": device_ip.get(device, ""),
                "persons": sorted(persons),
                "login_dates": sorted(d.strftime("%Y-%m-%dT%H:%M:%S") for d in dts),
            },
            "description": f"设备 {device}（IP {device_ip.get(device, '未知')}）被 {len(persons)} 名自然人登录，疑似代账团伙或黑产集中操控",
        })

    # 同一 IP 关联多台设备（跨设备聚集）
    ip_to_persons: Dict[str, set] = defaultdict(set)
    ip_to_devices: Dict[str, set] = defaultdict(set)
    for device, ip in device_ip.items():
        if not ip:
            continue
        ip_to_persons[ip] |= persons_of_device.get(device, set())
        ip_to_devices[ip].add(device)
    for ip, persons in ip_to_persons.items():
        if len(persons) >= min_persons and len(ip_to_devices[ip]) >= 2:
            findings.append({
                "pattern": "设备聚集",
                "node_ids": sorted(persons) + sorted(ip_to_devices[ip]),
                "evidence": {"ip": ip, "devices": sorted(ip_to_devices[ip]), "persons": sorted(persons)},
                "description": f"同一 IP {ip} 关联 {len(ip_to_devices[ip])} 台设备、{len(persons)} 名自然人，疑似黑产设备农场",
            })

    return findings


# ======================================================================
# 检测二：快进快出（资金过账不留痕）
# ======================================================================
def detect_fast_in_fast_out(
    graph_json: Dict[str, Any],
    window_hours: Optional[float] = None,
    min_amount: Optional[float] = None,
    fast_ratio: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    检出「短时间窗口内资金快进快出」的账户：进多少、出多少，账户只当过账通道。

    依据：洗钱 / 骗贷常用过账账户「快进快出」制造流水假象，资金停留时间极短。
    """
    window_hours = window_hours if window_hours is not None else DEFAULT_CONFIG["flow_window_hours"]
    min_amount = min_amount if min_amount is not None else DEFAULT_CONFIG["min_amount"]
    fast_ratio = fast_ratio if fast_ratio is not None else DEFAULT_CONFIG["fast_ratio"]

    # account_id -> {incoming: [(amount, dt, edge_id, src)], outgoing: [...]}
    flows: Dict[str, Dict[str, List[tuple]]] = defaultdict(lambda: {"in": [], "out": []})
    for e in _edges_of_type(graph_json, "TRANSFER"):
        props = e.get("properties", {}) or {}
        amount = props.get("amount")
        dt = _parse_dt(props.get("date"))
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        record = (amount, dt, e["id"], e["source"])
        flows[e["target"]]["in"].append(record)
        flows[e["source"]]["out"].append((amount, dt, e["id"], e["target"]))

    findings: List[Dict[str, Any]] = []
    for acct, f in flows.items():
        if not f["in"] or not f["out"]:
            continue
        # 在窗口内同时存在大额流入与流出，且流出占流入比例 ≥ fast_ratio
        best: Dict[str, Any] = {}
        best_total_in = 0.0
        for in_a, in_dt, in_eid, in_src in f["in"]:
            if in_a < min_amount:
                continue
            for out_a, out_dt, out_eid, out_tgt in f["out"]:
                if in_dt and out_dt and _hours_between(in_dt, out_dt) > window_hours:
                    continue
                if out_a / in_a >= fast_ratio:
                    total_in = in_a
                    if total_in > best_total_in:
                        best_total_in = total_in
                        best = {
                            "account_id": acct,
                            "in_amount": in_a,
                            "out_amount": out_a,
                            "in_edge": in_eid,
                            "out_edge": out_eid,
                            "in_date": in_dt.strftime("%Y-%m-%dT%H:%M:%S") if in_dt else "",
                            "out_date": out_dt.strftime("%Y-%m-%dT%H:%M:%S") if out_dt else "",
                        }
        if best:
            findings.append({
                "pattern": "快进快出",
                "node_ids": [best["account_id"]],
                "evidence": best,
                "description": f"账户 {best['account_id']} 在窗口内流入 {best['in_amount']:.0f} 元、流出 {best['out_amount']:.0f} 元，资金快进快出、过账不留痕",
            })
    return findings


# ======================================================================
# 检测三：分散转入集中转出（多账户汇入 → 单账户集中转出）
# ======================================================================
def detect_dispersed_in_concentrated_out(
    graph_json: Dict[str, Any],
    window_hours: Optional[float] = None,
    min_amount: Optional[float] = None,
    min_sources: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    检出「短期内资金分散转入、集中转出」的账户簇（3号令·第十二条）。

    依据：洗钱典型手法——多个无关账户把资金汇入一个「归集账户」，再集中转出
    至单一去向；或反向「集中转入、分散转出」。均属可疑交易特征。
    """
    window_hours = window_hours if window_hours is not None else DEFAULT_CONFIG["flow_window_hours"]
    min_amount = min_amount if min_amount is not None else DEFAULT_CONFIG["min_amount"]
    min_sources = min_sources if min_sources is not None else DEFAULT_CONFIG["min_dispersed_sources"]

    flows: Dict[str, Dict[str, List[tuple]]] = defaultdict(lambda: {"in": [], "out": []})
    for e in _edges_of_type(graph_json, "TRANSFER"):
        props = e.get("properties", {}) or {}
        amount = props.get("amount")
        dt = _parse_dt(props.get("date"))
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        flows[e["target"]]["in"].append((amount, dt, e["source"]))
        flows[e["source"]]["out"].append((amount, dt, e["target"]))

    findings: List[Dict[str, Any]] = []
    for acct, f in flows.items():
        if not f["in"] or not f["out"]:
            continue
        # 在窗口内，把「大额转入」按时间聚成一组，统计独立源与独立去向
        window_ins = [(a, d, s) for a, d, s in f["in"] if a >= min_amount]
        window_outs = [(a, d, t) for a, d, t in f["out"] if a >= min_amount]
        if not window_ins or not window_outs:
            continue

        # 时间基准取「该账户最早一笔大额流入」，窗口内其余交易均需落于其中
        anchor = min(d for _, d, _ in window_ins if d is not None) if any(d for _, d, _ in window_ins) else None
        if anchor is None:
            continue
        ins_in_window = [(a, d, s) for a, d, s in window_ins if d is None or _hours_between(anchor, d) <= window_hours]
        outs_in_window = [(a, d, t) for a, d, t in window_outs if d is None or _hours_between(anchor, d) <= window_hours]

        src_set = {s for _, _, s in ins_in_window}
        tgt_set = {t for _, _, t in outs_in_window}
        total_in = sum(a for a, _, _ in ins_in_window)

        # 分散转入 + 集中转出（或反向）
        if len(src_set) >= min_sources and len(tgt_set) == 1:
            findings.append({
                "pattern": "分散转入集中转出",
                "node_ids": [acct] + sorted(src_set) + sorted(tgt_set),
                "evidence": {
                    "account_id": acct,
                    "source_accounts": sorted(src_set),
                    "target_accounts": sorted(tgt_set),
                    "total_in": total_in,
                    "window_hours": window_hours,
                },
                "description": f"账户 {acct} 在 {window_hours}h 内接收 {len(src_set)} 个账户资金、集中转出至单一去向，符合「分散转入集中转出」可疑特征",
            })
        elif len(tgt_set) >= min_sources and len(src_set) == 1:
            findings.append({
                "pattern": "分散转入集中转出",
                "node_ids": [acct] + sorted(src_set) + sorted(tgt_set),
                "evidence": {
                    "account_id": acct,
                    "source_accounts": sorted(src_set),
                    "target_accounts": sorted(tgt_set),
                    "total_in": total_in,
                    "window_hours": window_hours,
                },
                "description": f"账户 {acct} 在 {window_hours}h 内接收单笔资金、分散转出至 {len(tgt_set)} 个账户，符合「集中转入分散转出」可疑特征",
            })

    return findings


# ======================================================================
# 统一入口
# ======================================================================
def run_spatiotemporal_detection(
    graph_json: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    在给定子图上执行全部时空聚集检测，返回合并后的 findings 列表。

    Args:
        graph_json: serialize_graph_result 产出的 {nodes, edges} 子图。
        config:     可选阈值覆盖；缺省使用 DEFAULT_CONFIG。

    Returns:
        findings（按 pattern 分组），每条含 pattern / node_ids / evidence / description。
        结果按确定性检出，与 LLM 无关，可复现、可审计。
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    findings: List[Dict[str, Any]] = []
    findings += detect_device_clustering(
        graph_json,
        window_hours=cfg["device_window_hours"],
        min_persons=cfg["min_shared_persons"],
    )
    findings += detect_fast_in_fast_out(
        graph_json,
        window_hours=cfg["flow_window_hours"],
        min_amount=cfg["min_amount"],
        fast_ratio=cfg["fast_ratio"],
    )
    findings += detect_dispersed_in_concentrated_out(
        graph_json,
        window_hours=cfg["flow_window_hours"],
        min_amount=cfg["min_amount"],
        min_sources=cfg["min_dispersed_sources"],
    )
    return findings[: cfg["max_findings"]]


# ======================================================================
# 检测四：等额短时资金回环（虚假贸易 / 骗贷）
# ======================================================================
def _account_owners(graph_json: Dict[str, Any]) -> Dict[str, set]:
    """account_id -> 持有该账户的公司/自然人 id 集合（HAS_ACCOUNT 反向索引）。"""
    owners: Dict[str, set] = {}
    for e in graph_json.get("edges", []):
        if e.get("type") == "HAS_ACCOUNT":
            owners.setdefault(e["target"], set()).add(e["source"])
    return owners


def detect_money_loops(
    graph_json: Dict[str, Any],
    min_amount: Optional[float] = None,
    tolerance: float = 0.2,
    window_hours: Optional[float] = None,
) -> set:
    """
    检出「等额、短时」的资金回环账户（确定性）。

    资金回环 = 虚假贸易 / 骗贷的确定性签名：≥3 个账户构成转账环，且环上各笔金额
    相近（相对偏差 ≤ tolerance）、发生时间跨度 ≤ window_hours、单笔 ≥ min_amount。
    仅有拓扑环但金额/时间不匹配的随机环（正常经营流水）**不算**，从而避免把海量
    随机小额流水（沙盒随机转账 5k~80 万）误判为回环。
    """
    min_amount = min_amount if min_amount is not None else DEFAULT_CONFIG["min_amount"]
    window_hours = window_hours if window_hours is not None else DEFAULT_CONFIG["flow_window_hours"]

    adj: Dict[str, List[tuple]] = {}
    for e in _edges_of_type(graph_json, "TRANSFER"):
        props = e.get("properties", {}) or {}
        try:
            amount = float(props.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount < min_amount:
            continue
        adj.setdefault(e["source"], []).append((e["target"], amount, _parse_dt(props.get("date"))))

    loop_nodes: set = set()

    def dfs(start: str, path: List[str], amounts: List[float], dts: List[Optional[datetime]]) -> None:
        u = path[-1]
        for v, amt, dt in adj.get(u, []):
            if v == start:
                cyc_amounts = amounts + [amt]
                cyc_dts = [d for d in dts + ([dt] if dt else []) if d is not None]
                if len(cyc_amounts) >= 3:
                    lo, hi = min(cyc_amounts), max(cyc_amounts)
                    equal_ok = lo > 0 and (hi - lo) / lo <= tolerance
                    time_ok = (len(cyc_dts) < 2) or (
                        (max(cyc_dts) - min(cyc_dts)).total_seconds() / 3600.0 <= window_hours
                    )
                    if equal_ok and time_ok:
                        loop_nodes.update(path)
                continue
            if v in path or len(path) >= 6:
                continue
            dfs(start, path + [v], amounts + [amt], dts + ([dt] if dt else []))

    for n in list(adj.keys()):
        dfs(n, [n], [], [])
    return loop_nodes


def detect_ubo_self_financing(
    graph_json: Dict[str, Any],
    min_amount: Optional[float] = None,
) -> set:
    """
    检出「影子实控人自融」涉及的节点（确定性）：同一自然人（直接或经一层中间公司）
    持股 ≥2 家公司，且这些公司的账户之间存在大额 TRANSFER 往来（自融走账）。

    返回 {实控人} ∪ {被穿透命中的公司} ∪ {中间控股层} ∪ {参与自融走账的账户}。
    """
    min_amount = min_amount if min_amount is not None else DEFAULT_CONFIG["min_amount"]
    edges = graph_json.get("edges", [])
    labels = _node_labels(graph_json)

    owns: Dict[str, set] = {}
    for e in edges:
        if e.get("type") == "OWNS":
            owns.setdefault(e["source"], set()).add(e["target"])

    owners = _account_owners(graph_json)
    transfer_pairs: set = set()
    for e in edges:
        if e.get("type") != "TRANSFER":
            continue
        try:
            amount = float((e.get("properties") or {}).get("amount"))
        except (TypeError, ValueError):
            continue
        if amount >= min_amount:
            transfer_pairs.add(frozenset((e["source"], e["target"])))

    suspicious: set = set()
    for pid, lbl in labels.items():
        if "Person" not in lbl:
            continue
        companies: set = set()
        direct = owns.get(pid, set())
        companies |= direct
        for mid in direct:
            companies |= owns.get(mid, set())
        companies = {c for c in companies if "Company" in labels.get(c, set())}
        if len(companies) < 2:
            continue
        accts = [a for c in companies for a, o in owners.items() if c in o]
        cross_accts: set = set()
        for i in range(len(accts)):
            for j in range(i + 1, len(accts)):
                if frozenset((accts[i], accts[j])) in transfer_pairs:
                    cross_accts.update((accts[i], accts[j]))
        if cross_accts:
            suspicious.add(pid)
            suspicious |= companies
            suspicious |= direct & set(labels)
            suspicious |= cross_accts
    return suspicious


# ======================================================================
# 统一确定性检测入口（risk_flag + 回环 + 自融 + 时空聚集）
# ======================================================================
def run_deterministic_detection(
    graph_json: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    整合所有确定性检测（非 LLM），作为「图谱 grounding」的权威证据锚点。

    返回：
      suspicious_nodes : 合并去重后的高危节点 id（供决策层 / 规则基线 / 前端高亮）
      findings         : 时空聚集 findings（供合规 Agent 解读 + 前端展示）
      risk_flag_nodes  : 直接命中 risk_flag 的节点
      loop_accounts    : 落在等额短时回环上的账户
      ubo_nodes        : 自融穿透命中的节点
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    nodes = graph_json.get("nodes", [])
    risk_flag_nodes = ({n["id"] for n in nodes if (n.get("properties") or {}).get("risk_flag") is True}
                       if cfg.get("use_prior_flags", False) else set())

    loop_accounts = detect_money_loops(
        graph_json, min_amount=cfg["min_amount"], window_hours=cfg["flow_window_hours"]
    )
    owners = _account_owners(graph_json)
    loop_nodes = set(loop_accounts)
    for acct in loop_accounts:
        loop_nodes |= owners.get(acct, set())

    ubo_nodes = detect_ubo_self_financing(graph_json, min_amount=cfg["min_amount"])

    findings = run_spatiotemporal_detection(graph_json, cfg)
    finding_nodes = {nid for f in findings for nid in f.get("node_ids", [])}

    suspicious = risk_flag_nodes | loop_nodes | ubo_nodes | finding_nodes
    return {
        "suspicious_nodes": sorted(suspicious),
        "findings": findings,
        "risk_flag_nodes": sorted(risk_flag_nodes),
        "loop_accounts": sorted(loop_accounts),
        "ubo_nodes": sorted(ubo_nodes),
    }


def format_deterministic_summary(det: Dict[str, Any]) -> str:
    """把确定性检测结果压缩为供决策层使用的权威证据锚点文本。"""
    nodes = det.get("suspicious_nodes", [])
    if not nodes:
        return "（未检出等额短时回环、自融穿透或时空聚集等确定性风险模式）"
    lines = [f"确定性命中 {len(nodes)} 个高危节点：{', '.join(nodes)}"]
    if det.get("risk_flag_nodes"):
        lines.append(f"- risk_flag 命中：{', '.join(det['risk_flag_nodes'])}")
    if det.get("loop_accounts"):
        lines.append(f"- 等额短时回环账户：{', '.join(det['loop_accounts'])}")
    if det.get("ubo_nodes"):
        lines.append(f"- 自融穿透：{', '.join(det['ubo_nodes'])}")
    if det.get("findings"):
        lines.append(f"- 时空聚集：{len(det['findings'])} 项")
    return "\n".join(lines)


def format_findings_for_prompt(findings: List[Dict[str, Any]]) -> str:
    """把 findings 压缩为可注入 LLM 提示词的文本块（用于「确定性检出 → LLM 解释」）。"""
    if not findings:
        return "（确定性时空聚集检测：未检出高危聚集模式）"
    lines = []
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. [{f['pattern']}] {f['description']}")
        ev = f.get("evidence", {})
        if ev:
            detail = ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("persons", "login_dates", "source_accounts", "target_accounts", "devices"))
            lines.append(f"   证据: {detail}")
    return "\n".join(lines)


# ======================================================================
# 自检（无需 Neo4j / LLM）
# ======================================================================
def _synthetic_graph() -> Dict[str, Any]:
    """构造覆盖「设备聚集 / 快进快出 / 分散转入集中转出」三类模式的最小合成子图。"""
    def node(nid, labels, **props):
        return {"id": nid, "labels": labels, "properties": props}

    def edge(s, t, etype, **props):
        return {"id": f"{s}->{t}", "type": etype, "source": s, "target": t, "properties": props}

    return {
        "nodes": [
            node("P1", ["Person"]), node("P2", ["Person"]),
            node("DEV_SHARED", ["Device"], ip="10.0.0.9", mac="AA:BB"),
            node("ACCT_X", ["Account"]), node("ACCT_A", ["Account"]),
            node("ACCT_B", ["Account"]), node("ACCT_C", ["Account"]),
            node("ACCT_OUT", ["Account"]),
        ],
        "edges": [
            # 设备聚集：同一设备被 P1/P2 登录
            edge("P1", "DEV_SHARED", "LOGGED_IN", date="2024-05-10T09:00:00"),
            edge("P2", "DEV_SHARED", "LOGGED_IN", date="2024-05-10T10:00:00"),
            # 快进快出：ACCT_X 1小时内进 500万 出 490万
            edge("ACCT_A", "ACCT_X", "TRANSFER", amount=5_000_000.0, date="2024-05-10T08:00:00"),
            edge("ACCT_X", "ACCT_B", "TRANSFER", amount=4_900_000.0, date="2024-05-10T08:30:00"),
            # 分散转入集中转出：A/B/C 汇入 OUT，OUT 集中转出
            edge("ACCT_A", "ACCT_OUT", "TRANSFER", amount=2_000_000.0, date="2024-05-10T07:00:00"),
            edge("ACCT_B", "ACCT_OUT", "TRANSFER", amount=2_000_000.0, date="2024-05-10T07:10:00"),
            edge("ACCT_C", "ACCT_OUT", "TRANSFER", amount=2_000_000.0, date="2024-05-10T07:20:00"),
            edge("ACCT_OUT", "ACCT_X", "TRANSFER", amount=6_000_000.0, date="2024-05-10T08:00:00"),
        ],
    }


def selftest() -> bool:
    g = _synthetic_graph()
    findings = run_spatiotemporal_detection(g)
    patterns = {f["pattern"] for f in findings}
    ok = {"设备聚集", "快进快出", "分散转入集中转出"} <= patterns
    print(f"[selftest] 时空聚集检测命中模式: {patterns}")
    for f in findings:
        print(f"          - [{f['pattern']}] {f['description']}")
    print("[selftest] prompt 文本片段:")
    print(format_findings_for_prompt(findings))
    return ok


if __name__ == "__main__":
    ok = selftest()
    print("\n" + ("✅ 时空聚集检测自检通过" if ok else "❌ 自检存在失败项"))
    raise SystemExit(0 if ok else 1)
