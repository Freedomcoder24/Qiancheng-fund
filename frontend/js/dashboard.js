/**
 * Dashboard 业务逻辑（Phase 5 从 app.js 拆分）
 *
 * 职责：账户总览、收益趋势、持仓表格、基金历史表现、
 * 添加/编辑/删除持仓（Modal）、基金查询（Phase 2 保留能力）。
 *
 * 原则：所有金额/收益数字直接来自后端 API，前端只负责展示，
 * 绝不在 JS 里重新计算市值/收益（金融计算由 Python 后端负责）。
 */

// ---------------- 页面状态 ----------------

let holdingsCache = [];      // 当前持仓列表（供下拉框 / 统计复用）
let editingHoldingId = null; // 正在编辑的持仓 id（null = 添加模式）
let pendingDeleteId = null;  // 确认弹窗中待删除的持仓 id
let selectedPerfPeriod = 30; // 历史表现当前选中的区间
const TREND_PERIOD = 90;     // 收益趋势固定回放近 90 天

// ---------------- 轻提示 toast ----------------

// showToast('添加成功', 'success' | 'error' | 'info')，2.5 秒后自动消失
function showToast(message, type = 'info') {
  const box = document.getElementById('toast-box');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), 2500);
}

// ---------------- 账户总览 ----------------

async function loadSummary() {
  try {
    const data = await api.getSummary();
    document.getElementById('total-invested').textContent = formatMoney(data.total_invested);
    document.getElementById('total-asset').textContent = formatMoney(data.total_market_value);
    document.getElementById('holding-count').textContent = data.holding_count;
    document.getElementById('total-profit').textContent = formatMoney(data.total_profit);
    document.getElementById('total-profit-rate').textContent = formatPercent(data.total_profit_rate);

    // 颜色语义：涨红跌绿（与全站统一）
    setColorClass(document.getElementById('total-profit'), data.total_profit);
    setColorClass(document.getElementById('total-profit-rate'), data.total_profit_rate);
  } catch (err) {
    console.error('加载账户概览失败：', err);
    showToast(friendlyError(err), 'error');
  }
}

// ---------------- 持仓列表 / 表格 ----------------

async function loadHoldings() {
  const tbody = document.getElementById('holdings-tbody');
  const emptyEl = document.getElementById('holdings-empty');
  const statusEl = document.getElementById('holdings-status');
  const emptyHero = document.getElementById('empty-hero');
  const trendSelect = document.getElementById('trend-select');

  statusEl.textContent = '正在加载持仓...';
  statusEl.style.display = 'block';

  try {
    const holdings = await api.getHoldings();
    holdingsCache = holdings;
    statusEl.style.display = 'none';

    // 无持仓：显示空状态引导 + 隐藏总览卡片（避免一排 ¥0）+ 收起表格与趋势下拉
    if (holdings.length === 0) {
      document.getElementById('summary-cards').style.display = 'none';
      emptyHero.style.display = 'block';
      emptyEl.textContent = '还没有持仓，点击右上角"+ 添加持仓"开始';
      emptyEl.style.display = 'block';
      tbody.innerHTML = '';
      document.getElementById('holdings-table').style.display = 'none';
      trendSelect.style.display = 'none';
      updateHeaderNavDate(holdings);
      return;
    }

    document.getElementById('summary-cards').style.display = '';
    emptyHero.style.display = 'none';
    emptyEl.style.display = 'none';
    document.getElementById('holdings-table').style.display = 'table';

    // 渲染表格（td 的 data-label 供手机端卡片式布局显示字段名）
    tbody.innerHTML = holdings
      .map((h) => {
        const profitClass = h.profit > 0 ? 'up' : h.profit < 0 ? 'down' : '';
        return `
        <tr>
          <td class="t-fund">
            <span class="t-name">${h.fund_name}</span>
            <span class="t-code">${h.fund_code}</span>
          </td>
          <td class="num" data-label="持有份额">${h.shares.toLocaleString('zh-CN')}</td>
          <td class="num" data-label="成本价">${h.cost_price.toFixed(4)}</td>
          <td class="num" data-label="最新净值">${h.latest_nav !== null ? h.latest_nav.toFixed(4) : '--'}
            <small style="color:#bbb;">${h.latest_nav_date || ''}</small></td>
          <td class="num" data-label="当前市值">${formatMoney(h.market_value)}</td>
          <td class="num profit-cell ${profitClass}" data-label="累计收益">
            ${formatMoney(h.profit)}<small>${formatPercent(h.profit_rate)}</small></td>
          <td class="cell-actions" data-label="操作">
            <button class="btn btn-light btn-sm" data-history="${h.id}">历史</button>
            <button class="btn btn-light btn-sm" data-edit="${h.id}">编辑</button>
            <button class="btn btn-danger btn-sm" data-delete="${h.id}">删除</button>
          </td>
        </tr>`;
      })
      .join('');

    // 绑定操作按钮
    tbody.querySelectorAll('[data-history]').forEach((btn) =>
      btn.addEventListener('click', () => jumpToTrend(Number(btn.dataset.history)))
    );
    tbody.querySelectorAll('[data-edit]').forEach((btn) =>
      btn.addEventListener('click', () => openEditModal(Number(btn.dataset.edit)))
    );
    tbody.querySelectorAll('[data-delete]').forEach((btn) =>
      btn.addEventListener('click', () => openDeleteConfirm(Number(btn.dataset.delete)))
    );

    // 顶部 header 显示最新净值日期（取所有持仓中最新的日期）
    updateHeaderNavDate(holdings);

    // 同步两个下拉框（收益趋势 / 历史表现）
    fillHoldingSelects();
    // 盈利持仓统计（展示层数据：直接用后端返回的 profit 字段统计）
    const winCount = holdings.filter((h) => h.profit > 0).length;
    document.getElementById('win-count').textContent = `${winCount} / ${holdings.length}`;

    // 收益趋势：保持当前选择，没有则默认第一只
    const current = trendSelect.value;
    if (!current || !holdings.some((h) => String(h.id) === current)) {
      trendSelect.value = String(holdings[0].id);
    }
    await loadTrend(Number(trendSelect.value));
  } catch (err) {
    console.error('加载持仓失败：', err);
    statusEl.textContent = '持仓加载失败：' + friendlyError(err);
  }
}

// 顶部"最新净值日期"（来自后端真实数据，不使用"实时"字样）
function updateHeaderNavDate(holdings) {
  const el = document.getElementById('nav-date-info');
  if (!holdings || holdings.length === 0) {
    el.textContent = '暂无持仓数据';
    return;
  }
  const dates = holdings.map((h) => h.latest_nav_date).filter(Boolean).sort();
  el.textContent = `最新确认净值日期：${dates[dates.length - 1] || '--'}`;
}

