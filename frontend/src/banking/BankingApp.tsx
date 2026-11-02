import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity, ArrowDownLeft, ArrowRight, ArrowUpRight, BarChart3, Bell, CalendarClock, Check,
  CreditCard, Fingerprint, LockKeyhole, MessageCircle, MoreHorizontal,
  Pause, Play, Repeat2, Send, ShieldCheck, Sparkles, TrendingUp, Wallet, X,
} from 'lucide-react'
import { api, startSession, type Bills, type Card, type CardAction, type Contact, type Intent, type Notification, type Overview, type Plan, type Subscription, type SubscriptionAction, type Transfer, type WealthHolding, type WealthOrder, type WealthProduct } from './api'
import './banking.css'
import { useDialog } from './useDialog'

type Message = { id: number; role: 'agent' | 'user'; text: string; time: string }
type Panel = 'home' | 'bills' | 'activity' | 'subscriptions' | 'plans' | 'wealth' | 'cards' | 'security'

const tips = ['给妈妈转 800 元', '我的余额还有多少？', '明天预留 500 元，提醒云上视频续费']
const time = () => new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date())
const greeting = () => { const hour = new Date().getHours(); return hour < 5 ? '夜深了' : hour < 11 ? '早上好' : hour < 14 ? '中午好' : hour < 19 ? '下午好' : '晚上好' }
const currency = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function Logo() {
  return <div className="banking-logo" aria-label="星河银行演示系统"><span className="banking-logo-mark"><span /></span><span>星河<span className="banking-logo-light">银行</span></span></div>
}

function SideBar({ panel, setPanel }: { panel: Panel; setPanel: (value: Panel) => void }) {
  return <aside className="banking-sidebar">
    <div className="banking-sidebar-top"><Logo /><div className="banking-sidebar-caption">BANKING, MADE HUMAN.</div></div>
    <nav aria-label="主导航">
      <button className={panel === 'home' ? 'active' : ''} onClick={() => setPanel('home')}><Wallet size={19} />总览</button>
      <button className={panel === 'bills' ? 'active' : ''} onClick={() => setPanel('bills')}><BarChart3 size={19} />账单分析</button>
      <button className={panel === 'activity' ? 'active' : ''} onClick={() => setPanel('activity')}><Activity size={19} />交易记录</button>
      <button className={panel === 'subscriptions' ? 'active' : ''} onClick={() => setPanel('subscriptions')}><Repeat2 size={19} />订阅管理</button>
      <button className={panel === 'plans' ? 'active' : ''} onClick={() => setPanel('plans')}><CalendarClock size={19} />跨场景计划</button>
      <button className={panel === 'wealth' ? 'active' : ''} onClick={() => setPanel('wealth')}><TrendingUp size={19} />理财实验室</button>
      <button className={panel === 'cards' ? 'active' : ''} onClick={() => setPanel('cards')}><CreditCard size={19} />卡片管理</button>
      <button className={panel === 'security' ? 'active' : ''} onClick={() => setPanel('security')}><ShieldCheck size={19} />安全中心</button>
    </nav>
    <div className="banking-sidebar-bottom"><div className="banking-sandbox-dot" /><div><strong>演示沙箱</strong><span>所有资金与账户均为虚构</span></div></div>
  </aside>
}

function CardDialog({ action, busy, error, returnFocus, onClose, onConfirm, onVerify, onExecute }: {
  action: CardAction; busy: boolean; error: string | null; returnFocus: HTMLElement | null; onClose: () => void;
  onConfirm: () => void; onVerify: (code: string) => void; onExecute: () => void;
}) {
  const dialogRef = useDialog(onClose, returnFocus)
  const [code, setCode] = useState('')
  const label = { freeze: '冻结卡片', unfreeze: '解除冻结', report_lost: '卡片挂失' }[action.action]
  return <div className="banking-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section ref={dialogRef} className="banking-modal" role="dialog" aria-modal="true" aria-labelledby="card-action-title">
    <div className="banking-modal-head"><span className="banking-modal-icon"><CreditCard size={21} /></span><button className="banking-icon-button" onClick={onClose} aria-label="关闭"><X size={20} /></button></div>
    <div className={`banking-level ${action.level === 'red' ? 'red' : 'yellow'}`}><span />{action.level === 'red' ? '红色 · 演示强验证' : '黄色 · 需明确确认'}</div>
    <h2 id="card-action-title">{action.status === 'EXECUTED' ? `${label}已完成` : `确认${label}`}</h2><p className="banking-modal-description">演示卡片 · 尾号 2388</p>
    <div className="banking-security-note"><ShieldCheck size={17} /><span>{action.action === 'report_lost' ? '挂失后不能直接解除，请确认卡片确已遗失。' : '操作会改变演示卡片的可用状态。'}</span></div>
    {action.status === 'AWAITING_VERIFICATION' && <div className="banking-verify-box"><label htmlFor="card-otp">演示验证码</label><input id="card-otp" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} placeholder="6 位验证码" inputMode="numeric" /><small>沙箱验证码：<strong>{action.demo_code}</strong> · 非真实多因子认证</small></div>}
    {error && <div className="banking-error" role="alert">{error}</div>}
    {action.status === 'PREPARED' && <button className="banking-primary full" disabled={busy} onClick={onConfirm}>确认操作 <ArrowRight size={17} /></button>}
    {action.status === 'AWAITING_VERIFICATION' && <button className="banking-primary full" disabled={busy || code.length !== 6} onClick={() => onVerify(code)}>完成演示验证 <ArrowRight size={17} /></button>}
    {action.status === 'READY' && <button className="banking-primary full" disabled={busy} onClick={onExecute}>执行{label} <ArrowRight size={17} /></button>}
    {action.status === 'EXECUTED' && <button className="banking-primary full" onClick={onClose}>完成 <Check size={17} /></button>}
    <div className="banking-modal-foot"><ShieldCheck size={14} /> 服务端权限校验 · 状态变更留痕</div>
  </section></div>
}

