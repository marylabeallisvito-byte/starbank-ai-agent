import { request } from './api'
import type { MonthlyArrangement } from './MonthlyImpact'
export type MissionNode = { id: string; title: string; level: string; depends_on: string[]; status: string; output: Record<string, unknown> | null }
export type Evidence = { merchant: string; amount: string; transaction_ids: string[]; explanation: string }
export type Mission = { monthly?:MonthlyArrangement; id: string; title: string; status: string; updated_at: string; input: { risk_policy?:'pause_all_on_suspicion'|'reserve_before_review'; not_before?: string | null; goal_proposal_id?: string; bill_source?: 'sandbox' | 'imported'; bill_period?: string; source_record_count?: number; amount: string; minimum_balance: string; subscription_name: string | null; cancel_subscription: boolean }; nodes: MissionNode[]; evidence: Evidence[]; digest: string; reason: string | null; released: boolean; created_at: string; events: { event: string; detail: string; created_at: string }[]; reviews: { id: string; node_id?:string|null; status: string; reason: string; decision_note: string | null; created_at?:string; decided_at?:string|null }[] }
export type ChatMessage = { id: number | string; role: 'user' | 'agent'; text: string; attachment?: { kind: string; mission_id?: string; goal_id?: string; recipient_id?: string; amount?: string; subscription_id?: string; run_at?: string; funds_authorized?: boolean; capability?: {title:string;effect:string;next_step:string;route:{view:string;section:string|null}|null} } | null; created_at: string }
export type AuthStatus = { mode: 'passkey' | 'demo'; enrolled: boolean; origin: string; rp_id: string; identity_scope: string }
export type Review = { review: { id: string; reason: string; created_at: string }; mission: Mission }
export const trustApi = {
  missions: () => request<{ items: Mission[] }>('/missions'),
  conversation: () => request<{ items: ChatMessage[] }>('/conversation'),
  message: (text: string, client_id: string, context?:{goal_id:string;goal_digest:string}) => request<{ reply: string; attachment: ChatMessage['attachment'] }>('/conversation/messages', 'POST', { text, client_id,...context }),
  revise: (mission: Mission, amount: string, minimum_balance: string) => request<Mission>(`/missions/${mission.id}/revise`, 'POST', { amount, minimum_balance, digest: mission.digest }),
  action: (mission: Mission, action: string) => request<Mission>(`/missions/${mission.id}/action`, 'POST', { action, digest: mission.digest }),
  authStatus: () => request<AuthStatus>('/auth/status'),
  actionStatus: (kind: string, id: string) => request<{ status: string; digest: string }>(`/auth/actions/${kind}/${id}`),
}

const bytes = (value: string): ArrayBuffer => Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0)).buffer
const b64 = (buffer: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
type Options = Record<string, unknown> & { challenge: string; user?: { id: string; name: string; displayName: string }; allowCredentials?: { id: string; type: string }[]; excludeCredentials?: { id: string; type: string }[] }
function serialize(credential: PublicKeyCredential) {
  const response = credential.response
  const common = { id: credential.id, rawId: b64(credential.rawId), type: credential.type, authenticatorAttachment: credential.authenticatorAttachment, clientExtensionResults: credential.getClientExtensionResults() }
  if (response instanceof AuthenticatorAttestationResponse) return { ...common, response: { clientDataJSON: b64(response.clientDataJSON), attestationObject: b64(response.attestationObject), transports: response.getTransports() } }
  const assertion = response as AuthenticatorAssertionResponse
  return { ...common, response: { clientDataJSON: b64(assertion.clientDataJSON), authenticatorData: b64(assertion.authenticatorData), signature: b64(assertion.signature), userHandle: assertion.userHandle ? b64(assertion.userHandle) : null } }
}
export async function enrollPasskey() {
  if (typeof window.PublicKeyCredential === 'undefined' || !navigator.credentials) throw new Error('当前浏览器未提供 WebAuthn，请使用支持通行密钥的系统浏览器打开本机页面')
  const result = await request<{ challenge_id: string; options: Options }>('/auth/register/options', 'POST')
  const options = result.options
  const credential = await navigator.credentials.create({ publicKey: { ...options, challenge: bytes(options.challenge), user: { ...options.user!, id: bytes(options.user!.id) }, excludeCredentials: options.excludeCredentials?.map(c => ({ ...c, id: bytes(c.id) })) } as unknown as PublicKeyCredentialCreationOptions }) as PublicKeyCredential | null
  if (!credential) throw new Error('设备验证已取消')
  return request('/auth/register/verify', 'POST', { challenge_id: result.challenge_id, credential: serialize(credential) })
}
export async function authorizePasskey(kind: string, action_id: string) {
  if (typeof window.PublicKeyCredential === 'undefined' || !navigator.credentials) throw new Error('当前浏览器未提供 WebAuthn，请使用支持通行密钥的系统浏览器办理红色操作')
  const result = await request<{ challenge_id: string; options: Options }>('/auth/authorize/options', 'POST', { kind, action_id })
  const credential = await navigator.credentials.get({ publicKey: { ...result.options, challenge: bytes(result.options.challenge), allowCredentials: result.options.allowCredentials?.map(c => ({ ...c, id: bytes(c.id) })) } as unknown as PublicKeyCredentialRequestOptions }) as PublicKeyCredential | null
  if (!credential) throw new Error('设备验证已取消')
  return request('/auth/authorize/verify', 'POST', { challenge_id: result.challenge_id, credential: serialize(credential) })
}
export async function operatorRequest<T>(token: string, path = '/operator/reviews', body?: unknown): Promise<T> {
  const response = await fetch(`/api/banking${path}`, { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json', 'X-Banking-Operator': token }, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(15000) })
  if (!response.ok) { const error = await response.json(); throw new Error(error.detail || '复核服务不可用') }
  return response.json()
}

export async function checkPasskey() {
  if (typeof window.PublicKeyCredential === 'undefined' || !navigator.credentials) throw new Error('请在 Chrome 或 Edge 打开配置的 localhost 页面后验证。')
  const result = await request<{ challenge_id: string; options: Options }>('/auth/device-check/options', 'POST')
  const credential = await navigator.credentials.get({ publicKey: { ...result.options, challenge: bytes(result.options.challenge), allowCredentials: result.options.allowCredentials?.map(c => ({ ...c, id: bytes(c.id) })) } as unknown as PublicKeyCredentialRequestOptions }) as PublicKeyCredential | null
  if (!credential) throw new Error('设备验证已取消；没有生成资金授权。')
  return request<{verified:boolean;verification_id:string;checked_at:string;origin:string;user_verified:boolean;funds_moved:boolean}>('/auth/device-check/verify', 'POST', { challenge_id: result.challenge_id, credential: serialize(credential) })
}
