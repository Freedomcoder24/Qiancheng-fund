/**
 * API 封装层（Phase 5 从 app.js 拆分）
 *
 * 所有后端请求集中在这里，业务代码不直接写 fetch。
 * 约定：成功返回 JSON；失败抛 Error，message 为"用户可读"的提示文案，
 * 调试信息（状态码、响应体）只进 console，不暴露到界面。
 */

// 通用请求封装
async function request(url, options = {}) {
  let resp;
  try {
    resp = await fetch(url, options);
  } catch (err) {
    console.error('网络请求失败：', url, err);
    throw new Error('网络异常，请确认后端服务已启动');
  }

  let data = null;
  try {
    data = await resp.json();
  } catch (err) {
    // 响应体不是 JSON 时忽略，走下面的状态码判断
  }

  if (!resp.ok) {
    console.error(`API ${resp.status}: ${url}`, data);
    // 后端 detail 已经是中文可读文案，优先使用
    const msg = data && data.detail ? data.detail : `请求失败（HTTP ${resp.status}）`;
    throw new Error(msg);
  }
  return data;
}

// 把错误转成界面提示：数据源/网络类错误统一为友好文案，其余原样展示
function friendlyError(err) {
  const msg = err && err.message ? err.message : String(err);
  if (msg.includes('HTTP 503') || msg.includes('网络异常') || msg.includes('接口')) {
    return '基金数据暂时无法获取，请稍后重试';
  }
  return msg;
}

// 后端 API 集中定义（Phase 1~4 已有的接口，全部复用，无新增）
const api = {
  // 账户概览
  getSummary: () => request('/api/portfolio/summary'),
  // 持仓 CRUD
  getHoldings: () => request('/api/portfolio/holdings'),
  getHolding: (id) => request(`/api/portfolio/holdings/${id}`),
  createHolding: (body) =>
    request('/api/portfolio/holdings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateHolding: (id, body) =>
    request(`/api/portfolio/holdings/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteHolding: (id) =>
    request(`/api/portfolio/holdings/${id}`, { method: 'DELETE' }),
  // 持仓历史模拟市值（收益趋势图数据源）
  getSimHistory: (id, period) =>
    request(`/api/portfolio/holdings/${id}/history?period=${period}`),
  // 基金区间历史表现（收益 + 最大回撤）
  getPerformance: (code, period) =>
    request(`/api/funds/${code}/performance?period=${period}`),
  // 基金查询 / 历史净值 / 搜索（Phase 2 保留能力）
  getFundDetail: (code) => request(`/api/funds/${code}`),
  getFundHistory: (code) =>
    request(`/api/funds/${code}/history?page=1&page_size=20`),
  searchFunds: (keyword) =>
    request(`/api/funds/search?keyword=${encodeURIComponent(keyword)}`),
  // AI 配置状态（Phase 6，只返回是否已配置，不含 API Key）
  getAIStatus: () => request('/api/ai/status'),
  // AI 账户分析（生成较慢，按钮侧已有 loading 提示）
  runAIAnalysis: () =>
    request('/api/ai/analysis', { method: 'POST' }),
  // Web AI API 配置（Phase 9）：读取 / 保存 / 清除 / 测试连接
  getAIConfig: () => request('/api/ai/config'),
  saveAIConfig: (body) =>
    request('/api/ai/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  clearAIConfig: () => request('/api/ai/config', { method: 'DELETE' }),
  testAIConfig: (body) =>
    request('/api/ai/config/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  // 智能监控检查（Phase 7，固定规则，返回提醒列表）
  getMonitor: () => request('/api/monitor'),
  // 最近一次自动检查快照（Phase 8 定时任务保存，未执行过时返回 null）
  getAutoLatest: () => request('/api/auto/latest'),
  // 最新 AI 每日报告（Phase 8，每天一份，没有时返回 null）
  getDailyReport: () => request('/api/auto/daily-report'),
  // ---- Phase 12：投资目标 / 候选池 ----
  // 目标设置状态
  getGoal: () => request('/api/goal'),
  // 保存目标收益率（%）
  saveGoal: (rate) =>
    request('/api/goal', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_return_rate: rate }),
    }),
  // 目标进度量化分析（当前收益 → 距离目标 → 风险指标）
  getGoalAnalysis: () => request('/api/goal/analysis'),
  // AI 目标结论（只解释数据与风险，不产生交易指令）
  runGoalConclusion: () =>
    request('/api/ai/goal-conclusion', { method: 'POST' }),
  // 候选池（符合当前量化筛选条件的候选基金，不是推荐）
  getCandidates: (count) => request(`/api/funds/candidates?count=${count || 20}`),
  // ---- Phase 13：候选池筛选条件（存库，重启不丢） ----
  getCandidateFilter: () => request('/api/candidates/filter'),
  saveCandidateFilter: (body) =>
    request('/api/candidates/filter', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  // AI 解释候选基金为什么入选（不是推荐）
  runCandidateAnalysis: () =>
    request('/api/ai/candidate-analysis', { method: 'POST' }),
};