function TransferDialog({ transfer, busy, error, returnFocus, onClose, onConfirm, onVerify, onExecute }: {
  transfer: Transfer; busy: boolean; error: string | null; returnFocus: HTMLElement | null; onClose: () => void;
  onConfirm: () => void; onVerify: (code: string) => void; onExecute: () => void;
}) {
  const dialogRef = useDialog(onClose, returnFocus)
  const [code, setCode] = useState('')
  const [demoCode, setDemoCode] = useState<string | null>(null)
  const red = transfer.level === 'red'
  useEffect(() => {
    if (transfer.status === 'AWAITING_VERIFICATION') {
      api.demoCode(transfer.id).then((value) => setDemoCode(value.code)).catch(() => setDemoCode(null))
    }
  }, [transfer.id, transfer.status])
  return <div className="banking-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialogRef} className="banking-modal" role="dialog" aria-modal="true" aria-labelledby="transfer-title">
      <div className="banking-modal-head"><span className="banking-modal-icon"><ArrowUpRight size={21} /></span><button className="banking-icon-button" onClick={onClose} aria-label="关闭"><X size={20} /></button></div>
      <div className={`banking-level ${red ? 'red' : 'yellow'}`}><span />{red ? '高级验证' : '需你确认'}</div>
      <h2 id="transfer-title">{transfer.status === 'COMMITTED' ? '转账已完成' : '核对转账信息'}</h2>
      <p className="banking-modal-description">{transfer.status === 'COMMITTED' ? '以下为仿真账本生成的交易回执。' : '请确认每一项细节，提交前不会扣款。'}</p>
      <div className="banking-modal-amount">{currency(transfer.amount)}</div>
      <div className="banking-transfer-lines">
        <div><span>收款人</span><strong>{transfer.recipient.name}</strong></div>
        <div><span>收款账户</span><strong>{transfer.recipient.bank} ···· {transfer.recipient.account_tail}</strong></div>
        <div><span>备注</span><strong>{transfer.note || '无'}</strong></div>
        <div><span>安全级别</span><strong>{red ? '红色 · 演示强验证' : '黄色 · 明确确认'}</strong></div>
        <div><span>交易前检查</span><strong>{transfer.risk.tier === 'review' ? '需加强核验' : '常规检查'}</strong></div>
        {transfer.receipt && <div><span>沙箱回执</span><strong>{transfer.receipt}</strong></div>}
      </div>
      {transfer.risk.factors.length > 0 && transfer.status !== 'COMMITTED' && <div className="banking-security-note"><LockKeyhole size={17} /><span>{transfer.risk.factors.map((factor) => factor.description).join('；')}。{red ? '因此本笔交易或日累计金额需演示强验证。' : '请核对收款信息。'}</span></div>}
      {transfer.status === 'AWAITING_VERIFICATION' && <div className="banking-verify-box"><label htmlFor="banking-otp">输入演示验证码</label><input id="banking-otp" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" placeholder="6 位验证码" /><small>沙箱验证码：<strong>{demoCode ?? '获取中…'}</strong> · 仅用于演示，非真实多因子认证</small></div>}
      {error && <div className="banking-error" role="alert">{error}</div>}
      {transfer.status === 'PREPARED' && <button className="banking-primary full" disabled={busy} onClick={onConfirm}>确认转账信息 <ArrowRight size={17} /></button>}
      {transfer.status === 'AWAITING_VERIFICATION' && <button className="banking-primary full" disabled={busy || code.length !== 6} onClick={() => onVerify(code)}>完成演示验证 <ArrowRight size={17} /></button>}
      {transfer.status === 'READY' && <button className="banking-primary full" disabled={busy} onClick={onExecute}>执行沙箱转账 <ArrowRight size={17} /></button>}
      {transfer.status === 'COMMITTED' && <button className="banking-primary full" onClick={onClose}>完成 <Check size={17} /></button>}
      <div className="banking-modal-foot"><ShieldCheck size={14} /> 服务端权限校验 · 幂等执行 · 全程留痕</div>
    </section>
  </div>
}

export function BillsPage({ data }: { data: Bills | null }) {
  const max = Math.max(1, ...(data?.categories.map((item) => Number(item.amount)) ?? []))
  return <div className="banking-content banking-subpage"><div className="banking-eyebrow"><BarChart3 size={14} /> SPENDING INSIGHTS</div><h1>看清每一笔<span className="banking-heading-dot">.</span></h1><p>分类由真实运行的合成交易记录计算，不由模型编造金额。</p>
    <div className="banking-bills-grid"><div className="banking-bill-total"><span>{data?.month ?? '本月'} · 总支出</span><strong>{data ? currency(data.total) : '—'}</strong><small>{data?.transactions.length ?? 0} 笔演示消费 · 点击下方记录核对来源</small><div className="banking-bill-decoration"><span /></div></div><div className="banking-bill-anomaly"><div><ShieldCheck size={20} /><span>异常提醒</span></div><strong>{data?.anomalies.length ?? 0} 条待核对</strong><p>{data?.anomalies[0]?.explanation ?? '当前没有发现疑似重复扣费。'}</p></div></div>
    <div className="banking-bills-columns"><section className="banking-section banking-bill-section"><div className="banking-section-kicker">BY CATEGORY</div><h2>消费去向</h2>{data?.categories.map((item) => <div className="banking-category" key={item.name}><div><strong>{item.name}</strong><span>{currency(item.amount)}</span></div><div className="banking-category-track"><span style={{ width: `${Math.max(6, Number(item.amount) / max * 100)}%` }} /></div><small>{item.transaction_ids.length} 笔交易可追溯</small></div>)}</section><section className="banking-section banking-bill-section"><div className="banking-section-kicker">TRANSACTION SOURCE</div><h2>原始记录</h2><div className="banking-bill-list">{data?.transactions.map((item) => <div className="banking-transaction" key={item.id}><span className="banking-transaction-icon"><ArrowUpRight size={17} /></span><div><strong>{item.merchant}</strong><small>{item.category} · {new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(item.occurred_at))} · {item.id.slice(0, 8)}</small></div><b>−{currency(item.amount)}</b></div>)}</div></section></div>
  </div>
}

export function CardsPage({ card, busy, onAction }: { card: Card | null; busy: boolean; onAction: (action: CardAction['action']) => void }) {
  const status = { ACTIVE: '正常使用', FROZEN: '已冻结', LOST: '已挂失' }[card?.status ?? 'ACTIVE']
  return <div className="banking-content banking-subpage"><div className="banking-eyebrow"><CreditCard size={14} /> YOUR CARD</div><h1>你的卡，由你掌控<span className="banking-heading-dot">.</span></h1><p>卡片操作在服务端核验状态，执行后实时更新。</p>
    <div className="banking-card-grid"><div className="banking-physical-card"><div className="banking-card-top"><Logo /><span>DEBIT</span></div><div className="banking-card-wave" /><div className="banking-card-number">•••• &nbsp; •••• &nbsp; •••• &nbsp; {card?.tail ?? '2388'}</div><div className="banking-card-bottom"><span>LIN · DEMO</span><span>✦ ✦</span></div></div><div className="banking-card-controls"><div className="banking-section-kicker">CARD CONTROLS</div><h2>星河借记卡</h2><div className={`banking-card-status ${card?.status === 'ACTIVE' ? 'active' : 'inactive'}`}><span />{status}</div><p>以下操作仅改变演示卡片状态，不影响真实银行卡。</p><div className="banking-card-buttons">{card?.status === 'ACTIVE' && <button onClick={() => onAction('freeze')} disabled={busy}>暂时冻结 <ArrowRight size={16} /></button>}{card?.status === 'FROZEN' && <button onClick={() => onAction('unfreeze')} disabled={busy}>解除冻结 <ArrowRight size={16} /></button>}{card?.status !== 'LOST' && <button onClick={() => onAction('report_lost')} disabled={busy} className="danger">卡片挂失 <ArrowRight size={16} /></button>}{card?.status === 'LOST' && <div className="banking-card-complete"><ShieldCheck size={17} /> 此卡已挂失，无法在沙箱内恢复。</div>}</div></div></div>
    <div className="banking-trust wide"><div><Fingerprint size={20} /><strong>按风险分级处理</strong></div><p>冻结需要明确确认；解除冻结和挂失属于红色操作，必须完成设备验证。每次状态变化记录在服务端审计中。</p></div>
  </div>
}