// 填充两个下拉框：收益趋势（按持仓 id）、历史表现（按基金代码，选中自动填代码）
function fillHoldingSelects() {
  const trendSelect = document.getElementById('trend-select');
  const perfSelect = document.getElementById('perf-holding');

  if (holdingsCache.length > 0) {
    const options = holdingsCache
      .map((h) => `<option value="${h.id}" data-code="${h.fund_code}">${h.fund_name}（${h.fund_code}）</option>`)
      .join('');
    trendSelect.innerHTML = options;
    trendSelect.style.display = 'inline-block';

    perfSelect.innerHTML = `<option value="">选择持仓基金</option>` + options;
    perfSelect.style.display = 'inline-block';
  } else {
    trendSelect.style.display = 'none';
    perfSelect.style.display = 'none';
  }
}

// ---------------- 收益趋势（历史模拟收益，方案 A：单只持仓） ----------------

async function loadTrend(holdingId) {
  const statusEl = document.getElementById('trend-status');
  const chartEl = document.getElementById('trend-chart');

  if (!holdingId) {
    statusEl.textContent = '添加持仓后即可查看历史模拟收益趋势';
    statusEl.style.display = 'block';
    chartEl.style.display = 'none';
    return;
  }

  statusEl.textContent = '正在加载历史数据...';
  statusEl.style.display = 'block';
  chartEl.style.display = 'none';

  try {
    const data = await api.getSimHistory(holdingId, TREND_PERIOD);
    statusEl.style.display = 'none';
    document.getElementById('trend-select').value = String(holdingId);
    // 先显示容器（有尺寸后图表才能初始化），再渲染
    chartEl.style.display = 'block';
    renderTrendChart(data.points);
  } catch (err) {
    console.error('加载收益趋势失败：', err);
    statusEl.textContent = '历史数据加载失败：' + friendlyError(err);
  }
}

// 点击持仓行"历史"：选中该基金并滚动到收益趋势
function jumpToTrend(holdingId) {
  const select = document.getElementById('trend-select');
  if (select.style.display !== 'none') {
    select.value = String(holdingId);
  }
  loadTrend(holdingId);
  document.getElementById('trend-panel').scrollIntoView({ behavior: 'smooth' });
}

// ---------------- 基金历史表现（复用 Phase 4 API） ----------------

async function loadPerformance(period) {
  const code = document.getElementById('perf-code').value.trim();
  const statusEl = document.getElementById('perf-status');
  const resultEl = document.getElementById('perf-result');

  if (!/^\d{6}$/.test(code)) {
    resultEl.style.display = 'none';
    statusEl.textContent = '请输入 6 位数字基金代码（或从下拉框选择持仓基金）';
    statusEl.style.display = 'block';
    return;
  }

  resultEl.style.display = 'none';
  statusEl.textContent = '正在分析...';
  statusEl.style.display = 'block';

  try {
    const data = await api.getPerformance(code, period);
    statusEl.style.display = 'none';

    document.getElementById('perf-name').textContent =
      `${data.fund_name}（${data.fund_code}）· 近 ${data.period} 天`;
    const returnEl = document.getElementById('perf-return');
    returnEl.textContent = formatPercent(data.period_return);
    setColorClass(returnEl, data.period_return);
    const ddEl = document.getElementById('perf-drawdown');
    ddEl.textContent = data.max_drawdown.toFixed(2) + '%';
    setColorClass(ddEl, data.max_drawdown);
    document.getElementById('perf-start-nav').textContent = data.start_nav.toFixed(4);
    document.getElementById('perf-end-nav').textContent = data.end_nav.toFixed(4);
    resultEl.style.display = 'block';

    renderNavChart(data.nav_points);
  } catch (err) {
    console.error('基金历史表现分析失败：', err);
    statusEl.textContent = '分析失败：' + friendlyError(err);
  }
}

// ---------------- 添加 / 编辑持仓 Modal ----------------

function openAddModal() {
  editingHoldingId = null;
  document.getElementById('modal-title').textContent = '添加持仓';
  document.getElementById('holding-form').reset();
  document.getElementById('holding-code').disabled = false;
  document.getElementById('holding-fund-name').textContent = '';
  document.getElementById('holding-submit-btn').textContent = '添加';
  document.getElementById('holding-modal').style.display = 'flex';
}

// 点击"编辑"：先取该持仓最新数据填入表单
async function openEditModal(holdingId) {
  try {
    const h = await api.getHolding(holdingId);
    editingHoldingId = holdingId;
    document.getElementById('modal-title').textContent = '编辑持仓';
    document.getElementById('holding-code').value = h.fund_code;
    document.getElementById('holding-code').disabled = true; // 换基金请删除后重新添加
    document.getElementById('holding-fund-name').textContent = h.fund_name;
    document.getElementById('holding-shares').value = h.shares;
    document.getElementById('holding-cost').value = h.cost_price;
    document.getElementById('holding-submit-btn').textContent = '保存';
    document.getElementById('holding-modal').style.display = 'flex';
  } catch (err) {
    console.error('获取持仓失败：', err);
    showToast(friendlyError(err), 'error');
  }
}

function closeHoldingModal() {
  document.getElementById('holding-modal').style.display = 'none';
  editingHoldingId = null;
}

// 提交添加 / 编辑（对应后端 POST / PUT）
async function submitHoldingForm(event) {
  event.preventDefault();
  const code = document.getElementById('holding-code').value.trim();
  const shares = document.getElementById('holding-shares').value;
  const cost = document.getElementById('holding-cost').value;

  // 前端基础校验（后端仍会校验一次）
  if (!editingHoldingId && !/^\d{6}$/.test(code)) {
    showToast('请输入 6 位数字基金代码', 'error');
    return;
  }
  if (!(Number(shares) > 0) || !(Number(cost) > 0)) {
    showToast('份额和成本必须大于 0', 'error');
    return;
  }

  const submitBtn = document.getElementById('holding-submit-btn');
  submitBtn.disabled = true;

  try {
    if (editingHoldingId) {
      await api.updateHolding(editingHoldingId, { shares: Number(shares), cost_price: Number(cost) });
      showToast('持仓已更新', 'success');
    } else {
      await api.createHolding({ fund_code: code, shares: Number(shares), cost_price: Number(cost) });
      showToast('添加成功', 'success');
    }
    closeHoldingModal();
    await refreshDashboard();
  } catch (err) {
    console.error('提交持仓失败：', err);
    showToast(friendlyError(err), 'error');
  } finally {
    submitBtn.disabled = false;
  }
}

// 表单输入代码后自动查询基金名称（对应 GET /api/funds/{code}）
async function lookupFormFundName() {
  const code = document.getElementById('holding-code').value.trim();
  const nameEl = document.getElementById('holding-fund-name');
  if (editingHoldingId || !/^\d{6}$/.test(code)) {
    nameEl.textContent = '';
    return;
  }
  nameEl.textContent = '查询中...';
  try {
    const data = await api.getFundDetail(code);
    nameEl.textContent = data.name;
  } catch (err) {
    console.error('查询基金名称失败：', err);
    nameEl.textContent = '未找到该基金';
  }
}

