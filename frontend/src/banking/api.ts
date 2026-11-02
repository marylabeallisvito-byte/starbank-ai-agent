import type { GraphEvidence } from './EvidenceLab'
export type Contact = { id: string; name: string; bank: string; account_tail: string }
export type Transfer = {
  id: string; recipient: Contact; amount: string; note: string;
  level: 'yellow' | 'red'; status: string; digest: string;
  expires_at: string; receipt: string | null; risk: { graph?: GraphEvidence; tier: 'normal' | 'review'; factors: { code: string; description: string; transaction_ids: string[] }[]; duplicate_transfer_id: string | null; source: string }; sandbox: true;
}
export type Overview = {
  balance: string; reserved: string; total_balance: string; daily_spend: string; daily_limit: string;
  contacts: Contact[]; transactions: Transfer[]; sandbox: true;
}
export type Intent = {
  kind: 'entity_lookup' | 'statement_import' | 'fx_budget' | 'public_notices' | 'balance' | 'transactions' | 'transfer_draft' | 'plan_draft' | 'bills' | 'card' | 'subscriptions' | 'wealth' | 'clarification' | 'unsupported';
  reply: string; recipient_id?: string; amount?: string; subscription_id?: string; run_at?: string; transactions?: Transfer[]; sandbox: true;
}
export type Bill = { id: string; merchant: string; category: string; amount: string; occurred_at: string }
export type Bills = { month: string; total: string; categories: { name: string; amount: string; transaction_ids: string[] }[]; anomalies: { kind: string; merchant: string; amount: string; transaction_ids: string[]; explanation: string }[]; transactions: Bill[]; sandbox: true }
export type Card = { tail: string; status: 'ACTIVE' | 'FROZEN' | 'LOST'; updated_at: string; sandbox: true }
export type CardAction = { id: string; action: 'freeze' | 'unfreeze' | 'report_lost'; status: string; level: 'yellow' | 'red'; digest: string; expires_at: string; demo_code: string | null; sandbox: true }
export type Subscription = { id: string; merchant: string; amount: string; next_charge_at: string; status: 'ACTIVE' | 'CANCELLED'; sandbox: true }
export type SubscriptionAction = { id: string; subscription_id: string; status: string; level: 'yellow'; digest: string; expires_at: string; sandbox: true }
export type PlanNode = { id: string; kind: string; depends_on: string[]; status: 'PENDING' | 'COMPLETED'; output: Record<string, unknown> | null }
export type Plan = { id: string; title: string; status: 'DRAFT' | 'SCHEDULED' | 'PAUSED' | 'NEEDS_ATTENTION' | 'CANCELLED' | 'COMPLETED' | 'RELEASED'; run_at: string; amount: string; subscription_id: string; digest: string; nodes: PlanNode[]; failure_reason: string | null; created_at: string; sandbox: true }
export type Notification = { id: string; plan_id: string; message: string; created_at: string }
export type WealthProduct = { id: string; name: string; type: string; risk_level: number; description: string; suitable: boolean }
export type WealthHolding = { product: WealthProduct; principal: string; updated_at: string }
export type WealthOrder = { id: string; product: WealthProduct; amount: string; action: 'BUY' | 'REDEEM'; status: 'PREPARED' | 'AWAITING_VERIFICATION' | 'READY' | 'COMMITTED'; level: 'red'; digest: string; expires_at: string; demo_code: string | null; receipt: string | null; sandbox: true }

const TOKEN_KEY = 'finrisk-banking-sandbox-session'

export function getToken() { return sessionStorage.getItem(TOKEN_KEY) }

