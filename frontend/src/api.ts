// =====================================================================
// 后端 API 客户端：REST（axios）+ SSE 流（fetch ReadableStream）
// =====================================================================
import axios from 'axios';
import { LOCAL_DEMO_GRAPH, LOCAL_GRAPH_SCHEMA } from './demoGraph';
import type { GraphData, ReviewResult, SchemaResponse } from './types';

// 经 Vite 代理，开发环境使用相对路径即可（见 vite.config.ts 的 /api proxy）
const BASE = '/api';

export interface PublicObservation {
  edge_id: string;
  relation_type: string;
  relation_label: string;
  source_entity: { id: string; name: string };
  target_entity: { id: string; name: string };
  statement: string;
  as_of: string;
  source_id: string;
  source_title: string;
  source_page: number;
  source_record_id: string;
  source_url: string;
  qualification: string | null;
}

export interface PublicInvestigationReport {
  report_id: string;
  generated_at: string;
  snapshot_sha256: string;
  data_class: 'public_disclosure';
  entity: { id: string; name: string; type: string };
  case_as_of: string;
  summary: string;
  categories: { holdings: number; subsidiaries: number; investments: number };
  sources: { id: string; title: string; url: string; as_of: string; published_at: string }[];
  observations: PublicObservation[];
  limitations: string[];
}

export async function fetchPublicInvestigation(entityId: string): Promise<PublicInvestigationReport> {
  const response = await axios.get<PublicInvestigationReport>(`${BASE}/public-case/investigate`, {
    params: { entity_id: entityId }, timeout: 6000,
  });
  return response.data;
}

export interface EllipticBenchmarkReport {
  schema_version: 1;
  generated_at: string;
  dataset: {
    name: string; source_url: string; license: string; archive_sha256: string;
    transaction_count: number; edge_count: number;
    label_counts: { illicit: number; licit: number; unknown: number };
  };
  protocol: { type: string; time_step_windows: Record<string, number[]>; scope: string; comparability: string };
  splits: Record<string, { n: number; positives: number }>;
  models: Record<string, {
    threshold_from_validation: number;
    test: { n: number; positives: number; prevalence: number; pr_auc: number; roc_auc: number;
      precision: number; recall: number; f1: number;
      confusion: { tn: number; fp: number; fn: number; tp: number } };
  }>;
}

export async function fetchEllipticBenchmark(): Promise<EllipticBenchmarkReport> {
  const response = await axios.get<EllipticBenchmarkReport>(`${BASE}/benchmarks/elliptic`, { timeout: 6000 });
  return response.data;
}

export interface ServiceHealth {
  online: boolean;
  graphSource?: 'neo4j' | 'demo';
  nodes?: number;
  relationships?: number;
}

export async function fetchServiceHealth(): Promise<ServiceHealth> {
  try {
    const response = await axios.get<{
      status?: string;
      graph_source?: 'neo4j' | 'demo';
      dependencies?: { neo4j?: { nodes?: number; relationships?: number } };
    }>(`${BASE}/health`, { timeout: 3500 });
    return {
      online: response.data?.status === 'ok',
      graphSource: response.data?.graph_source,
      nodes: response.data?.dependencies?.neo4j?.nodes,
      relationships: response.data?.dependencies?.neo4j?.relationships,
    };
  } catch {
    return { online: false };
  }
}

function ensureGraphData(value: unknown): GraphData {
  if (!value || typeof value !== 'object') throw new Error('图谱接口返回了无效响应');
  const graph = value as Partial<GraphData>;
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) {
    throw new Error('图谱接口缺少 nodes 或 edges 数据');
  }
  return graph as GraphData;
}

/** 拉取首屏核心骨干网络 */
export async function fetchGraphSample(): Promise<GraphData> {
  const resp = await axios.get<unknown>(`${BASE}/graph/sample`, { timeout: 2500 });
  return ensureGraphData(resp.data);
}

/** Curated public filing excerpt. The checked-in JSON is also the backend endpoint's source. */
export async function fetchPublicCase(): Promise<GraphData> {
  try {
    const response = await axios.get<unknown>(`${BASE}/graph/public-case`, { timeout: 3000 });
    return ensureGraphData(response.data);
  } catch {
    const response = await fetch('/data/catl-disclosure.json');
    if (!response.ok) throw new Error('公开披露案例暂时不可用');
    return ensureGraphData(await response.json());
  }
}

export interface LeiRecord {
  lei: string;
  legal_name: string;
  jurisdiction: string | null;
  country: string | null;
  entity_status: string | null;
  registration_status: string | null;
  last_updated: string | null;
  source_url: string | null;
}

export interface LeiSearchResponse {
  query: string;
  source: 'GLEIF API';
  retrieved_at: string;
  golden_copy_at: string | null;
  total: number;
  records: LeiRecord[];
  cached: boolean;
}

