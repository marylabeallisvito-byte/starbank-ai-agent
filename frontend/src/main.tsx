import PageBoundary, { Recovery } from './PageBoundary'
import { createRoot } from 'react-dom/client'
import './index.css'

// G6 v5 会在 React 开发态 StrictMode 的模拟卸载期间继续完成异步绘制，
// 导致已销毁画布被再次 draw。应用级副作用已在组件内显式清理，因此入口不再
// 启用 StrictMode 的双挂载探测，保证开发与生产环境行为一致。
const root = createRoot(document.getElementById('root')!)
const fresh = new URLSearchParams(window.location.search)
if (fresh.get('research_fresh') === '1') { sessionStorage.removeItem('finrisk-banking-sandbox-session'); fresh.delete('research_fresh'); window.history.replaceState(null, '', window.location.pathname + (fresh.size ? '?' + fresh.toString() : '')) }
const classic = window.location.pathname === '/banking/classic'
if (classic) import('./banking/research.css')
const page = window.location.pathname === '/banking/research'
  ? import('./banking/ResearchStudy.tsx')
  : classic ? import('./banking/BankingApp.tsx')
  : window.location.pathname.startsWith('/banking')
  ? import('./banking/BankingWorkspace.tsx')
  : import('./App.tsx')
page.then(({ default: Page }) => root.render(<PageBoundary><div className={classic ? 'research-baseline' : undefined}><Page /></div></PageBoundary>))
  .catch(() => root.render(<Recovery onRetry={() => window.location.reload()} />))
