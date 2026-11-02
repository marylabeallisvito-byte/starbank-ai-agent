import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import {
  Activity, AlertCircle, ArrowUp, BadgeCheck, Brain, Building2, Check, ChevronDown,
  ExternalLink, FileSearch, Gavel, GitBranch, Layers3, Loader2, Menu, Network, PanelLeftClose,
  Maximize2, Minus, PanelRightClose, Pause, Play, Plus, RefreshCw, Scale, Search,
  ShieldCheck, Sparkles, Square, UserRound, WalletCards, X,
} from 'lucide-react';
import type { GraphViewerHandle } from './components/GraphViewer';
import CaseJourney from './components/CaseJourney';
import PublicInvestigation from './components/PublicInvestigation';
import { fetchGraphSchema, fetchServiceHealth, searchLeiEntities, streamAnalyze, submitReview } from './api';
import type { LeiSearchResponse } from './api';
import { LOCAL_DEMO_GRAPH } from './demoGraph';
import type {
  DecisionPayload, ErrorPayload, GraphData, GraphEdge, GraphNode, GraphPayload, LogEntry,
  ReportPayload, SchemaResponse, StatusPayload,
} from './types';

const GraphViewer = lazy(() => import('./components/GraphViewer'));
const BenchmarkLab = lazy(() => import('./components/BenchmarkLab'));

type Stage = 'idle' | 'accepted' | 'extracting' | 'reviewing' | 'deciding' | 'done';
type AgentKey = 'risk' | 'compliance' | 'decision';
type AgentStatus = 'idle' | 'processing' | 'done';
type MobileView = 'investigation' | 'graph' | 'decision';
type InspectorTab = 'overview' | 'evidence' | 'review';
type ServiceState = 'checking' | 'online' | 'offline';
type CaseMode = 'public' | 'simulation';
interface GraphInventory { nodes: number; relationships: number }

interface AgentState { status: AgentStatus; finding: string; verdict?: string }
interface ReviewState {
  investigationId: string | null;
  analysisSource: 'neo4j' | 'demo' | 'local_demo';
  query: string; decision: string;
  suspiciousNodes: string[];
  suspiciousEdgeIds: string[];
}
interface TimeRange { min: number; max: number; count: number }

const INITIAL_AGENTS: Record<AgentKey, AgentState> = {
  risk: { status: 'idle', finding: '' },
  compliance: { status: 'idle', finding: '' },
  decision: { status: 'idle', finding: '' },
};
const STAGES = [
  { key: 'accepted', label: '受理' }, { key: 'extracting', label: '取证' },
  { key: 'reviewing', label: '审查' }, { key: 'deciding', label: '裁决' },
] as const;
const STAGE_INDEX: Record<Stage, number> = {
  idle: -1, accepted: 0, extracting: 1, reviewing: 2, deciding: 3, done: 4,
};
const NODE_TITLE: Record<string, string> = {
  start: '任务受理', retrieve: '图谱取证', risk: '风险审查',
  compliance: '合规审查', decision: '综合裁决',
};
const TYPE_META: Record<string, { label: string; icon: ReactNode; tone: string }> = {
  Company: { label: '企业', icon: <Building2 size={16} />, tone: 'text-[#8AB4F8] bg-[#8AB4F8]/10' },
  Person: { label: '自然人', icon: <UserRound size={16} />, tone: 'text-[#81C995] bg-[#81C995]/10' },
  Account: { label: '账户', icon: <WalletCards size={16} />, tone: 'text-[#FDD663] bg-[#FDD663]/10' },
  Device: { label: '设备', icon: <Network size={16} />, tone: 'text-[#C58AF9] bg-[#C58AF9]/10' },
  device_cluster: { label: '设备组', icon: <Network size={16} />, tone: 'text-[#C58AF9] bg-[#C58AF9]/10' },
};