/** Direct query of GLEIF via our bounded server route; errors remain visible to the user. */
export async function searchLeiEntities(query: string): Promise<LeiSearchResponse> {
  const response = await axios.get<LeiSearchResponse>(`${BASE}/sources/gleif/search`, {
    params: { q: query.trim(), limit: 8 }, timeout: 12000,
  });
  return response.data;
}

/** 按需扩展：以某节点为中心向外扩展 1~2 度关系（懒加载） */
export async function fetchGraphExpand(nodeId: string, depth = 1): Promise<GraphData> {
  const resp = await axios.get<unknown>(`${BASE}/graph/expand`, {
    params: { node_id: nodeId, depth },
    timeout: 8000,
  });
  const data = ensureGraphData(resp.data);
  if (data.meta?.source === 'demo') throw new Error('图数据库暂时不可用，无法扩展当前真实图谱');
  return data;
}

/** 拉取图例 Schema */
export async function fetchGraphSchema(): Promise<SchemaResponse> {
  try {
    const resp = await axios.get<SchemaResponse>(`${BASE}/graph/schema`, { timeout: 5000 });
    return resp.data;
  } catch {
    return LOCAL_GRAPH_SCHEMA;
  }
}

/** 人在回路：信贷员确认/驳回 AI 结论，回写图谱与审计日志（创新点四） */
export async function submitReview(payload: {
  investigation_id?: string | null;
  query: string;
  ai_decision: string;
  suspicious_nodes: string[];
  human_decision: 'confirm' | 'reject';
  reviewer?: string;
  rationale?: string;
}, reviewToken = ''): Promise<ReviewResult> {
  const saveLocally = (): ReviewResult => {
    const record = { ...payload, reviewer: payload.reviewer ?? '信贷员', rationale: payload.rationale ?? '', timestamp: new Date().toISOString() };
    const key = 'finrisk-offline-reviews';
    try {
      const previous = JSON.parse(localStorage.getItem(key) ?? '[]') as unknown[];
      localStorage.setItem(key, JSON.stringify([...previous, record].slice(-100)));
    } catch { throw new Error('浏览器无法保存本次模拟复核'); }
    return { local_only: true, record, audit: { appended: false }, label: { appended: false }, graph: { written: false, reason: '仅保存在此浏览器；未写入服务端或图谱' } };
  };
  if (!payload.investigation_id) return saveLocally();
  try {
    const resp = await axios.post<ReviewResult>(`${BASE}/review`, {
      investigation_id: payload.investigation_id,
      human_decision: payload.human_decision,
      reviewer: payload.reviewer,
      rationale: payload.rationale,
    }, {
      timeout: 8000,
      headers: reviewToken ? { 'X-Review-Token': reviewToken } : undefined,
    });
    return resp.data;
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 401) throw new Error('复核口令无效，请核对后重试');
    if (axios.isAxiosError(error) && error.response?.status === 503) throw new Error('服务端尚未配置复核权限');
    if (axios.isAxiosError(error) && error.response?.status === 404) throw new Error('这次调查已复核或服务已重置，请重新调查');
    if (axios.isAxiosError(error) && error.response) throw new Error(`服务端拒绝本次复核（HTTP ${error.response.status}），请检查填写内容后重试`);
    console.warn('[api] 后端复核接口不可达，复核结果仅保存在当前浏览器。', error);
    return saveLocally();
  }
}

// =====================================================================
// SSE 流解析
// ---------------------------------------------------------------------
// 注意：POST /api/analyze 无法使用原生 EventSource（其仅支持 GET），
// 故采用 fetch + ReadableStream 逐块读取、按 SSE 事件帧（空行分隔）解析。
// =====================================================================

/** 单条 SSE 事件 */
export interface SSEMessage {
  event: string;
  data: unknown;
}

