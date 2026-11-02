// =====================================================================
// 前后端共享的数据类型定义（与 backend/main.py 的响应结构一一对应）
// =====================================================================

/** 图节点（backend `_to_g6_graph` 输出） */
export interface GraphNode {
  id: string;
  label: string;
  /** 节点标签：Company / Person / Account / Device */
  type: string;
  properties: Record<string, unknown>;
}

/** 图边（backend `_to_g6_graph` 输出） */
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  /** 关系类型：OWNS / LEGAL_REP / TRANSFER / HAS_ACCOUNT / LOGGED_IN */
  type: string;
  properties: Record<string, unknown>;
}

/** 全景图 / 子图数据（AntV G6 友好格式） */
export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** 实时 Neo4j 或可离线运行的内置演示数据。 */
  meta?: {
    source: 'neo4j' | 'demo' | 'public_disclosure';
    degraded: boolean;
    message?: string;
    case_id?: string;
    case_title?: string;
    as_of?: string;
    published_at?: string;
    source_title?: string;
    source_url?: string;
    sources?: { id: string; title: string; url: string; as_of: string; published_at: string }[];
  };
}

/** 节点类型元数据（图例用） */
export interface NodeTypeMeta {
  label: string;
  name: string;
  color: string;
  properties: string[];
}

/** 边类型元数据（图例用） */
export interface EdgeTypeMeta {
  type: string;
  name: string;
  color: string;
  properties: string[];
}

/** GET /api/graph/schema 响应 */
export interface SchemaResponse {
  node_types: NodeTypeMeta[];
  edge_types: EdgeTypeMeta[];
}

// =====================================================================
// SSE 事件负载（POST /api/analyze）
// =====================================================================

/** status 事件：流程进度提示 */
export interface StatusPayload {
  node: string;
  message: string;
}

/** graph 事件：抽取到的子图 */
export interface GraphPayload {
  cypher: string;
  data: GraphData;
}

/** 确定性时空聚集检测 finding（创新点二 · 非 LLM） */
export interface AmlFinding {
  pattern: string;
  node_ids: string[];
  evidence: Record<string, unknown>;
  description: string;
}

/** 法条引用校验结果（创新点一） */
export interface CitationResult {
  valid: { clause_id: string; text: string }[];
  invalid: string[];
}

/** report 事件：风控 / 合规 Agent 审查报告 */
export interface ReportPayload {
  agent: 'risk' | 'compliance';
  content: string;
  /** 合规 Agent 附加：法条引用校验 / 证据 / 风险定性 / 时空聚集检测 */
  citations?: CitationResult;
  evidence?: { edge_id: string; type?: string; amount?: number; date?: string; 模式?: string }[];
  risk_labels?: string;
  aml_findings?: AmlFinding[];
}

/** decision 事件：最终决策 + 风险评分 + 置信度 + 前端高亮 JSON */
export interface DecisionPayload {
  decision: string;
  investigation_id?: string | null;
  analysis_source?: 'neo4j' | 'demo' | 'local_demo';
  risk_score?: number | null;
  confidence?: number | null;
  final_report: string;
  highlight: {
    suspicious_nodes: string[];
    suspicious_edges_types: string[];
    /** 后端根据节点与关系类型解析出的精确证据边，前端不得自行扩大高亮范围。 */
    suspicious_edge_ids?: string[];
  };
}

/** 人在回路 · 审批回写结果（POST /api/review） */
export interface ReviewResult {
  local_only?: boolean;
  record: {
    query: string;
    ai_decision: string;
    suspicious_nodes: string[];
    human_decision: string;
    reviewer: string;
    rationale: string;
    timestamp: string;
  };
  audit: { appended: boolean };
  label: { appended: boolean };
  graph: { written: boolean; nodes?: number; human_review?: string; reason?: string };
}

/** error 事件 */
export interface ErrorPayload {
  message: string;
}

// =====================================================================
// 左侧「思维流」日志条目
// =====================================================================
export interface LogEntry {
  id: number;
  kind: 'status' | 'report' | 'decision' | 'error' | 'done' | 'user';
  /** 时间戳（HH:mm:ss，精炼时间点推进用） */
  time: string;
  /** 标题（如 Agent 名 / 阶段名） */
  title?: string;
  /** 正文内容 */
  content: string;
}
