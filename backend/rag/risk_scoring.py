"""
backend/rag/risk_scoring.py
============================
RiskScoringEngine —— 模拟 GraphSAGE 的确定性节点风险评分引擎（prompts.txt 指令1）。

设计定位
--------
竞赛要求「引入时序与 GNN 接口，模拟运行 GraphSAGE 算法对节点进行初步风险评分」。
GraphSAGE（Hamilton et al., 2017）的核心思想是「采样邻居 + 聚合邻居特征」的
归纳式图表示学习：每个节点的表征由其自身特征与邻居表征的聚合共同决定，经 K 层
迭代后，风险会沿边向邻近节点传播。

本模块在【不引入任何深度学习框架】的前提下，用**确定性的图消息传递**逼近
GraphSAGE 的评分能力，做到可复现、可审计、零 LLM 调用：

    1. 特征初始化（risk seed）：为每个节点构造「风险种子」标量——
       大额转账参与度（Account）、设备复用（Device）；已知风险标签默认不参与推断；
    2. K 层邻居聚合：h_v^(k+1) = (1-α)·h_v^(k) + α·mean(h_u^(k) | u∈N(v))
       —— 即 GraphSAGE 的 mean 聚合器 + 自身残差连接；
    3. 归一化：线性缩放到 0~100，作为「风险沿图传播后的连续强度」。

与 `spatiotemporal_aml.py` 的确定性检测互补：确定性检测回答「是否命中硬性拓扑」，
风险评分回答「风险在网络中的传播强度与相对排序」。评分作为 metadata 注入下游
Agent，帮助其理解「哪些节点虽无显式标记、但被高危邻居连累」。

性能与并发
----------
纯函数式设计：`score()` 全程只在局部变量中构建邻接表与特征，不修改任何共享状态，
单个 `RiskScoringEngine` 实例可被多线程并发复用（天然线程安全）。邻接表只构建一次，
K 层聚合为 O(K·(V+E))，子图规模（<300 节点）下亚毫秒级完成。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional


# ======================================================================
# 评分引擎
# ======================================================================
class RiskScoringEngine:
    """
    模拟 GraphSAGE 的确定性节点风险评分器。

    Args:
        layers:    消息传递层数（K），对应 GraphSAGE 的聚合深度，默认 2。
        alpha:     邻居聚合权重（0~1）；(1-α) 为自身特征保留权重，默认 0.5。
        min_amount:判定「大额转账」的金额阈值（元），用于 Account 风险种子，默认 100 万。
        shared_device_threshold:设备复用判定阈值（自然人登录数），默认 2。

    使用方式：
        engine = RiskScoringEngine()
        scores = engine.score(graph_json)          # {node_id: 0~100}
    """

    def __init__(
        self,
        layers: int = 2,
        alpha: float = 0.5,
        min_amount: float = 1_000_000,
        shared_device_threshold: int = 2,
        use_prior_flags: bool = False,
    ) -> None:
        self.layers = max(1, int(layers))
        self.alpha = max(0.0, min(1.0, float(alpha)))
        self.min_amount = float(min_amount)
        self.shared_device_threshold = max(1, int(shared_device_threshold))
        self.use_prior_flags = use_prior_flags

    # ------------------------------------------------------------------
    # 特征初始化：为每个节点构造 [0,1] 的风险种子
    # ------------------------------------------------------------------
    def _seed_features(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> Dict[str, float]:
        """
        构造风险种子（特征向量退化为单标量，满足「初步风险评分」的轻量定位）。

        种子规则（确定性、可解释）：
        - risk_flag=true           → 仅在显式启用外部先验标签时贡献 1.0；
        - Account 参与大额转账     → 每笔 ≥min_amount 的相邻转账贡献 0.5（封顶 1.0）；
        - Device 被多人登录（复用） → 复用人数越多贡献越高（封顶 1.0）。
        """
        labels = {n["id"]: set(n.get("labels", []) or []) for n in nodes}
        large_degree: Dict[str, int] = defaultdict(int)      # Account 大额转账参与次数
        device_persons: Dict[str, set] = defaultdict(set)    # Device → 登录它的自然人

        for e in edges:
            etype = e.get("type")
            props = e.get("properties", {}) or {}
            s, t = e["source"], e["target"]
            if etype == "TRANSFER":
                try:
                    amount = float(props.get("amount"))
                except (TypeError, ValueError):
                    amount = 0.0
                if amount >= self.min_amount:
                    large_degree[s] += 1
                    large_degree[t] += 1
            elif etype == "LOGGED_IN":
                # LOGGED_IN 方向为 Person -> Device；防御性判断任一端为 Device
                if "Device" in labels.get(t, set()):
                    device_persons[t].add(s)
                elif "Device" in labels.get(s, set()):
                    device_persons[s].add(t)

        seeds: Dict[str, float] = {}
        for n in nodes:
            nid = n["id"]
            props = n.get("properties", {}) or {}
            score = 1.0 if self.use_prior_flags and props.get("risk_flag") is True else 0.0
            if "Account" in labels[nid] and large_degree[nid] > 0:
                score += min(1.0, 0.5 * large_degree[nid])
            if "Device" in labels[nid]:
                k = len(device_persons[nid])
                if k >= self.shared_device_threshold:
                    # 复用人数越多越可疑：2 人 → 0.5，3 人 → 1.0（封顶）
                    score += min(1.0, 0.5 * (k - self.shared_device_threshold + 1))
            seeds[nid] = min(1.0, score)
        return seeds

    # ------------------------------------------------------------------
    # 主流程：种子 → K 层邻居聚合 → 0~100 归一化
    # ------------------------------------------------------------------
    def score(self, graph_json: Dict[str, Any]) -> Dict[str, float]:
        """
        对子图 `{nodes, edges}` 执行 GraphSAGE 式评分，返回 {node_id: 0~100}。

        Args:
            graph_json: serialize_graph_result 产出的 {nodes, edges} 子图，
                        边须含 source / target / type / properties。

        Returns:
            节点 id → 风险评分（0~100，越大越危险）。空图返回空 dict。
            采用「最大归一化」：子图内最危险节点恒为 100，其余按其相对强度折算，
            故评分是子图内的相对风险排序，非跨图可比绝对分。
        """
        nodes = graph_json.get("nodes", [])
        edges = graph_json.get("edges", [])
        if not nodes:
            return {}

        # 1) 邻接表（无向，风险沿任意关系双向传播）
        adjacency: Dict[str, List[str]] = defaultdict(list)
        for e in edges:
            s, t = e["source"], e["target"]
            adjacency[s].append(t)
            adjacency[t].append(s)

        # 2) 特征初始化
        h = self._seed_features(nodes, edges)

        # 3) K 层消息传递（GraphSAGE mean 聚合器 + 残差连接）
        for _ in range(self.layers):
            next_h: Dict[str, float] = {}
            for nid, cur in h.items():
                nbrs = adjacency.get(nid, [])
                agg = (sum(h.get(u, 0.0) for u in nbrs) / len(nbrs)) if nbrs else cur
                next_h[nid] = (1 - self.alpha) * cur + self.alpha * agg
            h = next_h

        # 4) 最大归一化到 0~100（全零子图保持全零，避免除零）
        max_h = max(h.values()) if h else 0.0
        if max_h <= 0.0:
            return {nid: 0.0 for nid in h}
        return {nid: round(100.0 * v / max_h, 1) for nid, v in h.items()}


# ======================================================================
# 便捷工具：评分 → 提示词文本
# ======================================================================
def format_risk_scores(scores: Dict[str, float], top_k: int = 10) -> str:
    """
    把风险评分压缩为可注入 LLM 提示词的文本块（按评分降序取 top_k）。

    供「初步风险评分 metadata」注入合规 / 决策 Agent，帮助其感知「风险沿图传播后
    的相对高危节点」，而不只是显式 risk_flag 标记。
    """
    if not scores:
        return "（无风险评分：子图为空）"
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"- {nid}: {score:.1f}" for nid, score in ranked[:top_k]]
    return "\n".join(lines)


# ======================================================================
# 自检（无需 Neo4j / LLM）
# ======================================================================
def _synthetic_graph() -> Dict[str, Any]:
    """构造最小合成子图：一个 risk_flag 公司 + 其账户 + 一个「无辜」独立节点。"""
    def node(nid, labels, **props):
        return {"id": nid, "labels": labels, "properties": props}

    def edge(s, t, etype, **props):
        return {"id": f"{s}->{t}", "type": etype, "source": s, "target": t, "properties": props}

    return {
        "nodes": [
            node("BAD_CO", ["Company"], risk_flag=True),
            node("BAD_ACCT", ["Account"]),
            node("CLEAN_CO", ["Company"], risk_flag=False),
            node("CLEAN_ACCT", ["Account"]),
        ],
        "edges": [
            edge("BAD_CO", "BAD_ACCT", "HAS_ACCOUNT"),
            edge("BAD_ACCT", "CLEAN_ACCT", "TRANSFER", amount=5_000_000.0, date="2024-05-10T10:00:00"),
            edge("CLEAN_CO", "CLEAN_ACCT", "HAS_ACCOUNT"),
        ],
    }


def selftest() -> bool:
    g = _synthetic_graph()
    scores = RiskScoringEngine(use_prior_flags=True).score(g)
    ok = True

    # 1) risk_flag 公司为最危险节点（100 分）
    ok &= abs(scores.get("BAD_CO", 0.0) - 100.0) < 1e-9
    # 2) 与其相邻的账户被「传染」出非零风险（风险沿边传播）
    ok &= scores.get("BAD_ACCT", 0.0) > 0.0
    # 3) 无辜节点风险显著低于危险节点
    ok &= scores.get("CLEAN_CO", 0.0) < scores.get("BAD_CO", 0.0)
    ok &= scores.get("CLEAN_ACCT", 0.0) < scores.get("BAD_ACCT", 0.0)
    # 4) 评分落在 [0,100]
    ok &= all(0.0 <= v <= 100.0 for v in scores.values())
    # Default inference cannot read the same synthetic label later used as ground truth.
    flag_only = {"nodes": [{"id": "FLAGGED", "labels": ["Company"], "properties": {"risk_flag": True}}], "edges": []}
    ok &= RiskScoringEngine().score(flag_only)["FLAGGED"] == 0.0

    print("[selftest] 风险评分（GraphSAGE 模拟）:")
    for nid, v in sorted(scores.items(), key=lambda kv: -kv[1]):
        print(f"          {nid:<12} {v:>5}")
    print("[selftest] 提示词文本片段:")
    print(format_risk_scores(scores, top_k=3))
    return ok


if __name__ == "__main__":
    ok = selftest()
    print("\n" + ("✅ 风险评分引擎自检通过" if ok else "❌ 自检存在失败项"))
    raise SystemExit(0 if ok else 1)
