import { useId, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { Focus, GitBranch, Minus, Plus, X } from 'lucide-react'
import type { GraphEvidence } from './EvidenceLab'

const subscribeViewport = (notify: () => void) => { const query = window.matchMedia('(max-width:700px)'); query.addEventListener('change', notify); return () => query.removeEventListener('change', notify) }
const isPortrait = () => window.matchMedia('(max-width:700px)').matches

const kinds: Record<string, string> = { Person: '用户', Account: '账户', Device: '设备', Task: '任务', Finding: '线索', Record: '记录', Document: '来源' }
const relations: Record<string, string> = { OWNS: '持有账户', LOGGED_IN: '登录设备', TRANSFER: '资金流转', SUPPORTS: '支持线索', INCLUDES: '原始记录', AFFECTS: '进入复核' }
const stamp = (v: string) => new Date(v).toLocaleString('zh-CN', { hour12: false })

/** Bounded SVG renderer: no remote graph, generated queries or inferred edges. */
export default function EvidenceNetwork({ report, selected, onSelect }: { report: GraphEvidence; selected: string[]; onSelect: (ids: string[]) => void }) {
  const portrait = useSyncExternalStore(subscribeViewport, isPortrait)
  const width = portrait ? 420 : 720, height = portrait ? 500 : 420
  const cx = width / 2, cy = height / 2
  const id = useId().replace(/:/g, '')
  const [zoom, setZoom] = useState(1)
  const [offset, setOffset] = useState({ x: 0, y: 0 })
  const [inspected, setInspected] = useState<{ kind: 'node' | 'edge'; id: string } | null>(null)
  const drag = useRef<{ x: number; y: number; ox: number; oy: number; scale: number; moved: boolean } | null>(null)
  const nodes = report.graph.nodes
  const edges = report.graph.edges
  const positions = useMemo(() => {
    const result: Record<string, [number, number]> = {}
    // Stable, data-driven radial layout: arbitrary node IDs do not crash rendering.
    if (report.coverage === 'mission_snapshot') {
      for (const label of ['Document', 'Record', 'Finding', 'Task']) {
        const group = nodes.filter(n => n.labels.includes(label))
        group.forEach((node, i) => {
          if (portrait) result[node.id] = label === 'Document' ? [210 + (i - (group.length - 1) / 2) * 150, 65] : label === 'Record' ? [110 + i * 200, 205] : label === 'Finding' ? [210, 325] : [210, 425]
          else result[node.id] = [label === 'Document' ? 90 : label === 'Record' ? 270 : label === 'Finding' ? 465 : 630, group.length === 1 ? 210 : 115 + i * 190]
        })
      }
      return result
    }
    const ordered = [...nodes].sort((a, b) => a.id === report.recipient_id ? -1 : b.id === report.recipient_id ? 1 : a.id.localeCompare(b.id))
    ordered.forEach((node, i) => {
      const angle = -Math.PI / 2 + i * Math.PI * 2 / Math.max(ordered.length, 1)
      result[node.id] = ordered.length === 1 ? [cx, cy] : [cx + Math.cos(angle) * (portrait ? 148 : 236), cy + Math.sin(angle) * (portrait ? 170 : 137)]
    })
    return result
  }, [nodes, report.recipient_id, report.coverage, portrait, cx, cy])
  const node = inspected?.kind === 'node' ? nodes.find(n => n.id === inspected.id) : undefined
  const edge = inspected?.kind === 'edge' ? edges.find(e => e.id === inspected.id) : undefined
  const name = (nodeId: string) => nodes.find(n => n.id === nodeId)?.properties.name ?? nodeId
  const chooseEdge = (edgeId: string) => { if (drag.current?.moved) return; setInspected({ kind: 'edge', id: edgeId }); onSelect([edgeId]) }
  const chooseNode = (nodeId: string) => { if (drag.current?.moved) return; setInspected({ kind: 'node', id: nodeId }); onSelect(edges.filter(e => e.source === nodeId || e.target === nodeId).map(e => e.id)) }
  const reset = () => { setZoom(1); setOffset({ x: 0, y: 0 }); setInspected(null); onSelect([]) }
  return <section className="evi-map" aria-label="交互证据网络">
    <div className="evi-map-top"><span><i />{report.coverage === 'mission_snapshot' ? '任务来源网络' : '关联证据网络'}</span><span>{nodes.length} 个节点 · {edges.length} 条关系{report.coverage === 'mission_snapshot' ? '' : ' · 合成'}</span></div>
    {nodes.length ? <>
      <div className="evi-network-toolbar"><span>{report.coverage === 'mission_snapshot' ? '任务采集快照 · 只读追溯' : report.source_status === 'expired' ? '历史快照 · 已过期' : '当前会话 · 可追溯来源'}</span><div><button aria-label="缩小网络图" disabled={zoom <= .7} onClick={() => setZoom(z => Math.max(.7, +(z - .2).toFixed(1)))}><Minus size={16} /></button><output aria-label="网络图缩放比例">{Math.round(zoom * 100)}%</output><button aria-label="放大网络图" disabled={zoom >= 2.5} onClick={() => setZoom(z => Math.min(2.5, +(z + .2).toFixed(1)))}><Plus size={16} /></button><button aria-label="重置网络视图" onClick={reset}><Focus size={16} /></button></div></div>
      <svg className="evi-network" viewBox={`0 0 ${width} ${height}`} aria-label="可选择节点和关系的来源网络。也可使用下方文字列表。"
        onPointerDown={event => { if (event.button !== 0 || (event.target as Element).closest('[role="button"]')) return; const rect = event.currentTarget.getBoundingClientRect(); drag.current = { x: event.clientX, y: event.clientY, ox: offset.x, oy: offset.y, scale: width / rect.width, moved: false }; event.currentTarget.setPointerCapture(event.pointerId) }}
        onPointerMove={event => { const start = drag.current; if (!start || !event.buttons) return; start.moved ||= Math.hypot(event.clientX - start.x, event.clientY - start.y) > 4; setOffset({ x: Math.max(-600, Math.min(600, start.ox + (event.clientX - start.x) * start.scale)), y: Math.max(-350, Math.min(350, start.oy + (event.clientY - start.y) * start.scale)) }) }}
        onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }}>
        <defs><pattern id={`${id}-grid`} width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r=".8" fill="#a3bdad" opacity=".23" /></pattern><marker id={`${id}-arrow`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#c8e7a4" /></marker></defs>
        <rect width={width} height={height} fill={`url(#${id}-grid)`} />
        <g transform={`translate(${cx + offset.x} ${cy + offset.y}) scale(${zoom}) translate(${-cx} ${-cy})`}>
          {edges.map(e => {
            if (!positions[e.source] || !positions[e.target]) return null
            const [x1, y1] = positions[e.source], [x2, y2] = positions[e.target]
            const dx = x2 - x1, dy = y2 - y1, len = Math.hypot(dx, dy) || 1
            let labelX = (x1 + x2) / 2 + dy / len * (portrait ? 24 : 18)
            let labelY = (y1 + y2) / 2 + dx / len * (!portrait && e.type === 'TRANSFER' && Math.abs(dx) > Math.abs(dy) ? 22 : portrait ? -24 : -18)
            if (portrait && report.coverage === 'mission_snapshot') {
              if (e.type === 'SUPPORTS') { labelX = x1; labelY = 280 }
              if (e.type === 'AFFECTS') { labelX = 310; labelY = 375 }
              if (e.type === 'INCLUDES') { labelX = x2; labelY = 150 }
            }
            const line = { x1: x1 + dx * 29 / len, y1: y1 + dy * 29 / len, x2: x2 - dx * 35 / len, y2: y2 - dy * 35 / len }
            const active = selected.includes(e.id)
            return <g key={e.id} role="button" tabIndex={0} aria-label={`查看来源 ${e.id}：${name(e.source)} ${relations[e.type] ?? e.type} ${name(e.target)}`} aria-pressed={active} className={`evi-network-edge ${active ? 'selected' : ''}`} onClick={() => chooseEdge(e.id)} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); chooseEdge(e.id) } }}>
              <line {...line} stroke="transparent" strokeWidth="20" /><line {...line} className="evi-edge-line" stroke={active ? '#dcf7a8' : '#8fac9b'} strokeWidth={active ? 3 : 1.5} strokeDasharray={e.type === 'LOGGED_IN' ? '5 5' : undefined} markerEnd={['TRANSFER', 'SUPPORTS', 'INCLUDES', 'AFFECTS'].includes(e.type) ? `url(#${id}-arrow)` : undefined} />
              <rect x={labelX - 45} y={labelY - 12} width="90" height="24" rx="12" fill="#244a3b" /><text x={labelX} y={labelY + 4} textAnchor="middle" fill={active ? '#e1f6b7' : '#d2dfd2'} fontSize={portrait ? 14 : 12}>{e.properties.amount !== undefined ? `¥${e.properties.amount.toLocaleString()}` : relations[e.type] ?? e.type}</text>
            </g>
          })}
          {nodes.map(n => { const [x, y] = positions[n.id]; const active = edges.some(e => selected.includes(e.id) && [e.source, e.target].includes(n.id)); return <g key={n.id} role="button" tabIndex={0} className="evi-network-node" aria-label={`查看节点 ${n.properties.name}，${kinds[n.labels[0]] ?? '实体'}`} onClick={() => chooseNode(n.id)} onKeyDown={event => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); chooseNode(n.id) } }}>
            <circle cx={x} cy={y} r={portrait ? 37 : 34} fill="none" stroke={active ? '#d4ed9f' : 'transparent'} strokeWidth="2" /><circle cx={x} cy={y} r={portrait ? 31 : 27} fill={(n.labels.includes('Person') || n.labels.includes('Task')) ? '#d6e9ab' : '#3a6250'} stroke="#9bb69d" /><text x={x} y={y + 5} textAnchor="middle" fill={(n.labels.includes('Person') || n.labels.includes('Task')) ? '#244a39' : '#edf4e7'} fontSize="15">{kinds[n.labels[0]] ?? '实体'}</text><text x={x} y={y + 48} textAnchor="middle" fill="#edf3e7" fontSize={portrait ? 16 : 13}>{n.properties.name.length > 12 ? `${n.properties.name.slice(0, 11)}…` : n.properties.name}</text><title>{n.properties.name} · {n.id}</title>
          </g> })}
        </g>
      </svg>
      {inspected && <div className="evi-network-inspector" aria-live="polite"><button aria-label="关闭网络详情" onClick={() => { setInspected(null); onSelect([]) }}><X size={16} /></button>{node ? <><small>{kinds[node.labels[0]] ?? '实体'} · {node.id}</small><strong>{node.properties.name}</strong><p>{edges.filter(e => [e.source, e.target].includes(node.id)).length} 条直接来源已高亮。关联本身不代表异常。</p></> : edge ? <><small>原始来源 · {edge.id}</small><strong>{name(edge.source)} → {name(edge.target)}</strong><p>{relations[edge.type] ?? edge.type}{edge.properties.amount !== undefined ? ` · ¥${edge.properties.amount.toLocaleString()}` : ''} · {stamp(edge.properties.date)}</p></> : null}</div>}
      <div className="evi-map-bottom"><span>点选节点或连线查看来源</span><span>拖动画布 · 缩放探索</span></div>
    </> : <div className="evi-map-empty"><GitBranch size={40} /><h3>从一份明确的来源开始</h3><p>选择上方模拟情境，加载当前会话的关系网络。<br />未加载来源时，不提供图谱风险结论。</p></div>}
  </section>
}
