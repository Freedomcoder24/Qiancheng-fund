/**
 * AI 助手对话面板（Phase 17，Phase 18 升级：多轮会话 + SSE 流式输出）
 *
 * 职责边界：
 * - 前端只负责"发送问题 + 消费事件流 + 展示"，Agent 判断、Tool Calling、
 *   红线检测全部由后端负责（/api/agent/chat/stream，SSE）；
 * - 红线安全性：后端最终回复先完整生成并通过红线检测，之后才分块推送，
 *   前端不会出现"先显示违规文本再被撤销"的情况；
 * - 多轮会话：首次请求后从 session 事件取得 session_id 并保存，
 *   后续请求回传以延续上下文；「新建会话」丢弃 id 重新开始；
 * - 回答渲染：先整体 HTML 转义再做有限 Markdown 转换（加粗 / 行内代码 /
 *   代码块 / 标题 / 列表），杜绝注入；其余内容一律 textContent；
 * - 会话记录仅保存在当前页面（后端会话为进程内存，服务重启即清空）。
 */

// 工具名 → 用户可读标签（done 事件徽章展示用；流式状态 label 由后端下发）
const AGENT_TOOL_LABELS = {
  get_fund_detail: '查询基金基本信息',
  get_fund_history: '查询历史净值',
  get_fund_performance: '查询区间表现',
  get_holdings: '查询持仓列表',
  get_portfolio_summary: '查询账户汇总',
  get_holding_history: '查询持仓历史走势',
  get_market_index: '查询大盘行情',
};

// 欢迎语（纯展示，不进入请求）
const AGENT_WELCOME =
  '你好，我是 AI 助手。可以试着问我：\n' +
  '· 今天大盘怎么样？\n' +
  '· 帮我分析一下我现在的持仓\n' +
  '· 查一下基金 000001 的最新净值\n' +
  '· 我的第一只持仓最近一个月走势怎么样？\n' +
  '分析需要查询后端数据，通常需要 10~60 秒，请耐心等待。';

// 运行状态
let agentBusy = false;
let agentTimerId = null;
let agentElapsedSeconds = 0;
let agentSessionId = ''; // Phase 18：当前会话 ID（后端下发，空串 = 下轮新建）

// ============ 安全渲染工具 ============

// HTML 转义（所有动态文本的渲染都从这里过一遍）
function escapeAgentHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// 有限 Markdown 转换：先整体转义再拼 HTML，支持
// ```代码块``` / `行内代码` / **加粗** / # 标题 / - 与 1. 列表 / 换行
function renderAgentMarkdown(text) {
  const lines = escapeAgentHtml(text).split(/\r?\n/);
  let html = '';
  let listMode = null; // 'ul' | 'ol' | null
  let inCode = false;

  const closeList = () => {
    if (listMode) {
      html += listMode === 'ul' ? '</ul>' : '</ol>';
      listMode = null;
    }
  };
  // 行内元素：行内代码 / 加粗（内容已转义，替换标签安全）
  const inline = (s) =>
    s.replace(/`([^`]+)`/g, '<code>$1</code>')
     .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  for (const raw of lines) {
    // 代码围栏开合
    if (/^\s*```/.test(raw)) {
      if (inCode) {
        html += '</code></pre>';
        inCode = false;
      } else {
        closeList();
        html += '<pre class="agent-pre"><code>';
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      html += raw + '\n';
      continue;
    }

    const line = raw.trim();
    if (!line) {
      closeList();
      continue;
    }
    // 标题（模型输出的 ### 小标题统一渲染为面板小节标题）
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      closeList();
      html += `<h4 class="agent-md-h">${inline(heading[1])}</h4>`;
      continue;
    }
    // 无序列表（- / * / •）
    const ulItem = line.match(/^[-*•]\s+(.*)$/);
    if (ulItem) {
      if (listMode !== 'ul') {
        closeList();
        html += '<ul class="agent-md-list">';
        listMode = 'ul';
      }
      html += `<li>${inline(ulItem[1])}</li>`;
      continue;
    }
    // 有序列表（1. / 1、）
    const olItem = line.match(/^\d+[.、]\s+(.*)$/);
    if (olItem) {
      if (listMode !== 'ol') {
        closeList();
        html += '<ol class="agent-md-list">';
        listMode = 'ol';
      }
      html += `<li>${inline(olItem[1])}</li>`;
      continue;
    }
    // 普通段落
    closeList();
    html += `<p>${inline(line)}</p>`;
  }
  closeList();
  if (inCode) html += '</code></pre>'; // 未闭合围栏兜底
  return html;
}

