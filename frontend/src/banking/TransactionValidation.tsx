import { useEffect,useState } from 'react'
import { Fingerprint } from 'lucide-react'
import { request } from './api'

type Acceptance={verified:boolean;result:{receipt:string;amount:string;committed_at:string;user_verified:boolean}|null}
export default function TransactionValidation({enrolled,busy,revision,onPrepare}:{enrolled:boolean;busy:boolean;revision:string;onPrepare:()=>void}){
  const [result,setResult]=useState<Acceptance|null>(null),[error,setError]=useState('')
  useEffect(()=>{let active=true;request<Acceptance>('/execution-device-acceptance').then(v=>{if(active){setResult(v);setError('')}}).catch(()=>{if(active)setError('完整交易验收记录暂不可用')});return()=>{active=false}},[revision])
  return <section className="device-validation"><span className="atlas-overline">FROM YOUR SIGNATURE TO THE RECEIPT</span><h2>再验证一次，完整的授权链。</h2><p>使用当前虚构账户，为妈妈准备 ¥1,001 的红色转账预览。由你本人核对后完成 Windows Hello，系统再提交仿真账本；没有真实银行资金流动。</p><button className="atlas-button dark" disabled={!enrolled||busy} onClick={onPrepare}><Fingerprint size={17}/>准备红色仿真转账预览</button>{!enrolled&&<p>请在 localhost 的系统浏览器中，先绑定当前会话设备。</p>}{error&&<p role="alert">{error}</p>}{result?.verified&&result.result&&<div className="device-check-result" role="status"><div><strong>红色转账的设备授权与账本回执已记录</strong><p>¥{result.result.amount} · {new Date(result.result.committed_at).toLocaleString('zh-CN')}</p><code>{result.result.receipt}</code><small>此记录证明本会话完成了签名授权及仿真入账，不等同于银行实名核验或硬件认证。</small></div></div>}</section>
}
