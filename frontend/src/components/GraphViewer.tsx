// =====================================================================
// GraphViewer.tsx —— AntV G6 v5 图谱可视化组件（极深碳灰 · 克制高级物理引擎）
// ---------------------------------------------------------------------
// 职责：
//   1. 挂载时初始化 G6 力导向图谱，并加载后端 /api/graph/sample 核心骨干网络；
//   2. force 布局：强斥力(nodeStrength<0) + 防重叠(nodeSize 放大) + 中心引力 → 球状发散、无重叠；
//   3. 节点高级质感：统一「深色圆形 + 类型色描边」，高危节点用「发光红描边」而非大红填充；
//   4. 标签智能显隐（LOD）：zoom<0.6 隐藏全部标签（只留节点），放大聚焦后再显示；
//   5. 标签防遮挡：深色半透明背景框挡住背后连线，保证小字号可读；
//   6. 边：细线 + 低透明度 + lineDash 流动动画；资金回环边以警示红 + 发光高亮；
//   7. 对外暴露 setGraph / highlight / resetHighlight，与 SSE 联动（含镜头运镜聚焦）。
//
// G6 v5 关键 API（与 v4 不同）：
//   - 样式回调形参为节点 datum；style 下 data 为业务数据、style 为视觉样式；
//   - updateNodeData/updateEdgeData 会对 data/style 做浅合并（不覆盖已有字段）；
//   - halo(光环) 是 v5 的内置「外发光」机制（无 CSS shadow），呼吸动画即驱动 halo；
//   - 标签背景框经 Prefix<'label',...> 暴露 labelBackground*（含 Stroke/Radius），深色底板防遮挡；
//   - setElementState(states, true) 批量设置元素状态（异步，附 .catch 兜底）。
// =====================================================================
import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import { Graph } from '@antv/g6';
import { fetchGraphExpand, fetchPublicCase } from '../api';
import type { GraphData, GraphEdge, GraphNode } from '../types';

// ---- 节点类型 → 描边色（统一深色圆形，仅用「类型色描边 + 尺寸」区分语义） ----
const COLOR_BY_TYPE: Record<string, string> = {
  Company: '#8AB4F8', // 公司（核心）：Google Blue
  Person: '#81C995', // 自然人：Google Green
  Account: '#FDD663', // 账户：低调琥珀（半透明描边，仅作流转介质）
  Device: '#C58AF9', // 设备：Google Purple
  device_cluster: '#C58AF9', // 聚合设备簇：Google Purple
};

// ---- 节点类型 → 尺寸（核心主体公司/人 与 次要流转介质账户 彻底拉开视觉重量比） ----
const SIZE_BY_TYPE: Record<string, number> = {
  Company: 34,
  Person: 26,
  Account: 16,
  Device: 20,
  device_cluster: 24,
};

// ---- 半透明实体圆盘：以低饱和类型色构成内核，不再使用“空心垫圈” ----
const FILL_BY_TYPE: Record<string, string> = {
  Company: '#21446F',
  Person: '#1D503C',
  Account: '#59451B',
  Device: '#4B2D65',
  device_cluster: '#4B2D65',
};
const NODE_FILL = '#202124';

function svgIcon(paths: string): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#E8F0FE" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

const ICON_BY_TYPE: Record<string, string> = {
  Company: svgIcon('<path d="M3 21h18M6 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2"/>'),
  Person: svgIcon('<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>'),
  Account: svgIcon('<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 9.5h19M6 15h4"/>'),
  Device: svgIcon('<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>'),
  device_cluster: svgIcon('<rect x="3" y="4" width="14" height="11" rx="2"/><path d="M7 19h14V8M8 19h6"/>'),
};

// ---- 关系类型 → 边颜色（默认以低透明度呈现，仅图例/高亮时强） ----
// 普通关系边：近乎透明的星轨背景（探照灯：默认隐形，仅高危资金路径亮红浮现）
const EDGE_COLOR_BY_TYPE: Record<string, string> = {
  TRANSFER: 'rgba(253, 214, 99, 0.46)',
  OWNS: 'rgba(138, 180, 248, 0.42)',
  LEGAL_REP: 'rgba(129, 201, 149, 0.34)',
  HAS_ACCOUNT: 'rgba(197, 138, 249, 0.30)',
  LOGGED_IN: 'rgba(255, 152, 69, 0.34)',
  GROUP_SUBSIDIARY: 'rgba(138, 180, 248, 0.72)',
  CONTROLLING_SHAREHOLDER: 'rgba(129, 201, 149, 0.72)',
  DISCLOSED_HOLDING: 'rgba(129, 201, 149, 0.56)',
  PARENT_INVESTMENT: 'rgba(197, 138, 249, 0.56)',
};
const EDGE_BACKGROUND = 'rgba(189, 193, 198, 0.22)';

// 高危 TRANSFER 资金回环的「Google Red」——探照灯聚焦色
const LOOP_EDGE_COLOR = '#F28B82';

// 高危节点的「Google Red」外发光色（探照灯聚焦体系）
const SUSPICIOUS_COLOR = '#F28B82';

