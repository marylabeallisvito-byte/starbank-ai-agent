import { operatorRequest } from './trustApi'

export type WorkforceSession={session_token:string;expires_at:string;identity:{id:string;display_name:string;role:string;permissions:string[]}}
let active:WorkforceSession|null=null
export function workforceSession(){if(active&&Date.parse(active.expires_at)<=Date.now())active=null;return active}
export async function signInWorkforce(credential:string){
  const clean=credential.trim()
  if(!/^[A-Za-z0-9_-]{24,200}$/.test(clean))throw new Error('只输入令牌内容，不含变量名、等号或引号；不要选择 .env 文件。')
  const result=await operatorRequest<WorkforceSession>(clean,'/operator/login',{})
  active=result
  return result
}
export async function signOutWorkforce(){if(active)await operatorRequest(active.session_token,'/operator/logout',{});active=null}
export function forgetWorkforce(){active=null}
