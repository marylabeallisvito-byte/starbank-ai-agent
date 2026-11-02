import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, BadgeCheck, Check, ChevronDown, Download, ExternalLink, FileSearch, Fingerprint, Loader2, Search, X } from 'lucide-react';
import { fetchPublicInvestigation, type PublicInvestigationReport } from '../api';
import type { GraphData, GraphEdge } from '../types';

type Category = 'all' | 'holdings' | 'subsidiaries' | 'investments';
const CATEGORY_LABEL: Record<Category, string> = { all: '全部证据', holdings: '股东', subsidiaries: '子公司', investments: '母公司投资' };

function categoryOf(type: string): Category {
  if (type === 'CONTROLLING_SHAREHOLDER' || type === 'DISCLOSED_HOLDING') return 'holdings';
  if (type === 'GROUP_SUBSIDIARY') return 'subsidiaries';
  return 'investments';
}

export default function PublicInvestigation({ graph, initialEntityId, onClose, onFocusEdge }: {
  graph: GraphData;
  initialEntityId: string;
  onClose: () => void;
  onFocusEdge: (edge: GraphEdge) => void;
}) {
  const [entityId, setEntityId] = useState(initialEntityId);
  const [report, setReport] = useState<PublicInvestigationReport | null>(null);
  const [category, setCategory] = useState<Category>('all');
  const [checked, setChecked] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [activeId, setActiveId] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let active = true;
    void fetchPublicInvestigation(entityId).then((data) => {
      if (!active) return;
      setReport(data);
      setActiveId(data.observations[0]?.edge_id ?? null);
      setLoading(false);
    }).catch(() => {
      if (!active) return;
      setError('无法生成核验结果。请检查后端服务后重试。');
      setLoading(false);
    });
    return () => { active = false; };
  }, [entityId, attempt]);

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => { window.removeEventListener('keydown', onKey); previousFocus?.focus(); };
  }, [onClose]);

  const visible = useMemo(() => report?.observations.filter((item) => category === 'all' || categoryOf(item.relation_type) === category) ?? [], [report, category]);
  const active = visible.find((item) => item.edge_id === activeId) ?? visible[0];
  const checkedSet = new Set(checked);
  const changeEntity = (next: string) => {
    setEntityId(next); setReport(null); setCategory('all'); setChecked([]);
    setActiveId(null); setError(''); setLoading(true);
  };
  const changeCategory = (next: Category) => {
    setCategory(next);
    setActiveId(null);
  };
  const exportReport = () => {
    if (!report) return;
    const output = { ...report, manual_review: {
      scope: '浏览器内人工核对清单；仅记录本次会话的勾选，不代表第三方审计或法律认定',
      checked_edge_ids: checked,
      exported_at: new Date().toISOString(),
    } };
    const url = URL.createObjectURL(new Blob([JSON.stringify(output, null, 2)], { type: 'application/json;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = `${report.report_id}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return <div className="public-investigation-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section role="dialog" aria-modal="true" aria-labelledby="public-investigation-title" className="public-investigation">
      <header className="public-investigation__header">
        <div><p className="public-investigation__eyebrow"><span />公开证据核验 <em>来源可追溯</em></p><h2 id="public-investigation-title">从披露关系，到可复核结论。</h2><p>选择主体，逐条核对原始披露，导出带来源指针的核验记录。</p></div>
        <button ref={closeRef} className="public-investigation__close" aria-label="关闭公开核验" onClick={onClose}><X size={18} /></button>
      </header>
      <div className="public-investigation__toolbar">
        <label className="public-investigation__select"><Search size={17} /><select aria-label="选择公开主体" value={entityId} onChange={(event) => changeEntity(event.target.value)}>{graph.nodes.map((node) => <option key={node.id} value={node.id}>{node.label}</option>)}</select><ChevronDown size={16} /></label>
        <span className="public-investigation__snapshot"><Fingerprint size={15} />固定披露快照 · 最新 {graph.meta?.as_of ?? '—'}</span>
      </div>
      {loading ? <div className="public-investigation__loading"><Loader2 className="animate-spin" size={22} />正在整理披露证据…</div> : error ? <div className="public-investigation__loading public-investigation__error">{error}<button onClick={() => { setError(''); setLoading(true); setAttempt((value) => value + 1); }}>重试</button></div> : report && <>
        <div className="public-investigation__summary"><div><span>核验对象</span><strong>{report.entity.name}</strong><p>{report.summary}</p></div><div className="public-investigation__summary-metrics"><div><strong>{report.observations.length}</strong><span>直接关系</span></div><div><strong>{report.sources.length}</strong><span>原始文件</span></div><div><strong>{checked.length}<small>/{report.observations.length}</small></strong><span>人工核对</span></div></div></div>
        <div className="public-investigation__body"><aside className="public-investigation__rail"><div className="public-investigation__tabs" role="group" aria-label="核验证据类别">{(['all', 'holdings', 'subsidiaries', 'investments'] as Category[]).map((item) => <button key={item} type="button" className={category === item ? 'is-active' : ''} aria-pressed={category === item} onClick={() => changeCategory(item)}>{CATEGORY_LABEL[item]}</button>)}</div><div className="public-investigation__results">{visible.map((item, index) => <button type="button" key={item.edge_id} className={`public-investigation__result ${active?.edge_id === item.edge_id ? 'is-active' : ''}`} onClick={() => setActiveId(item.edge_id)}><span className="public-investigation__result-index">{String(index + 1).padStart(2, '0')}</span><span className="public-investigation__result-text"><strong>{item.source_entity.id === entityId ? item.target_entity.name : item.source_entity.name}</strong><small>{item.relation_label} · {item.as_of}</small></span>{checkedSet.has(item.edge_id) && <Check size={15} className="public-investigation__checked" />}</button>)}{visible.length === 0 && <p className="public-investigation__empty">该主体在此类别中没有直接披露关系。</p>}</div></aside>
          <div className="public-investigation__evidence">{active ? <><div className="public-investigation__evidence-top"><p><BadgeCheck size={16} />SOURCE LINKED / {active.source_id}</p><span>{active.relation_label}</span></div><h3>{active.source_entity.name}<ArrowRight size={20} />{active.target_entity.name}</h3><p className="public-investigation__statement">{active.statement}</p><div className="public-investigation__facts"><div><span>披露时点</span><strong>{active.as_of}</strong></div><div><span>文件页码</span><strong>第 {active.source_page} 页</strong></div><div><span>来源记录</span><strong>{active.source_record_id}</strong></div></div>{active.qualification && <p className="public-investigation__qualification">{active.qualification}</p>}<div className="public-investigation__actions"><a href={active.source_url} target="_blank" rel="noopener noreferrer"><FileSearch size={16} />打开原始披露<ExternalLink size={14} /></a><button type="button" className={checkedSet.has(active.edge_id) ? 'is-checked' : ''} onClick={() => setChecked((current) => checkedSet.has(active.edge_id) ? current.filter((id) => id !== active.edge_id) : [...current, active.edge_id])}><Check size={16} />{checkedSet.has(active.edge_id) ? '已标记人工核对' : '标记已核对原文'}</button><button type="button" onClick={() => { const edge = graph.edges.find((item) => item.id === active.edge_id); if (edge) { onFocusEdge(edge); onClose(); } }}><ArrowRight size={16} />定位图谱</button></div></> : <div className="public-investigation__empty">请选择左侧关系，查看原始证据。</div>}</div></div>
        <footer className="public-investigation__footer"><p>仅依据公开披露组织事实，不推断真实资金流、欺诈或最终受益人。勾选仅在本次会话中有效。</p><button type="button" onClick={exportReport}><Download size={16} />导出核验记录</button></footer>
      </>}
    </section>
  </div>;
}
