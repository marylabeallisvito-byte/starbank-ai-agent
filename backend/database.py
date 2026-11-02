"""
backend/database.py
===================
Neo4j 图数据库连接「单例」模块。

设计说明
--------
本模块通过「单例模式」统一管理 Neo4j Driver 的生命周期，核心收益：

1. 避免重复建连：Neo4j Driver 内部维护连接池，创建成本较高；若在业务各处
   反复 new Driver，会造成连接池堆积与资源泄漏。单例保证全局仅存在一个 Driver。
2. 统一收敛读写入口：所有对图数据库的访问都经过 execute_query 等基础方法，
   上层业务（穿透审查、资金回环检测、多 Agent 图谱检索）无需感知 Driver 细节，
   实现高内聚低耦合。
3. 线程安全性：Neo4j Driver 本身线程安全，可在多线程 / 多 Agent 并发场景下共享；
   本单例仅负责 Driver 的惰性创建与复用，不做额外加锁。

使用方式
--------
    from database import Neo4jDatabase
    db = Neo4jDatabase()                              # 获取单例（多次调用返回同一实例）
    records = db.execute_query("MATCH (n:Company) RETURN n LIMIT 10")
    ...
    db.close()                                        # 进程退出前释放连接池
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from neo4j import GraphDatabase

# 定位项目根目录：本文件位于 backend/ 下，向上回溯一层即为项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 加载 .env 环境变量；override=False 表示不覆盖系统已存在的同名变量（外部注入优先）
load_dotenv(PROJECT_ROOT / ".env", override=False)


class Neo4jDatabase:
    """
    Neo4j 图数据库连接单例。

    Attributes:
        _instance: 类级单例实例，首次 __new__ 后复用。
        _driver:   类级 Neo4j Driver（惰性创建，内部维护连接池）。
    """

    _instance: Optional["Neo4jDatabase"] = None
    _driver = None

    def __new__(cls) -> "Neo4jDatabase":
        # 经典单例实现：仅首次实例化时真正创建对象，后续均复用同一实例
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ------------------------------------------------------------------
    # Driver 生命周期管理
    # ------------------------------------------------------------------
    @classmethod
    def _ensure_driver(cls):
        """
        惰性创建并复用 Neo4j Driver。

        仅在首次访问时根据环境变量初始化 Driver，此后复用，避免重复建连。
        """
        if cls._driver is None:
            uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
            user = os.getenv("NEO4J_USER", "neo4j")
            password = os.getenv("NEO4J_PASSWORD", "")
            # 本地首次启动时 Neo4j 可能尚未运行。缩短连接失败等待，图谱接口即可
            # 迅速切换到内置演示数据，而不是让前端长时间停在加载态。
            timeout = float(os.getenv("NEO4J_CONNECTION_TIMEOUT", "2.0"))
            cls._driver = GraphDatabase.driver(
                uri,
                auth=(user, password),
                connection_timeout=timeout,
                connection_acquisition_timeout=timeout,
            )
        return cls._driver

    # ------------------------------------------------------------------
    # 基础查询接口
    # ------------------------------------------------------------------
    def execute_query(
        self,
        cypher: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """
        执行单条 Cypher 语句（读 / 写均支持，自动提交事务）。

        Args:
            cypher:      Cypher 查询语句。
            parameters:  参数化查询的参数字典。采用参数化而非字符串拼接，
                         从根本上杜绝 Cypher 注入风险。

        Returns:
            结果记录列表，每条记录为 neo4j.Record（Mapping 子类，支持 r["k"] 与 r.values()）。
            对于 CREATE / MERGE 等写操作，返回空列表 []。

        重要说明（neo4j 驱动 6.x 兼容性）
        --------------------------------
        这里【刻意】返回原始 Record，而非 `record.data()` 的序列化结果：驱动 6.x 的
        `Record.data()` 会把 Node/Relationship 序列化成普通 dict（Node 丢失 labels，
        Relationship 变成 [dict, type, dict] 三元组、端点丢失），从而破坏下游
        `serialize_graph_result` 的图对象收集逻辑。返回原始 Record 可完整保留
        Node/Relationship 对象（含 labels / element_id / 属性 / 端点）。
        """
        parameters = parameters or {}
        with self._ensure_driver().session() as session:
            result = session.run(cypher, parameters)
            # 返回原始 Record 列表，保留 Node/Relationship 等图对象（详见 Returns 说明）
            return list(result)

    def execute_write(
        self,
        cypher: str,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        以「写事务」方式执行 Cypher。

        底层使用 session.execute_write，由驱动自动处理瞬时故障重试与事务提交，
        保证多语句写操作的事务原子性（要么全部成功，要么全部回滚）。
        建议对需要原子性的批量写操作优先使用本方法。
        """
        parameters = parameters or {}

        def _unit_of_work(tx):
            result = tx.run(cypher, parameters)
            # 与 execute_query 保持一致：返回原始 Record，保留图对象（避免驱动 6.x 序列化丢信息）
            return list(result)

        with self._ensure_driver().session() as session:
            return session.execute_write(_unit_of_work)

    def close(self) -> None:
        """
        显式关闭 Driver，释放底层连接池资源。

        通常在应用 / 脚本进程结束前调用，否则连接池可能持有连接导致进程无法正常退出。
        """
        if self._driver is not None:
            self._driver.close()
            self._driver = None