// ---- 缩放锁（Prompt 1）—— 绝对禁止节点/文字被无限放大 ----
// G6 v5 的缩放范围键为 `zoomRange: [min, max]`（非 v4 的 minZoom/maxZoom），
// 默认 [0.01, 10] 会导致 focusElement/fitView 把子图拉到占据整屏。收紧到 [0.2, 2]。
const MIN_ZOOM = 0.2;
const MAX_ZOOM = 2;

function layoutFor(source: 'public' | 'simulation', narrow: boolean) {
  return {
    type: 'd3-force' as const,
    nodeStrength: source === 'public' ? (narrow ? -150 : -240) : (narrow ? -45 : -120),
    preventOverlap: true,
    collideStrength: 1,
    collideIterations: 2,
    linkDistance: source === 'public' ? (narrow ? 92 : 115) : (narrow ? 56 : 88),
    edgeStrength: 0.72,
    centerStrength: narrow ? 0.3 : 0.18,
    iterations: narrow ? 80 : 100,
  };
}

// 标签物理字号：锁定 10px（提示词2：进一步瘦身，杜绝「肥大标签」）；
// 受 MAX_ZOOM 钳制，屏幕上最大 10*2=20px。
const LABEL_FONT_SIZE = 11;

/** 判断是否为「长数字」（银行卡号/账号）：≥9 位且主要是数字（允许空格/横杠分隔）。 */
function isLongNumber(text: string): boolean {
  return text.replace(/\D/g, '').length >= 9;
}

/**
 * 公司名中文折行：第一行最多 5 字、第二行最多 5 字 + 省略号（最多两行），
 * 与节点间距/斥力协同，杜绝「单行长文本横向扫荡」造成的标签撞车叠罗汉。
 */
function wrapCompanyName(name: string): string {
  const MAX = 5; // 每行最多 5 字
  if (name.length <= MAX) return name;
  const first = name.slice(0, MAX);
  const second = name.slice(MAX, MAX * 2);
  return second ? `${first}\n${second}${name.length > MAX * 2 ? '…' : ''}` : first;
}

/**
 * 节点标签格式化：
 *   · 长数字（银行账号）：前 4 位 + *** + 后 4 位（服务端已脱敏，此处兜底）；
 *   · 公司名：按固定 5 字折行（最多两行 + 省略号）；
 *   · 其余（自然人 / 设备等）：原样返回。
 */
function formatNodeLabel(text: string, nodeType?: string): string {
  const raw = text ?? '';
  if (isLongNumber(raw)) {
    const digits = raw.replace(/\D/g, '');
    return `${digits.slice(0, 4)}***${digits.slice(-4)}`;
  }
  if (nodeType === 'Company') return wrapCompanyName(raw);
  return raw;
}

/** 分级注记显隐：账户微点平时零视觉，仅 hover 时以悬浮文字充当 Tooltip；主体节点标签常驻。 */
function shouldShowLabel(data: any): boolean {
  if (data?.data_class === 'public_disclosure') return data?.public_key_label === true || data?.hovered === true;
  if (data?.nodeType === 'Account') return data?.hovered === true || data?.showRiskLabel === true;
  if (data?.display_priority === 'context') return data?.nodeType === 'Company' && data?.risk_flag === true;
  return true;
}

function formatEdgeEvidence(data: any): string {
  if (!data?.showRiskLabel) return '';
  if (data.edgeType === 'TRANSFER' && Number.isFinite(Number(data.amount))) {
    return `¥${Math.round(Number(data.amount)).toLocaleString('en-US')}`;
  }
  if (['OWNS', 'GROUP_SUBSIDIARY', 'DISCLOSED_HOLDING', 'CONTROLLING_SHAREHOLDER'].includes(data.edgeType) && Number.isFinite(Number(data.ratio))) {
    return `${Number((Number(data.ratio) * 100).toFixed(2))}%`;
  }
  if (data.edgeType === 'PARENT_INVESTMENT' && Number.isFinite(Number(data.book_value_thousand_cny))) {
    return `账面 ${(Number(data.book_value_thousand_cny) / 100000).toFixed(1)} 亿元`;
  }
  return '';
}

function labelPlacementFor(id: string): 'top' | 'bottom' {
  let hash = 0;
  for (let index = 0; index < id.length; index += 1) hash += id.charCodeAt(index);
  return hash % 2 === 0 ? 'top' : 'bottom';
}

