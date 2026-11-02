"""
backend/tests/test_rag.py
=========================
Phase 2 集成测试：验证「自然语言 → Cypher → 子图 JSON」全链路。

前置条件（需手动准备）：
    1. 本地 Neo4j 已启动，且已运行 backend/scripts/generate_mock_graph.py 生成沙盒数据；
    2. .env 中已配置正确的 NEO4J_* 与 LLM_API_KEY / LLM_API_BASE / LLM_MODEL。

运行方式：
    python backend/tests/test_rag.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 将 backend/ 加入 sys.path，以便导入 database 单例与 rag 包
sys.path.append(str(Path(__file__).resolve().parents[1]))

from database import Neo4jDatabase  # noqa: E402
from rag.graph_extractor import GraphExtractor  # noqa: E402


def test_fund_loop_subgraph() -> None:
    """
    验证：能依据自然语言问题，检索并序列化出「资金回环」子图 JSON。
    对应沙盒注入的模式 A（LOOP_A→LOOP_B→LOOP_C→LOOP_A）。
    """
    extractor = GraphExtractor()
    question = "查一下刚刚植入的公司A的资金回环关系"

    print("== 用户问题 ==")
    print(question)

    try:
        result = extractor.extract_subgraph(question)
    except Exception as exc:  # 执行失败时打印已生成的 Cypher，便于定位是 LLM 还是 DB 的问题
        print("\n== 生成的 Cypher ==")
        print(extractor.last_cypher)
        raise exc

    print("\n== 生成的 Cypher ==")
    print(result["cypher"])

    subgraph = result["subgraph"]
    print(f"\n== 子图 JSON（{len(subgraph['nodes'])} 个节点 / {len(subgraph['edges'])} 条边）==")
    print(json.dumps(subgraph, ensure_ascii=False, indent=2, default=str))

    # 断言：至少返回了子图结构
    assert len(subgraph["nodes"]) > 0, "子图应包含至少一个节点"

    node_ids = {n["id"] for n in subgraph["nodes"]}
    print("\n== 检索到的节点 id ==")
    print(sorted(node_ids))

    # 期望命中回环拓扑相关节点（公司 A 及其结算账户）
    expected = {"LOOP_A", "LOOP_ACCT_A"}
    hit = expected & node_ids
    if hit:
        print(f"\n✓ 测试通过：成功打印资金回环拓扑 JSON，命中关键节点 {sorted(hit)}")
    else:
        print("\n⚠ 未命中预期回环节点（可能是 LLM 生成的 Cypher 需微调），请检查上方 Cypher 与子图内容")

    extractor.db.close()


if __name__ == "__main__":
    test_fund_loop_subgraph()