// ============ 消息 DOM 构建（结构化部分一律 textContent） ============

function getMessagesBox() {
  return document.getElementById('agent-messages');
}

function scrollChatToBottom() {
  const box = getMessagesBox();
  box.scrollTop = box.scrollHeight;
}

function appendUserMessage(text) {
  const box = getMessagesBox();
  const el = document.createElement('div');
  el.className = 'agent-msg agent-msg-user';
  el.textContent = text;
  box.appendChild(el);
  scrollChatToBottom();
}

function appendAgentWelcome() {
  const box = getMessagesBox();
  const el = document.createElement('div');
  el.className = 'agent-msg agent-msg-ai agent-msg-welcome';
  el.textContent = AGENT_WELCOME;
  box.appendChild(el);
}

// 会话提示（轻提示条：新建会话 / 清空当前会话后的状态说明）
function appendSessionNote(text) {
  const box = getMessagesBox();
  const el = document.createElement('div');
  el.className = 'agent-session-note';
  el.textContent = text;
  box.appendChild(el);
  scrollChatToBottom();
}

// AI 回复气泡（流式：body 先建壳，delta 到达后逐步填充）
function createAgentAnswerBubble() {
  const box = getMessagesBox();
  const el = document.createElement('div');
  el.className = 'agent-msg agent-msg-ai';
  const body = document.createElement('div');
  body.className = 'agent-md';
  el.appendChild(body);
  box.appendChild(el);
  scrollChatToBottom();
  return { el, body };
}

// 完成 AI 气泡：工具链徽章 + 元信息 + 免责声明（done 事件数据）
function finalizeAgentAnswerBubble(bubble, done) {
  // 工作过程：轻量工具链标签（无工具则不展示）
  if (done.tools_used && done.tools_used.length > 0) {
    const tools = document.createElement('div');
    tools.className = 'agent-tools';
    const label = document.createElement('span');
    label.className = 'agent-tools-label';
    label.textContent = '已查询：';
    tools.appendChild(label);
    done.tools_used.forEach((t, index) => {
      if (index > 0) {
        const arrow = document.createElement('span');
        arrow.className = 'agent-tools-arrow';
        arrow.textContent = '→';
        tools.appendChild(arrow);
      }
      const chip = document.createElement('span');
      chip.className = 'agent-tool-chip';
      chip.textContent = AGENT_TOOL_LABELS[t.tool] || t.tool;
      chip.title = `工具 ${t.tool}`; // 悬停可见原始工具名，平时不打扰
      tools.appendChild(chip);
    });
    bubble.el.insertBefore(tools, bubble.body);
  }

  const meta = document.createElement('p');
  meta.className = 'agent-msg-meta';
  meta.textContent = `${done.model} · ${done.rounds} 轮分析 · ${done.generated_at}`;
  bubble.el.appendChild(meta);

  const disclaimer = document.createElement('p');
  disclaimer.className = 'agent-msg-disclaimer';
  disclaimer.textContent = done.disclaimer || '';
  bubble.el.appendChild(disclaimer);
  scrollChatToBottom();
}

// 错误气泡：保留后端 detail 完整文案（含 502 红线拒绝信息）
function appendAgentError(message) {
  const box = getMessagesBox();
  const el = document.createElement('div');
  el.className = 'agent-msg agent-msg-error';
  el.textContent = message;
  box.appendChild(el);
  scrollChatToBottom();
}

// ============ 加载状态（阶段文字 + 计时两条线） ============

function setAgentInputDisabled(disabled) {
  document.getElementById('agent-input').disabled = disabled;
  document.getElementById('agent-send-btn').disabled = disabled;
}

