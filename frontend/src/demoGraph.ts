import type { GraphData, SchemaResponse } from './types';

/** 后端完全不可达时的前端最后一道可运行保障。 */
const BASE_DEMO_GRAPH: GraphData = {
  meta: {
    source: 'demo',
    degraded: true,
    message: 'FastAPI 当前未连接，页面正在使用前端内置演示图谱。',
  },
  nodes: [
    { id: 'LOOP_A', label: '辰星贸易', type: 'Company', properties: { industry: '批发业', risk_flag: true } },
    { id: 'LOOP_B', label: '远航供应链', type: 'Company', properties: { industry: '供应链服务', risk_flag: true } },
    { id: 'LOOP_C', label: '嘉禾实业', type: 'Company', properties: { industry: '商务服务', risk_flag: true } },
    { id: 'PERSON_A', label: '陈明', type: 'Person', properties: { risk_flag: false } },
    { id: 'PERSON_B', label: '林涛', type: 'Person', properties: { risk_flag: false } },
    { id: 'PERSON_C', label: '周岚', type: 'Person', properties: { risk_flag: false } },
    { id: 'ACCT_LOOP_A', label: '6222***1001', type: 'Account', properties: { bank: '华夏商业银行', balance: 312000 } },
    { id: 'ACCT_LOOP_B', label: '6222***1002', type: 'Account', properties: { bank: '东海商业银行', balance: 286000 } },
    { id: 'ACCT_LOOP_C', label: '6222***1003', type: 'Account', properties: { bank: '华夏商业银行', balance: 341000 } },
    { id: 'PERSON_BOSS', label: '高远', type: 'Person', properties: { risk_flag: true } },
    { id: 'HOLDING_X', label: '启明控股', type: 'Company', properties: { industry: '投资管理', risk_flag: true } },
    { id: 'SHELL_Y', label: '瀚海咨询', type: 'Company', properties: { industry: '咨询服务', risk_flag: true } },
    { id: 'TARGET_Z', label: '融达科技', type: 'Company', properties: { industry: '软件服务', risk_flag: true } },
    { id: 'DEVICE_SHARED', label: '共享登录设备', type: 'Device', properties: { ip: '10.24.18.76', risk_flag: true } },
  ],
  edges: [
    { id: 'legal-a', source: 'PERSON_A', target: 'LOOP_A', label: 'LEGAL_REP', type: 'LEGAL_REP', properties: {} },
    { id: 'legal-b', source: 'PERSON_B', target: 'LOOP_B', label: 'LEGAL_REP', type: 'LEGAL_REP', properties: {} },
    { id: 'legal-c', source: 'PERSON_C', target: 'LOOP_C', label: 'LEGAL_REP', type: 'LEGAL_REP', properties: {} },
    { id: 'account-a', source: 'LOOP_A', target: 'ACCT_LOOP_A', label: 'HAS_ACCOUNT', type: 'HAS_ACCOUNT', properties: {} },
    { id: 'account-b', source: 'LOOP_B', target: 'ACCT_LOOP_B', label: 'HAS_ACCOUNT', type: 'HAS_ACCOUNT', properties: {} },
    { id: 'account-c', source: 'LOOP_C', target: 'ACCT_LOOP_C', label: 'HAS_ACCOUNT', type: 'HAS_ACCOUNT', properties: {} },
    { id: 'transfer-a-b', source: 'ACCT_LOOP_A', target: 'ACCT_LOOP_B', label: 'TRANSFER', type: 'TRANSFER', properties: { amount: 1850000, date: '2026-08-18T09:12:00+08:00' } },
    { id: 'transfer-b-c', source: 'ACCT_LOOP_B', target: 'ACCT_LOOP_C', label: 'TRANSFER', type: 'TRANSFER', properties: { amount: 1815000, date: '2026-08-18T12:47:00+08:00' } },
    { id: 'transfer-c-a', source: 'ACCT_LOOP_C', target: 'ACCT_LOOP_A', label: 'TRANSFER', type: 'TRANSFER', properties: { amount: 1798000, date: '2026-08-18T16:31:00+08:00' } },
    { id: 'owns-boss-holding', source: 'PERSON_BOSS', target: 'HOLDING_X', label: 'OWNS', type: 'OWNS', properties: { ratio: 0.82 } },
    { id: 'owns-holding-shell', source: 'HOLDING_X', target: 'SHELL_Y', label: 'OWNS', type: 'OWNS', properties: { ratio: 0.67 } },
    { id: 'owns-shell-target', source: 'SHELL_Y', target: 'TARGET_Z', label: 'OWNS', type: 'OWNS', properties: { ratio: 0.74 } },
    { id: 'login-boss', source: 'PERSON_BOSS', target: 'DEVICE_SHARED', label: 'LOGGED_IN', type: 'LOGGED_IN', properties: { date: '2026-08-17T21:08:00+08:00' } },
    { id: 'login-a', source: 'PERSON_A', target: 'DEVICE_SHARED', label: 'LOGGED_IN', type: 'LOGGED_IN', properties: { date: '2026-08-18T08:56:00+08:00' } },
  ],
};

