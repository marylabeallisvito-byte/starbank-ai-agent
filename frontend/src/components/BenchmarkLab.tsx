import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, Database, ExternalLink, Fingerprint, ShieldAlert, X } from 'lucide-react';
import { fetchEllipticBenchmark, type EllipticBenchmarkReport } from '../api';

const MODELS = [
  { key: 'numeric_features', title: '匿名交易特征', note: '官方数值特征 · 无图结构' },
  { key: 'graph_topology', title: '图结构特征', note: '仅使用流向边 · 不读取标签' },
  { key: 'combined', title: '组合特征', note: '交易特征 + 图结构' },
] as const;

function percent(value: number, digits = 1) { return `${(value * 100).toFixed(digits)}%`; }
function count(value: number) { return value.toLocaleString('zh-CN'); }

export default function BenchmarkLab({ onClose }: { onClose: () => void }) {
  const [report, setReport] = useState<EllipticBenchmarkReport | null>(null);
  const [error, setError] = useState('');
  const [active, setActive] = useState<(typeof MODELS)[number]['key']>('combined');
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    void fetchEllipticBenchmark().then(setReport).catch(() => setError('独立评测报告暂时不可用。请按项目说明运行评测命令。'));
    return () => { window.removeEventListener('keydown', onKey); previous?.focus(); };
  }, [onClose]);

  const selected = report?.models[active]?.test;
  const baseline = report?.models.combined?.test.prevalence ?? 0;
  return <div className="benchmark-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="benchmark-lab" role="dialog" aria-modal="true" aria-labelledby="benchmark-title">
      <header className="benchmark-lab__header">
        <div><p className="benchmark-lab__eyebrow"><Activity size={14} /> INDEPENDENT EVALUATION / RESEARCH</p>
          <h2 id="benchmark-title">让成绩接受真实数据检验。</h2>
          <p>独立链上交易测试集 · 按时间隔离 · 只展示可复现的聚合指标</p>
        </div>
        <button ref={closeRef} className="benchmark-lab__close" type="button" onClick={onClose} aria-label="关闭独立评测"><X size={19} /></button>
      </header>
      {!report ? <div className="benchmark-lab__loading">{error || '正在读取独立评测结果…'}</div> : <>
        <div className="benchmark-lab__ribbon"><span><Database size={15} /> REAL ON-CHAIN DATA</span><span>ELLIPTIC · BITCOIN</span><span>CC BY-NC-ND 4.0</span></div>
        <div className="benchmark-lab__hero">
          <div><p className="benchmark-lab__section-label">DATASET / 原始样本</p><strong>{count(report.dataset.transaction_count)}</strong><span>笔真实链上交易</span></div>
          <div><p className="benchmark-lab__section-label">GRAPH / 交易流向</p><strong>{count(report.dataset.edge_count)}</strong><span>条有向资金流边</span></div>
          <div><p className="benchmark-lab__section-label">HOLDOUT / 独立测试</p><strong>{count(report.splits.test.n)}</strong><span>笔已标注交易 · 仅用于最终测试</span></div>
        </div>
        <div className="benchmark-lab__content">
          <div className="benchmark-lab__main">
            <div className="benchmark-lab__title-row"><div><p className="benchmark-lab__section-label">MODEL COMPARISON</p><h3>三组基线，统一时间窗。</h3></div><span>训练 1–34 · 验证 35–41 · 测试 42–49</span></div>
            <div className="benchmark-lab__models" role="group" aria-label="评测模型">{MODELS.map((model) => {
              const metric = report.models[model.key]?.test;
              return <button key={model.key} type="button" className={active === model.key ? 'is-active' : ''} onClick={() => setActive(model.key)} aria-pressed={active === model.key}>
                <span>{model.title}</span><strong>{metric ? percent(metric.pr_auc) : '—'}</strong><small>PR-AUC</small>
              </button>;
            })}</div>
            {selected && <><div className="benchmark-lab__metric-grid">
              <div><span>PR-AUC</span><strong>{percent(selected.pr_auc)}</strong><small>测试集阳性率 {percent(baseline)}</small></div>
              <div><span>精确率</span><strong>{percent(selected.precision)}</strong><small>预测为非法中真实命中的比例</small></div>
              <div><span>召回率</span><strong>{percent(selected.recall)}</strong><small>已标注非法交易的检出比例</small></div>
              <div><span>误报数</span><strong>{count(selected.confusion.fp)}</strong><small>测试集中的合法交易</small></div>
            </div>
              <div className="benchmark-lab__compare"><div><span>所选模型的 PR-AUC</span><strong>{percent(selected.pr_auc)}</strong></div><div className="benchmark-lab__track"><i style={{ width: `${Math.min(100, selected.pr_auc * 100)}%` }} /></div><div><span>测试集随机基线 / 阳性率</span><strong>{percent(selected.prevalence)}</strong></div><div className="benchmark-lab__track is-baseline"><i style={{ width: `${Math.min(100, selected.prevalence * 100)}%` }} /></div></div>
              <p className="benchmark-lab__model-note">{MODELS.find((model) => model.key === active)?.note}。决策阈值只在验证集选择，测试集未参与调参。PR-AUC 越高越好；图中横条使用完整 0–100% 量程。</p>
            </>}
          </div>
          <aside className="benchmark-lab__aside">
            <div className="benchmark-lab__scope"><ShieldAlert size={18} /><h3>结论的边界</h3><p>这是比特币链上交易分类实验，不含企业账户、银行流水、授信审批或真实客户身份。评测的是单独的研究基线，不能当作当前 GraphRAG 信贷工作流的准确率。</p></div>
            <div className="benchmark-lab__provenance"><p className="benchmark-lab__section-label">PROVENANCE</p><a href={report.dataset.source_url} target="_blank" rel="noopener noreferrer">Elliptic 官方数据卡 <ExternalLink size={14} /></a><p><Fingerprint size={13} /> 数据包 SHA-256</p><code title={report.dataset.archive_sha256}>{report.dataset.archive_sha256}</code><p>未知类别 {count(report.dataset.label_counts.unknown)} 笔未纳入监督指标；原始数据仅保存在本机，不随产品分发。</p></div>
          </aside>
        </div>
        <footer className="benchmark-lab__footer"><span>独立测试样本 {count(report.splits.test.n)} 笔，其中已标注非法 {count(report.splits.test.positives)} 笔。</span><a href={report.dataset.source_url} target="_blank" rel="noopener noreferrer">查看数据许可与原始说明 <ArrowUpRight size={15} /></a></footer>
      </>}
    </section>
  </div>;
}