export function SubscriptionsPage({ items, busy, onCancel }: { items: Subscription[]; busy: boolean; onCancel: (id: string) => void }) {
  const active = items.filter((item) => item.status === 'ACTIVE')
  const monthly = active.reduce((sum, item) => sum + Number(item.amount), 0)
  return <div className="banking-content banking-subpage"><div className="banking-eyebrow"><Repeat2 size={14} /> RECURRING PAYMENTS</div><h1>订阅，一目了然<span className="banking-heading-dot">.</span></h1><p>模拟商户的续费时间与金额可查；取消前必须核对并确认。</p>
    <div className="banking-subscription-summary"><div><span>每月预计续费</span><strong>{currency(monthly.toFixed(2))}</strong></div><div><span>生效中的订阅</span><strong>{active.length} 项</strong></div><Repeat2 size={100} /></div>
    <section className="banking-section banking-subscription-list"><div className="banking-section-kicker">YOUR SUBSCRIPTIONS</div><h2>管理自动续费</h2>{items.map((item) => <div className="banking-subscription-row" key={item.id}><span className="banking-subscription-icon"><Repeat2 size={19} /></span><div className="banking-subscription-name"><strong>{item.merchant}</strong><small>{item.status === 'ACTIVE' ? `下次续费 ${new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric' }).format(new Date(item.next_charge_at))}` : '已取消 · 不再续费'}</small></div><b>{currency(item.amount)} / 月</b>{item.status === 'ACTIVE' ? <button onClick={() => onCancel(item.id)} disabled={busy}>取消订阅</button> : <span className="banking-subscription-ended">已取消</span>}</div>)}</section>
    <div className="banking-trust wide"><div><ShieldCheck size={19} /><strong>可控范围</strong></div><p>仅演示沙箱内的模拟商户可直接取消。外部平台订阅需要前往对应商户办理，智能助理不会谎称已替你取消。</p></div>
  </div>
}

function SubscriptionDialog({ action, subscription, busy, error, returnFocus, onClose, onConfirm, onExecute }: {
  action: SubscriptionAction; subscription: Subscription; busy: boolean; error: string | null; returnFocus: HTMLElement | null;
  onClose: () => void; onConfirm: () => void; onExecute: () => void;
}) {
  const dialogRef = useDialog(onClose, returnFocus)
  return <div className="banking-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section ref={dialogRef} className="banking-modal" role="dialog" aria-modal="true" aria-labelledby="subscription-title">
    <div className="banking-modal-head"><span className="banking-modal-icon"><Repeat2 size={21} /></span><button className="banking-icon-button" onClick={onClose} aria-label="关闭"><X size={20} /></button></div><div className="banking-level yellow"><span />黄色 · 需你确认</div>
    <h2 id="subscription-title">{action.status === 'EXECUTED' ? '模拟订阅已取消' : '确认取消订阅'}</h2><p className="banking-modal-description">操作只影响以下沙箱商户。</p>
    <div className="banking-transfer-lines"><div><span>商户</span><strong>{subscription.merchant}</strong></div><div><span>续费金额</span><strong>{currency(subscription.amount)} / 月</strong></div><div><span>原定续费</span><strong>{new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' }).format(new Date(subscription.next_charge_at))}</strong></div></div>
    {error && <div className="banking-error" role="alert">{error}</div>}
    {action.status === 'PREPARED' && <button className="banking-primary full" onClick={onConfirm} disabled={busy}>确认取消详情 <ArrowRight size={17} /></button>}
    {action.status === 'READY' && <button className="banking-primary full" onClick={onExecute} disabled={busy}>执行模拟取消 <ArrowRight size={17} /></button>}
    {action.status === 'EXECUTED' && <button className="banking-primary full" onClick={onClose}>完成 <Check size={17} /></button>}
    <div className="banking-modal-foot"><ShieldCheck size={14} /> 明确确认 · 幂等执行 · 审计留痕</div>
  </section></div>
}

export function WealthPage({ products, holdings, riskLevel, busy, onAssess, onPrepare, onRedeem }: {
  products: WealthProduct[]; holdings: WealthHolding[]; riskLevel: number | null; busy: boolean;
  onAssess: (answers: { experience: number; loss_tolerance: number; horizon: number }) => void;
  onPrepare: (productId: string, amount: string) => void;
  onRedeem: (productId: string, amount: string) => void;
}) {
  const [answers, setAnswers] = useState({ experience: 0, loss_tolerance: 0, horizon: 0 })
  const [productId, setProductId] = useState('cash')
  const [amount, setAmount] = useState('100.00')
  const questions: { key: keyof typeof answers; label: string; choices: string[] }[] = [
    { key: 'experience', label: '你对投资产品的经验', choices: ['刚开始了解', '有一些经验', '经验较丰富'] },
    { key: 'loss_tolerance', label: '短期本金出现亏损时', choices: ['不能接受', '可以接受少量', '能承受较大波动'] },
    { key: 'horizon', label: '计划持有的时间', choices: ['少于一年', '一到三年', '三年以上'] },
  ]
  return <div className="banking-content banking-subpage"><div className="banking-eyebrow"><TrendingUp size={14} /> WEALTH LAB</div><h1>每一步，都看得清楚<span className="banking-heading-dot">.</span></h1><p>比较虚构产品、完成风险测评，再核对申购。所有金额和持仓均为沙箱数据。</p>
    <div className="banking-wealth-grid"><section className="banking-section banking-wealth-assess"><div className="banking-section-kicker">SUITABILITY FIRST</div><h2>风险承受能力</h2><p>系统取三项答案中最谨慎的一项确定演示等级。结果只用于本沙箱的准入判断。</p>{questions.map((question) => <label key={question.key}>{question.label}<select value={answers[question.key]} onChange={(event) => setAnswers((current) => ({ ...current, [question.key]: Number(event.target.value) }))}>{question.choices.map((choice, index) => <option key={choice} value={index}>{choice}</option>)}</select></label>)}<button className="banking-primary" onClick={() => onAssess(answers)} disabled={busy}>完成测评 <ArrowRight size={17} /></button><div className="banking-wealth-level">当前等级：<strong>{riskLevel ? { 1: '谨慎型', 2: '稳健型', 3: '进取型' }[riskLevel] : '尚未测评'}</strong></div></section>
      <section className="banking-section banking-wealth-order"><div className="banking-section-kicker">REVIEW BEFORE BUY</div><h2>模拟申购</h2><p>申购属于红色操作，必须核对详情并完成通行密钥验证。此处没有收益承诺。</p><label>选择产品<select value={productId} onChange={(event) => setProductId(event.target.value)}>{products.map((product) => <option key={product.id} value={product.id}>{product.name} · R{product.risk_level}</option>)}</select></label><label>申购金额（元）<input inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} /></label><button className="banking-primary" onClick={() => onPrepare(productId, amount)} disabled={busy || !riskLevel || !amount || !products.find((product) => product.id === productId)?.suitable}>预览申购 <ArrowRight size={17} /></button><div className="banking-wealth-level">不符合当前风险等级的产品无法提交预览。</div></section></div>
    <div className="banking-plan-list-head"><div><div className="banking-section-kicker">COMPARE</div><h2>产品对比</h2></div></div><div className="banking-wealth-products">{products.map((product) => <article className="banking-section banking-wealth-product" key={product.id}><span className="banking-wealth-risk">R{product.risk_level} · {product.type}</span><h3>{product.name}</h3><p>{product.description}</p><strong>{product.suitable ? '符合当前模拟测评' : '需完成测评或等级不匹配'}</strong></article>)}</div>
    <section className="banking-section banking-wealth-holdings"><div className="banking-section-kicker">HOLDINGS</div><h2>模拟持仓</h2>{holdings.length ? holdings.map((holding) => <div key={holding.product.id}><span>{holding.product.name}</span><strong>{currency(holding.principal)}</strong><button disabled={busy || Number(holding.principal) <= 0} onClick={() => onRedeem(holding.product.id, holding.principal)}>赎回全部 <ArrowRight size={13} /></button></div>) : <p>暂无模拟持仓。申购完成后，服务端账本会在这里显示本金。</p>}</section>
  </div>
}

