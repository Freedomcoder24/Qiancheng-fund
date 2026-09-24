/**
 * 钱程似锦（Qiancheng）前端入口（Phase 5）
 *
 * 脚本拆分后的分工：
 *   api.js       —— fetch 封装 + 统一错误文案（api / friendlyError）
 *   charts.js    —— ECharts 实例管理与图表渲染（renderTrendChart / renderNavChart）
 *   dashboard.js —— 业务逻辑（总览 / 持仓 / 趋势 / 表现 / Modal / 删除确认）
 *   app.js       —— 全局工具函数 + 后端健康检查 + 页面入口与事件绑定
 *
 * 注意：dashboard.js 在运行时调用本文件的 formatMoney / formatPercent / setColorClass，
 * 由于都是顶层 function 声明（脚本加载后即成为全局函数），调用时序没有问题。
 */

// ============ 全局工具函数 ============

// 格式化金额：1234.5 -> ¥1,234.50；负数 -> -¥1,234.50
function formatMoney(value) {
  const sign = value < 0 ? '-' : '';
  const abs = Math.abs(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return sign + '¥' + abs;
}

// 格式化百分比：1.56 -> +1.56%
function formatPercent(value) {
  const sign = value > 0 ? '+' : '';
  return sign + value.toFixed(2) + '%';
}

// 给"涨跌"文字设置颜色（国内习惯：涨红跌绿，全站统一）
function setColorClass(element, value) {
  element.classList.remove('up', 'down');
  if (value > 0) element.classList.add('up');
  if (value < 0) element.classList.add('down');
}

// ============ 后端健康检查 ============

// 检查后端健康状态（对应后端 GET /api/health）
async function checkHealth() {
  const statusEl = document.getElementById('api-status');
  try {
    const resp = await fetch('/api/health');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    statusEl.textContent = '后端连接正常';
    statusEl.classList.add('ok');
  } catch (err) {
    statusEl.textContent = '后端未连接，请先启动 FastAPI 服务';
    statusEl.classList.add('bad');
  }
}

// ============ 页面入口与事件绑定 ============

document.addEventListener('DOMContentLoaded', () => {
  // 首次加载：健康检查 + 账户总览 + 持仓（持仓加载完成后会自动填充下拉框并渲染趋势图）
  checkHealth();
  loadSummary();
  loadHoldings();
  loadMonitor();
  // Phase 8：最近一次自动检查 + AI 每日报告（只读展示，刷新页面即可更新）
  loadAutoCheck();
  loadDailyReport();
  // Phase 12：投资目标（读取目标 → 填充进度条与指标）
  loadGoal();
  // Phase 13：候选池筛选条件（读取已保存条件 → 回填控件）
  loadCandidateFilter();

  // ---- 智能监控：重新检查（Phase 7） ----
  document.getElementById('monitor-refresh-btn').addEventListener('click', loadMonitor);

  // ---- 空状态引导 & 持仓面板：打开"添加持仓"Modal ----
  document.getElementById('empty-add-btn').addEventListener('click', openAddModal);
  document.getElementById('open-add-modal-btn').addEventListener('click', openAddModal);

  // ---- 添加 / 编辑持仓 Modal ----
  document.getElementById('holding-form').addEventListener('submit', submitHoldingForm);
  document.getElementById('holding-cancel-btn').addEventListener('click', closeHoldingModal);
  document.getElementById('modal-close-btn').addEventListener('click', closeHoldingModal);
  // 代码输满 6 位后自动查询基金名称（失焦时也触发一次）
  document.getElementById('holding-code').addEventListener('input', (e) => {
    if (e.target.value.length === 6) lookupFormFundName();
  });
  document.getElementById('holding-code').addEventListener('blur', lookupFormFundName);

  // ---- 删除确认 Modal ----
  document.getElementById('confirm-cancel-btn').addEventListener('click', closeDeleteConfirm);
  document.getElementById('confirm-ok-btn').addEventListener('click', doDeleteHolding);

  // ---- 收益趋势：切换下拉框中的持仓基金 ----
  document.getElementById('trend-select').addEventListener('change', (e) => {
    loadTrend(Number(e.target.value));
  });

  // ---- 基金历史表现 ----
  // 从下拉框选择持仓基金：把该基金代码填入输入框并自动分析
  document.getElementById('perf-holding').addEventListener('change', (e) => {
    const option = e.target.selectedOptions[0];
    const code = option ? option.dataset.code : '';
    if (!code) return;
    document.getElementById('perf-code').value = code;
    loadPerformance(selectedPerfPeriod);
  });
  // 手动输入代码后回车分析
  document.getElementById('perf-code').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') loadPerformance(selectedPerfPeriod);
  });
  // 区间按钮组（7 / 30 / 90 / 180 天）：切换选中态并立即重新分析
  document.querySelectorAll('.period-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.period-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      selectedPerfPeriod = Number(btn.dataset.period);
      loadPerformance(selectedPerfPeriod);
    });
  });

  // ---- AI 分析（Phase 6） ----
  checkAIStatus();
  document.getElementById('ai-analysis-btn').addEventListener('click', runAIAnalysis);

  // ---- AI API 配置（Phase 9：保存 / 测试连接 / 清除） ----
  loadAIConfig();
  document.getElementById('ai-config-save-btn').addEventListener('click', saveAIConfig);
  document.getElementById('ai-config-test-btn').addEventListener('click', testAIConfig);
  document.getElementById('ai-config-clear-btn').addEventListener('click', clearAIConfig);

  // ---- 投资目标（Phase 12：保存 / AI 结论） ----
  document.getElementById('goal-save-btn').addEventListener('click', saveGoal);
  document.getElementById('goal-ai-btn').addEventListener('click', runGoalConclusion);

  // ---- 基金关注（Phase 12：手动加载候选池 / AI 解读） ----
  document.getElementById('candidates-load-btn').addEventListener('click', loadCandidates);
  document.getElementById('candidates-ai-btn').addEventListener('click', runCandidateAnalysis);

  // ---- 候选池筛选条件（Phase 13：保存） ----
  document.getElementById('filter-save-btn').addEventListener('click', saveCandidateFilter);

  // ---- AI 助手对话面板（Phase 15/17：Agent 对话，后端 Tool Calling） ----
  initAgentChat();

  // ---- 面板折叠按钮（智能监控 / 最近一次自动检查）：切换正文显隐 ----
  document.querySelectorAll('.collapse-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const panel = document.getElementById(btn.dataset.target);
      const collapsed = panel.classList.toggle('collapsed');
      btn.textContent = collapsed ? '展开' : '折叠';
    });
  });

  // ---- 基金查询（Phase 2 保留能力） ----
  document.getElementById('query-btn').addEventListener('click', queryFund);
  document.getElementById('fund-code-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') queryFund();
  });
  document.getElementById('history-btn').addEventListener('click', loadHistory);
  document.getElementById('search-btn').addEventListener('click', searchFunds);
  document.getElementById('search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') searchFunds();
  });
});