async function* streamLocalDemoAnalyze(query: string): AsyncGenerator<SSEMessage> {
  const ubo = /实控|ubo|股权|自融/i.test(query);
  const funds = /资金|回环|转账|交易|快进快出|洗钱/i.test(query);
  const supported = ubo || funds;
  const suspiciousNodes = !supported ? [] : ubo
    ? ['PERSON_BOSS', 'HOLDING_X', 'SHELL_Y', 'TARGET_Z']
    : ['ACCT_LOOP_A', 'ACCT_LOOP_B', 'ACCT_LOOP_C'];
  const suspiciousEdgeType = ubo ? 'OWNS' : 'TRANSFER';
  const suspiciousNodeSet = new Set(suspiciousNodes);
  const suspiciousEdgeIds = LOCAL_DEMO_GRAPH.edges
    .filter((edge) => edge.type === suspiciousEdgeType && suspiciousNodeSet.has(edge.source) && suspiciousNodeSet.has(edge.target))
    .map((edge) => edge.id);
  const riskFinding = !supported
    ? '当前演示图谱未找到与该问题匹配的案件证据；不能据此判断主体风险。'
    : ubo
    ? '四层持股关系最终汇聚至高远，启明控股与瀚海咨询形成控制权传导，关联授信存在集中暴露。'
    : '三账户在 7 小时 19 分内完成 546.3 万元循环转账，回流金额保留率达 97.2%，显著偏离正常贸易结算节奏。';
  const complianceFinding = !supported
    ? '缺少可定位的交易、主体关系及原始记录，需先确认调查对象与补充授权数据。'
    : ubo
    ? '复杂所有权结构触发受益所有人穿透识别要求，需补充最终自然人控制证明并开展强化尽调。'
    : '短期大额资金快进快出并闭环回流，命中可疑交易识别特征，需核验交易背景、发票与最终受益人。';
  const decisionFinding = !supported
    ? '待补证：请提供明确的企业或账户线索，并核对原始交易与主体资料后重新调查。'
    : ubo
    ? '暂缓自动授信审批；在受益所有人、关联交易与资金用途资料核验完成前，转入人工增强审查。'
    : '标记为高风险并暂缓自动审批；补齐资金用途凭证和交易对手关系说明后，由信贷员完成最终裁决。';
  const clause = ubo
    ? { clause_id: '反洗钱法·第十七条', text: '金融机构应当采取合理措施识别受益所有人，了解实际控制客户或交易的自然人，穿透复杂所有权结构。' }
    : { clause_id: '3号令·第十二条', text: '可疑交易特征包括短期内资金快进快出，以及与客户身份、经营规模明显不符的交易。' };

  yield { event: 'status', data: { node: 'start', message: '后端服务不可用，当前使用浏览器本地模拟数据；结论仅用于演示' } };
  yield { event: 'status', data: { node: 'retrieve', message: '证据快照已完成，启动风险与合规双轨审查' } };
  yield { event: 'graph', data: { cypher: 'LOCAL_DEMO_FALLBACK', data: LOCAL_DEMO_GRAPH } };
  yield { event: 'report', data: { agent: 'risk', content: riskFinding } };
  yield {
    event: 'report',
    data: {
      agent: 'compliance',
      content: complianceFinding,
      risk_labels: supported ? '资金关系异常 / 需人工复核' : '待补充案件证据',
      citations: { valid: supported ? [clause] : [], invalid: [] },
      aml_findings: [{ pattern: 'local_demo_check', node_ids: suspiciousNodes, evidence: { edge_ids: suspiciousEdgeIds }, description: complianceFinding }],
    },
  };
  yield { event: 'status', data: { node: 'decision', message: '多智能体证据汇总完毕' } };
  yield {
    event: 'decision',
    data: {
      decision: supported ? 'FLAGGED' : 'NEEDS_EVIDENCE', analysis_source: 'local_demo', investigation_id: null, risk_score: null, confidence: null, final_report: decisionFinding,
      highlight: { suspicious_nodes: suspiciousNodes, suspicious_edges_types: [suspiciousEdgeType], suspicious_edge_ids: suspiciousEdgeIds },
    },
  };
  yield { event: 'done', data: { message: '本地调查流程结束' } };
}

/**
 * 发起审批流请求，并逐条 yield 解析后的 SSE 事件。
 * 传入 AbortSignal 可在中途停止（abort 后 reader.read 抛 AbortError，由调用方兜底复位）。
 * 用法：for await (const msg of streamAnalyze(query, signal)) { ... }
 */
export async function* streamAnalyze(
  query: string,
  signal?: AbortSignal,
  allowLocalDemo = false,
): AsyncGenerator<SSEMessage> {
  let resp: Response;
  try {
    resp = await fetch(`${BASE}/analyze`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
      signal,
    });
  } catch (error) {
    if ((error as { name?: string }).name === 'AbortError') throw error;
    if (!allowLocalDemo) throw new Error('分析服务暂时不可用，请稍后重试');
    yield* streamLocalDemoAnalyze(query);
    return;
  }

  if (!resp.ok || !resp.body) {
    if (!allowLocalDemo) throw new Error(`分析服务返回错误（HTTP ${resp.status}），请稍后重试`);
    console.warn(`[api] 审批流接口返回 HTTP ${resp.status}，切换本地演示流程。`);
    yield* streamLocalDemoAnalyze(query);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE 事件帧之间以空行（\n\n 或 \r\n\r\n）分隔
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const msg = parseSSEFrame(frame);
      if (msg) yield msg;
    }
  }
}

/** 解析单个 SSE 事件帧为 {event, data} */
function parseSSEFrame(raw: string): SSEMessage | null {
  let event = 'message';
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (dataLines.length === 0) return null;

  const payload = dataLines.join('\n');
  try {
    return { event, data: JSON.parse(payload) };
  } catch {
    return { event, data: payload };
  }
}