function WealthDialog({ order, busy, error, returnFocus, onClose, onConfirm, onVerify, onExecute }: {
  order: WealthOrder; busy: boolean; error: string | null; returnFocus: HTMLElement | null; onClose: () => void;
  onConfirm: () => void; onVerify: (code: string) => void; onExecute: () => void;
}) {
  const dialogRef = useDialog(onClose, returnFocus)
  const [code, setCode] = useState('')
  const action = order.action === 'BUY' ? '申购' : '赎回'
  return <div className="banking-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><section ref={dialogRef} className="banking-modal" role="dialog" aria-modal="true" aria-labelledby="wealth-order-title"><div className="banking-modal-head"><span className="banking-modal-icon"><TrendingUp size={21} /></span><button className="banking-icon-button" onClick={onClose} aria-label="关闭"><X size={20} /></button></div><div className="banking-level red"><span />红色 · 演示强验证</div><h2 id="wealth-order-title">{order.status === 'COMMITTED' ? `模拟${action}完成` : `核对模拟${action}`}</h2><p className="banking-modal-description">虚构产品，没有真实收益或资金流转。</p><div className="banking-modal-amount">{currency(order.amount)}</div><div className="banking-transfer-lines"><div><span>产品</span><strong>{order.product.name}</strong></div><div><span>操作</span><strong>{action}</strong></div><div><span>风险等级</span><strong>R{order.product.risk_level}</strong></div><div><span>验证要求</span><strong>红色 · 明确确认 + 演示验证码</strong></div>{order.receipt && <div><span>沙箱回执</span><strong>{order.receipt}</strong></div>}</div>{order.status === 'AWAITING_VERIFICATION' && <div className="banking-verify-box"><label htmlFor="wealth-otp">演示验证码</label><input id="wealth-otp" value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} inputMode="numeric" placeholder="6 位验证码" /><small>沙箱验证码：<strong>{order.demo_code}</strong> · 非真实多因子认证</small></div>}{error && <div className="banking-error" role="alert">{error}</div>}{order.status === 'PREPARED' && <button className="banking-primary full" disabled={busy} onClick={onConfirm}>确认{action}详情 <ArrowRight size={17} /></button>}{order.status === 'AWAITING_VERIFICATION' && <button className="banking-primary full" disabled={busy || code.length !== 6} onClick={() => onVerify(code)}>完成演示验证 <ArrowRight size={17} /></button>}{order.status === 'READY' && <button className="banking-primary full" disabled={busy} onClick={onExecute}>执行模拟{action} <ArrowRight size={17} /></button>}{order.status === 'COMMITTED' && <button className="banking-primary full" onClick={onClose}>完成 <Check size={17} /></button>}<div className="banking-modal-foot"><ShieldCheck size={14} /> 服务端复核 · 幂等执行 · 审计留痕</div></section></div>
}