function buildLocalDemoGraph(): GraphData {
  const nodes = [...BASE_DEMO_GRAPH.nodes];
  const edges = [...BASE_DEMO_GRAPH.edges];
  const companies = ['北辰数科', '海岳物流', '锦川实业', '云岭商贸', '澄海智造', '华曜资本', '凌峰供应链', '新桥医药', '星港能源', '柏舟咨询', '远景材料', '开源数据', '融信服务', '衡泰工程', '蓝湾贸易', '安澜科技', '卓越投资', '启航设备'];
  const people = ['许安', '沈悦', '韩哲', '顾宁', '唐可', '魏然', '宋扬', '陆清', '白川', '叶蓁', '江临', '罗嘉', '程野', '苏禾', '杜衡', '夏一', '方舟', '余森'];
  const banks = ['华夏商业银行', '东海商业银行', '城市联合银行', '中州商业银行'];
  companies.forEach((companyName, offset) => {
    const index = offset + 1;
    const suffix = String(index).padStart(2, '0');
    const nextSuffix = String(index % companies.length + 1).padStart(2, '0');
    const deviceSuffix = String(offset % 4 + 1).padStart(2, '0');
    const risk = [3, 7, 11, 15].includes(index);
    nodes.push(
      { id: `DEMO_COMPANY_${suffix}`, label: companyName, type: 'Company', properties: { industry: ['科技服务', '供应链', '先进制造'][index % 3], registered_capital: 500 + index * 125, risk_flag: risk, display_priority: 'context' } },
      { id: `DEMO_PERSON_${suffix}`, label: people[offset], type: 'Person', properties: { risk_flag: risk && index % 2 === 1, display_priority: 'context' } },
      { id: `DEMO_ACCOUNT_${suffix}`, label: `6222***${String(index).padStart(4, '0')}`, type: 'Account', properties: { bank: banks[index % banks.length], balance: 86000 + index * 17300, display_priority: 'context' } },
    );
    edges.push(
      { id: `demo-legal-${suffix}`, source: `DEMO_PERSON_${suffix}`, target: `DEMO_COMPANY_${suffix}`, label: 'LEGAL_REP', type: 'LEGAL_REP', properties: {} },
      { id: `demo-account-${suffix}`, source: `DEMO_COMPANY_${suffix}`, target: `DEMO_ACCOUNT_${suffix}`, label: 'HAS_ACCOUNT', type: 'HAS_ACCOUNT', properties: {} },
      { id: `demo-transfer-${suffix}`, source: `DEMO_ACCOUNT_${suffix}`, target: `DEMO_ACCOUNT_${nextSuffix}`, label: 'TRANSFER', type: 'TRANSFER', properties: { amount: 98000 + index * 27100, date: `2026-08-${String(index % 18 + 1).padStart(2, '0')}T${String(8 + index % 10).padStart(2, '0')}:24:00+08:00` } },
      { id: `demo-login-${suffix}`, source: `DEMO_PERSON_${suffix}`, target: `DEMO_DEVICE_${deviceSuffix}`, label: 'LOGGED_IN', type: 'LOGGED_IN', properties: { date: `2026-08-${String(index % 18 + 1).padStart(2, '0')}T21:08:00+08:00` } },
    );
    if (index < companies.length) edges.push({ id: `demo-owns-${suffix}`, source: `DEMO_COMPANY_${suffix}`, target: `DEMO_COMPANY_${String(index + 1).padStart(2, '0')}`, label: 'OWNS', type: 'OWNS', properties: { ratio: Number((0.18 + index % 5 * 0.09).toFixed(2)) } });
  });
  for (let index = 1; index <= 4; index += 1) {
    const suffix = String(index).padStart(2, '0');
    nodes.push({ id: `DEMO_DEVICE_${suffix}`, label: `关联终端 ${suffix}`, type: 'Device', properties: { ip: `10.38.${index}.17`, device_type: 'Desktop', risk_flag: [2, 4].includes(index), display_priority: 'context' } });
  }
  edges.push(
    { id: 'demo-bridge-core-a', source: 'LOOP_C', target: 'DEMO_COMPANY_01', label: 'OWNS', type: 'OWNS', properties: { ratio: 0.24 } },
    { id: 'demo-bridge-core-b', source: 'TARGET_Z', target: 'DEMO_COMPANY_10', label: 'OWNS', type: 'OWNS', properties: { ratio: 0.31 } },
    { id: 'demo-transfer-core', source: 'ACCT_LOOP_C', target: 'DEMO_ACCOUNT_06', label: 'TRANSFER', type: 'TRANSFER', properties: { amount: 720000, date: '2026-08-18T18:42:00+08:00' } },
  );
  return { ...BASE_DEMO_GRAPH, nodes, edges };
}

