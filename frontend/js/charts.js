/**
 * ECharts 图表模块（Phase 5 从 app.js 拆分）
 *
 * 实例统一管理：同一个容器第一次 init，之后只 setOption（复用 Phase 4 方案），
 * 避免重复查询时无限创建实例；窗口缩放时统一 resize；
 * ECharts CDN 加载失败时降级为文字提示，不影响其他功能。
 */

// 实例缓存：domId -> echarts 实例
const chartInstances = {};

// 获取图表实例；CDN 未加载时在容器内显示提示并返回 null
function getChart(domId) {
  if (typeof echarts === 'undefined') {
    document.getElementById(domId).innerHTML =
      '<p class="empty-tip">图表组件（ECharts CDN）加载失败，请检查网络后刷新页面</p>';
    return null;
  }
  if (!chartInstances[domId]) {
    chartInstances[domId] = echarts.init(document.getElementById(domId));
  }
  return chartInstances[domId];
}

// 通用折线图渲染：xDates 为日期数组，seriesList 为 { name, data } 数组
function renderLineChart(domId, xDates, seriesList) {
  const chart = getChart(domId);
  if (!chart) return;

  // 点太多时隐藏圆点，曲线更清晰
  const showSymbol = xDates.length <= 30;

  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: seriesList.length > 1 ? { data: seriesList.map((s) => s.name), top: 0 } : undefined,
    grid: { left: 70, right: 20, top: seriesList.length > 1 ? 30 : 20, bottom: 30 },
    xAxis: { type: 'category', data: xDates },
    // scale 避免纵轴从 0 开始，看不出波动
    yAxis: { type: 'value', scale: true },
    series: seriesList.map((s) => ({
      name: s.name,
      type: 'line',
      data: s.data,
      showSymbol,
      areaStyle: s.area ? { opacity: 0.15 } : undefined,
      lineStyle: { width: 2 },
    })),
  });
}

// 净值走势图（基金历史表现区）
function renderNavChart(navPoints) {
  renderLineChart(
    'nav-chart',
    navPoints.map((p) => p.date),
    [{ name: '单位净值', data: navPoints.map((p) => p.nav) }]
  );
}

// 历史模拟收益趋势图（收益趋势区：模拟收益 + 模拟市值两条线）
function renderTrendChart(points) {
  renderLineChart(
    'trend-chart',
    points.map((p) => p.date),
    [
      { name: '模拟收益', data: points.map((p) => p.simulated_profit), area: true },
      { name: '模拟市值', data: points.map((p) => p.market_value) },
    ]
  );
}

// 窗口尺寸变化时重绘所有图表（ECharts 不会自动适应容器变化）
window.addEventListener('resize', () => {
  Object.values(chartInstances).forEach((chart) => chart.resize());
});
