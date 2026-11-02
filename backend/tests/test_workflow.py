"""
backend/tests/test_workflow.py
==============================
Phase 3 控制台测试：运行多 Agent 风控决策工作流。

前置条件（需手动准备）：
    1. 本地 Neo4j 已启动，且已运行 backend/scripts/generate_mock_graph.py 生成沙盒数据；
    2. .env 已配置正确的 NEO4J_* 与 LLM_API_KEY / LLM_API_BASE / LLM_MODEL。

运行方式：
    python backend/tests/test_workflow.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 将 backend/ 加入 sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))

from agents.workflow import run_risk_control  # noqa: E402


def main() -> None:
    query = "查一下刚刚植入的公司A的资金回环关系，并评估其风险"
    print("== 用户查询 ==")
    print(query, "\n")

    result = run_risk_control(query)

    print("== 生成的 Cypher ==")
    print(result.get("cypher", ""), "\n")

    print("== 风控分析报告（供应链集中度 / 空壳欺诈 / 资金链断裂）==")
    print(result.get("risk_report", ""), "\n")

    print("== 合规审查报告（AML / 自融穿透 / 虚假回环 / 设备聚集）==")
    print(result.get("compliance_report", ""), "\n")

    print("== 最终决策 ==")
    print(result.get("final_decision", ""), "\n")

    print("== 结构化结果（前端高亮用）==")
    print("decision            :", result.get("decision", ""))
    print("suspicious_nodes    :", json.dumps(result.get("suspicious_nodes", []), ensure_ascii=False))
    print("suspicious_edges    :", json.dumps(result.get("suspicious_edges_types", []), ensure_ascii=False), "\n")

    subgraph = result.get("graph_json", {})
    print(f"== 子图规模：{len(subgraph.get('nodes', []))} 节点 / {len(subgraph.get('edges', []))} 边 ==")


if __name__ == "__main__":
    main()