// ---- 镜头聚焦：把高危闭环平滑运镜到视野中心，再向右让出安全区 ----
// 注：G6 v5 的 easing 走 @antv/g 命名（kebab-case），「easeCubic」这类 v4 写法
// 会被 complexEasingSyntax 归一化后查不到而回退成 linear，故用 'ease-in-out-cubic'。
async function focusOnSuspicious(graph: Graph, ids: string[]): Promise<void> {
  if (!ids.length) return;
  try {
    // 安全的边界聚焦（Prompt 1）：聚焦高危节点到视口中心，受 zoomRange([0.1,1.5]) 约束，
    // 绝不把子图拉到「节点占据半个屏幕」的压抑尺度（替代全局 fitView 的过度缩放）。
    await graph.focusElement(ids, { duration: 800, easing: 'ease-in-out-cubic' });
    // 双保险：若 focusElement 内部 fitView 语义短暂越界，主动夹紧回安全区间
    const zoom = graph.getZoom();
    if (zoom > MAX_ZOOM) {
      await graph.zoomTo(MAX_ZOOM, { duration: 300, easing: 'ease-in-out-cubic' });
    } else if (zoom < MIN_ZOOM) {
      await graph.zoomTo(MIN_ZOOM, { duration: 300, easing: 'ease-in-out-cubic' });
    }
  } catch (err) {
    console.warn('[GraphViewer] 镜头聚焦失败：', err);
  }
}

/** 将后端 {nodes, edges} 结构转换为 G6 v5 的数据结构（统一圆形节点） */
function toG6Data(data: GraphData) {
  return {
    nodes: data.nodes.map((n) => {
      const isCluster = n.type === 'device_cluster';
      // 聚合设备簇的标签显示为「N 台设备」，其余用业务展示名
      const label = isCluster
        ? `${(n.properties?.count as number) ?? '?'} 台设备`
        : n.label;
      return {
        id: n.id,
        type: 'circle',
        data: { label, nodeType: n.type, ...n.properties, public_key_label: n.id === 'CATL-300750' },
      };
    }),
    edges: data.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      data: { label: e.label, edgeType: e.type, ...e.properties },
    })),
  };
}

/** 对外暴露的命令句柄（供父组件通过 ref 调用） */
export interface GraphViewerHandle {
  /** 替换整个画布数据（切换到聚焦子图） */
  setGraph(data: GraphData): void;
  /** 高亮可疑节点（按 id）；高危边=source 与 target 同属可疑节点集（资金闭环） */
  highlight(suspiciousNodes: string[], suspiciousEdgeTypes: string[], suspiciousEdgeIds?: string[]): void;
  /** 清除高亮 / 变暗状态，恢复原样 */
  resetHighlight(): void;
  /** 时序过滤（指令3 · Time Slider）：仅展示 date<=maxTs 的 TRANSFER 边，null=展示全部 */
  filterByTime(maxTs: number | null): void;
  /** 重新拉取首屏图谱，用于网络错误后的显式恢复。 */
  reload(): void;
  /** 专业画布镜头控制。 */
  zoomIn(): void;
  zoomOut(): void;
  fitView(): void;
  /** 将镜头与高亮收束到一条可核对的证据关系。 */
  focusEvidence(edgeId: string): void;
}

/** GraphViewer 组件入参 */
export interface GraphViewerProps {
  /** 首屏核心骨干网络加载完成后回调，用于父组件初始化时序滑动条的时间区间 */
  onGraphLoaded?: (data: GraphData) => void;
  /** 节点选择回调，驱动右侧实体检查器。 */
  onNodeSelect?: (node: GraphNode) => void;
  onEdgeSelect?: (edge: GraphEdge) => void;
  /** 加载失败时向产品界面报告，不再只写控制台。 */
  onGraphError?: (message: string, fatal?: boolean) => void;
}

