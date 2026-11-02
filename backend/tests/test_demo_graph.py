"""无需 Neo4j 或 LLM 的演示降级数据自检。"""
from backend.demo_graph import expand_demo_graph, get_demo_case, get_demo_graph


def test_demo_graph_integrity() -> None:
    graph = get_demo_graph()
    node_ids = {node["id"] for node in graph["nodes"]}
    edge_ids = {edge["id"] for edge in graph["edges"]}

    assert len(node_ids) == len(graph["nodes"])
    assert len(edge_ids) == len(graph["edges"])
    assert len(graph["nodes"]) >= 70
    assert len(graph["edges"]) >= 100
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in graph["edges"])


def test_demo_expand_and_cases() -> None:
    expanded = expand_demo_graph("ACCT_LOOP_A", 2)
    assert {"ACCT_LOOP_A", "ACCT_LOOP_B", "ACCT_LOOP_C"}.issubset(
        {node["id"] for node in expanded["nodes"]}
    )
    assert get_demo_case("调查资金回环")["edge_types"] == ["TRANSFER"]
    assert get_demo_case("穿透 UBO 股权")["edge_types"] == ["OWNS"]
    assert get_demo_case("审查未知主体")["edge_types"] == []
    assert get_demo_case("审查未知主体")["score"] is None
