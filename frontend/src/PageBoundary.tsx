import { Component, type ReactNode } from 'react'

function Recovery({ onRetry }: { onRetry: () => void }) {
  return <main className="app-recovery" role="alert"><span>星河 · 连接恢复</span><h1>页面暂时未能打开。</h1><p>服务更新或连接中断可能导致页面资源失效。请重新加载；已保存的服务端记录会保留。</p><button onClick={onRetry}>重新加载页面</button><a href="/banking">返回银行工作台</a><small>若仍无法打开，请确认本机前后端服务均在运行。</small></main>
}
export default class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() { return this.state.failed ? <Recovery onRetry={() => window.location.reload()} /> : this.props.children }
}

export { Recovery }
