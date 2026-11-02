import { BankingError,getToken,request,type Transfer,type CardAction,type WealthOrder,type Subscription,type SubscriptionAction } from './api'

export type Pending = {kind:'transfer';value:Transfer}|{kind:'card';value:CardAction}|{kind:'wealth';value:WealthOrder}|{kind:'subscription';value:{action:SubscriptionAction;subscription:Subscription}}
export type RecoveryRecord={version:1;owner:string;kind:Pending['kind'];id:string;digest:string;key:string;phase:'prepared'|'submitted'|'unknown'|'verified';saved_at:string}
export type RecoveryResult={pending:Pending;complete:boolean;expired:boolean;can_continue:boolean;guidance:string;checked_at:string}
const STORAGE='banking-operation-recovery-v1'
export function actionValue(pending:Pending){return pending.kind==='subscription'?pending.value.action:pending.value}
async function owner(){const token=getToken();if(!token)throw new BankingError('银行会话尚未就绪',401);return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token)))).map(v=>v.toString(16).padStart(2,'0')).join('')}
export async function recordOperation(pending:Pending,key:string,phase:RecoveryRecord['phase']):Promise<RecoveryRecord>{
  const value=actionValue(pending)
  const record:RecoveryRecord={version:1,owner:await owner(),kind:pending.kind,id:value.id,digest:value.digest,key,phase,saved_at:new Date().toISOString()}
  try{sessionStorage.setItem(STORAGE,JSON.stringify(record))}catch{throw new BankingError('无法保存本次恢复索引，已停止提交；请检查浏览器存储设置。',0)}
  return record
}
export async function loadOperation():Promise<RecoveryRecord|null>{
  const raw=sessionStorage.getItem(STORAGE);if(!raw)return null
  if(raw.length>4096)throw new BankingError('恢复索引格式异常，请勿重新办理，先查交易记录。',409)
  let value:RecoveryRecord
  try{value=JSON.parse(raw)}catch{throw new BankingError('恢复索引损坏，请先核对交易记录。',409)}
  if(value.version!==1||!['transfer','wealth','card','subscription'].includes(value.kind)||!['prepared','submitted','unknown','verified'].includes(value.phase)||!/^\w[\w-]{0,63}$/.test(value.id)||!/^[a-f0-9]{64}$/.test(value.digest)||typeof value.key!=='string'||value.key.length<8||value.key.length>128)throw new BankingError('恢复索引校验失败，请先核对交易记录。',409)
  return value.owner===await owner()?value:null
}
export async function inspectOperation(record:RecoveryRecord):Promise<RecoveryResult>{
  const response=await request<{kind:string;complete:boolean;expired:boolean;can_continue:boolean;guidance:string;checked_at:string;result:unknown}>(`/execution/${record.kind}/${encodeURIComponent(record.id)}?expected_digest=${encodeURIComponent(record.digest)}`)
  const result=response.result
  let pending:Pending
  if(record.kind==='transfer')pending={kind:'transfer',value:result as Transfer}
  else if(record.kind==='wealth')pending={kind:'wealth',value:result as WealthOrder}
  else if(record.kind==='card')pending={kind:'card',value:(result as {action:CardAction}).action}
  else pending={kind:'subscription',value:result as {action:SubscriptionAction;subscription:Subscription}}
  const value=actionValue(pending)
  if(response.kind!==record.kind||!value||value.id!==record.id||value.digest!==record.digest)throw new BankingError('服务端回执与原操作不一致，已停止恢复。',409)
  return {pending,complete:response.complete,expired:response.expired,can_continue:response.can_continue===true,guidance:response.guidance,checked_at:response.checked_at}
}
export function clearOperation(){sessionStorage.removeItem(STORAGE)}