const GraphViewer = forwardRef<GraphViewerHandle, GraphViewerProps>(
  ({ onGraphLoaded, onNodeSelect, onEdgeSelect, onGraphError }, ref) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);
  const requestVersionRef = useRef(0);
  const graphClassRef = useRef<'public' | 'demo' | 'neo4j'>('public');

  // 动画循环句柄 + 当前高亮节点集合 + 缩放驱动的标签显隐状态
  const animRafRef = useRef<number | null>(null);
  const highlightIdsRef = useRef<string[]>([]); // 高危节点（呼吸光环用）
  const highlightEdgeIdsRef = useRef<string[]>([]); // 高危边（流动光效用）
  const hoverIdRef = useRef<string | null>(null); // 当前 hover 节点（账户悬浮文字用）

  // ---- 统一动画循环：边流动(lineDashOffset) + 嫌疑节点呼吸(halo) ----
  const stopAnimations = () => {
    if (animRafRef.current !== null) {
      cancelAnimationFrame(animRafRef.current);
      animRafRef.current = null;
    }
  };

  const startAnimations = () => {
    stopAnimations();
    const graph = graphRef.current;
    if (!graph) return;
    const start = performance.now();
    let frame = 0;

    const loop = (now: number) => {
      const g = graphRef.current;
      if (!g) return; // 已销毁，停止循环
      const t = (now - start) / 1000;
      let needsDraw = false;
      const phase = (Math.sin((t * 2 * Math.PI) / 1.6) + 1) / 2; // 1.6s 一个呼吸周期

      // 1) 高危边：红色激光流动（lineDashOffset）+ 微光呼吸，仅针对闭环那几条边
      const edgeIds = highlightEdgeIdsRef.current;
      if (frame % 2 === 0 && edgeIds.length) {
        const offset = -(t * 12) % 8; // 虚线周期 [4,4] → 以 8px 为步长向前流动
        g.updateEdgeData(
          edgeIds.map((id) => ({
            id,
            style: {
              lineDashOffset: offset,
              haloOpacity: 0.25 + phase * 0.15,
              haloLineWidth: 2 + phase * 2,
            },
          })),
        );
        needsDraw = true;
      }

      // 2) 嫌疑节点呼吸灯：光环周期性脉动（仅当存在高亮节点时）
      //    克制呼吸：光环透明度/宽度变化幅度极小，仅做「微微起伏」，避免廉价大红光晕
      const ids = highlightIdsRef.current;
      if (ids.length) {
        const existing = new Set(g.getNodeData().map((n) => String(n.id)));
        const valid = ids.filter((id) => existing.has(id));
        if (valid.length) {
          g.updateNodeData(
            valid.map((id) => ({
              id,
              style: {
                halo: true,
                haloFill: SUSPICIOUS_COLOR,
                haloOpacity: 0.25 + phase * 0.15, // 0.25 ~ 0.40
                haloLineWidth: 10 + phase * 4, // 10 ~ 14
              },
            })),
          );
          needsDraw = true;
        }
      }

      if (needsDraw) g.draw();
      frame += 1;
      animRafRef.current = requestAnimationFrame(loop);
    };
    animRafRef.current = requestAnimationFrame(loop);
  };

  // ---- 时序过滤（指令3 · Time Slider）----
  // 只改变边的透明度（不重排布局），让资金流沿时间轴「渐进浮现」：
  //   · maxTs == null  → 恢复默认透明度（0.18，展示全部）；
  //   · 回放模式       → 可见 TRANSFER 边提亮（0.55）突出资金流，未到时间戳的边隐藏（0），
  //                     其余关系边压暗（0.12）作背景衬托。
  // 同步清空所有边的状态（高亮/变暗），避免状态透明度覆盖时间过滤，二者语义互斥。
  const applyTimeFilter = (maxTs: number | null) => {
    const graph = graphRef.current;
    if (!graph) return;

    const edgeStates: Record<string, string[]> = {};
    const updates: Array<{ id: string; style: { opacity: number } }> = [];

    for (const e of graph.getEdgeData()) {
      const id = String(e.id);
      const type = (e.data?.edgeType as string) ?? '';
      edgeStates[id] = []; // 清空该边的状态

      if (maxTs != null && type === 'TRANSFER') {
        const ts = Date.parse((e.data?.date as string) ?? '');
        const visible = Number.isNaN(ts) ? true : ts <= maxTs;
        updates.push({ id, style: { opacity: visible ? 0.55 : 0 } });
      } else if (maxTs != null) {
        updates.push({ id, style: { opacity: 0.12 } });
      } else {
        updates.push({ id, style: { opacity: 0.18 } });
      }
    }

    graph.setElementState(edgeStates, true).catch(() => {});
    graph.updateEdgeData(updates);
    graph.draw();
  };

  // ---- 初始化 G6 实例（仅挂载时执行一次） ----
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const isNarrow = container.clientWidth < 640;
    const isCompactCanvas = container.clientWidth < 1280;

    const graph = new Graph({
      container,
      // 只在内容真正溢出时缩小适配；禁止把稀疏图强行放大到四个屏幕边缘。
      autoFit: { type: 'view', options: { when: 'overflow' } },
      padding: [88, 44, isCompactCanvas ? 235 : 170, 44],
      zoom: 1.15,
      zoomRange: [MIN_ZOOM, MAX_ZOOM], // 全局缩放锁定（Prompt 1）：0.1 ~ 1.5，禁止无限放大
      behaviors: [
        'drag-canvas',
        { type: 'zoom-canvas', key: 'wheel-zoom', trigger: ['wheel'] },
        { type: 'zoom-canvas', key: 'pinch-zoom', trigger: ['pinch'] },
      ],
      // 高级物理引擎：强斥力 + 防重叠(节点尺寸放大) + 中心引力 → 优雅球状发散
      layout: layoutFor('public', isNarrow),
      node: {
        style: {
          zIndex: 10, // 节点层级高于边（所有文字/节点绝对浮在连线之上）
          size: (d: any) => d.data?.data_class === 'public_disclosure'
            ? (d.id === 'CATL-300750' ? 34 : d.data?.nodeType === 'Company' ? 24 : 20)
            : SIZE_BY_TYPE[d.data?.nodeType] ?? 24,
          fill: (d: any) => FILL_BY_TYPE[d.data?.nodeType] ?? NODE_FILL,
          fillOpacity: 0.9,
          stroke: (d: any) => COLOR_BY_TYPE[d.data?.nodeType] ?? '#71717a',
          lineWidth: (d: any) =>
            d.data?.nodeType === 'Company' ? 1.5 : 1.2,
          icon: true,
          iconSrc: (d: any) => ICON_BY_TYPE[d.data?.nodeType] ?? ICON_BY_TYPE.Company,
          iconWidth: (d: any) => d.data?.nodeType === 'Account' ? 10 : d.data?.nodeType === 'Device' ? 11 : 13,
          iconHeight: (d: any) => d.data?.nodeType === 'Account' ? 10 : d.data?.nodeType === 'Device' ? 11 : 13,
          iconOpacity: 0.92,
          // 仅公司带蓝色外发光（halo = v5 内置辉光，形成「双层浅蓝环」）；账户微点无光晕
          halo: (d: any) => d.data?.nodeType === 'Company',
          haloFill: (d: any) => COLOR_BY_TYPE[d.data?.nodeType] ?? '#8AB4F8',
          haloOpacity: 0.16,
          haloLineWidth: 9,
          cursor: 'pointer',
          // 标签：账户默认零视觉（仅 hover 悬浮），公司/人常驻
          labelText: (d: any) => {
            const type = d.data?.nodeType;
            if (isNarrow && (type === 'Person' || type === 'Device' || type === 'device_cluster')) return '';
            return shouldShowLabel(d.data) ? formatNodeLabel(d.data?.display_name ?? d.data?.label ?? d.id, type) : '';
          },
          // 中文语义折行：formatNodeLabel 已按公司名第 6~8 字手动插入 \n；
          // 关闭原生 wordWrap 避免二次断行，labelMaxLines 放开到 2（默认 1 会截断手动 \n）。
          labelWordWrap: false,
          labelMaxLines: 2,
          labelFill: '#BDC1C6', // 标签文字：Google 中性灰
          labelFontSize: LABEL_FONT_SIZE,
          labelPlacement: (d: any) => labelPlacementFor(String(d.id)),
          labelOffsetY: 6,
          // 去黑块化：取消实心底板，改用微弱羽化阴影托底，保证文字可读又不遮挡
          labelBackground: false,
          labelShadowColor: 'rgba(0, 0, 0, 0.85)',
          labelShadowBlur: 4,
          labelShadowOffsetX: 0,
          labelShadowOffsetY: 1,
        },
        state: {
          // 嫌疑节点：保持原尺寸 + 克制的红色描边（呼吸光环由动画循环统一驱动）。
          // 关键：保持深色底、绝不用大红填充（高级感源于「克制」），文字也不随节点放大。
          highlight: {
            size: 28,
            fill: '#5C2730',
            fillOpacity: 0.96,
            stroke: SUSPICIOUS_COLOR,
            lineWidth: 2.2,
            halo: true,
            haloFill: SUSPICIOUS_COLOR,
            haloOpacity: 0.26,
            haloLineWidth: 12,
            labelFill: SUSPICIOUS_COLOR,
            labelFontSize: LABEL_FONT_SIZE, // 锁定字号，与普通态一致，绝不野蛮放大
          },
          selected: {
            stroke: '#AECBFA',
            lineWidth: 3,
            halo: true,
            haloFill: '#8AB4F8',
            haloOpacity: 0.22,
            haloLineWidth: 12,
          },
          // 非相关节点：压到极暗，推入背景
          dimmed: { opacity: 0.1 },
        },
      },
      edge: {
        style: {
          zIndex: 0, // 连线沉底：低于节点层级，绝不穿过标签文字
          stroke: (d: any) => EDGE_COLOR_BY_TYPE[d.data?.edgeType] ?? EDGE_BACKGROUND,
          lineWidth: (d: any) => d.data?.edgeType === 'TRANSFER' ? 1.35 : 1.05,
          opacity: 0.82,
          lineDash: [], // 实线（无流动动画，退化为背景）
          lineDashOffset: 0,
          endArrow: (d: any) => d.data?.data_class === 'public_disclosure',
          endArrowType: 'triangle',
          endArrowSize: 4,
          endArrowFill: (d: any) => EDGE_COLOR_BY_TYPE[d.data?.edgeType] ?? EDGE_BACKGROUND,
          labelText: (d: any) => formatEdgeEvidence(d.data),
          labelFill: '#FCE8E6',
          labelFontSize: 10,
          labelFontWeight: 600,
          labelPlacement: 'center',
          labelAutoRotate: false,
          labelBackground: true,
          labelBackgroundFill: 'rgba(44, 24, 27, 0.96)',
          labelBackgroundStroke: 'rgba(242, 139, 130, 0.52)',
          labelBackgroundLineWidth: 1,
          labelBackgroundRadius: 8,
          labelBackgroundPadding: [3, 7],
        },
        state: {
          // 高危资金闭环：仅 source 与 target 同属可疑节点集时才命中——激光红虚线 + 呼吸微光
          highlight: {
            stroke: LOOP_EDGE_COLOR,
            lineWidth: 2.5,
            opacity: 1,
            lineDash: [4, 4],
            halo: true,
            haloFill: LOOP_EDGE_COLOR,
            haloOpacity: 0.3,
            haloLineWidth: 2,
            endArrow: true,
            endArrowType: 'triangle',
            endArrowSize: 6,
            endArrowFill: LOOP_EDGE_COLOR,
            endArrowStroke: LOOP_EDGE_COLOR,
          },
          publicEvidence: {
            stroke: '#AECBFA',
            lineWidth: 2.5,
            opacity: 1,
            halo: true,
            haloFill: '#8AB4F8',
            haloOpacity: 0.22,
            haloLineWidth: 2,
            endArrow: true,
            endArrowType: 'triangle',
            endArrowSize: 6,
            endArrowFill: '#AECBFA',
            endArrowStroke: '#AECBFA',
          },
          dimmed: { opacity: 0.06 },
        },
      },
    });

    graphRef.current = graph;
    graph.render();

    // 挂载时加载首屏核心骨干网络
    // 注：React StrictMode 开发模式下 effect 会「挂载→卸载→再挂载」执行两次，
    // 首次卸载会 destroy graph，若异步 fetch 在其后 resolve 则会对已销毁实例操作而报错。
    // 故用 disposed 标志位拦截卸载后的异步回调，避免「加载全景图失败」。
    let disposed = false;
    let settleTimer: number | undefined;
    const requestVersion = ++requestVersionRef.current;
    fetchPublicCase()
      .then(async (data) => {
        if (disposed || requestVersion !== requestVersionRef.current) return;
        graphClassRef.current = 'public';
        graph.setData(toG6Data(data));
        await graph.render();
        if (disposed || requestVersion !== requestVersionRef.current) return;
        await graph.fitView({ when: 'overflow' });
        onGraphLoaded?.(data);
        // d3-force 在首帧 render resolve 后仍可能继续收敛；布局稳定后再做一次镜头校准，
        // 避免比赛大屏首开时节点被推到上下边界外，用户无需手动点击“适配全图”。
        settleTimer = window.setTimeout(() => {
          if (!disposed) void graph.fitView({ when: 'overflow' }, { duration: 420 }).catch(() => {});
        }, 650);
        startAnimations();
      })
      .catch((err) => {
        if (disposed || requestVersion !== requestVersionRef.current) return;
        console.error('[GraphViewer] 加载核心骨干网络失败：', err);
        onGraphError?.('图谱数据暂时不可用，请检查服务连接后重试。');
      });

    // 点击节点 → 按需扩展 2 度邻域，并将新节点/边增量合并进画布（懒加载）
    graph.on('node:click', (e: any) => {
      if (disposed) return; // 图已销毁，忽略残留的点击事件
      const id = String(e.target?.id ?? '');
      if (!id) return;
      const selected = graph.getNodeData().find((node) => String(node.id) === id);
      if (selected) {
        const raw = { ...(selected.data ?? {}) } as Record<string, unknown>;
        const label = String(raw.label ?? id);
        const type = String(raw.nodeType ?? 'Unknown');
        delete raw.label;
        delete raw.nodeType;
        delete raw.hovered;
        delete raw.display_name;
        delete raw.public_key_label;
        onNodeSelect?.({ id, label, type, properties: raw });
      }
      const selectionStates: Record<string, string[]> = {};
      for (const node of graph.getNodeData()) {
        const nodeId = String(node.id);
        selectionStates[nodeId] = nodeId === id ? ['selected'] : [];
      }
      graph.setElementState(selectionStates, true).catch(() => {});
      if (graphClassRef.current !== 'neo4j') return;
      fetchGraphExpand(id, 2)
        .then((data) => {
          const g = graphRef.current;
          if (!g) return;
          // 与画布现有元素按 id 去重，只追加真正的新增部分
          const currentNodes = new Set(g.getNodeData().map((n) => String(n.id)));
          const currentEdges = new Set(g.getEdgeData().map((e) => String(e.id)));
          const converted = toG6Data(data);
          const newNodes = converted.nodes.filter((n) => !currentNodes.has(n.id));
          const newEdges = converted.edges.filter((e) => !currentEdges.has(e.id));
          if (!newNodes.length && !newEdges.length) return;
          g.addData({ nodes: newNodes, edges: newEdges });
          g.render();
        })
        .catch((err) => {
          console.error('[GraphViewer] 扩展节点失败：', err);
          onGraphError?.('无法展开该实体的关联网络，请稍后重试。', false);
        });
    });

    graph.on('edge:click', (event: any) => {
      if (disposed) return;
      const id = String(event.target?.id ?? '');
      const edge = graph.getEdgeData().find((item) => String(item.id) === id);
      if (!edge) return;
      const source = String((edge.source as any)?.id ?? edge.source);
      const target = String((edge.target as any)?.id ?? edge.target);
      const data = { ...(edge.data ?? {}) } as Record<string, unknown>;
      const type = String(data.edgeType ?? 'UNKNOWN');
      delete data.edgeType;
      delete data.label;
      delete data.showRiskLabel;
      onEdgeSelect?.({ id, source, target, type, label: type, properties: data });
    });

    // Hover 显隐账户标签（LOD）：pointerenter/leave 切换 data.hovered，触发账户标签显隐
    const applyHover = (id: string | null) => {
      const g = graphRef.current;
      if (!g) return;
      const prev = hoverIdRef.current;
      if (prev === id) return;
      hoverIdRef.current = id;
      const updates: Array<{ id: string; data: { hovered: boolean } }> = [];
      if (prev) updates.push({ id: prev, data: { hovered: false } });
      if (id) updates.push({ id, data: { hovered: true } });
      g.updateNodeData(updates);
      g.draw();
    };
    graph.on('node:pointerenter', (e: any) => {
      if (disposed) return;
      applyHover(String(e.target?.id ?? ''));
    });
    graph.on('node:pointerleave', () => {
      if (disposed) return;
      applyHover(null);
    });

    // 自适应：容器尺寸变化（拖拽分屏/投屏）时防抖重算画布尺寸
    let resizeTimer: number | undefined;
    const resizeObserver = new ResizeObserver(() => {
      if (disposed) return;
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        if (disposed) return;
        const { clientWidth: w, clientHeight: h } = container;
        // display:none 期间 ResizeObserver 会报告 0×0；不得用这个瞬时尺寸改写 G6 镜头。
        if (w < 240 || h < 240) return;
        try {
          graph.resize();
        } catch {
          if (w && h) graph.setSize(w, h);
        }
        // 手机旋转、分屏与抽屉切换后重新校准镜头；窄屏必须完整纳入视口，
        // 桌面端只在溢出时缩小，避免稀疏图被无意义地放大。
        void graph.fitView({ when: 'overflow' }, { duration: 240 }).catch(() => {});
      }, 150);
    });
    resizeObserver.observe(container);

    return () => {
      disposed = true;
      stopAnimations();
      resizeObserver.disconnect();
      window.clearTimeout(resizeTimer);
      window.clearTimeout(settleTimer);
      graph.destroy();
      graphRef.current = null;
    };
    // 仅初始化一次；内部仅引用 ref，无外部依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- 对外命令 ----
  useImperativeHandle(
    ref,
    () => ({
      setGraph(data: GraphData) {
        const graph = graphRef.current;
        if (!graph) return;
        const version = ++requestVersionRef.current;
        // 切换数据源前终止上一张图的力导向迭代与逐帧动画。
        // 否则旧布局仍会尝试更新已经从画布移除的节点。
        graph.stopLayout();
        stopAnimations();
        graphClassRef.current = data.meta?.source === 'public_disclosure' ? 'public' : data.meta?.source === 'neo4j' ? 'neo4j' : 'demo';
        graph.setOptions({ layout: layoutFor(graphClassRef.current === 'public' ? 'public' : 'simulation', (containerRef.current?.clientWidth ?? 1440) < 640) });
        highlightIdsRef.current = [];
        highlightEdgeIdsRef.current = [];
        hoverIdRef.current = null;
        graph.setData(toG6Data(data));
        void graph.render().then(() => {
          if (version === requestVersionRef.current) startAnimations();
        }).catch(() => onGraphError?.('图谱切换失败，请重试。'));
      },
      highlight(suspiciousNodes: string[], _suspiciousEdgeTypes: string[], suspiciousEdgeIds = []) {
        const graph = graphRef.current;
        if (!graph) return;

        // 只对画布中真实存在的节点做高亮，忽略子图外的可疑 id，从根源杜绝 G6「Node not found」
        const existingIds = new Set(graph.getNodeData().map((n) => String(n.id)));
        const nodeSet = new Set(suspiciousNodes.filter((id) => existingIds.has(id)));
        const exactEdgeSet = new Set(suspiciousEdgeIds);
        const nodeStates: Record<string, string[]> = {};
        const edgeStates: Record<string, string[]> = {};
        const nodeEvidenceUpdates: Array<{ id: string; data: { showRiskLabel: boolean } }> = [];
        const edgeEvidenceUpdates: Array<{ id: string; data: { showRiskLabel: boolean } }> = [];
        const ids: string[] = [];
        const edgeIds: string[] = [];

        // 有精确证据边时压低上下文，避免大量无关节点争夺注意力。
        for (const n of graph.getNodeData()) {
          const id = String(n.id);
          const hit = nodeSet.has(id);
          nodeStates[id] = hit ? ['highlight'] : exactEdgeSet.size ? ['dimmed'] : [];
          nodeEvidenceUpdates.push({ id, data: { showRiskLabel: hit } });
          if (hit) ids.push(id);
        }
        // 边：仅当 source 与 target 同属可疑节点集（资金闭环）才判为高危，其余保持透明背景
        for (const e of graph.getEdgeData()) {
          const id = String(e.id);
          const hit = exactEdgeSet.has(id);
          edgeStates[id] = hit ? ['highlight'] : exactEdgeSet.size ? ['dimmed'] : [];
          edgeEvidenceUpdates.push({ id, data: { showRiskLabel: hit } });
          if (hit) edgeIds.push(id);
        }

        // setElementState 为异步方法，内部逐个 id 查元素；附 .catch 兜底，杜绝未捕获异常
        graph.updateNodeData(nodeEvidenceUpdates);
        graph.updateEdgeData(edgeEvidenceUpdates);
        graph.setElementState(nodeStates, true).catch(() => {});
        graph.setElementState(edgeStates, true).catch(() => {});

        highlightIdsRef.current = ids;
        highlightEdgeIdsRef.current = edgeIds;
        graph.draw();

        // 镜头运镜：平滑聚焦到高危闭环，并让出左侧面板安全区
        void focusOnSuspicious(graph, ids);
      },
      resetHighlight() {
        const graph = graphRef.current;
        if (!graph) return;
        highlightIdsRef.current = [];
        highlightEdgeIdsRef.current = [];
        hoverIdRef.current = null;

        const nodeStates: Record<string, string[]> = {};
        const edgeStates: Record<string, string[]> = {};
        for (const n of graph.getNodeData()) nodeStates[String(n.id)] = [];
        for (const e of graph.getEdgeData()) edgeStates[String(e.id)] = [];

        // 清除光环、复位悬停标志（账户悬浮文字随之隐藏）
        graph.updateNodeData(
          graph.getNodeData().map((n) => ({
            id: String(n.id),
            data: { hovered: false, showRiskLabel: false },
            style: { halo: false },
          })),
        );
        graph.updateEdgeData(graph.getEdgeData().map((e) => ({ id: String(e.id), data: { showRiskLabel: false } })));
        graph.setElementState(nodeStates);
        graph.setElementState(edgeStates);
        graph.draw();
      },
      filterByTime(maxTs: number | null) {
        applyTimeFilter(maxTs);
      },
      reload() {
        const graph = graphRef.current;
        if (!graph) return;
        const requestVersion = ++requestVersionRef.current;
        graph.stopLayout();
        stopAnimations();
        onGraphError?.('');
        fetchPublicCase()
          .then(async (data) => {
            if (requestVersion !== requestVersionRef.current) return;
            graphClassRef.current = 'public';
            graph.setOptions({ layout: layoutFor('public', (containerRef.current?.clientWidth ?? 1440) < 640) });
            graph.setData(toG6Data(data));
            await graph.render();
            if (requestVersion !== requestVersionRef.current) return;
            startAnimations();
            onGraphLoaded?.(data);
          })
          .catch(() => { if (requestVersion === requestVersionRef.current) onGraphError?.('图谱数据暂时不可用，请检查服务连接后重试。'); });
      },
      zoomIn() {
        const graph = graphRef.current;
        if (!graph) return;
        void graph.zoomTo(Math.min(MAX_ZOOM, graph.getZoom() * 1.22), { duration: 220, easing: 'ease-in-out-cubic' });
      },
      zoomOut() {
        const graph = graphRef.current;
        if (!graph) return;
        void graph.zoomTo(Math.max(MIN_ZOOM, graph.getZoom() / 1.22), { duration: 220, easing: 'ease-in-out-cubic' });
      },
      fitView() {
        const graph = graphRef.current;
        if (!graph) return;
        void graph.fitView({ when: 'always' });
      },
      focusEvidence(edgeId: string) {
        const graph = graphRef.current;
        if (!graph) return;
        const edge = graph.getEdgeData().find((item) => String(item.id) === edgeId);
        if (!edge) return;
        const source = String((edge.source as any)?.id ?? edge.source);
        const target = String((edge.target as any)?.id ?? edge.target);
        const isPublic = edge.data?.data_class === 'public_disclosure';
        const nodeStates: Record<string, string[]> = {};
        const edgeStates: Record<string, string[]> = {};
        for (const node of graph.getNodeData()) nodeStates[String(node.id)] = [source, target].includes(String(node.id)) ? [isPublic ? 'selected' : 'highlight'] : ['dimmed'];
        for (const item of graph.getEdgeData()) edgeStates[String(item.id)] = String(item.id) === edgeId ? [isPublic ? 'publicEvidence' : 'highlight'] : ['dimmed'];
        graph.updateEdgeData(graph.getEdgeData().map((item) => ({ id: String(item.id), data: { showRiskLabel: String(item.id) === edgeId } })));
        graph.setElementState(nodeStates, true).catch(() => {});
        graph.setElementState(edgeStates, true).catch(() => {});
        highlightIdsRef.current = isPublic ? [] : [source, target];
        highlightEdgeIdsRef.current = isPublic ? [] : [edgeId];
        graph.draw();
        void focusOnSuspicious(graph, [source, target]);
      },
    }),
    [onGraphError, onGraphLoaded],
  );

  return (
    <div
      ref={containerRef}
      className="graph-canvas-surface h-full w-full"
      onPointerMove={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        event.currentTarget.style.setProperty('--spot-x', `${event.clientX - rect.left}px`);
        event.currentTarget.style.setProperty('--spot-y', `${event.clientY - rect.top}px`);
        event.currentTarget.style.setProperty('--spot-strength', '1');
      }}
      onPointerLeave={(event) => {
        event.currentTarget.style.setProperty('--spot-strength', '0');
      }}
    />
  );
  },
);

GraphViewer.displayName = 'GraphViewer';
export default GraphViewer;
