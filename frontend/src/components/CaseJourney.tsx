import { useEffect, useState } from 'react';
import { ArrowRight, ChevronDown, ChevronLeft, ChevronRight, CirclePause, CirclePlay, Fingerprint, Route } from 'lucide-react';
import type { GraphData, GraphEdge } from '../types';

interface Props {
  graph: GraphData;
  evidence: GraphEdge[];
  activeId: string | null;
  running: boolean;
  hasResult: boolean;
  onStart: (query: string) => void;
  onSelect: (edge: GraphEdge, openDetails?: boolean) => void;
}

const EXAMPLE_QUERY = '调查同日大额资金回环与快进快出路径';

function label(graph: GraphData, id: string): string {
  return graph.nodes.find((node) => node.id === id)?.label ?? id;
}

function amount(edge: GraphEdge): string {
  const value = Number(edge.properties.amount);
  return Number.isFinite(value) ? `¥${value.toLocaleString('zh-CN')}` : '金额待核';
}

export default function CaseJourney({ graph, evidence, activeId, running, hasResult, onStart, onSelect }: Props) {
  const [playing, setPlaying] = useState(false);
  const [collapsed, setCollapsed] = useState(() => window.matchMedia('(max-width: 1279px)').matches);
  const activeIndex = evidence.findIndex((edge) => edge.id === activeId);

  useEffect(() => {
    const compact = window.matchMedia('(max-width: 1279px)');
    const update = () => setCollapsed(compact.matches);
    compact.addEventListener('change', update);
    return () => compact.removeEventListener('change', update);
  }, []);

  useEffect(() => {
    if (!playing || evidence.length < 2) return;
    const timer = window.setInterval(() => {
      const index = evidence.findIndex((edge) => edge.id === activeId);
      if (index >= evidence.length - 1) { setPlaying(false); return; }
      onSelect(evidence[index + 1], false);
    }, 2200);
    return () => window.clearInterval(timer);
  }, [playing, activeId, evidence, onSelect]);

  const move = (direction: number) => {
    if (!evidence.length) return;
    const next = Math.max(0, Math.min(evidence.length - 1, activeIndex + direction));
    onSelect(evidence[next], false);
  };

  return <section className={`case-journey pointer-events-auto ${collapsed ? 'is-collapsed' : ''}`} aria-label="案件调查导览">
    <div className="case-journey__eyebrow"><span className="case-journey__signal" />模拟调查导览 <span className="ml-auto text-xs tracking-normal text-[#FDD663]">虚构案例</span></div>
    {!hasResult ? <>
      <div className="case-journey__rule" />
      <p className="case-journey__kicker">案件 01 · 供应链金融</p>
      <h2 className="case-journey__title">让隐藏的<br /><span>资金路径</span>浮现。</h2>
      <p className="case-journey__body">从一条线索出发，穿透企业、账户与交易；每一步判断都能回到可核对的图谱关系。</p>
      <button className="case-journey__primary" disabled={running} onClick={() => onStart(EXAMPLE_QUERY)}><span>{running ? '正在调查' : '开始示范调查'}</span><ArrowRight size={17} /></button>
      <div className="case-journey__footer"><Fingerprint size={15} /><span>点击节点展开关联 · 点击连线核对证据</span></div>
    </> : <>
      <div className="case-journey__rule" />
      <div className="flex items-center gap-2"><Route size={16} className="text-[#AECBFA]" /><p className="case-journey__kicker">证据路径 / {evidence.length} 条关系</p></div>
      <h2 className="case-journey__result-title">{evidence.length ? <>逐笔核对，<br />还原风险轨迹。</> : <>证据不足，<br />先补齐线索。</>}</h2>
      <button className="case-journey__toggle" type="button" aria-expanded={!collapsed} onClick={() => setCollapsed((value) => !value)}>{collapsed ? '展开证据路径' : '收起证据路径'}<ChevronDown size={15} className={collapsed ? '' : 'rotate-180'} /></button>
      {evidence.length ? <>
        <div className="case-journey__steps" role="group" aria-label="可疑关系记录">
          {evidence.map((edge, index) => <button key={edge.id} type="button" aria-current={activeId === edge.id ? 'step' : undefined} className={`case-journey__step ${activeId === edge.id ? 'is-active' : ''}`} onClick={() => { setPlaying(false); onSelect(edge); }}>
            <span className="case-journey__step-number">{String(index + 1).padStart(2, '0')}</span>
            <span className="min-w-0 flex-1"><span className="block truncate text-[12px] font-medium text-white/90">{label(graph, edge.source)} → {label(graph, edge.target)}</span><span className="mt-1 block text-[11px] text-white/45">{edge.type === 'TRANSFER' ? amount(edge) : edge.type} · {edge.properties.date ? new Date(String(edge.properties.date)).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '关系记录'}</span></span><ChevronRight size={15} className="shrink-0 text-white/35" />
          </button>)}
        </div>
        <div className="case-journey__transport"><button type="button" aria-label="上一条证据" disabled={activeIndex <= 0} onClick={() => { setPlaying(false); move(-1); }}><ChevronLeft size={18} /></button><button type="button" aria-label={playing ? '暂停证据回放' : '播放证据回放'} onClick={() => { if (!playing && activeIndex < 0) onSelect(evidence[0], false); setPlaying((value) => !value); }}>{playing ? <CirclePause size={20} /> : <CirclePlay size={20} />}</button><button type="button" aria-label="下一条证据" disabled={activeIndex >= evidence.length - 1} onClick={() => { setPlaying(false); move(1); }}><ChevronRight size={18} /></button><span>{activeIndex < 0 ? '选择证据' : `${activeIndex + 1} / ${evidence.length}`}</span></div>
      </> : <p className="case-journey__body">本次结论没有返回可逐笔定位的关系 ID。请选择图谱连线查看已有字段，并人工核对原始记录。</p>}
    </>}
  </section>;
}
