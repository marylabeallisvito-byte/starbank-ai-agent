import { useState } from 'react'
import { ArrowRight, CheckCheck, GitCompareArrows, LoaderCircle } from 'lucide-react'
import { request } from './api'
import type { Mission } from './trustApi'

type Proposal = { proposal_digest: string; added: string[]; removed: string[]; modified: string[]; record_count: number; preserved_nodes: string[]; message: string }
export default function MissionRecovery({ mission, onUpdated }: { mission: Mission; onUpdated: (value: Mission) => void }) {
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function inspect() { setBusy(true); setError(''); try { setProposal(await request<Proposal>(`/missions/${mission.id}/changes`)) } catch (e) { setProposal(null); setError(e instanceof Error ? e.message : '来源差异读取失败') } finally { setBusy(false) } }
  async function accept() { if (!proposal) return; setBusy(true); setError(''); try { onUpdated(await request<Mission>(`/missions/${mission.id}/replan`, 'POST', { proposal_digest: proposal.proposal_digest })) } catch (e) { setError(e instanceof Error ? e.message : '任务调整失败，请重新核对差异') } finally { setBusy(false) } }
  return <section className="mission-recovery"><div><GitCompareArrows size={21} /><span><strong>需要核对来源是否变化？</strong><p>仅在账单新增、修改或移除时更新任务。等待人工复核不代表来源已经变化。</p></span></div><button className="atlas-button" disabled={busy} onClick={inspect}>{busy ? <LoaderCircle size={15} className="spin" /> : <ArrowRight size={15} />}核对来源变化</button>{error && <p className="atlas-error" role="alert">{error}</p>}{proposal && <><div className="mission-change-counts"><span>新增 <b>{proposal.added.length}</b></span><span>移除 <b>{proposal.removed.length}</b></span><span>修改 <b>{proposal.modified.length}</b></span></div><p>{proposal.message}</p><div className="mission-preserved"><CheckCheck size={16} />{proposal.preserved_nodes.length ? `保留：${proposal.preserved_nodes.map(id => id === 'reserve' ? '已预留预算' : '已取消订阅').join('、')}` : '尚无已完成的资金或订阅操作，本次不会动账。'}</div><details><summary>查看变化记录索引</summary>{[['新增', proposal.added], ['移除', proposal.removed], ['修改', proposal.modified]].map(([label, ids]) => <div key={label as string}><strong>{label}</strong>{(ids as string[]).slice(0, 10).map(id => <code key={id}>{id}</code>)}{(ids as string[]).length > 10 && <p>其余 {(ids as string[]).length - 10} 条保留在服务端差异核对中。</p>}</div>)}</details><button className="atlas-button dark" disabled={busy} onClick={accept}>确认新来源 · 暂停等待恢复 <ArrowRight size={16} /></button><small>旧复核决定会失效；必要时重新复核，执行仍由你手动恢复。</small></>}</section>
}