// ---------------- 删除持仓（带确认） ----------------

function openDeleteConfirm(holdingId) {
  const holding = holdingsCache.find((h) => h.id === holdingId);
  if (!holding) return;
  pendingDeleteId = holdingId;
  document.getElementById('confirm-text').textContent =
    `确定删除「${holding.fund_name}」吗？删除后不可恢复。`;
  document.getElementById('confirm-modal').style.display = 'flex';
}

function closeDeleteConfirm() {
  document.getElementById('confirm-modal').style.display = 'none';
  pendingDeleteId = null;
}

async function doDeleteHolding() {
  if (!pendingDeleteId) return;
  const id = pendingDeleteId;
  closeDeleteConfirm();
  try {
    await api.deleteHolding(id);
    showToast('删除成功', 'success');
    await refreshDashboard();
  } catch (err) {
    console.error('删除持仓失败：', err);
    showToast(friendlyError(err), 'error');
  }
}

// 持仓变化后的统一刷新：概览 + 持仓表格 + 收益趋势
async function refreshDashboard() {
  await loadSummary();
  await loadHoldings();
}

// ---------------- 基金查询（Phase 2 保留能力） ----------------

async function queryFund() {
  const code = document.getElementById('fund-code-input').value.trim();
  if (!/^\d{6}$/.test(code)) {
    showToast('请输入 6 位数字基金代码', 'error');
    return;
  }

  const detailEl = document.getElementById('fund-detail');
  const statusEl = document.getElementById('detail-status');
  const contentEl = document.getElementById('detail-content');
  detailEl.style.display = 'block';
  contentEl.style.display = 'none';
  document.getElementById('history-box').style.display = 'none';
  statusEl.textContent = '查询中...';
  statusEl.style.display = 'block';

  try {
    const data = await api.getFundDetail(code);

    document.getElementById('detail-name').textContent = data.name;
    document.getElementById('detail-type').textContent =
      data.fund_type ? `类型：${data.fund_type}` : '';
    document.getElementById('detail-nav').textContent =
      data.latest_nav ? data.latest_nav.unit_nav ?? '--' : '--';
    document.getElementById('detail-nav-date').textContent =
      data.latest_nav ? data.latest_nav.date : '--';

    const changeEl = document.getElementById('detail-change');
    if (data.latest_nav && data.latest_nav.daily_change !== null) {
      changeEl.textContent = formatPercent(data.latest_nav.daily_change);
      setColorClass(changeEl, data.latest_nav.daily_change);
    } else {
      changeEl.textContent = '--';
    }

    document.getElementById('estimate-tip').style.display =
      data.valuation_available ? 'none' : 'block';

    statusEl.style.display = 'none';
    contentEl.style.display = 'block';
  } catch (err) {
    console.error('查询基金失败：', err);
    statusEl.textContent = friendlyError(err);
  }
}

async function loadHistory() {
  const code = document.getElementById('fund-code-input').value.trim();
  const box = document.getElementById('history-box');
  const tbody = document.querySelector('#history-table tbody');
  if (!/^\d{6}$/.test(code)) return;

  box.style.display = 'block';
  tbody.innerHTML = '<tr><td colspan="4" class="empty-tip">正在加载历史净值...</td></tr>';

  try {
    const data = await api.getFundHistory(code);
    if (data.items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-tip">暂无数据</td></tr>';
      return;
    }
    tbody.innerHTML = data.items
      .map(
        (it) => `
        <tr>
          <td>${it.date}</td>
          <td>${it.unit_nav ?? '--'}</td>
          <td>${it.accumulated_nav ?? '--'}</td>
          <td class="${it.daily_change > 0 ? 'up' : it.daily_change < 0 ? 'down' : ''}">
            ${it.daily_change !== null ? formatPercent(it.daily_change) : '--'}
          </td>
        </tr>`
      )
      .join('');
  } catch (err) {
    console.error('加载历史净值失败：', err);
    tbody.innerHTML =
      `<tr><td colspan="4" class="empty-tip">${friendlyError(err)}</td></tr>`;
  }
}

async function searchFunds() {
  const keyword = document.getElementById('search-input').value.trim();
  const listEl = document.getElementById('search-results');
  const tipEl = document.getElementById('search-tip');
  listEl.innerHTML = '';
  tipEl.style.display = 'none';
  if (!keyword) return;

  tipEl.textContent = '搜索中...';
  tipEl.style.display = 'block';

  try {
    const funds = await api.searchFunds(keyword);
    if (funds.length === 0) {
      tipEl.textContent = '没有找到相关基金';
      return;
    }
    tipEl.style.display = 'none';
    // 点击搜索结果：填入查询框并直接查询详情
    listEl.innerHTML = funds
      .map(
        (f) => `
        <li class="fund-item search-item" data-code="${f.code}">
          <span class="fund-name">${f.name}</span>
          <span class="fund-code">${f.code}</span>
          <span class="fund-meta">${f.fund_type || ''}</span>
        </li>`
      )
      .join('');
    listEl.querySelectorAll('.search-item').forEach((item) => {
      item.addEventListener('click', () => {
        document.getElementById('fund-code-input').value = item.dataset.code;
        queryFund();
      });
    });
  } catch (err) {
    console.error('搜索失败：', err);
    tipEl.textContent = friendlyError(err);
  }
}

// ---------------- AI 分析（Phase 6） ----------------

// 页面加载时检查 AI 配置状态：未配置时提前禁用按钮并提示，避免白等一次失败请求
async function checkAIStatus() {
  const statusEl = document.getElementById('ai-status-tip');
  const btn = document.getElementById('ai-analysis-btn');
  try {
    const status = await api.getAIStatus();
    if (!status.configured) {
      statusEl.textContent = 'AI 功能未配置：可在「AI API 配置」面板填写并保存，' +
        '或在 .env 中配置 AI_API_KEY、AI_BASE_URL、AI_MODEL 后重启服务';
      statusEl.style.display = 'block';
      btn.disabled = true;
    } else {
      statusEl.style.display = 'none';
      btn.disabled = false;
    }
  } catch (err) {
    // 状态检查失败不阻塞页面，点击按钮时会再次得到后端的明确错误
    console.error('检查 AI 配置状态失败：', err);
  }
}

// ---------------- AI API 配置（Phase 9：Web 配置优先于 .env） ----------------

