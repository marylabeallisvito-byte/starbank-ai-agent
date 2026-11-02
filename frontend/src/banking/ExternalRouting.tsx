import { useEffect, useState } from 'react'
import { request } from './api'

type Report = { generated_at:string;live_model:boolean;passed:number;total:number;by_category:Record<string,{passed:number;total:number}> }
export default function ExternalRouting(){
  const [reports,setReports]=useState<Report[]>([]),[error,setError]=useState('')
  useEffect(()=>{let active=true;request<{reports:Report[]}>('/evaluation/external-routing').then(v=>{if(active)setReports(v.reports)}).catch(()=>{if(active)setError('外部路由评测暂时无法读取')});return()=>{active=false}},[])
  return <section className="device-validation"><span className="atlas-overline">MEASURE WHAT STILL FAILS</span><h2>外部语料，真实暴露边界。</h2><p>使用 PolyAI BANKING77 原始英文测试集，预先固定四类共 40 条查询。这里衡量跨语言路由能力，完整失败样本保留在本机评测报告中。</p>{error&&<p role="alert">{error}</p>}{!error&&!reports.length&&<p>暂无可用报告。</p>}<div className="atlas-security-levels">{reports.map(r=><article key={String(r.live_model)}><span className="atlas-overline">{r.live_model?'MODEL ROUTING':'RULE ROUTING'}</span><h3>{r.passed} / {r.total}</h3><p>{new Date(r.generated_at).toLocaleString('zh-CN')} 记录的版本</p>{Object.entries(r.by_category).map(([category,n])=><p key={category}>{({lost_or_stolen_card:'卡片遗失',compromised_card:'卡片疑似泄露',transaction_charged_twice:'重复扣费',exchange_rate:'汇率查询'} as Record<string,string>)[category]??category}<strong style={{float:'right'}}>{n.passed}/{n.total}</strong></p>)}</article>)}</div><small>由本项目执行评测，不代表完整 77 类准确率、中文业务成功率或第三方认证。公开语料也可能存在于模型预训练中。</small><p><a href="https://github.com/PolyAI-LDN/task-specific-datasets/tree/master/banking_data" target="_blank" rel="noreferrer">数据来源与许可 ↗</a> · <a href="/banking/research">进入可用性试点 ↗</a></p></section>
}
