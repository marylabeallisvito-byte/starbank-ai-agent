import { useEffect, useState } from 'react'
import { ArrowUpRight, Check, RefreshCw } from 'lucide-react'
import { request } from './api'

type Health = {
  checked_at: string; runtime: { status: string; source_sha256: string; loaded_at: string }
  graphs: { recipient_id: string; status: string }[]
  notices: { status: string }; fx: { status: string }; plaid: { status: string; configured: boolean }
  evaluation: { status: string }; benchmark: { status: string }; scope: string
}
const labels: Record<string, string> = { fresh: '缓存有效', cached: '使用缓存', stale: '缓存待更新', unavailable: '尚无可用缓存', invalid: '校验异常', available: '可读取', missing: '尚未生成', outdated: '源码已变化', disconnected: '尚未连接', connected: '已连接', active: '已连接', invalid_configuration: '配置需检查', current: '运行版本一致', restart_required: '需重启后端' }

export default function SourceHealth({ ready, onNavigate }: { ready: boolean; onNavigate: (section: string) => void }) {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (!ready) return; let active = true; request<Health>('/readiness').then(v => { if (active) setHealth(v) }).catch(e => { if (active) setError(e.message) }); return () => { active = false } }, [ready])
  async function reload() { setBusy(true); setError(''); try { setHealth(await request<Health>('/readiness')) } catch (e) { setError(e instanceof Error ? e.message : '状态读取失败') } finally { setBusy(false) } }
  const rows = health ? [
    { title: '公开风险通知', state: health.notices.status, note: '公告索引，不能替代账户风险核查', target: 'notices' },
    { title: '参考汇率', state: health.fx.status, note: '日频参考数据，进入后可获取更新', target: 'fx' },
    { title: 'Plaid Sandbox', state: health.plaid.configured ? health.plaid.status : 'unconfigured', note: health.plaid.configured ? '已配置凭据，连接状态单独检查' : '需在服务端配置 Sandbox 凭据', target: 'plaid' },
    { title: '算法评测档案', state: health.benchmark.status, note: '离线合成数据评测，不参与资金授权', target: 'benchmark' },
  ] : []
  return <section className="source-health" aria-label="数据来源状态"><div className="source-health-heading"><div><span className="atlas-overline">SOURCE STATUS</span><h2>先看清连接，再使用数据。</h2><p>未连接、待更新与校验失败，各自有明确的处理方式。</p></div><button className="atlas-icon-button" aria-label="重新检查数据状态" disabled={!ready || busy} onClick={reload}><RefreshCw size={17} className={busy ? 'spin' : ''} /></button></div>
    {error && <p className="atlas-error" role="alert">{error}</p>}
    {health ? <><div className="source-health-grid">{rows.map(row => <button key={row.target} onClick={() => onNavigate(row.target)}><div><strong>{row.title}</strong><ArrowUpRight size={16} /></div><span className={`source-health-state ${['fresh', 'available', 'connected', 'active'].includes(row.state) ? 'ok' : ''}`}>{labels[row.state] ?? (row.state === 'unconfigured' ? '尚未配置' : '需要检查')}</span><small>{row.note}</small></button>)}</div><details><summary><Check size={14} />运行版本与评测状态</summary><dl><div><dt>后端运行状态</dt><dd>{labels[health.runtime.status] ?? health.runtime.status}</dd></div><div><dt>业务回归报告</dt><dd>{labels[health.evaluation.status] ?? health.evaluation.status}{health.evaluation.status === 'outdated' && ' · 需重新评测'}</dd></div><div><dt>当前会话图谱</dt><dd>{health.graphs.filter(g => g.status === 'fresh').length} 份有效 · {health.graphs.filter(g => g.status === 'expired').length} 份过期 · {health.graphs.filter(g => g.status === 'missing').length} 份未加载 · {health.graphs.filter(g => g.status === 'invalid').length} 份异常</dd></div><div><dt>运行源码摘要</dt><dd><code>{health.runtime.source_sha256.slice(0, 20)}…</code></dd></div></dl><p>{health.scope}</p></details><small className="source-health-time">最近检查 {new Date(health.checked_at).toLocaleString('zh-CN', { hour12: false })} · 状态来自本机记录，进入对应来源可同步更新。</small></> : !error && <p className="data-muted">{ready ? '正在检查本机数据状态…' : '等待银行会话建立…'}</p>}
  </section>
}