export class BankingError extends Error { status: number; constructor(message: string, status: number) { super(message); this.status = status } }
export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const token = getToken()
  let response: Response
  try { response = await fetch(`/api/banking${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Banking-Session': token } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  }) } catch (error) {
    throw new BankingError(error instanceof DOMException && ['TimeoutError', 'AbortError'].includes(error.name)
      ? '等待服务响应超时。若刚才提交了操作，请先查看任务或交易记录确认结果，再决定是否重试。'
      : '暂时无法连接银行服务，请检查本机后端是否运行。已保存的记录不会因此清除。', 0)
  }
  if (!response.ok) {
    const error = await response.json().catch(() => null) as { detail?: string } | null
    throw new BankingError(typeof error?.detail === 'string' ? error.detail : `请求未完成 (${response.status})`, response.status)
  }
  try { return await response.json() as T } catch { throw new BankingError('服务响应未完整返回，执行结果尚未核实。请先查验原操作，不要重新发起。', 0) }
}

export async function startSession() {
  let token = getToken()
  if (token) {
    try { await request<Overview>('/overview'); return token } catch (error) { if (!(error instanceof BankingError) || error.status !== 401) throw error; sessionStorage.removeItem(TOKEN_KEY) }
  }
  const response = await request<{ token: string }>('/session', 'POST')
  token = response.token
  sessionStorage.setItem(TOKEN_KEY, token)
  return token
}

export const api = {
  overview: () => request<Overview>('/overview'),
  intent: (text: string) => request<Intent>('/intent', 'POST', { text }),
  prepare: (recipient_id: string, amount: string, note: string) => request<Transfer>('/transfers/prepare', 'POST', { recipient_id, amount, note }),
  confirm: (transfer: Transfer) => request<Transfer>(`/transfers/${transfer.id}/confirm`, 'POST', { digest: transfer.digest }),
  demoCode: (id: string) => request<{ code: string; label: string }>(`/transfers/${id}/demo-code`),
  verify: (id: string, code: string) => request<{ verified: boolean; locked?: boolean; attempts_remaining?: number; transfer?: Transfer }>(`/transfers/${id}/verify`, 'POST', { code }),
  execute: (id: string, idempotency_key: string) => request<Transfer>(`/transfers/${id}/execute`, 'POST', { idempotency_key }),
  audit: () => request<{ integrity: boolean; events: { event: string; created_at: string; detail: Record<string, unknown>; event_hash: string }[] }>('/audit'),
  bills: () => request<Bills>('/bills'),
  card: () => request<Card>('/card'),
  prepareCard: (action: CardAction['action']) => request<CardAction>('/card/actions/prepare', 'POST', { action }),
  confirmCard: (action: CardAction) => request<CardAction>(`/card/actions/${action.id}/confirm`, 'POST', { digest: action.digest }),
  verifyCard: (id: string, code: string) => request<{ verified: boolean; locked?: boolean; attempts_remaining?: number; action?: CardAction }>(`/card/actions/${id}/verify`, 'POST', { code }),
  executeCard: (id: string, idempotency_key: string) => request<{ action: CardAction; card: Card }>(`/card/actions/${id}/execute`, 'POST', { idempotency_key }),
  subscriptions: () => request<{ items: Subscription[]; sandbox: true }>('/subscriptions'),
  consentSubscriptions: () => request('/consents/subscriptions', 'POST', { confirmed: true }),
  prepareSubscriptionCancel: (id: string) => request<{ action: SubscriptionAction; subscription: Subscription }>(`/subscriptions/${id}/cancel/prepare`, 'POST'),
  confirmSubscriptionCancel: (action: SubscriptionAction) => request<SubscriptionAction>(`/subscriptions/actions/${action.id}/confirm`, 'POST', { digest: action.digest }),
  executeSubscriptionCancel: (id: string, key: string) => request<{ action: SubscriptionAction; subscription: Subscription }>(`/subscriptions/actions/${id}/execute`, 'POST', { idempotency_key: key }),
  plans: () => request<{ items: Plan[] }>('/plans'),
  preparePlan: (amount: string, subscription_id: string, run_at: string) => request<Plan>('/plans/prepare', 'POST', { amount, subscription_id, run_at }),
  confirmPlan: (plan: Plan) => request<Plan>(`/plans/${plan.id}/confirm`, 'POST', { digest: plan.digest }),
  pausePlan: (id: string) => request<Plan>(`/plans/${id}/pause`, 'POST'),
  resumePlan: (id: string) => request<Plan>(`/plans/${id}/resume`, 'POST'),
  cancelPlan: (id: string) => request<Plan>(`/plans/${id}/cancel`, 'POST'),
  releasePlan: (id: string) => request<Plan>(`/plans/${id}/release`, 'POST'),
  runDuePlans: () => request<{ completed: number }>('/plans/run-due', 'POST'),
  notifications: () => request<{ items: Notification[] }>('/notifications'),
  wealthProducts: () => request<{ items: WealthProduct[]; risk_level: number | null }>('/wealth/products'),
  wealthHoldings: () => request<{ items: WealthHolding[] }>('/wealth/holdings'),
  assessWealth: (answers: { experience: number; loss_tolerance: number; horizon: number }) => request<{ risk_level: number; label: string }>('/wealth/profile', 'POST', answers),
  prepareWealth: (product_id: string, amount: string) => request<WealthOrder>('/wealth/orders/prepare', 'POST', { product_id, amount }),
  prepareRedemption: (product_id: string, amount: string) => request<WealthOrder>('/wealth/redemptions/prepare', 'POST', { product_id, amount }),
  confirmWealth: (order: WealthOrder) => request<WealthOrder>(`/wealth/orders/${order.id}/confirm`, 'POST', { digest: order.digest }),
  verifyWealth: (id: string, code: string) => request<{ verified: boolean; attempts_remaining?: number; locked?: boolean; order?: WealthOrder }>(`/wealth/orders/${id}/verify`, 'POST', { code }),
  executeWealth: (id: string, key: string) => request<WealthOrder>(`/wealth/orders/${id}/execute`, 'POST', { idempotency_key: key }),
}