export function PlansPage({ plans, subscriptions, notifications, draft, busy, onPrepare, onAction, onRunDue }: {
  plans: Plan[]; subscriptions: Subscription[]; notifications: Notification[]; draft: { amount: string; subscription_id: string; run_at: string } | null; busy: boolean;
  onPrepare: (amount: string, subscriptionId: string, runAt: string) => void;
  onAction: (plan: Plan, action: 'confirm' | 'pause' | 'resume' | 'cancel' | 'release') => void;
  onRunDue: () => void;
}) {
  const [amount, setAmount] = useState(draft?.amount ?? '500.00')
  const [subscriptionId, setSubscriptionId] = useState(draft?.subscription_id ?? '')
  const [runAt, setRunAt] = useState(() => draft ? new Date(new Date(draft.run_at).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : new Date(Date.now() - new Date().getTimezoneOffset() * 60000 + 120000).toISOString().slice(0, 16))
  const active = subscriptions.filter((item) => item.status === 'ACTIVE')
  const selectedSubscription = subscriptionId || active[0]?.id || ''
  const statusText: Record<Plan['status'], string> = { DRAFT: '等待确认', SCHEDULED: '等待执行', PAUSED: '已暂停', NEEDS_ATTENTION: '需要处理', CANCELLED: '已取消', COMPLETED: '已完成', RELEASED: '预算已释放' }
  const nodeText: Record<string, string> = { check_balance: '检查可用余额', reserve_budget: '预留旅行预算', remind_subscription: '提醒订阅续费' }
  return <div className="banking-content banking-subpage"><div className="banking-eyebrow"><CalendarClock size={14} /> CROSS-SCENE PLANS</div><h1>把未来安排妥当<span className="banking-heading-dot">.</span></h1><p>确认后计划持久保存。到期时依次检查余额、预留预算并生成续费提醒。</p>
    <div className="banking-plan-grid"><section className="banking-section banking-plan-create"><div className="banking-section-kicker">NEW PLAN</div><h2>创建跨场景计划</h2><p>计划中的动作按依赖顺序执行。预算只在执行时预留，不会转给第三方。</p><label>预留金额（元）<input value={amount} onChange={(event) => setAmount(event.target.value)} inputMode="decimal" /></label><label>关联订阅<select value={selectedSubscription} onChange={(event) => setSubscriptionId(event.target.value)}>{active.map((item) => <option value={item.id} key={item.id}>{item.merchant} · {currency(item.amount)} / 月</option>)}</select></label><label>执行时间<input type="datetime-local" value={runAt} onInput={(event) => setRunAt(event.currentTarget.value)} onChange={(event) => setRunAt(event.target.value)} /></label><button className="banking-primary" disabled={busy || !amount || !selectedSubscription || !runAt} onClick={() => onPrepare(amount, selectedSubscription, new Date(runAt).toISOString())}>预览计划 <ArrowRight size={17} /></button></section><section className="banking-section banking-plan-diagram"><div className="banking-section-kicker">EXECUTION DAG</div><h2>有序执行，可暂停</h2><div className="banking-plan-steps"><div><span>01</span><strong>检查余额</strong><small>确认当前可用资金</small></div><div><span>02</span><strong>预留预算</strong><small>从可用余额中单独留存</small></div><div><span>03</span><strong>续费提醒</strong><small>生成可追溯通知</small></div></div><p>任何前置步骤未通过，后续步骤都不会执行。已预留预算可单独释放。</p></section></div>
    <div className="banking-plan-list-head"><div><div className="banking-section-kicker">SAVED PLANS</div><h2>我的计划</h2></div><button onClick={onRunDue} disabled={busy}>检查到期任务 <Play size={15} /></button></div>
    {plans.length ? <div className="banking-plan-list">{plans.map((plan) => <article className="banking-plan-card" key={plan.id}><div className="banking-plan-card-top"><span className="banking-plan-card-icon"><CalendarClock size={19} /></span><span className={`banking-plan-state ${plan.status.toLowerCase()}`}>{statusText[plan.status]}</span></div><h3>{plan.title}</h3><p>执行时间：{new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(plan.run_at))}</p><div className="banking-plan-node-list">{plan.nodes.map((node) => <div key={node.id}><span className={node.status === 'COMPLETED' ? 'done' : ''}>{node.status === 'COMPLETED' ? <Check size={12} /> : <span />}</span>{nodeText[node.id] ?? node.id}</div>)}</div>{plan.failure_reason && <div className="banking-error">{plan.failure_reason}</div>}<div className="banking-plan-actions">{plan.status === 'DRAFT' && <button onClick={() => onAction(plan, 'confirm')} disabled={busy}>确认并启用 <Check size={14} /></button>}{plan.status === 'SCHEDULED' && <button onClick={() => onAction(plan, 'pause')} disabled={busy}>暂停 <Pause size={14} /></button>}{(plan.status === 'PAUSED' || plan.status === 'NEEDS_ATTENTION') && <button onClick={() => onAction(plan, 'resume')} disabled={busy}>恢复 <Play size={14} /></button>}{['DRAFT', 'SCHEDULED', 'PAUSED', 'NEEDS_ATTENTION'].includes(plan.status) && <button onClick={() => onAction(plan, 'cancel')} disabled={busy} className="subtle">取消计划</button>}{plan.status === 'COMPLETED' && <button onClick={() => onAction(plan, 'release')} disabled={busy}>释放预算</button>}</div></article>)}</div> : <div className="banking-plan-empty">还没有计划。创建后可在这里查看每个节点的执行状态。</div>}
    {notifications.length > 0 && <section className="banking-section banking-plan-notifications"><div className="banking-section-kicker">NOTIFICATIONS</div><h2>计划提醒</h2>{notifications.map((item) => <div key={item.id}><Bell size={17} /><span>{item.message}</span></div>)}</section>}
  </div>
}