function showAgentLoading() {
  const box = getMessagesBox();
  const el = document.createElement('div');
  el.className = 'agent-msg agent-msg-ai agent-msg-loading';

  const status = document.createElement('p');
  status.className = 'agent-loading-status ai-loading';
  status.textContent = 'AI 助手正在思考...';

  const text = document.createElement('p');
  text.className = 'agent-loading-text';
  text.textContent = '已用时 0 秒';

  const bar = document.createElement('div');
  bar.className = 'agent-typing';
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement('span');
    bar.appendChild(dot);
  }

  el.appendChild(status);
  el.appendChild(text);
  el.appendChild(bar);
  box.appendChild(el);
  scrollChatToBottom();
  return el;
}

// 更新阶段文字（thinking / tool / composing）
function setAgentLoadingStage(loadingEl, text) {
  const status = loadingEl.querySelector('.agent-loading-status');
  if (status) status.textContent = text;
}

function startAgentLoadingTimer(loadingEl) {
  agentElapsedSeconds = 0;
  const textEl = loadingEl.querySelector('.agent-loading-text');
  agentTimerId = setInterval(() => {
    agentElapsedSeconds += 1;
    const hint = agentElapsedSeconds >= 15 ? '（查询与生成为真实耗时，请稍候）' : '';
    textEl.textContent = `已用时 ${agentElapsedSeconds} 秒${hint}`;
  }, 1000);
}

function stopAgentLoadingTimer() {
  if (agentTimerId) {
    clearInterval(agentTimerId);
    agentTimerId = null;
  }
}

// ============ SSE 事件流消费 ============

// 解析一条 SSE data 行 → 事件对象（非法行忽略）
function parseAgentSseLine(rawLine) {
  if (!rawLine.startsWith('data:')) return null;
  try {
    return JSON.parse(rawLine.slice(5).trim());
  } catch (err) {
    console.error('SSE 事件解析失败：', rawLine, err);
    return null;
  }
}

// 处理单个事件；返回值：
// 'delta' → 已有增量进入气泡；'done' → 完成；'error' → 出错；其他 → 继续
function handleAgentEvent(ev, ctx) {
  switch (ev.type) {
    case 'session':
      agentSessionId = ev.session_id || '';
      return 'continue';

    case 'status':
      if (ev.stage === 'tool') {
        setAgentLoadingStage(ctx.loadingEl, `正在${ev.label || '查询数据'}...`);
      } else if (ev.stage === 'composing') {
        setAgentLoadingStage(ctx.loadingEl, '正在生成最终回复...');
      } else {
        setAgentLoadingStage(ctx.loadingEl, 'AI 助手正在思考...');
      }
      return 'continue';

    case 'tool_done':
      setAgentLoadingStage(
        ctx.loadingEl,
        (ev.ok ? '已完成：' : '查询失败：') + (ev.label || '') + '，继续分析...'
      );
      return 'continue';

    case 'delta': {
      if (!ctx.bubble) ctx.bubble = createAgentAnswerBubble();
      ctx.answerText += ev.text || '';
      ctx.bubble.body.innerHTML = renderAgentMarkdown(ctx.answerText);
      scrollChatToBottom();
      return 'delta';
    }

    case 'done':
      if (!ctx.bubble) ctx.bubble = createAgentAnswerBubble();
      // 以 done.answer 为准重渲染一次（保证与后端完全一致）
      ctx.bubble.body.innerHTML = renderAgentMarkdown(ev.answer || '');
      finalizeAgentAnswerBubble(ctx.bubble, ev);
      agentSessionId = ev.session_id || agentSessionId;
      return 'done';

    case 'error':
      appendAgentError(ev.detail || 'AI 助手暂时无法回答，请稍后重试');
      return 'error';

    default:
      return 'continue';
  }
}

