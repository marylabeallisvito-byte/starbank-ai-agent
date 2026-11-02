import { useEffect,useState } from 'react'
import { CheckCheck,Fingerprint,LoaderCircle } from 'lucide-react'
import { request } from './api'
import { checkPasskey } from './trustApi'

type Result={verified:boolean;verification_id?:string;checked_at?:string;origin?:string;user_verified?:boolean;funds_moved?:boolean}
export default function DeviceValidation({enrolled}:{enrolled:boolean}){
  const [result,setResult]=useState<Result|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
  useEffect(()=>{let active=true;request<{verified:boolean;result:Result|null}>('/auth/device-check/status').then(v=>{if(active&&v.result)setResult({...v.result,verified:v.verified})}).catch(()=>{});return()=>{active=false}},[])
  const [storage,setStorage]=useState<{enabled:boolean;scope:string}|null>(null),[storageError,setStorageError]=useState(false)
  useEffect(()=>{let active=true;request<{enabled:boolean;scope:string}>('/security/storage').then(v=>{if(active)setStorage(v)}).catch(()=>{if(active)setStorageError(true)});return()=>{active=false}},[])
  async function verify(){setBusy(true);setError('');try{setResult(await checkPasskey())}catch(e){setError(e instanceof Error?e.message:'设备验证未完成')}finally{setBusy(false)}}
  return <section className="device-validation"><span className="atlas-overline">VERIFY THE DEVICE. KEEP FUNDS STILL.</span><h2>用真实设备，完成一次验证。</h2><p>请在配置的 localhost 来源，用 Chrome 或 Edge 操作。系统弹窗中的 PIN、指纹或人脸由你本人完成；此测试只验证签名，不生成资金授权。</p><button className="atlas-button dark" disabled={!enrolled||busy} onClick={verify}>{busy?<LoaderCircle className="spin" size={17}/>:<Fingerprint size={17}/>}开始只读设备验证</button>{!enrolled&&<small>先在上方绑定通行密钥，随后即可测试。</small>}{error&&<p className="atlas-error" role="alert">{error}</p>}{result?.verified&&<div className="device-check-result" role="status"><CheckCheck size={21}/><div><strong>设备签名与用户验证已通过</strong><p>{result.checked_at?new Date(result.checked_at).toLocaleString('zh-CN'):''} · {result.origin}</p><code>验证记录 {result.verification_id}</code><small>本次未动账；此结果不等同于银行实名核验或硬件防伪认证。</small></div></div>}<div className="device-check-result"><div><strong>{storage ? (storage.enabled ? "敏感字段加密已启用" : "敏感字段加密未启用") : storageError ? "加密状态暂不可用" : "正在读取加密状态…"}</strong><p>{storage?.scope ?? (storageError ? "请检查后端服务连接。" : "正在从服务端核对加密配置。") }</p></div></div></section>
}