export const LOCAL_DEMO_GRAPH = buildLocalDemoGraph();

export const LOCAL_GRAPH_SCHEMA: SchemaResponse = {
  node_types: [
    { label: 'Company', name: '公司', color: '#8AB4F8', properties: [] },
    { label: 'Person', name: '自然人', color: '#81C995', properties: [] },
    { label: 'Account', name: '账户', color: '#FDD663', properties: [] },
    { label: 'Device', name: '设备', color: '#C58AF9', properties: [] },
  ],
  edge_types: [
    { type: 'OWNS', name: '持股', color: '#8AB4F8', properties: ['ratio'] },
    { type: 'CONTROLLING_SHAREHOLDER', name: '控股股东', color: '#81C995', properties: ['ratio', 'share_count'] },
    { type: 'DISCLOSED_HOLDING', name: '报告期末持股', color: '#81C995', properties: ['ratio', 'share_count'] },
    { type: 'GROUP_SUBSIDIARY', name: '集团子公司', color: '#8AB4F8', properties: ['ratio'] },
    { type: 'PARENT_INVESTMENT', name: '母公司投资账面值', color: '#C58AF9', properties: ['book_value_thousand_cny'] },
    { type: 'LEGAL_REP', name: '法人代表', color: '#81C995', properties: [] },
    { type: 'TRANSFER', name: '转账流水', color: '#FDD663', properties: ['amount', 'date'] },
    { type: 'HAS_ACCOUNT', name: '开户关系', color: '#C58AF9', properties: [] },
    { type: 'LOGGED_IN', name: '登录关系', color: '#FF9845', properties: ['date'] },
  ],
};

export function expandLocalDemoGraph(nodeId: string, depth: number): GraphData {
  const selected = new Set([nodeId]);
  let frontier = new Set([nodeId]);
  for (let level = 0; level < Math.max(1, Math.min(2, depth)); level += 1) {
    const neighbours = new Set<string>();
    for (const edge of LOCAL_DEMO_GRAPH.edges) {
      if (frontier.has(edge.source)) neighbours.add(edge.target);
      if (frontier.has(edge.target)) neighbours.add(edge.source);
    }
    frontier = new Set([...neighbours].filter((id) => !selected.has(id)));
    neighbours.forEach((id) => selected.add(id));
  }
  return {
    ...LOCAL_DEMO_GRAPH,
    nodes: LOCAL_DEMO_GRAPH.nodes.filter((node) => selected.has(node.id)),
    edges: LOCAL_DEMO_GRAPH.edges.filter((edge) => selected.has(edge.source) && selected.has(edge.target)),
  };
}
