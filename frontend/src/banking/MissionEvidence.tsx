import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, FileSearch, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react'
import { request } from './api'
import { operatorRequest } from './trustApi'
import EvidenceNetwork from './EvidenceNetwork'
import type { GraphEvidence } from './EvidenceLab'
import './evidence.css'
import './mission-evidence.css'

type SourceRecord = { id: string; merchant?: string; amount?: string; occurred_at?: string; filename?: string; file_hash?: string; batch_id?: string; current_status: string; redacted?: boolean }
type Evidence = { status: string; message?: string; captured_at: string; snapshot_hash: string; record_count: number; source: string; period: string; finding_count: number; finding_index: number; finding: { merchant: string; explanation: string; transaction_ids: string[] } | null; records: SourceRecord[]; authorization_effect: string; network: GraphEvidence }
const stamp = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'

export default function MissionEvidence({ missionId, updatedAt, operator, onVerified }: { missionId: string; updatedAt: string; operator?: { reviewId: string; token: string }; onVerified?: (missionId: string, hash: string | null) => void }) {
  const [open, setOpen] = useState(false)
  const [index, setIndex] = useState(0)
  const [report, setReport] = useState<Evidence | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [retry, setRetry] = useState(0)
  const operatorToken = operator?.token, reviewId = operator?.reviewId
  useEffect(() => {
    if (!open) return
    let active = true
    const pending = operatorToken && reviewId ? operatorRequest<Evidence>(operatorToken, `/operator/reviews/${reviewId}/evidence?finding=${index}`) : request<Evidence>(`/missions/${missionId}/evidence?finding=${index}`)
    pending.then(value => { if (active) { setReport(value); setError(''); setSelected([]); onVerified?.(missionId, value.status === 'available' ? value.snapshot_hash : null) } }).catch(e => { if (active) { setError(e.message); setReport(null); onVerified?.(missionId, null) } })
    return () => { active = false }
  }, [missionId, updatedAt, index, open, retry, operatorToken, reviewId, onVerified])
  const navigate = (next: number) => { setIndex(next); setReport(null); setSelected([]) }
  return <section className="mission-proof"><button className="mission-proof-trigger" aria-expanded={open} aria-controls={`proof-${missionId}`} onClick={() => setOpen(v => !v)}><span className="mission-proof-icon"><FileSearch size={21} /></span><span><strong>查看这份任务的证据链</strong><small>来源记录 → 核查线索 → 授权影响</small></span><span className="mission-proof-open">{open ? '收起' : '展开'} <ArrowRight size={16} /></span></button>
    {open && <div id={`proof-${missionId}`} className="mission-proof-body"><header><div><span className="atlas-overline">FOLLOW THE SOURCE</span><h3>判断如何形成？</h3><p>以下来源属于当前任务。查阅不会触发资金操作或批准复核。</p></div><button className="atlas-icon-button" aria-label="重新读取任务证据" onClick={() => setRetry(v => v + 1)}><RefreshCw size={16} /></button></header>
      {error ? <div className="atlas-error" role="alert">{error}，可重新读取。</div> : !report ? <p className="mission-proof-loading"><LoaderCircle size={18} className="spin" />正在读取任务来源…</p> : ['pending', 'legacy_unavailable'].includes(report.status) ? <p className="mission-proof-empty">{report.message}</p> : <>
        <div className={`mission-proof-status ${report.status}`} role="status"><ShieldCheck size={18} /><div><strong>{report.status === 'changed' ? '来源发生变化，请先核对' : '来源快照校验通过'}</strong><p>{report.authorization_effect}</p></div></div>
        <div className="mission-proof-meta"><span>{report.source === 'imported' ? '已确认导入账单' : '仿真银行账本'} · {report.period}</span><span>检查 {report.record_count} 条支出 · {report.finding_count} 条核查线索</span></div>
        {report.finding ? <><div className="mission-proof-pagination"><strong>{report.finding.merchant} · 疑似重复</strong><div><button aria-label="上一条核查线索" disabled={index === 0} onClick={() => navigate(index - 1)}><ArrowLeft size={15} /></button><span>{index + 1} / {report.finding_count}</span><button aria-label="下一条核查线索" disabled={index + 1 >= report.finding_count} onClick={() => navigate(index + 1)}><ArrowRight size={15} /></button></div></div><EvidenceNetwork key={report.network.revision} report={report.network} selected={selected} onSelect={setSelected} /><p className="mission-proof-rule">{report.finding.explanation}</p><div className="mission-proof-records">{report.records.map((record, i) => <button key={record.id} className={selected.includes(record.id) ? 'active' : ''} aria-pressed={selected.includes(record.id)} onClick={() => setSelected([record.id])}><div><span>原始记录 {i + 1}</span><small>{({ matching: '与当前来源一致', missing: '原始来源已移除', changed: '原始来源已变化', cleared: '已清除来源内容' } as Record<string, string>)[record.current_status] ?? '待核对'}</small></div><strong>{record.redacted ? '来源内容已清除' : `${record.merchant} · ¥${record.amount}`}</strong><p>{stamp(record.occurred_at)}<br />{record.filename ?? (record.redacted ? '—' : '本月仿真账本')}</p><code>{record.id}</code>{record.file_hash && <small>文件摘要 {record.file_hash.slice(0, 20)}…</small>}</button>)}</div></> : <p className="mission-proof-empty">本次检查没有命中当前重复记录规则。不能据此推断不存在其他风险。</p>}
        <footer><span>采集于 {stamp(report.captured_at)}</span><code>快照 {report.snapshot_hash.slice(0, 24)}…</code></footer>
      </>}
    </div>}
  </section>
}