export default function BankingApp() {
  const [panel, setPanel] = useState<Panel>('home')
  const [overview, setOverview] = useState<Overview | null>(null)
  const [billData, setBillData] = useState<Bills | null>(null)
  const [cardData, setCardData] = useState<Card | null>(null)
  const [cardAction, setCardAction] = useState<CardAction | null>(null)
  const [subscriptionItems, setSubscriptionItems] = useState<Subscription[]>([])
  const [subscriptionAction, setSubscriptionAction] = useState<{ action: SubscriptionAction; subscription: Subscription } | null>(null)
  const [plans, setPlans] = useState<Plan[]>([])
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [wealthProducts, setWealthProducts] = useState<WealthProduct[]>([])
  const [wealthHoldings, setWealthHoldings] = useState<WealthHolding[]>([])
  const [wealthRiskLevel, setWealthRiskLevel] = useState<number | null>(null)
  const [wealthOrder, setWealthOrder] = useState<WealthOrder | null>(null)
  const [auditIntegrity, setAuditIntegrity] = useState<boolean | null>(null)
  const [messages, setMessages] = useState<Message[]>([{ id: 1, role: 'agent', text: '你好，我是星河银行的智能助理。你可以查账单和订阅、准备转账，或安排一项跨场景计划。', time: time() }])
  const [input, setInput] = useState('')
  const [draft, setDraft] = useState<{ recipient_id: string; amount: string } | null>(null)
  const [planDraft, setPlanDraft] = useState<{ amount: string; subscription_id: string; run_at: string } | null>(null)
  const [contact, setContact] = useState('mom')
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [transfer, setTransfer] = useState<Transfer | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const transferKey = useRef<string>('')
  const cardKey = useRef<string>('')
  const subscriptionKey = useRef<string>('')
  const wealthKey = useRef<string>('')
  const [dialogOrigin, setDialogOrigin] = useState<HTMLElement | null>(null)
  const rememberDialogOrigin = () => { setDialogOrigin(document.activeElement instanceof HTMLElement ? document.activeElement : null) }

  const refresh = useCallback(async () => setOverview(await api.overview()), [])
  const refreshAuxiliary = useCallback(async () => {
    const [nextBills, nextCard, nextSubscriptions, nextPlans, nextNotifications, nextProducts, nextHoldings] = await Promise.all([api.bills(), api.card(), window.location.pathname === '/banking/classic' ? Promise.resolve({items: [] as Subscription[]}) : api.subscriptions(), api.plans(), api.notifications(), api.wealthProducts(), api.wealthHoldings()])
    setBillData(nextBills); setCardData(nextCard); setSubscriptionItems(nextSubscriptions.items); setPlans(nextPlans.items); setNotifications(nextNotifications.items)
    setWealthProducts(nextProducts.items); setWealthRiskLevel(nextProducts.risk_level); setWealthHoldings(nextHoldings.items)
  }, [])
  useEffect(() => {
    document.title = '星河银行 · 智能助理（竞赛沙箱）'
    startSession().then(async () => { await Promise.all([refresh(), refreshAuxiliary()]); setReady(true) }).catch((reason) => setError(reason instanceof Error ? reason.message : '服务暂不可用'))
  }, [refresh, refreshAuxiliary])
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }) }, [messages])
  useEffect(() => { window.scrollTo({ top: 0, behavior: 'instant' }) }, [panel])
  useEffect(() => { if (ready && panel === 'security') api.audit().then((value) => setAuditIntegrity(value.integrity)).catch(() => setAuditIntegrity(null)) }, [panel, ready])
  const addMessage = (role: Message['role'], text: string) => setMessages((items) => [...items, { id: Date.now() + Math.random(), role, text, time: time() }])

  async function send(text = input) {
    const value = text.trim()
    if (!value || busy || !ready) return
    setInput(''); setError(null); addMessage('user', value); setBusy(true)
    try {
      const result: Intent = await api.intent(value)
      addMessage('agent', result.reply)
      if (result.kind === 'transfer_draft' && result.recipient_id && result.amount) {
        setDraft({ recipient_id: result.recipient_id, amount: result.amount }); setContact(result.recipient_id); setAmount(result.amount)
      }
      if (result.kind === 'plan_draft' && result.amount && result.subscription_id && result.run_at) {
        setPlanDraft({ amount: result.amount, subscription_id: result.subscription_id, run_at: result.run_at }); setPanel('plans')
      }
      if (result.kind === 'bills') setPanel('bills')
      if (result.kind === 'card') setPanel('cards')
      if (result.kind === 'subscriptions') setPanel('subscriptions')
      if (result.kind === 'wealth') setPanel('wealth')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '请求失败') }
    finally { setBusy(false) }
  }

  async function prepare() {
    rememberDialogOrigin()
    setBusy(true); setError(null)
    try { const next = await api.prepare(contact, amount, note); transferKey.current = crypto.randomUUID(); setTransfer(next); setDraft(null) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法创建转账预览') }
    finally { setBusy(false) }
  }

  async function changeTransfer(action: () => Promise<Transfer>) {
    setBusy(true); setError(null)
    try { setTransfer(await action()) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '操作未完成') }
    finally { setBusy(false) }
  }

  async function verify(code: string) {
    if (!transfer) return
    setBusy(true); setError(null)
    try {
      const result = await api.verify(transfer.id, code)
      if (result.verified && result.transfer) setTransfer(result.transfer)
      else setError(result.locked ? '连续三次验证失败，账户已暂时锁定。' : `验证码错误，还可尝试 ${result.attempts_remaining ?? 0} 次。`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '验证未完成') }
    finally { setBusy(false) }
  }

  async function execute() {
    if (!transfer) return
    const id = transfer.id
    await changeTransfer(() => api.execute(id, transferKey.current))
    await refresh().catch(() => undefined)
  }

  async function prepareCard(action: CardAction['action']) {
    rememberDialogOrigin()
    setBusy(true); setError(null)
    try { cardKey.current = crypto.randomUUID(); setCardAction(await api.prepareCard(action)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法预览卡片操作') }
    finally { setBusy(false) }
  }

  async function changeCardAction(action: () => Promise<CardAction>) {
    setBusy(true); setError(null)
    try { setCardAction(await action()) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '操作未完成') }
    finally { setBusy(false) }
  }

  async function verifyCard(code: string) {
    if (!cardAction) return
    setBusy(true); setError(null)
    try {
      const result = await api.verifyCard(cardAction.id, code)
      if (result.verified && result.action) setCardAction(result.action)
      else setError(result.locked ? '连续三次验证失败，账户已暂时锁定。' : `验证码错误，还可尝试 ${result.attempts_remaining ?? 0} 次。`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '验证未完成') }
    finally { setBusy(false) }
  }

  async function executeCard() {
    if (!cardAction) return
    setBusy(true); setError(null)
    try { const result = await api.executeCard(cardAction.id, cardKey.current); setCardAction(result.action); setCardData(result.card) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '操作未完成') }
    finally { setBusy(false) }
  }

  async function prepareSubscriptionCancel(id: string) {
    rememberDialogOrigin()
    setBusy(true); setError(null)
    try { subscriptionKey.current = crypto.randomUUID(); setSubscriptionAction(await api.prepareSubscriptionCancel(id)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法预览取消操作') }
    finally { setBusy(false) }
  }

  async function confirmSubscriptionCancel() {
    if (!subscriptionAction) return
    setBusy(true); setError(null)
    try { setSubscriptionAction({ ...subscriptionAction, action: await api.confirmSubscriptionCancel(subscriptionAction.action) }) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '确认失败') }
    finally { setBusy(false) }
  }

  async function executeSubscriptionCancel() {
    if (!subscriptionAction) return
    setBusy(true); setError(null)
    try { const result = await api.executeSubscriptionCancel(subscriptionAction.action.id, subscriptionKey.current); setSubscriptionAction(result); setSubscriptionItems((items) => items.map((item) => item.id === result.subscription.id ? result.subscription : item)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '取消未完成') }
    finally { setBusy(false) }
  }

  async function preparePlan(amount: string, subscriptionId: string, runAt: string) {
    setBusy(true); setError(null)
    try { const plan = await api.preparePlan(amount, subscriptionId, runAt); setPlans((items) => [plan, ...items]) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '计划预览失败') }
    finally { setBusy(false) }
  }

  async function actOnPlan(plan: Plan, action: 'confirm' | 'pause' | 'resume' | 'cancel' | 'release') {
    setBusy(true); setError(null)
    try {
      const updated = await ({ confirm: () => api.confirmPlan(plan), pause: () => api.pausePlan(plan.id), resume: () => api.resumePlan(plan.id), cancel: () => api.cancelPlan(plan.id), release: () => api.releasePlan(plan.id) })[action]()
      setPlans((items) => items.map((item) => item.id === updated.id ? updated : item))
      if (action === 'release') await refresh()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '计划操作失败') }
    finally { setBusy(false) }
  }

  async function runDuePlans() {
    setBusy(true); setError(null)
    try { await api.runDuePlans(); await Promise.all([refresh(), refreshAuxiliary()]) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法检查到期任务') }
    finally { setBusy(false) }
  }

  async function assessWealth(answers: { experience: number; loss_tolerance: number; horizon: number }) {
    setBusy(true); setError(null)
    try { await api.assessWealth(answers); const data = await api.wealthProducts(); setWealthProducts(data.items); setWealthRiskLevel(data.risk_level) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '风险测评失败') }
    finally { setBusy(false) }
  }

  async function prepareWealth(productId: string, value: string) {
    rememberDialogOrigin()
    setBusy(true); setError(null)
    try { wealthKey.current = crypto.randomUUID(); setWealthOrder(await api.prepareWealth(productId, value)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法预览模拟申购') }
    finally { setBusy(false) }
  }

  async function prepareRedemption(productId: string, value: string) {
    rememberDialogOrigin()
    setBusy(true); setError(null)
    try { wealthKey.current = crypto.randomUUID(); setWealthOrder(await api.prepareRedemption(productId, value)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '无法预览模拟赎回') }
    finally { setBusy(false) }
  }

  async function confirmWealth() {
    if (!wealthOrder) return
    setBusy(true); setError(null)
    try { setWealthOrder(await api.confirmWealth(wealthOrder)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '确认失败') }
    finally { setBusy(false) }
  }

  async function verifyWealth(code: string) {
    if (!wealthOrder) return
    setBusy(true); setError(null)
    try { const result = await api.verifyWealth(wealthOrder.id, code); if (result.verified && result.order) setWealthOrder(result.order); else setError(result.locked ? '连续三次验证失败，账户已暂时锁定。' : `验证码错误，还可尝试 ${result.attempts_remaining ?? 0} 次。`) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '验证失败') }
    finally { setBusy(false) }
  }

  async function executeWealth() {
    if (!wealthOrder) return
    setBusy(true); setError(null)
    try { setWealthOrder(await api.executeWealth(wealthOrder.id, wealthKey.current)); await Promise.all([refresh(), refreshAuxiliary()]) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '申购未完成') }
    finally { setBusy(false) }
  }

  const transactions = overview?.transactions ?? []
  return <div className="banking-app">
    <SideBar panel={panel} setPanel={setPanel} />
    <main className="banking-main">
      <header className="banking-topbar"><div className="banking-mobile-logo"><Logo /></div><div className="banking-breadcrumb">个人银行 <span>/</span> {{ home: '总览', bills: '账单分析', activity: '交易记录', subscriptions: '订阅管理', plans: '跨场景计划', wealth: '理财实验室', cards: '卡片管理', security: '安全中心' }[panel]}</div><div className="banking-top-actions"><span className="banking-env"><span /> 安全沙箱</span><div className="banking-avatar" aria-label="演示用户">林</div></div></header>
      {panel === 'home' && <div className="banking-content">
        <div className="banking-page-heading"><div><div className="banking-eyebrow"><Sparkles size={14} /> 为你服务的银行</div><h1>{greeting()}，林女士<span className="banking-heading-dot">.</span></h1><p>把日常金融小事，交给一句话。</p></div><span className="banking-date">{new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' }).format(new Date())}</span></div>
        <section className="banking-hero"><div className="banking-hero-copy"><div className="banking-hero-label"><span /> 您的账户</div><h2>每一笔，都从容掌握。</h2><p>查余额、看交易、完成转账。简单表达，安全执行。</p><button onClick={() => document.getElementById('banking-chat-input')?.focus()}>与智能助理对话 <ArrowUpRight size={17} /></button></div><div className="banking-hero-orb"><div className="banking-orb-core"><span /></div><div className="banking-orb-ring ring-one" /><div className="banking-orb-ring ring-two" /></div><div className="banking-hero-index">01 / INTELLIGENT BANKING</div></section>
        <div className="banking-grid">
          <div className="banking-primary-column">
            <div className="banking-stats"><div className="banking-stat main"><div className="banking-stat-head"><span>可用余额</span><Wallet size={18} /></div><strong>{overview ? currency(overview.balance) : '—'}</strong><small>演示账户 · 尾号 2388</small></div><div className="banking-stat"><div className="banking-stat-head"><span>今日已转</span><ArrowUpRight size={18} /></div><strong>{overview ? currency(overview.daily_spend) : '—'}</strong><small>¥1,000 以上需演示强验证</small></div></div>
            <section className="banking-section banking-transfer-card"><div className="banking-section-title"><div><div className="banking-section-kicker">QUICK ACTION</div><h2>发起转账</h2></div><span className="banking-section-icon"><ArrowUpRight size={21} /></span></div><p>可从下方填写，也可以直接告诉智能助理。</p><div className="banking-form-row"><label>收款人<select value={contact} onChange={(event) => setContact(event.target.value)}>{overview?.contacts.map((item: Contact) => <option key={item.id} value={item.id}>{item.name} · {item.bank} {item.account_tail}</option>)}</select></label><label>金额（元）<input value={amount} onChange={(event) => setAmount(event.target.value)} inputMode="decimal" placeholder="0.00" /></label></div><label className="banking-note-label">备注 <span>可选</span><input value={note} onChange={(event) => setNote(event.target.value)} maxLength={100} placeholder="添加一条备注" /></label><button className="banking-primary" onClick={prepare} disabled={!ready || busy || !amount}>预览转账 <ArrowRight size={17} /></button>{draft && <div className="banking-draft-badge"><Sparkles size={14} /> 已从对话中填入转账信息，请核对</div>}</section>
            <section className="banking-section banking-activity"><div className="banking-section-title"><div><div className="banking-section-kicker">YOUR ACTIVITY</div><h2>最近交易</h2></div><button className="banking-text-button" onClick={() => setPanel('activity')}>查看全部 <ArrowRight size={16} /></button></div>{transactions.length ? transactions.slice(0, 3).map((item) => <div className="banking-transaction" key={item.id}><span className="banking-transaction-icon"><ArrowUpRight size={18} /></span><div><strong>转账给 {item.recipient.name}</strong><small>{item.note || '账户转账'} · {item.receipt}</small></div><b>−{currency(item.amount)}</b></div>) : <div className="banking-empty"><ArrowDownLeft size={20} />还没有交易。完成第一笔沙箱转账后会显示在这里。</div>}</section>
          </div>
          <div className="banking-secondary-column"><section className="banking-assistant"><div className="banking-assistant-head"><div className="banking-assistant-avatar"><Sparkles size={21} /></div><div><strong>星河智能助理</strong><span><span className="banking-online" /> 随时为你服务</span></div><MoreHorizontal size={21} /></div><div className="banking-messages" ref={scrollRef}>{messages.map((message) => <div className={`banking-message ${message.role}`} key={message.id}><div className="banking-message-bubble">{message.text}</div><time>{message.time}</time></div>)}{busy && <div className="banking-typing"><span /><span /><span /></div>}</div><div className="banking-suggestions">{tips.map((tip) => <button key={tip} onClick={() => send(tip)} disabled={busy || !ready}>{tip} <ArrowUpRight size={12} /></button>)}</div><form className="banking-chat-form" onSubmit={(event) => { event.preventDefault(); send() }}><input id="banking-chat-input" value={input} onChange={(event) => setInput(event.target.value)} placeholder="说说你想办理什么…" aria-label="向银行智能助理发送消息" /><button disabled={!ready || busy || !input.trim()} aria-label="发送消息"><Send size={18} /></button></form><div className="banking-chat-caption"><LockKeyhole size={12} /> 对话不会直接触发扣款，操作需逐步确认</div></section><div className="banking-trust"><div><ShieldCheck size={21} /><strong>安全，由你掌握</strong></div><p>查询即时响应。转账会展示完整信息，并根据金额进入确认或演示验证。</p><button onClick={() => setPanel('security')}>了解安全机制 <ArrowRight size={15} /></button></div></div>
        </div>
      </div>}
      {panel === 'bills' && <BillsPage data={billData} />}
      {panel === 'subscriptions' && <SubscriptionsPage items={subscriptionItems} busy={busy} onCancel={prepareSubscriptionCancel} />}
      {panel === 'plans' && <PlansPage key={planDraft ? `${planDraft.amount}-${planDraft.subscription_id}-${planDraft.run_at}` : 'manual'} plans={plans} subscriptions={subscriptionItems} notifications={notifications} draft={planDraft} busy={busy} onPrepare={preparePlan} onAction={actOnPlan} onRunDue={runDuePlans} />}
      {panel === 'wealth' && <WealthPage products={wealthProducts} holdings={wealthHoldings} riskLevel={wealthRiskLevel} busy={busy} onAssess={assessWealth} onPrepare={prepareWealth} onRedeem={prepareRedemption} />}
      {panel === 'cards' && <CardsPage card={cardData} busy={busy} onAction={prepareCard} />}
      {panel === 'activity' && <div className="banking-content banking-subpage"><div className="banking-eyebrow"><Activity size={14} /> ACTIVITY</div><h1>交易记录<span className="banking-heading-dot">.</span></h1><p>所有记录均由仿真账本生成，回执可复核。</p><div className="banking-history-card"><div className="banking-history-head"><strong>账户交易</strong><span>{transactions.length} 笔记录</span></div>{transactions.length ? transactions.map((item) => <div className="banking-transaction" key={item.id}><span className="banking-transaction-icon"><ArrowUpRight size={18} /></span><div><strong>转账给 {item.recipient.name}</strong><small>{item.recipient.bank} · {item.receipt}</small></div><b>−{currency(item.amount)}</b></div>) : <div className="banking-empty">暂无转账记录</div>}</div></div>}
      {panel === 'security' && <div className="banking-content banking-subpage"><div className="banking-eyebrow"><ShieldCheck size={14} /> SECURITY</div><h1>安全中心<span className="banking-heading-dot">.</span></h1><p>每一步都有明确边界，每一笔都有可核验依据。</p><div className="banking-security-grid"><div><span className="banking-security-icon green"><Check size={22} /></span><h2>绿色 · 直接查询</h2><p>余额和交易记录由服务端读取，不需要二次确认。</p></div><div><span className="banking-security-icon amber"><CreditCard size={22} /></span><h2>黄色 · 明确确认</h2><p>日累计不超过 ¥1,000 的转账，展示收款人与金额后才能继续。</p></div><div><span className="banking-security-icon rose"><Fingerprint size={22} /></span><h2>红色 · 演示强验证</h2><p>日累计超过 ¥1,000 的转账需要额外验证。此处为模拟流程。</p></div></div><div className="banking-trust wide"><div><LockKeyhole size={21} /><strong>安全说明</strong></div><p>此产品目前是独立的竞赛仿真沙箱，没有接入真实银行或支付通道；页面中的账户、资金、验证码与回执均为模拟数据。服务端执行权限校验、余额检查与幂等控制。</p></div><div className={`banking-audit-status ${auditIntegrity === false ? 'bad' : ''}`}><ShieldCheck size={18} /><span>沙箱审计事件链：{auditIntegrity === null ? '读取中' : auditIntegrity ? '校验通过' : '校验异常'}</span></div></div>}
      <footer className="banking-footer"><span>© 2026 星河银行 · 竞赛演示系统</span><span>DESIGNED FOR TRUST <span className="banking-footer-star">✦</span></span></footer>
    </main>
    <nav className="banking-mobile-nav" aria-label="移动导航"><button className={panel === 'home' ? 'active' : ''} onClick={() => setPanel('home')}><Wallet size={19} />总览</button><button className={panel === 'bills' ? 'active' : ''} onClick={() => setPanel('bills')}><BarChart3 size={19} />账单</button><button className={panel === 'wealth' ? 'active' : ''} onClick={() => setPanel('wealth')}><TrendingUp size={19} />理财</button><button className={panel === 'subscriptions' ? 'active' : ''} onClick={() => setPanel('subscriptions')}><Repeat2 size={19} />订阅</button><button className={panel === 'plans' ? 'active' : ''} onClick={() => setPanel('plans')}><CalendarClock size={19} />计划</button><button className={panel === 'cards' ? 'active' : ''} onClick={() => setPanel('cards')}><CreditCard size={19} />卡片</button><button className={panel === 'security' ? 'active' : ''} onClick={() => setPanel('security')}><ShieldCheck size={19} />安全</button></nav>
    {error && !transfer && !cardAction && !subscriptionAction && !wealthOrder && <div className="banking-toast" role="alert"><MessageCircle size={17} />{error}<button onClick={() => setError(null)} aria-label="关闭提示"><X size={16} /></button></div>}
    {transfer && <TransferDialog returnFocus={dialogOrigin} transfer={transfer} busy={busy} error={error} onClose={() => { setTransfer(null); setError(null); refresh().catch(() => undefined) }} onConfirm={() => changeTransfer(() => api.confirm(transfer))} onVerify={verify} onExecute={execute} />}
    {cardAction && <CardDialog returnFocus={dialogOrigin} action={cardAction} busy={busy} error={error} onClose={() => { setCardAction(null); setError(null); refreshAuxiliary().catch(() => undefined) }} onConfirm={() => changeCardAction(() => api.confirmCard(cardAction))} onVerify={verifyCard} onExecute={executeCard} />}
    {subscriptionAction && <SubscriptionDialog returnFocus={dialogOrigin} action={subscriptionAction.action} subscription={subscriptionAction.subscription} busy={busy} error={error} onClose={() => { setSubscriptionAction(null); setError(null); refreshAuxiliary().catch(() => undefined) }} onConfirm={confirmSubscriptionCancel} onExecute={executeSubscriptionCancel} />}
    {wealthOrder && <WealthDialog returnFocus={dialogOrigin} order={wealthOrder} busy={busy} error={error} onClose={() => { setWealthOrder(null); setError(null); refreshAuxiliary().catch(() => undefined) }} onConfirm={confirmWealth} onVerify={verifyWealth} onExecute={executeWealth} />}
  </div>
}