// 流式主流程：POST /api/agent/chat/stream 并逐块消费
async function streamAgentChat(message, ctx) {
  let resp;
  try {
    resp = await fetch('/api/agent/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, session_id: agentSessionId }),
    });
  } catch (err) {
    console.error('网络请求失败：', err);
    throw new Error('网络异常，请确认后端服务已启动');
  }

  if (!resp.ok || !resp.body) {
    // 400 / 422 等 HTTP 层错误：detail 已是中文可读文案
    let detail = `请求失败（HTTP ${resp.status}）`;
    try {
      const data = await resp.json();
      if (data && data.detail) detail = data.detail;
    } catch (err) { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let outcome = 'continue';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const rawEvent = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 2);
      if (!rawEvent) continue;
      const ev = parseAgentSseLine(rawEvent);
      if (!ev) continue;
      outcome = handleAgentEvent(ev, ctx);
      if (outcome === 'done' || outcome === 'error') return outcome;
    }
  }
  return outcome;
}

// ============ 发送主流程 ============

async function sendAgentMessage() {
  if (agentBusy) return;
  const input = document.getElementById('agent-input');
  const text = input.value.trim();
  if (!text) {
    showToast('请输入问题后再发送', 'info');
    return;
  }

  agentBusy = true;
  setAgentInputDisabled(true);
  input.value = '';
  input.style.height = 'auto';
  appendUserMessage(text);

  const loadingEl = showAgentLoading();
  startAgentLoadingTimer(loadingEl);

  const ctx = { loadingEl, bubble: null, answerText: '' };
  try {
    const outcome = await streamAgentChat(text, ctx);
    if (outcome !== 'done' && outcome !== 'error') {
      // 流异常中断（无 done / error 事件）
      appendAgentError('连接中断，本次回答未完成，请重试');
    }
  } catch (err) {
    appendAgentError(friendlyError(err));
  } finally {
    stopAgentLoadingTimer();
    loadingEl.remove();
    agentBusy = false;
    setAgentInputDisabled(false);
    document.getElementById('agent-input').focus();
  }
}

// ============ 会话管理 ============

// 新建会话：丢弃 session_id + 清空屏幕（后端旧会话由 TTL 自动过期）
function startNewAgentSession() {
  if (agentBusy) {
    showToast('AI 助手正在分析中，请等待本次回答完成', 'info');
    return;
  }
  agentSessionId = '';
  getMessagesBox().innerHTML = '';
  appendAgentWelcome();
  showToast('已开始新会话', 'success');
}

// 清空当前会话：只清屏幕，保留 session_id（上下文仍延续，如实提示）
function clearAgentChatScreen() {
  if (agentBusy) {
    showToast('AI 助手正在分析中，请等待本次回答完成', 'info');
    return;
  }
  getMessagesBox().innerHTML = '';
  appendAgentWelcome();
  appendSessionNote(
    agentSessionId
      ? '已清空屏幕。当前会话上下文仍保留，AI 可继续理解之前的提问。'
      : '已清空屏幕。'
  );
}

// 输入框高度自适应（上限 5 行左右）
function autoResizeAgentInput() {
  const input = document.getElementById('agent-input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
}

// ============ 初始化 ============

// 页面入口：由 app.js 在 DOMContentLoaded 中调用
function initAgentChat() {
  const input = document.getElementById('agent-input');
  const sendBtn = document.getElementById('agent-send-btn');
  const statusTip = document.getElementById('agent-status-tip');

  appendAgentWelcome();

  // AI 配置检查：未配置时提示并禁用输入（复用 AI 分析面板的检查接口）
  api.getAIStatus().then((status) => {
    if (!status.configured) {
      statusTip.textContent =
        'AI 功能未配置：请在下方「AI API 配置」面板填写并保存，或在 .env 中配置后重启服务';
      statusTip.style.display = 'block';
      setAgentInputDisabled(true);
    }
  }).catch(() => { /* 后端未启动时健康检查已有全局提示 */ });

  // 发送按钮 / Enter 发送（Shift+Enter 换行；中文输入法组词回车不发送）
  sendBtn.addEventListener('click', sendAgentMessage);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendAgentMessage();
    }
  });
  input.addEventListener('input', autoResizeAgentInput);

  // 会话管理按钮（Phase 18）
  document.getElementById('agent-new-session-btn').addEventListener('click', startNewAgentSession);
  document.getElementById('agent-clear-btn').addEventListener('click', clearAgentChatScreen);
}