// 展示配置状态行：来源（Web / .env）+ 脱敏 Key（后端保证不含完整 Key）
function renderAIConfigStatus(status) {
  const warnEl = document.getElementById('ai-config-secret-warn');
  const msgEl = document.getElementById('ai-config-msg');

  // 加密密钥未就绪：明确提示（此时无法保存 Web 配置，只能用 .env）
  if (!status.secret_key_ready) {
    warnEl.textContent = '未配置环境变量 FUND_PILOT_SECRET_KEY，无法保存 Web API Key' +
      '（防止明文落库）。请在 .env 或系统环境变量中设置后重启服务；' +
      '当前仍可使用 .env 中已有的 AI 配置。';
    warnEl.style.display = 'block';
  } else {
    warnEl.style.display = 'none';
  }

  const sourceText = { web: 'Web 配置', env: '.env 配置' }[status.key_source] || '未配置';
  const masked = status.api_key_masked ? ` · Key ${status.api_key_masked}` : '';
  const modelText = status.configured
    ? `已配置（来源：${sourceText}${masked}）· 模型：${status.model || '--'}`
    : '未配置：填写上方三项后点击"保存配置"';
  msgEl.textContent = modelText;
  msgEl.style.display = 'block';

  // 回显非敏感字段（Base URL / 模型名明文保存，可显示）；Key 输入框永远留空
  document.getElementById('ai-config-base-url').value = status.base_url || '';
  document.getElementById('ai-config-model').value = status.model || '';
  document.getElementById('ai-config-key').value = '';
  document.getElementById('ai-config-key').placeholder = status.api_key_masked
    ? `已配置（${status.api_key_masked}），留空表示不修改`
    : 'API Key（sk-...）';
}

// 页面加载时读取配置状态（GET /api/ai/config，响应中只有脱敏 Key）
async function loadAIConfig() {
  try {
    renderAIConfigStatus(await api.getAIConfig());
  } catch (err) {
    console.error('加载 AI 配置失败：', err);
  }
}

// 收集配置面板输入：Key 留空 = 不修改（仅对保存有意义，测试时原样传给后端处理）
function collectAIConfigInput() {
  return {
    api_key: document.getElementById('ai-config-key').value.trim(),
    base_url: document.getElementById('ai-config-base-url').value.trim(),
    model: document.getElementById('ai-config-model').value.trim(),
  };
}

// 保存 Web 配置（Key 加密落库，服务重启后自动恢复）
async function saveAIConfig() {
  const body = collectAIConfigInput();
  if (!body.api_key) {
    showToast('请输入 API Key（留空表示不修改，但首次保存必须填写）', 'error');
    return;
  }
  const btn = document.getElementById('ai-config-save-btn');
  btn.disabled = true;
  try {
    const status = await api.saveAIConfig(body);
    renderAIConfigStatus(status);
    showToast('配置已保存并立即生效', 'success');
    checkAIStatus(); // 配置可能从"未配置"变为"已配置"，同步 AI 分析按钮状态
  } catch (err) {
    console.error('保存 AI 配置失败：', err);
    showToast(friendlyError(err), 'error');
  } finally {
    btn.disabled = false;
    document.getElementById('ai-config-key').value = '';
  }
}