function stripCodeBlocks(text: string): string { return text.replace(/```[\s\S]*?```/g, '').trim(); }
function cleanLine(text: string, max = 72): string {
  const line = stripCodeBlocks(text).replace(/^#{1,6}\s*/gm, '').replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/^[-*•]\s+/gm, '').split('\n').map((item) => item.trim()).find((item) => item.length > 5) ?? '';
  return line.length > max ? `${line.slice(0, max)}…` : line;
}
function extractFindings(text: string, max = 3): string {
  return stripCodeBlocks(text).split('\n')
    .map((item) => item.replace(/^#{1,6}\s*/, '').replace(/^[-*•]\s+/, '').replace(/\*\*/g, '').trim())
    .filter((item) => item.length > 5).slice(0, max).join('\n');
}
function computeTimeRange(data: GraphData): TimeRange | null {
  const times = data.edges.filter((edge) => edge.type === 'TRANSFER')
    .map((edge) => Date.parse(String(edge.properties?.date ?? ''))).filter((time) => !Number.isNaN(time));
  return times.length ? { min: Math.min(...times), max: Math.max(...times), count: times.length } : null;
}
function formatTime(timestamp: number): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date).replaceAll('/', '-');
}
function displayProperty(key: string, value: unknown): string {
  if (value == null || value === '') return '—';
  if (key === 'risk_flag') return value ? '已标记' : '未标记';
  if (typeof value === 'number') return value.toLocaleString('zh-CN');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
function displayDecision(decision: string): string { return ({ NEEDS_EVIDENCE: '待补证', FLAGGED: '需人工复核', REJECT: '建议拒绝', APPROVE: '建议通过' } as Record<string, string>)[decision] ?? decision; }

export default function App() {
  const graphRef = useRef<GraphViewerHandle>(null);
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const sequenceRef = useRef(0);

  const [mobileView, setMobileView] = useState<MobileView>('graph');
  const [leftOpen, setLeftOpen] = useState(false);
  const [rightOpen, setRightOpen] = useState(true);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('overview');
  const [serviceState, setServiceState] = useState<ServiceState>('checking');
  const [graphInventory, setGraphInventory] = useState<GraphInventory | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [legendOpen, setLegendOpen] = useState(false);
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], edges: [] });
  const [caseMode, setCaseMode] = useState<CaseMode>('public');
  const [graphLoading, setGraphLoading] = useState(true);
  const [graphError, setGraphError] = useState('');
  const [graphNotice, setGraphNotice] = useState('');
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);
  const [input, setInput] = useState('');
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState<Stage>('idle');
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [agents, setAgents] = useState(INITIAL_AGENTS);
  const [review, setReview] = useState<ReviewState | null>(null);
  const [rationale, setRationale] = useState('');
  const [reviewing, setReviewing] = useState(false);
  const [reviewSubmitted, setReviewSubmitted] = useState(false);
  const [reviewResult, setReviewResult] = useState('');
  const [reviewToken, setReviewToken] = useState('');
  const [timeRange, setTimeRange] = useState<TimeRange | null>(null);
  const [timeCursor, setTimeCursor] = useState<number | null>(null);
  const [legalClauses, setLegalClauses] = useState<{ clause_id: string; text: string }[]>([]);
  const [invalidCitations, setInvalidCitations] = useState<string[]>([]);
  const [activeClause, setActiveClause] = useState<{ clause_id: string; text: string } | null>(null);
  const [leiExplorerOpen, setLeiExplorerOpen] = useState(false);
  const [publicInvestigationOpen, setPublicInvestigationOpen] = useState(false);
  const [benchmarkOpen, setBenchmarkOpen] = useState(false);
  const closeLeiExplorer = useCallback(() => setLeiExplorerOpen(false), []);
  const closePublicInvestigation = useCallback(() => setPublicInvestigationOpen(false), []);

  const inspectorVisible = rightOpen && Boolean(selectedNode || selectedEdge || review);
  const evidenceEdges = useMemo(() => {
    if (!review?.suspiciousEdgeIds.length) return [];
    const ids = new Set(review.suspiciousEdgeIds);
    return graphData.edges.filter((edge) => ids.has(edge.id)).sort((a, b) => Date.parse(String(a.properties.date ?? '')) - Date.parse(String(b.properties.date ?? '')));
  }, [graphData.edges, review]);

  // 窄屏下关系画布会在“调查 / 结果”页使用 display:none 隐藏。
  // 切回时等待布局完成再重算镜头，避免 G6 根据 0×0 容器把全图缩成点团。
  useEffect(() => {
    if (mobileView !== 'graph') return;
    const timer = window.setTimeout(() => graphRef.current?.fitView(), 320);
    return () => window.clearTimeout(timer);
  }, [mobileView]);
  const selectedNodeDegree = useMemo(() => selectedNode
    ? graphData.edges.filter((edge) => edge.source === selectedNode.id || edge.target === selectedNode.id).length : 0,
  [graphData.edges, selectedNode]);

  const addLog = (kind: LogEntry['kind'], title: string | undefined, content: string) => {
    const id = ++sequenceRef.current;
    const time = new Date(Date.now() + id * 1500).toLocaleTimeString('zh-CN', { hour12: false });
    setLogs((current) => [...current, { id, kind, title, content, time }]);
  };
  useEffect(() => { fetchGraphSchema().then(setSchema).catch(() => setSchema(null)); }, []);
  useEffect(() => {
    let active = true;
    const check = () => { void fetchServiceHealth().then((health) => { if (active) { setServiceState(health.online ? 'online' : 'offline'); setGraphInventory(health.online && health.graphSource === 'neo4j' ? { nodes: health.nodes ?? 0, relationships: health.relationships ?? 0 } : null); } }); };
    check();
    const timer = window.setInterval(check, 15000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [logs]);
  useEffect(() => { if (!graphNotice) return; const timer = window.setTimeout(() => setGraphNotice(''), 5000); return () => window.clearTimeout(timer); }, [graphNotice]);

  const handleGraphLoaded = (data: GraphData) => {
    setGraphData(data); setGraphLoading(false); setGraphError('');
    setCaseMode(data.meta?.source === 'public_disclosure' ? 'public' : 'simulation');
    const range = computeTimeRange(data); setTimeRange(range); setTimeCursor(range?.max ?? null);
  };

  const switchCaseMode = (mode: CaseMode) => {
    if (caseMode === mode || running) return;
    if (mode === 'public') { graphRef.current?.reload(); setGraphLoading(true); }
    else { graphRef.current?.setGraph(LOCAL_DEMO_GRAPH); handleGraphLoaded(LOCAL_DEMO_GRAPH); }
    setCaseMode(mode); setSelectedNode(null); setSelectedEdge(null); setReview(null); setReviewSubmitted(false);
    setRightOpen(false); setLeftOpen(false); setStage('idle'); setLogs([]); setMobileView('graph');
  };

  const selectEvidence = (edge: GraphEdge, openDetails = true) => {
    setSelectedEdge(edge); setSelectedNode(null); setInspectorTab('evidence'); setRightOpen(true);
    graphRef.current?.focusEvidence(edge.id);
    if (openDetails && window.matchMedia('(max-width: 1279px)').matches) setMobileView('decision');
  };

  const handleAnalyze = async (queryOverride?: string) => {
    const query = queryOverride?.trim() || input.trim();
    if (!query || running) return;
    sequenceRef.current = 0;
    setInput(''); setRunning(true); setStage('accepted'); setLogs([]); setAgents(INITIAL_AGENTS);
    setReview(null); setReviewResult(''); setReviewSubmitted(false); setRationale(''); setLegalClauses([]); setInvalidCitations([]);
    setSelectedNode(null); setSelectedEdge(null); setLeftOpen(true); setRightOpen(false); addLog('user', '调查指令', query);
    const controller = new AbortController(); abortRef.current = controller;
    let settled = false;
    try {
      for await (const message of streamAnalyze(query, controller.signal, caseMode === 'simulation')) {
        if (message.event === 'status') {
          const payload = message.data as StatusPayload;
          if (payload.node === 'start') setStage('extracting');
          if (payload.node === 'retrieve') {
            setStage('reviewing');
            setAgents((current) => ({ ...current, risk: { status: 'processing', finding: '' }, compliance: { status: 'processing', finding: '' } }));
          }
          if (payload.node === 'decision') {
            setStage('deciding');
            setAgents((current) => ({ ...current, decision: { status: 'processing', finding: '' } }));
          }
          addLog('status', NODE_TITLE[payload.node] ?? payload.node, payload.message);
        } else if (message.event === 'graph') {
          const payload = message.data as GraphPayload;
          setGraphData(payload.data); graphRef.current?.setGraph(payload.data);
          const range = computeTimeRange(payload.data); setTimeRange(range); setTimeCursor(range?.max ?? null);
          addLog('status', '图谱取证', `锁定 ${payload.data.nodes.length} 个实体与 ${payload.data.edges.length} 条关系`);
        } else if (message.event === 'report') {
          const payload = message.data as ReportPayload;
          const finding = payload.agent === 'compliance'
            ? [payload.risk_labels, payload.aml_findings?.[0]?.description, payload.citations?.valid?.map((item) => item.clause_id).join('、')].filter(Boolean).join('\n')
            : extractFindings(payload.content);
          setAgents((current) => ({ ...current, [payload.agent]: { status: 'done', finding } }));
          addLog('report', payload.agent === 'risk' ? '风险审查' : '合规审查', cleanLine(payload.content));
          if (payload.agent === 'compliance') {
            setLegalClauses(payload.citations?.valid ?? []); setInvalidCitations(payload.citations?.invalid ?? []);
          }
        } else if (message.event === 'decision') {
          settled = true;
          const payload = message.data as DecisionPayload;
          setStage('done');
          setAgents((current) => ({ ...current, decision: { status: 'done', finding: cleanLine(payload.final_report), verdict: payload.decision } }));
          setReview({ investigationId: payload.investigation_id ?? null, analysisSource: payload.analysis_source ?? 'local_demo', query, decision: payload.decision, suspiciousNodes: payload.highlight.suspicious_nodes, suspiciousEdgeIds: payload.highlight.suspicious_edge_ids ?? [] });
          setLeftOpen(false); setRightOpen(true);
          setInspectorTab('overview');
          if (payload.decision === 'NEEDS_EVIDENCE' && window.matchMedia('(max-width: 1279px)').matches) setMobileView('decision');
          graphRef.current?.highlight(payload.highlight.suspicious_nodes, payload.highlight.suspicious_edges_types, payload.highlight.suspicious_edge_ids);
          addLog('decision', '综合建议', `${displayDecision(payload.decision)} · ${payload.analysis_source === 'local_demo' ? '浏览器模拟，未连接分析服务' : '请逐条核对证据'}`);
        } else if (message.event === 'error') {
          settled = true;
          const payload = message.data as ErrorPayload; setStage('idle'); addLog('error', '流程异常', payload.message);
        }
      }
      if (!settled) { setStage('idle'); addLog('error', '流程异常', '分析连接已结束，但未收到有效结论；请重新调查。'); }
    } catch (error) {
      if ((error as { name?: string }).name === 'AbortError') { setStage('idle'); addLog('status', '任务受理', '调查已停止'); }
      else { setStage('idle'); addLog('error', '流程异常', error instanceof Error ? error.message : '服务暂时不可用'); }
    } finally {
      setRunning(false); abortRef.current = null;
      setAgents((current) => Object.fromEntries(Object.entries(current).map(([key, value]) => [key, value.status === 'processing' ? { ...value, status: 'idle' } : value])) as Record<AgentKey, AgentState>);
      inputRef.current?.focus();
    }
  };

  const handleReview = async (decision: 'confirm' | 'reject') => {
    if (!review || review.decision === 'NEEDS_EVIDENCE' || reviewSubmitted) return;
    if (rationale.trim().length < 5) { setReviewResult('请先填写至少 5 个字的复核依据。'); return; }
    setReviewing(true);
    try {
      const result = await submitReview({ investigation_id: review.investigationId, query: review.query, ai_decision: review.decision, suspicious_nodes: review.suspiciousNodes, human_decision: decision, reviewer: '信贷员', rationale }, reviewToken);
      setReviewResult(result.local_only
        ? '仅保存在当前浏览器，尚未提交服务端。'
        : result.graph.written
          ? (decision === 'confirm' ? `已确认并回写 ${result.graph.nodes ?? 0} 个实体` : '已驳回并记录到服务端')
          : `服务端已记录复核；图谱未回写：${result.graph.reason ?? '数据库不可用'}`);
      setReviewSubmitted(true);
    } catch (error) { setReviewResult(error instanceof Error ? error.message : '提交失败，请稍后重试'); }
    finally { setReviewing(false); }
  };

  return (
    <div className="flex h-dvh min-h-[560px] flex-col overflow-hidden bg-[#0F1115] text-[#E8EAED]">
      <TopBar running={running} serviceState={serviceState} graphSource={graphData.meta?.source} graphInventory={graphInventory} caseMode={caseMode} onMode={switchCaseMode} onMenu={() => { if (caseMode === 'public') setPublicInvestigationOpen(true); else setMobileView('investigation'); }} />
      <main
        className="relative min-h-0 flex-1 overflow-hidden"
        style={{
          '--left-safe': leftOpen ? '356px' : '92px',
          '--right-safe': inspectorVisible ? '456px' : '20px',
        } as CSSProperties}
      >
        <section className={`${mobileView === 'graph' ? 'flex' : 'hidden'} absolute inset-0 min-h-0 flex-col overflow-hidden xl:flex`}>
          <div className="canvas-inset-head pointer-events-none absolute top-4 z-20 flex items-start justify-between transition-[left,right] duration-300">
            <GraphHud data={graphData} selectedNode={selectedNode} inventory={graphInventory} caseMode={caseMode} />
            <div className="pointer-events-auto flex gap-2">
              <button className="icon-button" aria-label="显示图例" aria-expanded={legendOpen} onClick={() => setLegendOpen((value) => !value)}><Layers3 size={18} /></button>
              {(selectedNode || selectedEdge || review) && <button className="icon-button" aria-label="打开调查结果" onClick={() => { setRightOpen(true); setMobileView('decision'); }}><FileSearch size={18} /></button>}
            </div>
          </div>
          <Suspense fallback={null}>
          <GraphViewer ref={graphRef} onGraphLoaded={handleGraphLoaded} onNodeSelect={(node) => { setSelectedNode(node); setSelectedEdge(null); setLeftOpen(false); setRightOpen(true); setInspectorTab('overview'); }} onEdgeSelect={(edge) => selectEvidence(edge)} onGraphError={(message, fatal = true) => { if (fatal) { setGraphError(message); if (message) setGraphLoading(false); } else setGraphNotice(message); }} />
          </Suspense>
          {graphLoading && <GraphOverlay icon={<Loader2 className="animate-spin" />} title="正在载入关联网络" description="正在整理企业、账户与交易关系" />}
          {graphError && <GraphOverlay icon={<AlertCircle />} title="无法载入图谱" description={graphError} action={<button className="primary-button" onClick={() => { setGraphLoading(true); setGraphError(''); graphRef.current?.reload(); }}><RefreshCw size={16} />重试</button>} />}
          {graphNotice && <div role="alert" className="pointer-events-auto absolute bottom-24 right-4 z-30 max-w-xs rounded-xl border border-[#FDD663]/25 bg-[#202124] px-4 py-3 text-sm text-[#FDD663] shadow-xl">{graphNotice}</div>}
          {legendOpen && schema && <LegendPopover schema={schema} onClose={() => setLegendOpen(false)} />}
          {!graphLoading && !graphError && (caseMode === 'public'
            ? <PublicCaseCard graph={graphData} activeId={selectedEdge?.id ?? null} onSelect={selectEvidence} onSimulation={() => switchCaseMode('simulation')} onOpenLei={() => setLeiExplorerOpen(true)} onInvestigate={() => setPublicInvestigationOpen(true)} onOpenBenchmark={() => setBenchmarkOpen(true)} />
            : <CaseJourney graph={graphData} evidence={evidenceEdges} activeId={selectedEdge?.id ?? null} running={running} hasResult={Boolean(review)} onStart={handleAnalyze} onSelect={selectEvidence} />)}
          <div className="glass-surface pointer-events-auto absolute right-4 top-[142px] z-20 hidden flex-col overflow-hidden rounded-xl border sm:flex">
            <button className="canvas-tool" aria-label="放大图谱" onClick={() => graphRef.current?.zoomIn()}><Plus size={17} /></button>
            <button className="canvas-tool border-y border-white/[0.07]" aria-label="缩小图谱" onClick={() => graphRef.current?.zoomOut()}><Minus size={17} /></button>
            <button className="canvas-tool" aria-label="将图谱重新适配画布" onClick={() => graphRef.current?.fitView()}><Maximize2 size={16} /></button>
          </div>
          <div className="canvas-inset-controls pointer-events-none absolute bottom-4 z-20 flex flex-col items-center justify-center gap-3 transition-[left,right] duration-300 sm:bottom-5 xl:flex-row">
            {caseMode === 'simulation' && timeRange && timeCursor != null && <TimeSlider range={timeRange} cursor={timeCursor} onChange={(value) => { setTimeCursor(value); graphRef.current?.filterByTime(value); }} />}
            {caseMode === 'simulation' ? <CommandBar inputRef={inputRef} input={input} running={running} showSuggestions={stage === 'idle' && !review} onInput={setInput} onSubmit={() => handleAnalyze()} onSuggestion={(query) => handleAnalyze(query)} onStop={() => abortRef.current?.abort()} /> : <div className="source-bottom-note pointer-events-auto"><BadgeCheck size={15} />公开披露关系 · 不包含真实银行交易</div>}
          </div>
        </section>
        <InvestigationPanel compact={!leftOpen} visible={mobileView === 'investigation'} stage={stage} logs={logs} logRef={logRef} running={running} onToggle={() => setLeftOpen((value) => !value)} onShowGraph={() => setMobileView('graph')} />
        {inspectorVisible && <InspectorPanel visible={mobileView === 'decision'} activeTab={inspectorTab} onTab={setInspectorTab} selectedNode={selectedNode} selectedEdge={selectedEdge} graph={graphData} degree={selectedNodeDegree} review={review} agents={agents} legalClauses={legalClauses} invalidCitations={invalidCitations} rationale={rationale} reviewToken={reviewToken} reviewing={reviewing} reviewSubmitted={reviewSubmitted} reviewResult={reviewResult} onClose={() => { setRightOpen(false); setMobileView('graph'); }} onRationale={setRationale} onReviewToken={setReviewToken} onReview={handleReview} onOpenClause={setActiveClause} />}
        {review && review.decision !== 'NEEDS_EVIDENCE' && !running && mobileView !== 'decision' && <FloatingReviewDock review={review} onOpen={() => { setRightOpen(true); setInspectorTab('review'); setMobileView('decision'); }} />}
      </main>
      <MobileNavigation caseMode={caseMode} view={mobileView} hasResult={Boolean(selectedNode || selectedEdge || review)} onChange={setMobileView} onPublicEvidence={() => setPublicInvestigationOpen(true)} onSimulation={() => switchCaseMode('simulation')} />
      {activeClause && <LegalDialog clause={activeClause} onClose={() => setActiveClause(null)} />}
      {leiExplorerOpen && <LeiExplorer onClose={closeLeiExplorer} />}
      {publicInvestigationOpen && caseMode === 'public' && <PublicInvestigation graph={graphData} initialEntityId={selectedNode?.id ?? 'CATL-300750'} onClose={closePublicInvestigation} onFocusEdge={(edge) => selectEvidence(edge)} />}
      {benchmarkOpen && <Suspense fallback={null}><BenchmarkLab onClose={() => setBenchmarkOpen(false)} /></Suspense>}
    </div>
  );
}

function TopBar({ running, serviceState, graphSource, graphInventory, caseMode, onMode, onMenu }: { running: boolean; serviceState: ServiceState; graphSource?: NonNullable<GraphData['meta']>['source']; graphInventory: GraphInventory | null; caseMode: CaseMode; onMode: (mode: CaseMode) => void; onMenu: () => void }) {
  const status = running
    ? { label: '分析进行中', tone: 'animate-pulse bg-[#8AB4F8]' }
    : serviceState === 'checking'
      ? { label: '检查服务', tone: 'animate-pulse bg-[#FDD663]' }
      : serviceState === 'online' && graphSource === 'neo4j'
        ? { label: '模拟图谱 · 服务在线', tone: 'bg-[#FDD663]' }
        : serviceState === 'online'
          ? { label: '模拟数据 · 服务在线', tone: 'bg-[#FDD663]' }
          : { label: 'DEMO READY', tone: 'bg-[#FDD663]' };
  const statusTitle = caseMode === 'public' ? '当前展示项目内置的公开披露快照，关系保留原始来源指针' : graphInventory ? `Neo4j 已连接：${graphInventory.nodes} 个节点，${graphInventory.relationships} 条关系` : serviceState === 'offline' ? '演示证据快照已就绪' : undefined;
  return <header className="relative z-40 flex h-16 shrink-0 items-center justify-between gap-3 border-b border-white/[0.065] bg-[#14161A]/88 px-4 backdrop-blur-2xl sm:px-6">
    <div className="flex min-w-0 items-center gap-3"><button className="icon-button xl:hidden" aria-label={caseMode === 'public' ? '打开公开证据核验' : '打开调查记录'} onClick={onMenu}>{caseMode === 'public' ? <FileSearch size={19} /> : <Menu size={19} />}</button><div className="brand-mark"><ShieldCheck size={20} /></div><div className="min-w-0"><div className="flex items-center gap-2"><h1 className="truncate text-base font-semibold tracking-[-0.02em] text-white">穿透式金融风控</h1><span className="hidden rounded-full border border-[#8AB4F8]/20 px-2 py-0.5 text-[9px] font-semibold tracking-[0.14em] text-[#8AB4F8] md:inline">GRAPH INTELLIGENCE</span></div><p className="hidden text-xs text-[#9AA0A6] sm:block">可验证证据驱动的信贷调查工作台</p></div></div>
    <div className="flex items-center gap-3"><div className="case-switch" role="group" aria-label="数据案例"><button type="button" className={caseMode === 'public' ? 'is-active' : ''} aria-pressed={caseMode === 'public'} onClick={() => onMode('public')}>公开披露</button><button type="button" className={caseMode === 'simulation' ? 'is-active' : ''} aria-pressed={caseMode === 'simulation'} onClick={() => onMode('simulation')}>模拟沙盘</button></div><div className="hidden items-center gap-2 rounded-full border border-white/[0.055] bg-white/[0.035] px-3 py-1.5 text-xs text-[#BDC1C6] lg:flex" title={statusTitle}><span className={`h-2 w-2 rounded-full ${status.tone}`} />{caseMode === 'public' ? '公开快照' : status.label}{caseMode === 'simulation' && graphInventory && <span className="hidden text-[#80868B] sm:inline">· {graphInventory.nodes}/{graphInventory.relationships}</span>}</div></div>
  </header>;
}

function GraphHud({ data, selectedNode, inventory, caseMode }: { data: GraphData; selectedNode: GraphNode | null; inventory: GraphInventory | null; caseMode: CaseMode }) {
  const risks = data.nodes.filter((node) => node.properties?.risk_flag === true).length;
  const isPublic = caseMode === 'public';
  return <div className="glass-surface pointer-events-auto flex items-center overflow-hidden rounded-2xl border">
    <div className="flex items-center gap-3 px-3.5 py-2.5"><span className="flex h-8 w-8 items-center justify-center rounded-full bg-[#8AB4F8]/10"><Network size={16} className="text-[#8AB4F8]" /></span><div><div className="flex items-center gap-2"><p className="text-sm font-medium tracking-tight text-white">{isPublic ? '公开关系图谱' : '模拟关系画布'}</p><span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${isPublic ? 'bg-[#81C995]/10 text-[#81C995]' : 'bg-[#FDD663]/10 text-[#FDD663]'}`} title={data.meta?.message}>{isPublic ? '公开披露' : '模拟数据'}</span></div><p className="text-xs text-[#A7B4C6]">{selectedNode ? `当前聚焦 · ${selectedNode.label}` : isPublic ? data.meta?.as_of ? `年报与半年报 · 最新截至 ${data.meta.as_of}` : '正在读取公开披露关系' : inventory ? `沙盘图谱 ${inventory.nodes} 个实体` : '模拟风控关系视图'}</p></div></div>
    <div className="hidden items-center self-stretch border-l border-white/[0.07] sm:flex"><HudMetric label="实体" value={data.nodes.length} /><HudMetric label="关系" value={data.edges.length} /><HudMetric label={isPublic ? '来源' : '风险标记'} value={isPublic ? data.meta?.sources?.length ?? 0 : risks} alert={!isPublic} /></div>
  </div>;
}

function HudMetric({ label, value, alert = false }: { label: string; value: number; alert?: boolean }) { return <div className="financial-figures flex min-w-[62px] flex-col justify-center px-3"><span className="text-[9px] uppercase tracking-[0.13em] text-[#6F747B]">{label}</span><strong className={`mt-0.5 font-mono text-sm font-medium ${alert && value ? 'text-[#F28B82]' : 'text-[#DADCE0]'}`}>{value}</strong></div>; }

function InvestigationPanel({ compact, visible, stage, logs, logRef, running, onToggle, onShowGraph }: { compact: boolean; visible: boolean; stage: Stage; logs: LogEntry[]; logRef: React.RefObject<HTMLDivElement | null>; running: boolean; onToggle: () => void; onShowGraph: () => void }) {
  return <aside className={`glass-surface ${visible ? 'flex' : 'hidden'} absolute inset-0 z-30 flex-col border-0 transition-[width,transform] duration-300 xl:inset-y-4 xl:left-4 xl:flex xl:rounded-2xl xl:border ${compact ? 'xl:w-14' : 'xl:w-[320px]'}`}>
    <div className={`flex h-14 shrink-0 items-center ${compact ? 'justify-center' : 'justify-between'} px-3`}>{!compact && <div className="px-1"><p className="text-sm font-medium text-white">调查脉络</p><p className="text-[11px] text-[#80868B]">从任务到证据的实时推进</p></div>}<button className="icon-button border-0 bg-transparent" aria-label={compact ? '展开调查栏' : '收起调查栏'} onClick={onToggle}>{compact ? <Menu size={18} /> : <PanelLeftClose size={18} />}</button></div>
    {compact ? <div className="flex flex-1 flex-col items-center gap-4 pt-5">{STAGES.map((item, index) => <span key={item.key} className={`h-2 w-2 rounded-full ${index < STAGE_INDEX[stage] ? 'bg-[#81C995]' : index === STAGE_INDEX[stage] ? 'bg-[#8AB4F8]' : 'bg-white/15'}`} title={item.label} />)}</div> : <><StageRail stage={stage} /><div ref={logRef} className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-5 py-5">{logs.length === 0 ? <div className="flex h-full flex-col items-start justify-center px-2"><span className="mb-5 h-px w-10 bg-[#8AB4F8]" /><p className="text-lg font-medium tracking-tight text-[#E8EAED]">从一个线索开始</p><p className="mt-2 max-w-[230px] text-[13px] leading-6 text-[#9AA0A6]">输入企业、实控人或异常资金链路。关键节点与证据会沿时间线逐步浮现。</p><button className="secondary-button mt-5 xl:hidden" onClick={onShowGraph}><Network size={16} />前往关系图</button></div> : logs.map((log) => <LogItem key={log.id} log={log} />)}{running && <div className="ml-6 mt-1 flex items-center gap-2 text-xs text-[#8AB4F8]"><Loader2 size={13} className="animate-spin" />协同审查中</div>}</div></>}
  </aside>;
}

function StageRail({ stage }: { stage: Stage }) {
  const current = STAGE_INDEX[stage];
  return <div className="grid grid-cols-4 border-b border-white/[0.06] px-4 py-4">{STAGES.map((item, index) => { const done = index < current || stage === 'done'; const active = index === current && stage !== 'done'; return <div key={item.key} className="relative flex flex-col items-center gap-1.5">{index < STAGES.length - 1 && <span className={`absolute left-[58%] top-[11px] h-px w-[84%] ${done ? 'bg-[#81C995]/50' : 'bg-white/10'}`} />}<span className={`relative z-10 flex h-[22px] w-[22px] items-center justify-center rounded-full text-[11px] ${done ? 'bg-[#81C995] text-[#202124]' : active ? 'bg-[#8AB4F8] text-[#202124]' : 'bg-[#2B2D32] text-[#9AA0A6]'}`}>{done ? <Check size={13} strokeWidth={3} /> : index + 1}</span><span className={`text-xs ${done ? 'text-[#81C995]' : active ? 'text-[#AECBFA]' : 'text-[#80868B]'}`}>{item.label}</span></div>; })}</div>;
}

function LogItem({ log }: { log: LogEntry }) {
  const icon = log.kind === 'error' ? <AlertCircle size={14} /> : log.kind === 'decision' ? <Gavel size={14} /> : log.kind === 'report' ? <BadgeCheck size={14} /> : <Activity size={14} />;
  const tone = log.kind === 'error' ? 'text-[#F28B82]' : log.kind === 'decision' ? 'text-[#FDD663]' : log.kind === 'report' ? 'text-[#81C995]' : 'text-[#8AB4F8]';
  const prominent = log.kind === 'user' || log.kind === 'report' || log.kind === 'decision' || log.kind === 'error';
  return <article className="group relative pb-5 pl-7 last:pb-1"><span className="absolute bottom-0 left-[7px] top-5 w-px bg-gradient-to-b from-white/12 to-transparent group-last:hidden" /><span className={`absolute left-0 top-0.5 flex h-[15px] w-[15px] items-center justify-center rounded-full bg-[#17191D] ${tone}`}>{icon}</span><div className="flex min-w-0 items-center gap-2"><h3 className={`${prominent ? 'text-[13px] text-[#E8EAED]' : 'text-xs text-[#BDC1C6]'} truncate font-medium`}>{log.title}</h3><time className="ml-auto shrink-0 font-mono text-[10px] text-[#5F6368] opacity-0 transition group-hover:opacity-100">{log.time}</time></div><p className={`${prominent ? 'mt-1.5 text-[13px] leading-5 text-[#9AA0A6]' : 'mt-1 text-xs leading-5 text-[#80868B]'}`}>{log.content}</p></article>;
}
function GraphOverlay({ icon, title, description, action }: { icon: ReactNode; title: string; description: string; action?: ReactNode }) { return <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#0F1115]/75 backdrop-blur-sm"><div className="mx-5 flex max-w-sm flex-col items-center text-center text-[#9AA0A6]"><div className="mb-4 text-[#8AB4F8]">{icon}</div><h2 className="text-base font-medium text-white">{title}</h2><p className="mt-1 text-sm leading-6">{description}</p>{action && <div className="mt-4">{action}</div>}</div></div>; }

function CommandBar({ inputRef, input, running, showSuggestions, onInput, onSubmit, onSuggestion, onStop }: { inputRef: React.RefObject<HTMLInputElement | null>; input: string; running: boolean; showSuggestions: boolean; onInput: (value: string) => void; onSubmit: () => void; onSuggestion: (query: string) => void; onStop: () => void }) {
  const suggestions = [{ label: '识别大额资金回环', query: '调查同日大额资金回环与快进快出路径' }, { label: '穿透隐性实际控制人', query: '穿透启明控股的受益所有人与隐性控制链' }];
  return <form className="command-dock pointer-events-auto w-full max-w-[760px] rounded-[22px] border border-white/[0.11] bg-[#202124]/94 p-2 shadow-[0_18px_60px_rgba(0,0,0,0.42)] backdrop-blur-2xl focus-within:border-[#8AB4F8]/55 focus-within:ring-2 focus-within:ring-[#8AB4F8]/15 xl:max-w-[610px]" onSubmit={(event) => { event.preventDefault(); void onSubmit(); }}>{showSuggestions && !input && <div className="scrollbar-hide flex gap-2 overflow-x-auto px-2 pb-1 pt-1"><span className="flex shrink-0 items-center gap-1.5 pr-1 text-[10px] font-medium uppercase tracking-[0.13em] text-[#6F747B]"><Sparkles size={12} className="text-[#8AB4F8]" />Demo</span>{suggestions.map((item) => <button key={item.label} type="button" onClick={() => void onSuggestion(item.query)} className="shrink-0 rounded-full border border-white/[0.08] bg-white/[0.035] px-3 py-1.5 text-[11px] text-[#BDC1C6] transition hover:border-[#8AB4F8]/30 hover:bg-[#8AB4F8]/8 hover:text-white">{item.label}</button>)}</div>}<div className="flex items-center gap-2 pl-2"><Search size={18} className="shrink-0 text-[#9AA0A6]" /><input ref={inputRef} value={input} onChange={(event) => onInput(event.target.value)} disabled={running} aria-label="调查目标" placeholder="搜索企业、实控人或异常资金链路" className="min-w-0 flex-1 bg-transparent px-1 py-2.5 text-sm text-white placeholder:text-[#80868B] focus:outline-none disabled:opacity-60" /><button type={running ? 'button' : 'submit'} onClick={running ? onStop : undefined} disabled={!running && !input.trim()} className={`flex h-10 min-w-10 items-center justify-center rounded-[14px] transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${running ? 'bg-[#F28B82]/15 text-[#F28B82] hover:bg-[#F28B82]/25' : 'bg-[#8AB4F8] text-[#202124] shadow-[0_5px_18px_rgba(138,180,248,0.18)] hover:bg-[#AECBFA] disabled:opacity-35'}`} aria-label={running ? '停止调查' : '发起调查'}>{running ? <Square size={15} fill="currentColor" /> : <ArrowUp size={18} strokeWidth={2.5} />}</button></div></form>;
}

function TimeSlider({ range, cursor, onChange }: { range: TimeRange; cursor: number; onChange: (value: number) => void }) {
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    if (!playing) return;
    const step = Math.max(1, (range.max - range.min) / 160);
    const timer = window.setInterval(() => {
      const next = Math.min(range.max, cursor + step);
      onChange(next);
      if (next >= range.max) setPlaying(false);
    }, 60);
    return () => window.clearInterval(timer);
  }, [cursor, onChange, playing, range.max, range.min]);
  return <div className="glass-surface financial-figures pointer-events-auto flex w-full max-w-[640px] items-center gap-3 rounded-full border px-3 py-2 xl:max-w-[320px]"><button className="icon-button h-9 w-9" onClick={() => setPlaying((value) => !value)} aria-label={playing ? '暂停资金时序回放' : '播放资金时序回放'}>{playing ? <Pause size={16} /> : <Play size={16} />}</button><label htmlFor="timeline" className="hidden text-xs font-medium text-[#BDC1C6] sm:block xl:hidden">资金时序</label><input id="timeline" aria-label="资金时序游标" type="range" min={range.min} max={range.max} step={Math.max(1, Math.floor((range.max - range.min) / 200))} value={cursor} onChange={(event) => { setPlaying(false); onChange(Number(event.target.value)); }} className="min-w-0 flex-1 accent-[#8AB4F8]" /><time className="shrink-0 font-mono text-[11px] text-[#BDC1C6]">{formatTime(cursor)}</time></div>;
}

function LegendPopover({ schema, onClose }: { schema: SchemaResponse; onClose: () => void }) { return <section className="absolute right-4 top-[76px] z-30 w-64 rounded-2xl border border-white/10 bg-[#202124]/95 p-4 shadow-2xl backdrop-blur-2xl" aria-label="图谱图例"><div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-medium text-white">图谱图例</h2><button className="icon-button h-8 w-8" aria-label="关闭图例" onClick={onClose}><X size={15} /></button></div><div className="grid grid-cols-2 gap-2">{schema.node_types.map((node) => <div key={node.label} className="flex items-center gap-2 text-xs text-[#BDC1C6]"><span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: node.color }} />{node.name}</div>)}</div><div className="my-3 h-px bg-white/[0.08]" /><div className="space-y-2">{schema.edge_types.map((edge) => <div key={edge.type} className="flex items-center gap-2 text-xs text-[#BDC1C6]"><span className="w-7 border-t border-dashed" style={{ borderColor: edge.color }} />{edge.name}</div>)}</div></section>; }

function InspectorPanel(props: { visible: boolean; activeTab: InspectorTab; onTab: (tab: InspectorTab) => void; selectedNode: GraphNode | null; selectedEdge: GraphEdge | null; graph: GraphData; degree: number; review: ReviewState | null; agents: Record<AgentKey, AgentState>; legalClauses: { clause_id: string; text: string }[]; invalidCitations: string[]; rationale: string; reviewToken: string; reviewing: boolean; reviewSubmitted: boolean; reviewResult: string; onClose: () => void; onRationale: (value: string) => void; onReviewToken: (value: string) => void; onReview: (decision: 'confirm' | 'reject') => void; onOpenClause: (clause: { clause_id: string; text: string }) => void }) {
  const tabs: { key: InspectorTab; label: string; disabled?: boolean }[] = [
    { key: 'overview', label: '概览' },
    { key: 'evidence', label: '证据', disabled: !props.review && !props.selectedEdge },
    { key: 'review', label: '复核', disabled: !props.review || props.review.decision === 'NEEDS_EVIDENCE' },
  ];
  return <aside className={`glass-surface ${props.visible ? 'flex' : 'hidden'} absolute inset-0 z-30 min-w-0 flex-col border-0 xl:inset-y-4 xl:left-auto xl:right-4 xl:flex xl:w-[420px] xl:rounded-2xl xl:border`}>
    <div className="flex h-14 shrink-0 items-center justify-between gap-3 px-4"><div className="min-w-0"><p className="truncate text-sm font-medium text-white">{props.selectedEdge ? '关系证据' : props.selectedNode?.label ?? '调查结论'}</p><p className="truncate text-[11px] text-[#80868B]">{props.selectedEdge ? `图谱选择 · ${props.selectedEdge.type}` : props.selectedNode ? `图谱选择 · ${TYPE_META[props.selectedNode.type]?.label ?? props.selectedNode.type}` : '证据、依据与人工决策'}</p></div><div className="flex shrink-0 items-center gap-2">{props.review && <span className="rounded-full bg-[#F28B82]/10 px-2.5 py-1 text-[11px] tracking-wide text-[#F28B82]">{displayDecision(props.review.decision)}</span>}<button className="icon-button border-0 bg-transparent" aria-label="关闭调查结果" onClick={props.onClose}><PanelRightClose size={18} /></button></div></div>
    <div className="flex h-11 shrink-0 items-end gap-6 border-b border-white/[0.07] px-5" role="tablist" aria-label="调查结果视图">{tabs.map((tab) => <button key={tab.key} role="tab" aria-selected={props.activeTab === tab.key} disabled={tab.disabled} onClick={() => props.onTab(tab.key)} className={`relative h-11 text-xs font-medium transition disabled:opacity-30 ${props.activeTab === tab.key ? 'text-[#AECBFA]' : 'text-[#9AA0A6] hover:text-white'}`}>{tab.label}{props.activeTab === tab.key && <span className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-[#8AB4F8]" />}</button>)}</div>
    <div className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-5 py-5">
      {props.activeTab === 'overview' && <div>{props.review && <DecisionSummary review={props.review} graph={props.graph} />}{props.selectedNode ? <EntityOverview node={props.selectedNode} degree={props.degree} /> : !props.review && <EmptyInspector />}</div>}
      {props.activeTab === 'evidence' && <div>{props.selectedEdge && <EvidenceDetail edge={props.selectedEdge} graph={props.graph} />}{props.review && <AgentSummary agents={props.agents} />}{props.legalClauses.length > 0 && <section className="mt-7 border-t border-white/[0.07] pt-5"><SectionTitle icon={<Scale size={15} />} title="监管依据" /><p className="mt-1 text-xs leading-5 text-[#80868B]">引用经服务端白名单校验；具体案件适用性仍需人工核查。</p><div className="mt-3 flex flex-wrap gap-2">{props.legalClauses.map((clause) => <button key={clause.clause_id} className="rounded-lg border border-[#8AB4F8]/18 bg-[#8AB4F8]/[0.055] px-2.5 py-1.5 text-[11px] font-medium text-[#AECBFA] transition hover:border-[#8AB4F8]/35 hover:bg-[#8AB4F8]/10" onClick={() => props.onOpenClause(clause)}>{clause.clause_id}</button>)}</div>{props.invalidCitations.length > 0 && <p className="mt-3 text-xs text-[#9AA0A6]">已过滤 {props.invalidCitations.length} 条未验证引用</p>}</section>}</div>}
      {props.activeTab === 'review' && props.review && <ReviewWorkspace review={props.review} rationale={props.rationale} reviewToken={props.reviewToken} reviewing={props.reviewing} submitted={props.reviewSubmitted} result={props.reviewResult} onRationale={props.onRationale} onReviewToken={props.onReviewToken} onReview={props.onReview} />}
    </div>
  </aside>;
}

function DecisionSummary({ review, graph }: { review: ReviewState; graph: GraphData }) {
  const tone = review.decision === 'REJECT' ? 'text-[#F28B82]' : review.decision === 'FLAGGED' || review.decision === 'NEEDS_EVIDENCE' ? 'text-[#FDD663]' : 'text-[#81C995]';
  const locatedEdges = graph.edges.filter((edge) => review.suspiciousEdgeIds.includes(edge.id));
  return <section className="border-b border-white/[0.07] pb-6"><p className="text-xs font-medium text-[#9AA0A6]">{review.decision === 'NEEDS_EVIDENCE' ? '调查状态 · 等待补充线索' : '调查建议 · 待人工复核'}</p><div className="mt-3 flex items-baseline justify-between gap-3"><strong className={`text-2xl tracking-[-0.02em] ${tone}`}>{displayDecision(review.decision)}</strong><span className="text-xs text-[#9AA0A6]">非自动审批</span></div><dl className="mt-5 grid grid-cols-3 gap-4"><Metric label="数据来源" value={review.analysisSource === 'neo4j' ? '模拟图数据库' : review.analysisSource === 'local_demo' ? '浏览器模拟' : '内置模拟'} /><Metric label="可疑实体" value={review.suspiciousNodes.length} /><Metric label="定位关系" value={locatedEdges.length} /></dl><p className="mt-5 border-t border-white/[0.06] pt-4 text-xs leading-6 text-[#9AA0A6]">{review.decision === 'NEEDS_EVIDENCE' ? '当前案例不覆盖这个问题。请明确调查对象并补充授权数据。' : '这些关系来自当前模拟子图。分数未经独立校准，暂不展示为绝对风险概率；请逐条核对关系与原始凭证。'}</p></section>;
}

type PublicFilter = 'all' | 'holdings' | 'subsidiaries' | 'investments';
function PublicCaseCard({ graph, activeId, onSelect, onSimulation, onOpenLei, onInvestigate, onOpenBenchmark }: { graph: GraphData; activeId: string | null; onSelect: (edge: GraphEdge) => void; onSimulation: () => void; onOpenLei: () => void; onInvestigate: () => void; onOpenBenchmark: () => void }) {
  const [listExpanded, setListExpanded] = useState(false);
  const [filter, setFilter] = useState<PublicFilter>('all');
  const sources = graph.meta?.sources ?? [];
  const filtered = graph.edges.filter((edge) => filter === 'all'
    || (filter === 'holdings' && ['DISCLOSED_HOLDING', 'CONTROLLING_SHAREHOLDER'].includes(edge.type))
    || (filter === 'subsidiaries' && edge.type === 'GROUP_SUBSIDIARY')
    || (filter === 'investments' && edge.type === 'PARENT_INVESTMENT'));
  const tabs: { id: PublicFilter; label: string }[] = [
    { id: 'all', label: '全部' }, { id: 'holdings', label: '股东' },
    { id: 'subsidiaries', label: '子公司' }, { id: 'investments', label: '投资' },
  ];
  return <section className="case-journey public-case-card pointer-events-auto" aria-label="公开披露案例导览">
    <div className="case-journey__eyebrow"><span className="case-journey__signal" />公开披露关系 <span className="ml-auto text-xs tracking-normal text-[#81C995]">{sources.length} 份来源文件</span></div>
    <div className="case-journey__rule" />
    <p className="case-journey__kicker">案例 01 / 宁德时代</p>
    <h2 className="case-journey__title">更多关系，<br /><span>逐条可证。</span></h2>
    <p className="case-journey__body">整合 2025 年报与 2026 年半年报的股东、集团子公司及母公司投资记录。每条关系保留披露时点和原文页码。</p>
    <div className="public-case-card__stats"><div><strong>{graph.nodes.length}</strong><span>公开主体</span></div><div><strong>{graph.edges.length}</strong><span>披露关系</span></div><div><strong>0</strong><span>真实流水</span></div></div>
    <button type="button" className="public-case-card__investigate" onClick={onInvestigate}><span><FileSearch size={17} />启动公开关系核验</span><ArrowUp size={16} className="rotate-45" /></button>
    <details className="public-case-card__more"><summary>更多研究工具 <ChevronDown size={15} /></summary><button className="public-case-card__api" type="button" onClick={onOpenLei}><Search size={15} />GLEIF 金融机构查验<span>API</span><ArrowUp size={14} className="rotate-45" /></button><button className="public-case-card__benchmark" type="button" onClick={onOpenBenchmark}><Activity size={15} />链上交易独立评测<span>研究</span><ArrowUp size={14} className="rotate-45" /></button></details>
    <button type="button" className="public-case-card__mobile-toggle" aria-expanded={listExpanded} onClick={() => setListExpanded((value) => !value)}>{listExpanded ? '收起披露关系' : `查看 ${graph.edges.length} 条披露关系`}<ChevronDown size={15} className={listExpanded ? 'rotate-180' : ''} /></button>
    <div className={`public-case-card__detail ${listExpanded ? 'is-expanded' : ''}`}>
      <div className="public-case-card__filters" role="group" aria-label="筛选公开关系">{tabs.map((tab) => <button type="button" key={tab.id} className={filter === tab.id ? 'is-active' : ''} aria-pressed={filter === tab.id} onClick={() => setFilter(tab.id)}>{tab.label}</button>)}</div>
      <div className="public-case-card__list" aria-label="可核验关系列表">{filtered.map((edge) => {
        const party = edge.type === 'DISCLOSED_HOLDING' || edge.type === 'CONTROLLING_SHAREHOLDER' ? edge.source : edge.target;
        const partyName = graph.nodes.find((node) => node.id === party)?.label ?? party;
        const percent = typeof edge.properties.ratio === 'number' ? `${Number((edge.properties.ratio * 100).toFixed(2))}%` : null;
        const book = typeof edge.properties.book_value_thousand_cny === 'number' ? `${(edge.properties.book_value_thousand_cny / 100000).toFixed(1)} 亿元` : null;
        return <button key={edge.id} type="button" className={`public-case-card__row ${activeId === edge.id ? 'is-active' : ''}`} onClick={() => onSelect(edge)}><span className="public-case-card__row-main"><span className="truncate">{partyName}</span><small>{edge.label} · {edge.properties.source_system === 'CNINFO' ? '半年报' : '年报'}第 {String(edge.properties.source_page)} 页</small></span><span className="public-case-card__ratio">{percent ?? book ?? '—'}</span></button>;
      })}</div>
    </div>
    <div className="public-case-card__sources">{sources.map((source) => <a key={source.id} href={`${source.url}#page=${source.id === 'CNINFO-2026-H1' ? 65 : 207}`} target="_blank" rel="noopener noreferrer"><BadgeCheck size={14} />{source.title}<ExternalLink size={12} /></a>)}</div>
    <button className="public-case-card__simulation" type="button" onClick={onSimulation}>进入模拟资金流沙盘 <ArrowUp size={14} className="rotate-45" /></button>
    <p className="public-case-card__disclaimer">公开披露关系与虚构交易隔离；LEI 同名查询不自动认定为同一主体。</p>
  </section>;
}

function EvidenceDetail({ edge, graph }: { edge: GraphEdge; graph: GraphData }) {
  const source = graph.nodes.find((node) => node.id === edge.source)?.label ?? edge.source;
  const target = graph.nodes.find((node) => node.id === edge.target)?.label ?? edge.target;
  const amount = Number(edge.properties.amount);
  const record = edge.properties.record_id ?? edge.properties.source_record_id ?? edge.properties.transaction_id;
  const date = edge.properties.date ? new Date(String(edge.properties.date)) : null;
  const publicRecord = edge.properties.data_class === 'public_disclosure';
  const sourceUrl = typeof edge.properties.source_url === 'string' ? edge.properties.source_url : null;
  const ratio = typeof edge.properties.ratio === 'number' ? `${(edge.properties.ratio * 100).toFixed(2)}%` : null;
  const shares = typeof edge.properties.share_count === 'number' ? `${edge.properties.share_count.toLocaleString('zh-CN')} 股` : null;
  const bookValue = typeof edge.properties.book_value_thousand_cny === 'number' ? `${edge.properties.book_value_thousand_cny.toLocaleString('zh-CN')} 千元` : null;
  const sourceTitle = String(edge.properties.source_title ?? graph.meta?.source_title ?? '公开披露文件');
  const scope = edge.properties.identity_note ?? edge.properties.amount_scope ?? edge.properties.ratio_scope;
  return <section className="evidence-detail mb-7" aria-label="当前关系证据">
    <div className="flex items-center justify-between"><p className="text-[10px] font-semibold tracking-[0.18em] text-[#AECBFA]">SELECTED EVIDENCE</p><span className={`rounded-full border px-2 py-1 text-[10px] ${publicRecord ? 'border-[#81C995]/25 text-[#81C995]' : 'border-[#FDD663]/25 text-[#FDD663]'}`}>{publicRecord ? '公开披露' : '模拟数据'}</span></div>
    <h2 className="mt-4 text-[21px] font-semibold leading-tight tracking-tight text-white">{source}<span className="mx-2 text-[#8AB4F8]">→</span>{target}</h2>
    <p className="mt-2 text-xs text-white/45">{publicRecord ? edge.label : edge.type === 'TRANSFER' ? '模拟资金交易关系' : `模拟关系 · ${edge.type}`}</p>
    {publicRecord ? <>
      <p className="mt-5 text-sm leading-7 text-[#DADCE0]">{String(edge.properties.evidence_text ?? '')}</p>
      <p className="mt-4 text-xs leading-5 text-[#AECBFA]">{sourceTitle}</p>
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-5 border-y border-white/10 py-5">
        <Metric label="披露时点" value={String(edge.properties.as_of ?? '—')} />
        <Metric label="报告页码" value={`第 ${String(edge.properties.source_page ?? '—')} 页`} />
        {ratio && <Metric label="报告持股比例" value={ratio} />}
        {shares && <Metric label="期末持股数量" value={shares} />}
        {bookValue && <Metric label="投资账面价值" value={bookValue} />}
        <Metric label="来源记录号" value={record ? String(record) : '—'} />
      </dl>
      {scope && <p className="mt-4 text-xs leading-6 text-[#9AA0A6]">{String(scope)}</p>}
      {sourceUrl && <a className="evidence-source-link" href={`${sourceUrl}#page=${String(edge.properties.source_page ?? 1)}`} target="_blank" rel="noopener noreferrer"><FileSearch size={16} />打开原始披露，核对第 {String(edge.properties.source_page)} 页<ExternalLink size={15} /></a>}
    </> : <><dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-5 border-y border-white/10 py-5"><Metric label="模拟关系 ID" value={edge.id} /><Metric label="模拟金额" value={Number.isFinite(amount) ? `¥${amount.toLocaleString('zh-CN')}` : '未提供'} /><Metric label="模拟时间" value={date && !Number.isNaN(date.getTime()) ? formatTime(date.getTime()) : '未提供'} /><Metric label="真实凭证" value="无" /></dl><p className="mt-4 text-xs leading-6 text-[#9AA0A6]">这条关系仅用于演示图算法与取证交互，不代表任何真实交易。</p></>}
  </section>;
}

function EntityOverview({ node, degree }: { node: GraphNode; degree: number }) {
  const meta = TYPE_META[node.type] ?? { label: node.type, icon: <GitBranch size={16} />, tone: 'text-[#BDC1C6] bg-white/[0.06]' };
  const properties = Object.entries(node.properties).filter(([key]) => !['name', 'account_no'].includes(key)).slice(0, 8);
  return <section className={"pt-6"}><div className="flex items-center gap-3"><div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${meta.tone}`}>{meta.icon}</div><div className="min-w-0"><p className="text-[11px] uppercase tracking-wider text-[#80868B]">当前图谱选择</p><h2 className="mt-0.5 truncate text-lg font-medium tracking-tight text-white">{node.label}</h2></div></div><dl className="mt-5 grid grid-cols-3 gap-4 border-y border-white/[0.06] py-4"><Metric label="实体类型" value={meta.label} /><Metric label="直接关系" value={degree} /><Metric label={node.properties.data_class === 'public_disclosure' ? '数据性质' : '风险状态'} value={node.properties.data_class === 'public_disclosure' ? '公开披露' : node.properties.risk_flag ? '模拟标记' : '未标记'} /></dl>{properties.length > 0 && <dl className="mt-4">{properties.map(([key, value]) => <div key={key} className="grid grid-cols-[110px_1fr] gap-4 py-2 text-xs"><dt className="text-[#80868B]">{key}</dt><dd className="break-words text-right text-[#DADCE0]">{displayProperty(key, value)}</dd></div>)}</dl>}</section>;
}

function EmptyInspector() { return <div className="flex min-h-64 flex-col justify-center"><span className="h-px w-10 bg-[#8AB4F8]" /><h2 className="mt-5 text-lg font-medium text-white">选择一个图谱实体</h2><p className="mt-2 max-w-xs text-sm leading-6 text-[#9AA0A6]">属性、直接关系和风险状态会随当前选择实时更新。</p></div>; }
function Metric({ label, value }: { label: string; value: ReactNode }) { return <div className="min-w-0"><dt className="text-[10px] uppercase tracking-wider text-[#80868B]">{label}</dt><dd className="mt-1 truncate text-sm font-medium text-[#E8EAED]">{value}</dd></div>; }
function SectionTitle({ icon, title }: { icon: ReactNode; title: string }) { return <div className="flex items-center gap-2 text-sm font-medium text-[#DADCE0]"><span className="text-[#8AB4F8]">{icon}</span>{title}</div>; }

function AgentSummary({ agents }: { agents: Record<AgentKey, AgentState> }) {
  const rows = [{ key: 'risk' as const, label: '风险审查', icon: <Brain size={15} /> }, { key: 'compliance' as const, label: '合规审查', icon: <Scale size={15} /> }, { key: 'decision' as const, label: '决策汇总', icon: <Gavel size={15} /> }];
  return <section><SectionTitle icon={<Brain size={15} />} title="协同证据流" /><div className="mt-3">{rows.map((row) => { const state = agents[row.key]; return <details key={row.key} className="group border-b border-white/[0.06] last:border-0" open={state.status === 'done'}><summary className="flex cursor-pointer list-none items-center gap-3 py-4 text-sm"><span className="text-[#9AA0A6]">{row.icon}</span><span className="flex-1 text-[#DADCE0]">{row.label}</span><span className={`h-2 w-2 rounded-full ${state.status === 'done' ? 'bg-[#81C995]' : state.status === 'processing' ? 'animate-pulse bg-[#8AB4F8]' : 'bg-white/20'}`} /><ChevronDown size={14} className="text-[#80868B] transition group-open:rotate-180" /></summary>{state.finding && <p className="whitespace-pre-line pb-4 pl-7 text-[13px] leading-6 text-[#9AA0A6]">{state.finding}</p>}</details>; })}</div></section>;
}

function ReviewWorkspace({ review, rationale, reviewToken, reviewing, submitted, result, onRationale, onReviewToken, onReview }: { review: ReviewState; rationale: string; reviewToken: string; reviewing: boolean; submitted: boolean; result: string; onRationale: (value: string) => void; onReviewToken: (value: string) => void; onReview: (decision: 'confirm' | 'reject') => void }) {
  return <section>
    <div className="flex items-start justify-between gap-4 border-b border-white/[0.07] pb-5"><div><p className="text-xs text-[#A7B4C6]">待复核建议</p><p className="mt-1 text-lg text-[#FDD663]">{displayDecision(review.decision)}</p></div><div className="text-right"><p className="text-xs text-[#A7B4C6]">涉及实体</p><p className="mt-1 text-sm text-white">{review.suspiciousNodes.length} 个</p></div></div>
    <label htmlFor="rationale" className="mt-6 block text-xs font-medium text-[#BDC1C6]">复核说明</label>
    <textarea id="rationale" value={rationale} onChange={(event) => onRationale(event.target.value)} rows={6} placeholder="记录判断依据、补充证据或驳回原因。内容将进入审计记录。" className="mt-2 w-full resize-none border-0 border-b border-white/10 bg-transparent px-0 py-3 text-sm leading-6 text-white placeholder:text-[#5F6368] focus:border-[#8AB4F8] focus:outline-none" />
    {review.investigationId ? <><label htmlFor="review-token" className="mt-5 block text-xs font-medium text-[#BDC1C6]">复核口令 <span className="font-normal text-[#A7B4C6]">· 由管理员提供</span></label><input id="review-token" type="password" autoComplete="off" value={reviewToken} onChange={(event) => onReviewToken(event.target.value)} placeholder="仅保留在当前页面会话" className="mt-2 w-full border-0 border-b border-white/10 bg-transparent px-0 py-3 text-sm text-white placeholder:text-[#A7B4C6] focus:border-[#8AB4F8] focus:outline-none" /></> : <p className="mt-5 text-xs leading-5 text-[#FDD663]">浏览器本地模拟：复核只能保存在本机，不能回写服务端。</p>}
    <div className="mt-6 grid grid-cols-2 gap-3"><button className="primary-button" disabled={reviewing || submitted || rationale.trim().length < 5} onClick={() => onReview('confirm')}><Check size={16} />确认建议</button><button className="danger-button" disabled={reviewing || submitted || rationale.trim().length < 5} onClick={() => onReview('reject')}><X size={16} />驳回建议</button></div>
    {result && <p className="mt-4 text-xs leading-5 text-[#BDC1C6]" role="status">{result}</p>}
  </section>;
}

function FloatingReviewDock({ review, onOpen }: { review: ReviewState; onOpen: () => void }) {
  const blockers = Math.max(1, Math.min(2, review.suspiciousNodes.length));
  return <button className="review-action-dock glass-surface group absolute z-40 flex max-w-[calc(100%-32px)] items-center gap-3 rounded-full border px-3 py-2 text-left" onClick={onOpen} aria-label={`打开人工签批，${blockers} 项阻断风险`}>
    <span className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#F28B82]/12 text-[#F28B82]"><span className="absolute inset-0 animate-ping rounded-full border border-[#F28B82]/20" /><ShieldCheck size={17} /></span>
    <span className="min-w-0"><span className="block truncate text-xs font-medium text-white">等待人工确认 <span className="text-[#5F6368]">·</span> {blockers} 项阻断风险</span><span className="mt-0.5 block text-[10px] text-[#9AA0A6]">直接进入人在回路签批</span></span>
    <span className="ml-1 flex h-8 shrink-0 items-center gap-1 rounded-full bg-[#E8EAED] px-3 text-[11px] font-semibold text-[#202124] transition group-hover:bg-white">签批<ArrowUp size={13} className="rotate-45" /></span>
  </button>;
}

function MobileNavigation({ caseMode, view, hasResult, onChange, onPublicEvidence, onSimulation }: { caseMode: CaseMode; view: MobileView; hasResult: boolean; onChange: (view: MobileView) => void; onPublicEvidence: () => void; onSimulation: () => void }) {
  if (caseMode === 'public') return <nav className="relative z-50 grid h-16 shrink-0 grid-cols-3 border-t border-white/[0.08] bg-[#17191D] xl:hidden" aria-label="公开案例导航"><button onClick={() => onChange('graph')} className="flex flex-col items-center justify-center gap-1 text-xs text-[#8AB4F8]"><Network size={19} />关系图</button><button onClick={onPublicEvidence} className="flex flex-col items-center justify-center gap-1 text-xs text-[#BDC1C6]"><FileSearch size={19} />逐条核验</button><button onClick={onSimulation} className="flex flex-col items-center justify-center gap-1 text-xs text-[#BDC1C6]"><Activity size={19} />模拟沙盘</button></nav>;
  const items = [{ key: 'investigation' as const, label: '调查', icon: <Activity size={19} /> }, { key: 'graph' as const, label: '关系图', icon: <Network size={19} /> }, { key: 'decision' as const, label: '结果', icon: <FileSearch size={19} />, disabled: !hasResult }];
  return <nav className="relative z-50 grid h-16 shrink-0 grid-cols-3 border-t border-white/[0.08] bg-[#17191D] xl:hidden" aria-label="模拟调查导航">{items.map((item) => <button key={item.key} disabled={item.disabled} onClick={() => onChange(item.key)} className={`flex flex-col items-center justify-center gap-1 text-xs transition ${view === item.key ? 'text-[#8AB4F8]' : 'text-[#9AA0A6]'} disabled:opacity-35`}>{item.icon}{item.label}</button>)}</nav>;
}

function LeiExplorer({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState('UBS AG');
  const [result, setResult] = useState<LeiSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);
  const requestRef = useRef(0);
  const runSearch = (term: string) => {
    const normalized = term.trim();
    if (normalized.length < 2) { setError('请至少输入 2 个字符'); return; }
    const requestId = ++requestRef.current;
    setLoading(true); setError(''); setResult(null);
    void searchLeiEntities(normalized).then((data) => {
      if (requestRef.current === requestId) setResult(data);
    }).catch(() => {
      if (requestRef.current === requestId) setError('GLEIF 官方接口暂时无法访问。公开披露图谱仍可正常查看。');
    }).finally(() => { if (requestRef.current === requestId) setLoading(false); });
  };
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    searchRef.current?.focus();
    const initialSearch = window.setTimeout(() => runSearch('UBS AG'), 0);
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => { window.clearTimeout(initialSearch); requestRef.current += 1; window.removeEventListener('keydown', onKey); previousFocus?.focus(); };
  }, [onClose]);
  return <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/75 p-3 backdrop-blur-md sm:p-6" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section role="dialog" aria-modal="true" aria-labelledby="lei-title" className="lei-explorer flex w-full max-w-2xl flex-col overflow-hidden rounded-[26px] border border-white/10 bg-[#191d24] shadow-[0_30px_100px_rgba(0,0,0,.65)]">
      <header className="flex items-start justify-between gap-4 border-b border-white/[0.08] px-6 py-5"><div><p className="text-[10px] font-bold tracking-[0.17em] text-[#8AB4F8]">LIVE IDENTITY LOOKUP / GLEIF</p><h2 id="lei-title" className="mt-2 text-2xl font-semibold tracking-tight text-white">金融机构身份查验</h2><p className="mt-1 text-xs leading-5 text-[#9AA7BA]">实时检索全球法人识别编码 LEI，核对名称、辖区和登记状态。</p></div><button className="icon-button" type="button" aria-label="关闭 LEI 查询" onClick={onClose}><X size={18} /></button></header>
      <form className="flex gap-2 border-b border-white/[0.07] px-6 py-4" onSubmit={(event) => { event.preventDefault(); runSearch(query); }}><label className="lei-explorer__input"><Search size={17} /><input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} aria-label="机构法定名称" placeholder="输入机构法定名称，如 UBS AG" maxLength={120} /></label><button type="submit" className="primary-button shrink-0" disabled={loading}>{loading ? <Loader2 size={16} className="animate-spin" /> : '查询'}</button></form>
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5"><div className="lei-explorer__note"><BadgeCheck size={17} className="shrink-0 text-[#81C995]" /><p>同名结果可能属于不同法人。这里展示候选记录，不会自动把 LEI 绑定到图谱中的“UBS AG”股东，也不会据此推断交易关系。</p></div>
        {loading && <p className="py-10 text-center text-sm text-[#9AA7BA]">正在连接 GLEIF 官方 API…</p>}
        {error && <p role="alert" className="py-8 text-center text-sm text-[#FDD663]">{error}</p>}
        {result && <><div className="mb-3 mt-5 flex items-center justify-between gap-3"><p className="text-xs text-[#9AA7BA]">匹配记录 {result.total} 条 · 展示前 {result.records.length} 条</p><p className="text-[10px] text-[#77859A]">Golden Copy {result.golden_copy_at?.slice(0, 10) ?? '—'}</p></div><div className="space-y-2">{result.records.map((record) => <article key={record.lei} className="lei-explorer__record"><div className="min-w-0"><h3 className="break-words text-[13px] font-semibold text-[#ECF2FF]">{record.legal_name}</h3><p className="mt-1 font-mono text-[11px] tracking-[.04em] text-[#8AB4F8]">{record.lei}</p><p className="mt-2 text-[11px] text-[#8795AA]">辖区 {record.jurisdiction ?? '—'} · 登记状态 {record.registration_status ?? '—'} · 更新 {record.last_updated?.slice(0, 10) ?? '—'}</p></div>{record.source_url && <a href={record.source_url} target="_blank" rel="noopener noreferrer" aria-label={`打开 ${record.legal_name} 的 GLEIF 原始记录`}><ExternalLink size={16} /></a>}</article>)}</div>{result.records.length === 0 && <p className="py-8 text-center text-sm text-[#9AA7BA]">未找到匹配的 LEI 记录。请尝试完整法定名称。</p>}</>}
      </div>
      <footer className="border-t border-white/[0.07] px-6 py-3 text-[11px] text-[#77859A]">数据来源：Global Legal Entity Identifier Foundation · 查询时间以服务器响应为准</footer>
    </section>
  </div>;
}

function LegalDialog({ clause, onClose }: { clause: { clause_id: string; text: string }; onClose: () => void }) { const closeRef = useRef<HTMLButtonElement>(null); useEffect(() => { const previousFocus = document.activeElement as HTMLElement | null; closeRef.current?.focus(); const handleKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); }; window.addEventListener('keydown', handleKey); return () => { window.removeEventListener('keydown', handleKey); previousFocus?.focus(); }; }, [onClose]); return <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section role="dialog" aria-modal="true" aria-labelledby="legal-title" className="w-full max-w-xl rounded-2xl border border-white/10 bg-[#202124] shadow-2xl"><header className="flex items-center justify-between border-b border-white/[0.08] px-5 py-4"><div><p className="text-xs text-[#9AA0A6]">验证通过的监管依据</p><h2 id="legal-title" className="mt-0.5 text-base font-medium text-white">{clause.clause_id}</h2></div><button ref={closeRef} className="icon-button" aria-label="关闭监管依据" onClick={onClose}><X size={18} /></button></header><div className="px-5 py-5"><p className="text-sm leading-7 text-[#DADCE0]">{clause.text}</p></div><footer className="border-t border-white/[0.06] px-5 py-3 text-xs text-[#9AA0A6]">内容来自系统法条白名单，引用已通过服务端校验。</footer></section></div>; }