// 测试连接（最小化 AI 请求，消耗少量 Token；输入留空的字段用当前生效配置）
async function testAIConfig() {
  const btn = document.getElementById('ai-config-test-btn');
  const msgEl = document.getElementById('ai-config-msg');
  btn.disabled = true;
  btn.textContent = '测试中...';
  try {
    const result = await api.testAIConfig(collectAIConfigInput());
    msgEl.textContent = result.message;
    msgEl.style.display = 'block';
    showToast(result.ok ? '连接成功' : '连接失败，详见配置面板提示', result.ok ? 'success' : 'error');
  } catch (err) {
    console.error('AI 测试连接失败：', err);
    showToast(friendlyError(err), 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '测试连接';
  }
}

// 清除 Web 配置（自动回退 .env 配置）
async function clearAIConfig() {
  const btn = document.getElementById('ai-config-clear-btn');
  btn.disabled = true;
  try {
    const status = await api.clearAIConfig();
    renderAIConfigStatus(status);
    showToast('已清除 Web 配置，恢复使用 .env 配置', 'success');
    checkAIStatus();
  } catch (err) {
    console.error('清除 AI 配置失败：', err);
    showToast(friendlyError(err), 'error');
  } finally {
    btn.disabled = false;
  }
}

// 生成 AI 分析：按钮 loading → 展示三块结果；错误显示在面板内（文案较长，不用 toast）
async function runAIAnalysis() {
  const btn = document.getElementById('ai-analysis-btn');
  const loadingEl = document.getElementById('ai-loading');
  const errorEl = document.getElementById('ai-error');
  const resultEl = document.getElementById('ai-result');
  const statusEl = document.getElementById('ai-status-tip');

  btn.disabled = true;
  btn.textContent = 'AI 分析中...';
  loadingEl.style.display = 'block';
  errorEl.style.display = 'none';
  resultEl.style.display = 'none';
  statusEl.style.display = 'none';

  try {
    const data = await api.runAIAnalysis();

    document.getElementById('ai-meta').textContent =
      `模型：${data.model} · 生成时间：${data.generated_at}` +
      (data.data_date ? ` · 基于最新确认净值日期：${data.data_date}` : '');

    // 模型输出作为纯文本渲染（textContent，不做 HTML 注入）
    document.getElementById('ai-today-summary').textContent =
      data.today_summary || '（模型未返回总结内容）';
    document.getElementById('ai-profit-sources').textContent =
      data.profit_sources || '（模型未返回收益来源分析）';

    const riskList = document.getElementById('ai-risk-list');
    riskList.innerHTML = '';
    const risks = data.risk_warnings || [];
    document.getElementById('ai-risk-section').style.display = risks.length ? 'block' : 'none';
    risks.forEach((w) => {
      const li = document.createElement('li');
      li.textContent = w;
      riskList.appendChild(li);
    });

    resultEl.style.display = 'block';
  } catch (err) {
    console.error('AI 分析失败：', err);
    // 直接使用后端 detail 的中文文案（不经过 friendlyError，避免被映射成数据源类提示）
    errorEl.textContent = err && err.message ? err.message : 'AI 分析失败，请稍后重试';
    errorEl.style.display = 'block';
  } finally {
    loadingEl.style.display = 'none';
    btn.disabled = false;
    btn.textContent = '生成 AI 分析';
  }
}

// ---------------- 智能监控（Phase 7） ----------------

// 智能监控面板文字的构建辅助：全部用 createElement + textContent（纯文本渲染防注入）
function buildMonitorItem(alert) {
  const item = document.createElement('div');
  item.className = `monitor-item level-${alert.level}`;

  const head = document.createElement('div');
  head.className = 'monitor-item-head';

  const badge = document.createElement('span');
  badge.className = `level-badge level-${alert.level}`;
  badge.textContent = alert.level_label;
  head.appendChild(badge);

  const type = document.createElement('span');
  type.className = 'monitor-type';
  type.textContent = alert.type_label;
  head.appendChild(type);

  if (alert.fund_name) {
    const fund = document.createElement('span');
    fund.className = 'monitor-fund';
    fund.textContent = alert.fund_code ? `${alert.fund_name}（${alert.fund_code}）` : alert.fund_name;
    head.appendChild(fund);
  }

  const reason = document.createElement('p');
  reason.className = 'monitor-reason';
  reason.textContent = alert.reason;

  const detail = document.createElement('p');
  detail.className = 'monitor-detail';
  detail.textContent = alert.detail;

  item.appendChild(head);
  item.appendChild(reason);
  item.appendChild(detail);
  return item;
}

// 加载智能监控结果：页面加载时自动执行一次，也可点"重新检查"
async function loadMonitor() {
  const loadingEl = document.getElementById('monitor-loading');
  const listEl = document.getElementById('monitor-list');
  const emptyEl = document.getElementById('monitor-empty');
  const issuesEl = document.getElementById('monitor-issues');

  loadingEl.style.display = 'block';
  listEl.innerHTML = '';
  emptyEl.style.display = 'none';
  issuesEl.style.display = 'none';

  try {
    const data = await api.getMonitor();
    loadingEl.style.display = 'none';

    const alerts = data.alerts || [];
    if (alerts.length === 0) {
      // 无提醒：显示后端的整体说明（无持仓 / 当前未发现明显异常）
      emptyEl.textContent = data.summary || '当前未发现明显异常';
      emptyEl.style.display = 'block';
    } else {
      // 后端已按 高风险 → 注意 → 提示 排序，直接顺序渲染
      alerts.forEach((alert) => listEl.appendChild(buildMonitorItem(alert)));
    }

    // 数据不足 / 接口异常说明（如实展示，不伪造数据）
    const issues = data.data_issues || [];
    if (issues.length > 0) {
      issuesEl.textContent = '部分数据不完整，相关检查已跳过：' + issues.join('；');
      issuesEl.style.display = 'block';
    }
  } catch (err) {
    console.error('智能监控检查失败：', err);
    loadingEl.style.display = 'none';
    emptyEl.textContent = friendlyError(err);
    emptyEl.style.display = 'block';
  }
}

// ---------------- 最近一次自动检查（Phase 8 定时任务快照） ----------------

// 加载最近一次自动检查快照：无快照时显示引导文案，有快照时复用监控提醒样式渲染
async function loadAutoCheck() {
  const loadingEl = document.getElementById('auto-loading');
  const noneEl = document.getElementById('auto-none');
  const metaEl = document.getElementById('auto-meta');
  const listEl = document.getElementById('auto-alerts');
  const clearEl = document.getElementById('auto-clear');
  const issuesEl = document.getElementById('auto-issues');

  loadingEl.style.display = 'block';
  noneEl.style.display = 'none';
  metaEl.style.display = 'none';
  listEl.style.display = 'none';
  listEl.innerHTML = '';
  clearEl.style.display = 'none';
  issuesEl.style.display = 'none';

  try {
    const snap = await api.getAutoLatest();
    loadingEl.style.display = 'none';

    // 尚未执行过自动检查（Phase 9 优化后首轮在启动后约 5 秒即执行）
    if (!snap) {
      noneEl.textContent = '服务启动后尚未执行自动检查，' +
        '启动后几秒内会自动执行首轮检查，之后按配置间隔运行' +
        '（也可通过 /docs 的 POST /api/auto/run 手动触发）。';
      noneEl.style.display = 'block';
      return;
    }

    metaEl.textContent = `执行时间：${snap.executed_at} · 检查持仓 ${snap.checked_count} 只 · 提醒 ${snap.alert_count} 条`;
    metaEl.style.display = 'block';

    const alerts = snap.alerts || [];
    if (alerts.length === 0) {
      // 无提醒：显示快照里的整体说明（与监控面板口径一致）
      clearEl.textContent = snap.summary || '当前未发现明显异常';
      clearEl.style.display = 'block';
    } else {
      // 后端保存时已按等级排序，直接顺序渲染（复用 Phase 7 的提醒样式）
      alerts.forEach((alert) => listEl.appendChild(buildMonitorItem(alert)));
      listEl.style.display = 'block';
    }

    const issues = snap.data_issues || [];
    if (issues.length > 0) {
      issuesEl.textContent = '部分数据不完整或检查未完成：' + issues.join('；');
      issuesEl.style.display = 'block';
    }
  } catch (err) {
    console.error('加载自动检查记录失败：', err);
    loadingEl.style.display = 'none';
    noneEl.textContent = friendlyError(err);
    noneEl.style.display = 'block';
  }
}

// ---------------- AI 每日报告（Phase 8，每天一份） ----------------

// 加载最新 AI 每日报告：纯文本渲染（textContent），免责声明使用后端保存的固定文案
async function loadDailyReport() {
  const loadingEl = document.getElementById('report-loading');
  const noneEl = document.getElementById('report-none');
  const resultEl = document.getElementById('report-result');
  const disclaimerEl = document.getElementById('report-disclaimer');

  loadingEl.style.display = 'block';
  noneEl.style.display = 'none';
  resultEl.style.display = 'none';

  try {
    const data = await api.getDailyReport();
    loadingEl.style.display = 'none';

    if (!data) {
      noneEl.textContent = '报告尚未生成：后台任务会在每天首次自动检查时生成 AI 每日报告' +
        '（需在 .env 中配置 AI 且账户有持仓）。';
      noneEl.style.display = 'block';
      return;
    }

    document.getElementById('report-meta').textContent =
      `报告日期：${data.report_date} · 生成时间：${data.generated_at}` +
      (data.model ? ` · 模型：${data.model}` : '') +
      (data.data_date ? ` · 基于最新确认净值日期：${data.data_date}` : '');

    // 模型输出作为纯文本渲染（与 AI 分析面板同规格，防注入）
    document.getElementById('report-summary').textContent =
      (data.content && data.content.today_summary) || '（本份报告未包含总结内容）';
    document.getElementById('report-sources').textContent =
      (data.content && data.content.profit_sources) || '（本份报告未包含收益来源分析）';

    const riskList = document.getElementById('report-risk-list');
    riskList.innerHTML = '';
    const risks = (data.content && data.content.risk_warnings) || [];
    document.getElementById('report-risk-section').style.display =
      risks.length ? 'block' : 'none';
    risks.forEach((w) => {
      const li = document.createElement('li');
      li.textContent = w;
      riskList.appendChild(li);
    });

    // 免责声明以数据库里保存的文案为准
    if (data.disclaimer) disclaimerEl.textContent = data.disclaimer;

    resultEl.style.display = 'block';
  } catch (err) {
    console.error('加载 AI 每日报告失败：', err);
    loadingEl.style.display = 'none';
    noneEl.textContent = friendlyError(err);
    noneEl.style.display = 'block';
  }
}

// ---------------- 我的投资目标（Phase 12） ----------------

// 页面加载时读取目标设置：已设置则回填输入框并加载分析；未设置显示引导
async function loadGoal() {
  const guideEl = document.getElementById('goal-guide');
  const aiBtn = document.getElementById('goal-ai-btn');
  const aiStatusEl = document.getElementById('goal-ai-status');
  try {
    const goal = await api.getGoal();
    if (goal.is_set && goal.target_return_rate !== null) {
      document.getElementById('goal-rate-input').value = goal.target_return_rate;
      aiBtn.disabled = false;
      await loadGoalAnalysis();
    } else {
      guideEl.textContent = '还没有设置目标：先在上方输入一个目标收益率（如 5 或 10），' +
        '保存后这里会显示"当前收益 → 距离目标"的进度和需要注意的风险。';
      guideEl.style.display = 'block';
    }
  } catch (err) {
    console.error('加载投资目标失败：', err);
    guideEl.textContent = '投资目标加载失败：' + friendlyError(err);
    guideEl.style.display = 'block';
    aiStatusEl.style.display = 'none';
  }
}

// 保存目标收益率（0 < x ≤ 500，后端会再校验一次）
async function saveGoal() {
  const input = document.getElementById('goal-rate-input');
  const msgEl = document.getElementById('goal-msg');
  const value = Number(input.value);

  if (!(value > 0) || value > 500) {
    showToast('目标收益率必须是大于 0 且不超过 500 的数字（单位 %）', 'error');
    return;
  }

  const btn = document.getElementById('goal-save-btn');
  btn.disabled = true;
  try {
    await api.saveGoal(value);
    msgEl.style.display = 'none';
    showToast('目标已保存', 'success');
    document.getElementById('goal-ai-btn').disabled = false;
    await loadGoalAnalysis();
  } catch (err) {
    console.error('保存目标失败：', err);
    msgEl.textContent = friendlyError(err);
    msgEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
}

// 构建持仓明细行（createElement + textContent，纯文本渲染防注入）
function buildGoalHoldingRow(item) {
  const tr = document.createElement('tr');

  const tdFund = document.createElement('td');
  tdFund.className = 't-fund';
  const name = document.createElement('span');
  name.className = 't-name';
  name.textContent = item.fund_name;
  const code = document.createElement('span');
  code.className = 't-code';
  code.textContent = item.fund_code;
  tdFund.appendChild(name);
  tdFund.appendChild(code);
  tr.appendChild(tdFund);

  const rateTd = document.createElement('td');
  rateTd.className = 'num';
  rateTd.setAttribute('data-label', '当前收益率');
  const rateSpan = document.createElement('span');
  rateSpan.textContent = formatPercent(item.profit_rate);
  setColorClass(rateSpan, item.profit_rate);
  rateTd.appendChild(rateSpan);
  tr.appendChild(rateTd);

  const gapTd = document.createElement('td');
  gapTd.className = 'num';
  gapTd.setAttribute('data-label', '距离目标');
  gapTd.textContent = item.goal_gap_percent !== null && item.goal_gap_percent !== undefined
    ? formatPercent(-item.goal_gap_percent) + '（还差' + item.goal_gap_percent.toFixed(2) + '个百分点）'
    : '--';
  tr.appendChild(gapTd);

  const stateTd = document.createElement('td');
  stateTd.className = 'num';
  stateTd.setAttribute('data-label', '目标状态');
  // 达到目标只显示状态（不产生任何卖出结论）
  stateTd.textContent = item.achieved ? '已达到目标' : '未达到';
  if (item.achieved) stateTd.classList.add('up');
  tr.appendChild(stateTd);

  const ddTd = document.createElement('td');
  ddTd.className = 'num';
  ddTd.setAttribute('data-label', '近180天回撤');
  ddTd.textContent = item.drawdown_180d !== null && item.drawdown_180d !== undefined
    ? item.drawdown_180d.toFixed(2) + '%' : '--';
  tr.appendChild(ddTd);

  const volTd = document.createElement('td');
  volTd.className = 'num';
  volTd.setAttribute('data-label', '近30天波动');
  volTd.textContent = item.volatility_days_30d !== null && item.volatility_days_30d !== undefined
    ? item.volatility_days_30d + ' 天' : '--';
  tr.appendChild(volTd);

  return tr;
}

// 加载目标进度分析：进度条 + "当前收益 → 距离目标"链路 + 账户/持仓指标
async function loadGoalAnalysis() {
  const guideEl = document.getElementById('goal-guide');
  const progressBox = document.getElementById('goal-progress-box');
  const tableEl = document.getElementById('goal-holdings-table');
  const tbody = document.getElementById('goal-holdings-tbody');
  const issuesEl = document.getElementById('goal-issues');

  try {
    const data = await api.getGoalAnalysis();
    guideEl.style.display = 'none';

    if (!data.target_set) {
      progressBox.style.display = 'none';
      tableEl.style.display = 'none';
      guideEl.textContent = '还没有设置目标：先在上方输入一个目标收益率（如 5 或 10），' +
        '保存后这里会显示"当前收益 → 距离目标"的进度和需要注意的风险。';
      guideEl.style.display = 'block';
      return;
    }

    const acc = data.account;
    progressBox.style.display = 'block';

    // 进度条（宽度来自后端计算的 progress_percent，前端不重算）
    const fill = document.getElementById('goal-progress-fill');
    fill.style.width = (acc.progress_percent !== null && acc.progress_percent !== undefined
      ? acc.progress_percent : 0) + '%';

    // 链路文案：我的目标收益 → 当前收益 → 距离目标
    const linkEl = document.getElementById('goal-link-text');
    linkEl.textContent = '目标收益 ' + acc.target_return_rate.toFixed(2) + '% · 当前收益 ' +
      formatPercent(acc.current_profit_rate) + ' · ' +
      (acc.achieved ? '已达到目标（仅为状态提示，不构成卖出建议）'
                    : '还差 ' + acc.gap_percent.toFixed(2) + ' 个百分点');

    // 状态徽章（Phase 13，后端 classify_goal_status 分类，只描述状态不生成交易指令）
    const badge = document.getElementById('goal-status-badge');
    if (data.status && data.status_label) {
      const badgeClass = {
        risk_attention: 'level-danger',
        target_achieved: 'level-info',
        near_target: 'level-warning',
        far_from_target: 'level-neutral',
        not_set: 'level-neutral',
      }[data.status] || 'level-neutral';
      badge.className = 'level-badge ' + badgeClass;
      badge.textContent = data.status_label;
      badge.style.display = 'inline-block';
    } else {
      badge.style.display = 'none';
    }

    // 指标卡（同屏展示 收益 / 距离 / 历史 / 风险）
    const currentEl = document.getElementById('goal-current-rate');
    currentEl.textContent = formatPercent(acc.current_profit_rate) +
      '（' + formatMoney(acc.total_profit) + '）';
    setColorClass(currentEl, acc.current_profit_rate);
    document.getElementById('goal-target-rate').textContent =
      acc.target_return_rate.toFixed(2) + '%';

    const gapEl = document.getElementById('goal-gap');
    gapEl.textContent = acc.achieved
      ? '已达到目标'
      : acc.gap_percent.toFixed(2) + ' 个百分点';
    if (acc.achieved) gapEl.classList.add('up');
    document.getElementById('goal-required-profit').textContent = acc.achieved
      ? '' : '约还差 ' + formatMoney(acc.required_profit) + ' 收益';

    const ddEl = document.getElementById('goal-drawdown');
    ddEl.textContent = acc.drawdown_90d !== null && acc.drawdown_90d !== undefined
      ? acc.drawdown_90d.toFixed(2) + '%' : '数据不足';
    setColorClass(ddEl, acc.drawdown_90d || 0);
    document.getElementById('goal-volatility').textContent =
      acc.volatility_days_30d !== null && acc.volatility_days_30d !== undefined
        ? '近 30 天最大单日波动≥2% 有 ' + acc.volatility_days_30d + ' 天'
        : '';

    // 持仓明细
    tbody.innerHTML = '';
    (data.holdings || []).forEach((item) => tbody.appendChild(buildGoalHoldingRow(item)));
    tableEl.style.display = data.holdings && data.holdings.length ? 'table' : 'none';

    // 数据不足 / 获取失败说明（如实展示，不伪造）
    const issues = data.data_issues || [];
    if (issues.length) {
      issuesEl.textContent = '部分数据不完整：' + issues.join('；');
      issuesEl.style.display = 'block';
    } else {
      issuesEl.style.display = 'none';
    }
  } catch (err) {
    console.error('加载目标分析失败：', err);
    guideEl.textContent = '目标分析加载失败：' + friendlyError(err);
    guideEl.style.display = 'block';
  }
}

// 生成 AI 目标结论：只有接近目标 / 已达到目标 / 风险需要关注三种状态
async function runGoalConclusion() {
  const btn = document.getElementById('goal-ai-btn');
  const loadingEl = document.getElementById('goal-ai-loading');
  const errorEl = document.getElementById('goal-ai-error');
  const resultEl = document.getElementById('goal-ai-result');
  const statusEl = document.getElementById('goal-ai-status');

  btn.disabled = true;
  loadingEl.style.display = 'block';
  errorEl.style.display = 'none';
  resultEl.style.display = 'none';
  statusEl.style.display = 'none';

  try {
    const data = await api.runGoalConclusion();

    document.getElementById('goal-ai-meta').textContent =
      '模型：' + data.model + ' · 生成时间：' + data.generated_at +
      (data.data_date ? ' · 基于最新确认净值日期：' + data.data_date : '');

    // 三种结论状态的徽章（低置信时附加说明）
    const badge = document.getElementById('goal-ai-badge');
    badge.textContent = data.conclusion_label +
      (data.low_confidence ? '（AI 输出异常，已按风险关注处理）' : '');
    badge.className = 'level-badge level-' +
      ({ target_achieved: 'info', near_target: 'warning', risk_attention: 'danger' }[data.conclusion] || 'danger');

    // AI 文本纯文本渲染（textContent，防注入）
    document.getElementById('goal-ai-reason').textContent =
      data.reason || '（模型未返回结论依据）';

    const risksUl = document.getElementById('goal-ai-risks');
    risksUl.innerHTML = '';
    (data.risks || []).forEach((r) => {
      const li = document.createElement('li');
      li.textContent = r;
      risksUl.appendChild(li);
    });

    resultEl.style.display = 'block';
  } catch (err) {
    console.error('AI 目标结论失败：', err);
    // 失败降级：量化表格照常展示，仅提示 AI 不可用
    errorEl.textContent = (err && err.message ? err.message : 'AI 结论生成失败') +
      '（AI 暂时不可用，上方目标进度为后端量化数据，不受影响）';
    errorEl.style.display = 'block';
  } finally {
    loadingEl.style.display = 'none';
    btn.disabled = false;
  }
}

// ---------------- 基金关注 / 候选池（Phase 12） ----------------

let candidatesCache = []; // 已加载的候选池（供 AI 解读按钮判断）

// 构建候选池表格行（createElement + textContent，纯文本渲染防注入）
function buildCandidateRow(item) {
  const tr = document.createElement('tr');

  const tdFund = document.createElement('td');
  tdFund.className = 't-fund';
  const name = document.createElement('span');
  name.className = 't-name';
  name.textContent = item.name;
  const code = document.createElement('span');
  code.className = 't-code';
  code.textContent = item.code;
  tdFund.appendChild(name);
  tdFund.appendChild(code);
  tr.appendChild(tdFund);

  const typeTd = document.createElement('td');
  typeTd.className = 'num';
  typeTd.setAttribute('data-label', '类型');
  typeTd.textContent = item.fund_type || '--';
  tr.appendChild(typeTd);

  const makePctTd = (value, cls, label) => {
    const td = document.createElement('td');
    td.className = 'num';
    td.setAttribute('data-label', label);
    if (value === null || value === undefined) {
      td.textContent = '--';
      return td;
    }
    const span = document.createElement('span');
    span.textContent = formatPercent(value);
    if (cls) setColorClass(span, value);
    td.appendChild(span);
    return td;
  };
  tr.appendChild(makePctTd(item.return_1y, true, '近1年'));
  tr.appendChild(makePctTd(item.return_6m, true, '近6月'));
  tr.appendChild(makePctTd(item.return_3m, true, '近3月'));
  tr.appendChild(makePctTd(item.return_180d, true, '近180天'));

  const ddTd = document.createElement('td');
  ddTd.className = 'num';
  ddTd.setAttribute('data-label', '180天回撤');
  ddTd.textContent = item.max_drawdown_180d !== null && item.max_drawdown_180d !== undefined
    ? item.max_drawdown_180d.toFixed(2) + '%' : '--';
  tr.appendChild(ddTd);

  const volTd = document.createElement('td');
  volTd.className = 'num';
  volTd.setAttribute('data-label', '30天大波动');
  volTd.textContent = item.volatility_days_30d !== null && item.volatility_days_30d !== undefined
    ? item.volatility_days_30d + ' 天' : '--';
  tr.appendChild(volTd);

  return tr;
}

// 手动加载候选池（不自动加载，控制请求数）
async function loadCandidates() {
  const btn = document.getElementById('candidates-load-btn');
  const loadingEl = document.getElementById('candidates-loading');
  const guideEl = document.getElementById('candidates-guide');
  const errorEl = document.getElementById('candidates-error');
  const tableEl = document.getElementById('candidates-table');
  const tbody = document.getElementById('candidates-tbody');
  const metaEl = document.getElementById('candidates-meta');
  const issuesEl = document.getElementById('candidates-issues');
  const aiBtn = document.getElementById('candidates-ai-btn');

  btn.disabled = true;
  guideEl.style.display = 'none';
  errorEl.style.display = 'none';
  issuesEl.style.display = 'none';
  loadingEl.style.display = 'block';

  try {
    const data = await api.getCandidates(20);
    loadingEl.style.display = 'none';
    candidatesCache = data.candidates || [];

    tbody.innerHTML = '';
    candidatesCache.forEach((item) => tbody.appendChild(buildCandidateRow(item)));
    tableEl.style.display = candidatesCache.length ? 'table' : 'none';
    metaEl.textContent = '共 ' + data.count + ' 只 · 数据净值日期 ' + (data.nav_date || '--') +
      ' · 筛选规则：' + data.screening_rule;
    metaEl.style.display = 'block';
    // 同步刷新筛选条件说明（后端返回的规则文案，前端不拼规则）
    updateFilterDesc(data.screening_rule, true);

    const issues = data.data_issues || [];
    if (issues.length) {
      issuesEl.textContent = '部分基金未进入候选池：' + issues.join('；');
      issuesEl.style.display = 'block';
    }

    if (!candidatesCache.length) {
      errorEl.textContent = '本次筛选没有基金满足条件，可稍后重试';
      errorEl.style.display = 'block';
    }
    // 候选池加载成功后才允许 AI 解读
    aiBtn.disabled = !candidatesCache.length;
  } catch (err) {
    console.error('加载候选池失败：', err);
    loadingEl.style.display = 'none';
    errorEl.textContent = '候选池加载失败：' + friendlyError(err);
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
}

// AI 解释候选基金为什么入选（只解释筛选原因，不是推荐）
async function runCandidateAnalysis() {
  const btn = document.getElementById('candidates-ai-btn');
  const errorEl = document.getElementById('candidates-ai-error');
  const resultEl = document.getElementById('candidates-ai-result');
  // Phase 13：明确 Loading 状态（20~60 秒期间不让用户误以为卡死）
  const loadingEl = document.getElementById('candidates-ai-loading');

  btn.disabled = true;
  errorEl.style.display = 'none';
  resultEl.style.display = 'none';
  loadingEl.textContent = '正在分析 ' + candidatesCache.length +
    ' 只候选基金，请稍候（约需 20~60 秒，AI 只解读历史数据）...';
  loadingEl.style.display = 'block';

  try {
    const data = await api.runCandidateAnalysis();
    loadingEl.style.display = 'none';

    document.getElementById('candidates-ai-meta').textContent =
      '模型：' + data.model + ' · 生成时间：' + data.generated_at +
      (data.nav_date ? ' · 数据净值日期：' + data.nav_date : '');

    document.getElementById('candidates-overview').textContent =
      data.overview || '（模型未返回整体说明）';

    const hlUl = document.getElementById('candidates-highlights');
    hlUl.innerHTML = '';
    (data.highlights || []).forEach((h) => {
      const li = document.createElement('li');
      li.textContent = h.code + '：' + h.reason;
      hlUl.appendChild(li);
    });
    document.getElementById('candidates-highlights-section').style.display =
      (data.highlights || []).length ? 'block' : 'none';

    const caUl = document.getElementById('candidates-cautions');
    caUl.innerHTML = '';
    (data.cautions || []).forEach((c) => {
      const li = document.createElement('li');
      li.textContent = c;
      caUl.appendChild(li);
    });
    document.getElementById('candidates-cautions-section').style.display =
      (data.cautions || []).length ? 'block' : 'none';

    document.getElementById('candidates-background').textContent =
      data.market_background || '（模型未返回市场背景）';

    resultEl.style.display = 'block';
  } catch (err) {
    console.error('AI 候选池解读失败：', err);
    loadingEl.style.display = 'none';
    errorEl.textContent = (err && err.message ? err.message : 'AI 解读失败') +
      '（AI 暂时不可用，上方候选池为后端量化筛选数据，不受影响）';
    errorEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
}

// ---------------- Phase 13：候选池筛选条件（存库，重启不丢） ----------------

// 初始化：读取已保存的筛选条件并回填控件
async function loadCandidateFilter() {
  const msgEl = document.getElementById('filter-msg');
  try {
    const data = await api.getCandidateFilter();
    document.getElementById('filter-type-stock').checked = data.include_stock;
    document.getElementById('filter-type-mixed').checked = data.include_mixed;
    document.getElementById('filter-min-return-1y').value = data.min_return_1y || 0;
    const suffix = data.is_set ? '' : '（默认条件，可修改后保存）';
    updateFilterDesc(buildFilterDescText(data) + suffix, false);
  } catch (err) {
    console.error('加载筛选条件失败：', err);
    msgEl.textContent = '筛选条件加载失败：' + friendlyError(err);
    msgEl.style.display = 'block';
  }
}

// 由条件对象拼说明（仅用于本地回显；加载候选池后的说明用后端返回的规则文案）
function buildFilterDescText(data) {
  const types = [];
  if (data.include_stock) types.push('股票型');
  if (data.include_mixed) types.push('混合型');
  return '当前筛选条件：基金类型 ' + (types.join(' + ') || '无') +
    '，近 1 年收益下限 ' + (data.min_return_1y || 0) + '%';
}

// 更新筛选条件说明区（candidates 返回时用后端 screening_rule，初始化时用本地拼的）
function updateFilterDesc(text, fromServer) {
  const el = document.getElementById('filter-desc');
  el.textContent = fromServer ? '当前生效：' + text : text;
  el.style.display = 'block';
}

// 保存筛选条件（前端先校验至少勾一类；保存后不自动请求数据源，保持手动加载）
async function saveCandidateFilter() {
  const msgEl = document.getElementById('filter-msg');
  const includeStock = document.getElementById('filter-type-stock').checked;
  const includeMixed = document.getElementById('filter-type-mixed').checked;
  const minReturn = Number(document.getElementById('filter-min-return-1y').value);

  msgEl.style.display = 'none';
  if (!includeStock && !includeMixed) {
    msgEl.textContent = '至少勾选一个基金类型（股票型 / 混合型）';
    msgEl.style.display = 'block';
    return;
  }
  if (Number.isNaN(minReturn) || minReturn < -100 || minReturn > 1000) {
    msgEl.textContent = '近 1 年收益下限需在 -100 ~ 1000 之间（0 = 不过滤）';
    msgEl.style.display = 'block';
    return;
  }

  const btn = document.getElementById('filter-save-btn');
  btn.disabled = true;
  try {
    const data = await api.saveCandidateFilter({
      include_stock: includeStock,
      include_mixed: includeMixed,
      min_return_1y: minReturn,
    });
    updateFilterDesc(buildFilterDescText(data), false);
    showToast('筛选条件已保存，点击「加载候选池」生效', 'success');
  } catch (err) {
    console.error('保存筛选条件失败：', err);
    msgEl.textContent = '筛选条件保存失败：' + friendlyError(err);
    msgEl.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
}
