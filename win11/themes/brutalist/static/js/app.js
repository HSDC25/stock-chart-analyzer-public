// StockChart 前端应用（win11 四主题共享）
// 注意：本文件在 dark/minimalist/highend/brutalist 四个主题下是逐字节相同的
// 拷贝——修改任何逻辑必须改 dark 这份后复制到其余三份，主题视觉差异一律
// 通过各主题 style.css 里的 CSS 变量（--chart-* 等）表达，不要写死在本文件。
// 交互概览：指标筛选 / 排行榜 / 代码输入 / 技术分析 四种模式，
// 双击表格行打开 K 线图，图表配色在渲染时读取 CSS 变量自动适配主题。
// CSRF：所有 POST 必须带 X-CSRF-Token（ensureCsrfToken 自动处理）。
// 全局变量
let currentStock = null;
let _mlScreenPollSeq = 0; // ML 筛选轮询序号：丢弃旧轮询
let _rowOpenTimer = null; // 单击代码格延迟打开定时器（与双击区分）
let _preloadCompleted = false;
let _preloadRunning = false;
let _waitingForClose = false;
let _loadSeq = 0; // K线加载请求序号：丢弃过期响应
let _rankSeq = 0; // 排行榜请求序号：丢弃过期响应
var _chartResizeTimer = null; // 图表延迟 resize 定时器

// HTML 转义：所有拼进 innerHTML 的动态字符串必须先经过此函数
function escapeHtml(s) {
 return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
  return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
 });
}

// ─────────────────────────────────────────────────────────────
// CSRF：所有 POST 必须带 X-CSRF-Token（ensureCsrfToken 自动处理）。
// 首次调用向 /api/csrf-token 取回并缓存到 window._csrfToken。
// ─────────────────────────────────────────────────────────────
async function ensureCsrfToken() {
 if (window._csrfToken) return window._csrfToken;
 try {
  const resp = await fetch('/api/csrf-token');
  const data = await resp.json();
  if (data && data.csrf_token) {
   window._csrfToken = data.csrf_token;
   return window._csrfToken;
  }
 } catch (e) {
  console.error('获取 CSRF token 失败:', e);
 }
 return '';
}

// 统一 POST 封装：自动附带 CSRF 头，返回解析后的 JSON
async function postJson(url, bodyObj) {
 const resp = await fetch(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': await ensureCsrfToken() },
  body: JSON.stringify(bodyObj || {})
 });
 return resp.json();
}

// 初始化
document.addEventListener('DOMContentLoaded', function() {
 startRealtimeClock();
 // CSRF 预热：页面加载即取回 token，避免首次 POST 时阻塞
 ensureCsrfToken();

 // 事件委托：双击表格行打开 K 线图
 document.getElementById('stockListBody').addEventListener('dblclick', function(e) {
 var row = e.target.closest('tr');
 if (!row) return;
 if (_rowOpenTimer) { clearTimeout(_rowOpenTimer); _rowOpenTimer = null; } // 双击时取消单击延迟
 openStockFromRow(row);
 });

 // 事件委托：单击"股票代码"单元格也打开 K 线（300ms 延迟与双击区分）
 document.getElementById('stockListBody').addEventListener('click', function(e) {
 var row = e.target.closest('tr');
 if (!row) return;
 if (!e.target.closest('.stock-code')) return; // 仅代码格响应单击
 if (_rowOpenTimer) clearTimeout(_rowOpenTimer);
 _rowOpenTimer = setTimeout(function() {
  _rowOpenTimer = null;
  openStockFromRow(row);
 }, 300);
 });

 // 键盘可达性：表格行 tabindex=0，Enter/Space 打开 K 线
 document.getElementById('stockListBody').addEventListener('keydown', function(e) {
 if (e.key !== 'Enter' && e.key !== ' ') return;
 var row = e.target.closest('tr');
 if (!row) return;
 e.preventDefault();
 openStockFromRow(row);
 });

 // 事件委托：双击 ML 筛选结果打开 K 线图
 var mlResult = document.getElementById('mlScreenResult');
 if (mlResult) {
 mlResult.addEventListener('dblclick', function(e) {
  var item = e.target.closest('.stock-item');
  if (!item) return;
  var code = item.getAttribute('data-code');
  if (code) loadStockByCode(code);
 });
 // 键盘可达性：结果项 tabindex=0，Enter/Space 打开 K 线
 mlResult.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  var item = e.target.closest('.stock-item');
  if (!item) return;
  e.preventDefault();
  var code = item.getAttribute('data-code');
  if (code) loadStockByCode(code);
 });
 }

 // Esc 关闭图表面板（复用关闭按钮逻辑）
 document.addEventListener('keydown', function(e) {
 if (e.key !== 'Escape' && e.key !== 'Esc') return;
 var panel = document.getElementById('chartPanel');
 if (panel && panel.style.display === 'flex') closeChartPanel();
 });

 // 表头键盘排序：所有 tabindex=0 的排序 th，Enter/Space 触发与点击同款排序
 document.querySelectorAll('.stock-table thead th[tabindex="0"]').forEach(function(th) {
 th.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  e.preventDefault();
  th.click(); // 复用内联 onclick="sortTable(...)"
 });
 });

 // 检查市场状态（初始化一次，更新导航栏状态圆点）
 fetch('/api/market_status')
 .then(function(r) { return r.json(); })
 .then(function(data) {
 var dot = document.getElementById('statusDot');
 var text = document.getElementById('statusText');
 if (data.success) {
 if (data.is_market_closed) {
 dot.classList.add('closed');
 text.textContent = data.reason || '已收盘';
 } else {
 dot.classList.remove('closed');
 text.textContent = data.reason || '交易中';
 }
 } else {
 text.textContent = data.reason || '未知';
 }
 })
 .catch(function(e) {
 console.error('市场状态获取失败:', e);
 document.getElementById('statusText').textContent = '连接失败';
 });

 // 检查本地数据（右上角数据日期/更新时间 + 预加载状态）
 refreshDataStatus();
 // 初始化 K线图面板高度拖动
 initChartDrag();

 // 定时轮询预加载进度（页面不可见时跳过，减少无谓请求）
 setInterval(function() { if (document.hidden) return; checkPreloadStatus(); }, 3000);
 // 每 30 秒刷新一次数据状态，保证右上角更新时间与本地实际数据保持一致
 setInterval(function() { if (document.hidden) return; refreshDataStatus(); }, 30000);

 // 字号
 var savedSize = localStorage.getItem('fontSize') || 'medium';
 setFontSize(savedSize);
});

// 从表格行取股票代码并打开 K 线（dblclick / 单击代码格 / 键盘共用）
function openStockFromRow(tr) {
 if (!tr) return;
 var code = tr.getAttribute('data-code');
 if (!code) {
 var codeCell = tr.querySelector('.stock-code');
 if (codeCell) code = codeCell.textContent.trim();
 }
 if (code) loadStockByCode(code);
}

// 移除首次加载态的脉动动画（数据就绪后调用）
function removeLoadingPulse() {
 var badge = document.getElementById('dataDateLabel');
 if (badge) badge.classList.remove('loading-pulse');
 var preloadInfo = document.querySelector('.preload-info');
 if (preloadInfo) preloadInfo.classList.remove('loading-pulse');
 var emptyCell = document.querySelector('#stockListBody td.loading-pulse');
 if (emptyCell) emptyCell.classList.remove('loading-pulse');
}

// ─────────────────────────────────────────────────────────────
// 刷新数据状态：右上角数据日期/更新时间 + 预加载按钮状态
// 说明：同步模式下数据来自云端服务器下载的本地库，右上角必须
// 与实际数据（stock_metadata.updated_at / 本地库文件时间）保持一致。
// ─────────────────────────────────────────────────────────────
async function refreshDataStatus() {
 try {
  const resp = await fetch('/api/data-status');
  const data = await resp.json();
  if (!data.success) return;
  // 数据就绪：移除首次加载的脉动动画
  removeLoadingPulse();

  // 右上角：数据日期 + 实际更新时间
  var dateLabel = document.getElementById('dataDateLabel');
  if (data.latest_date && dateLabel) {
   // 优先显示真实数据更新时间（实时行情抓取时间），无则退回本地库同步时间
   var updateTime = data.data_update_time || data.file_mtime || '';
   var timeSuffix = '';
   if (updateTime) {
    timeSuffix = ' · 更新 ' + updateTime.slice(0, 16); // YYYY-MM-DD HH:MM
   }
   dateLabel.textContent = '数据: ' + data.latest_date + timeSuffix +
    (data.fresh ? '' : ' (非最新)');
   dateLabel.title = 'K线最新交易日: ' + data.latest_date +
    (data.data_update_time ? '\n数据更新时间: ' + data.data_update_time : '') +
    (data.file_mtime ? '\n本地同步时间: ' + data.file_mtime : '');
   if (data.fresh) {
    dateLabel.classList.add('data-fresh');
    dateLabel.classList.remove('data-stale');
   } else {
    dateLabel.classList.add('data-stale');
    dateLabel.classList.remove('data-fresh');
   }
  }

  var btn = document.getElementById('preloadBtn');
  var bar = document.getElementById('preloadBar');
  var percent = document.getElementById('preloadPercent');
  var detail = document.getElementById('preloadDetail');
  var count = document.getElementById('preloadCount');
  if (!btn || !bar || !detail) return;

  // 预加载运行中：不得覆盖按钮状态（避免把加载中的按钮恢复为可点）
  if (_preloadRunning) return;

  // 数据新鲜且无预加载运行：已就绪
  if (data.latest_date && data.stock_count >= 100 && data.fresh) {
   _preloadCompleted = true;
   bar.style.width = '100%';
   bar.setAttribute('aria-valuenow', '100');
   percent.textContent = '100%';
   detail.innerHTML = ' 数据已是最新<br><small>' + data.latest_date + ' | ' + data.stock_count + ' 只股票</small>';
   count.textContent = '';
   btn.textContent = ' 已就绪，点击重新加载';
   btn.disabled = false;
   btn.classList.add('btn-success');
  } else if (data.latest_date && data.stock_count >= 100) {
   // 有数据但非最新：等待收盘或等待云端更新，可手动重新加载（不自动发请求）
   var dot = document.getElementById('statusDot');
   var marketClosed = dot && dot.classList.contains('closed');
   detail.innerHTML = ' <b>数据非最新</b><br><small>数据日期: ' + data.latest_date +
    (marketClosed ? '<br>已收盘，可点击下方按钮重新加载' : '<br>等待收盘后更新') + '</small>';
   bar.style.width = '50%';
   bar.setAttribute('aria-valuenow', '50');
   percent.textContent = '等待';
   btn.textContent = ' 点击重新加载';
   btn.disabled = false;
  } else {
   // 无数据
   detail.textContent = '暂无数据，点击加载';
   btn.textContent = '开始加载';
   btn.disabled = false;
   bar.style.width = '0%';
   bar.setAttribute('aria-valuenow', '0');
  }
 } catch (e) {
  console.error('数据状态获取失败:', e);
 }
}

// 实时时钟
function startRealtimeClock() {
 function updateClock() {
 const now = new Date();
 document.getElementById('realtimeClock').textContent =
 now.getFullYear() + '-' +
 String(now.getMonth() + 1).padStart(2, '0') + '-' +
 String(now.getDate()).padStart(2, '0') + ' ' +
 String(now.getHours()).padStart(2, '0') + ':' +
 String(now.getMinutes()).padStart(2, '0') + ':' +
 String(now.getSeconds()).padStart(2, '0');
 }
 updateClock();
 setInterval(updateClock, 1000);
}

// 字号调整
function setFontSize(size) {
 // 显式映射：'extra-large' → 'btnExtraLarge'（不能用驼峰拼接）
 var sizeToBtn = { small: 'btnSmall', medium: 'btnMedium', large: 'btnLarge', 'extra-large': 'btnExtraLarge' };
 document.body.classList.remove('font-small', 'font-medium', 'font-large', 'font-extra-large');
 document.body.classList.add('font-' + size);

 var activeId = sizeToBtn[size] || 'btnMedium';
 ['btnSmall', 'btnMedium', 'btnLarge', 'btnExtraLarge'].forEach(function(id) {
 var el = document.getElementById(id);
 if (!el) return;
 var isActive = (id === activeId);
 el.classList.toggle('active', isActive);
 el.setAttribute('aria-pressed', isActive ? 'true' : 'false');
 });
 localStorage.setItem('fontSize', size);
}

// 开始预加载
async function startPreload() {
 const btn = document.getElementById('preloadBtn');
 if (_preloadCompleted) {
 if (!confirm('预加载已完成，是否要重新加载？')) return;
 _preloadCompleted = false;
 }
 btn.textContent = '启动中...';
 btn.disabled = true;
 try {
 const response = await fetch('/api/preload', {
  method: 'POST',
  headers: { 'X-CSRF-Token': await ensureCsrfToken() }
 });
 const data = await response.json();
 if (data.success) {
 btn.textContent = '加载进行中...';
 }
 } catch (e) {
 btn.textContent = '启动失败';
 setTimeout(function() { btn.textContent = '开始加载'; btn.disabled = false; }, 2000);
 }
}

// 检查预加载状态
async function checkPreloadStatus() {
 try {
 const response = await fetch('/api/preload/status');
 const data = await response.json();
 if (data.success) updatePreloadUI(data);
 } catch (e) {
 console.error('获取状态失败:', e);
 }
}

// 更新UI（带状态保护）
function updatePreloadUI(data) {
 const bar = document.getElementById('preloadBar');
 const percent = document.getElementById('preloadPercent');
 const detail = document.getElementById('preloadDetail');
 const count = document.getElementById('preloadCount');
 const btn = document.getElementById('preloadBtn');

 const total = data.total || 0;
 const loaded = data.loaded || 0;
 const running = data.running || false;

 _preloadRunning = running;

 // 预加载完成后，忽略所有轮询更新
 if (_preloadCompleted && !running) {
 return;
 }

 // 等待收盘状态，忽略所有轮询更新
 if (_waitingForClose && !running) {
 return;
 }

 const pct = total > 0 ? Math.round((loaded / total) * 100) : 0;
 bar.setAttribute('aria-valuenow', String(pct));

 if (running) {
 bar.style.width = pct + '%';
 bar.className = 'progress-fill active';
 percent.textContent = pct + '%';
 btn.textContent = '加载中...';
 btn.disabled = true;
 detail.textContent = data.message || '加载中...';
 } else if (loaded > 0 && loaded >= total && total > 0) {
 _preloadCompleted = true;
 bar.style.width = '100%';
 bar.className = 'progress-fill';
 percent.textContent = '100%';
 detail.innerHTML = ' <b>加载完成</b><br><small>' + loaded + ' 只股票</small>';
 count.textContent = '';
 btn.textContent = ' 已就绪，点击重新加载';
 btn.disabled = false;
 btn.classList.add('btn-success');
 // 加载完成后立即刷新右上角数据日期/更新时间
 refreshDataStatus();
 } else {
 btn.textContent = '开始加载';
 btn.disabled = false;
 }
}

// 获取输入值
function getValue(id) {
 const el = document.getElementById(id);
 return el && el.value ? parseFloat(el.value) : null;
}

// 获取代码列表
function getCodes() {
 const el = document.getElementById('codesInput');
 if (!el || !el.value) return null;
 return el.value.split(',').map(function(c) { return c.trim(); }).filter(function(c) { return c; });
}

// 搜索股票
async function searchStocks() {
 const btn = document.querySelector('#filterMode .btn-primary');
 const originalText = btn.textContent;
 btn.textContent = '筛选中...';
 btn.disabled = true;

 const criteria = {
 three_day_change_min: getValue('threeDayChangeMin'),
 volume_change_pct_min: getValue('volumeChangePctMin'),
 ...getSortParams()
 };

 try {
 const data = await postJson('/api/stocks', criteria);
 if (data.success) {
 renderStockList(data.data, data.total);
 } else {
 alert('搜索失败: ' + (data.error || '未知错误'));
 }
 } catch (e) {
 alert('请求失败: ' + e.message);
 } finally {
 btn.textContent = originalText;
 btn.disabled = false;
 }
}

// 按代码搜索
async function searchByCodes() {
 const codes = getCodes();
 if (!codes || codes.length === 0) {
 alert('请输入股票代码');
 return;
 }
 const btn = document.querySelector('#codeMode .btn-primary');
 const originalText = btn.textContent;
 btn.textContent = '加载中...';
 btn.disabled = true;

 try {
 const data = await postJson('/api/stocks', { codes: codes });
 if (data.success) {
 renderStockList(data.data, data.total);
 } else {
 alert('搜索失败: ' + (data.error || '未知错误'));
 }
 } catch (e) {
 alert('请求失败: ' + e.message);
 } finally {
 btn.textContent = originalText;
 btn.disabled = false;
 }
}

// 排序参数
function getSortParams() {
 const el = document.getElementById('sortOrder');
 if (!el || el.value === 'default') return {};
 if (el.value === 'price_asc') return { sort_by: 'price', sort_order: 'asc' };
 if (el.value === 'price_desc') return { sort_by: 'price', sort_order: 'desc' };
 return {};
}

// 渲染股票列表
// 右上角结果计数：实际筛选数 >= 100 时，额外展示被截断的数量
function updateCountBadge(total, shown) {
 var el = document.getElementById('stockCount');
 if (!el) return;
 total = total || 0;
 shown = shown || 0;
 if (total >= 100 && shown < total) {
  el.textContent = '实际筛选 ' + total + ' 只 · 显示前 ' + shown + ' 只（截断 ' + (total - shown) + ' 只）';
  el.classList.add('count-truncated');
 } else {
  el.textContent = shown + ' 只';
  el.classList.remove('count-truncated');
 }
}

function renderStockList(stocks, total) {
 const tbody = document.getElementById('stockListBody');
 const countEl = document.getElementById('stockCount');

 // 缓存数据供列排序使用
 window._currentTableData = stocks || [];
 window._currentTableIsRank = false;
 window._currentTableChangeField = 'three_day_change';
 window._currentTableTotal = total || 0;
 // 重置排序状态与箭头
 _currentSortField = '';
 _currentSortState = 0;
 document.querySelectorAll('.sort-arrow').forEach(function(el) { el.className = 'sort-arrow'; });

 if (!stocks || stocks.length === 0) {
 updateCountBadge(total || 0, 0);
 tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#999;padding:40px;">没有符合条件的股票</td></tr>';
 return;
 }

 updateCountBadge(total, stocks.length);

 tbody.innerHTML = stocks.map(function(stock, i) {
 const changeClass = stock.change_pct > 0 ? 'change-up' : (stock.change_pct < 0 ? 'change-down' : '');
 const t3Class = stock.three_day_change > 0 ? 'change-up' : (stock.three_day_change < 0 ? 'change-down' : '');
 return '<tr data-code="' + escapeHtml(stock.code) + '" tabindex="0" style="cursor:pointer;">' +
 '<td>' + (i + 1) + '</td>' +
 '<td class="stock-code">' + escapeHtml(stock.code) + '</td>' +
 '<td class="stock-name">' + escapeHtml(stock.name) + '</td>' +
 '<td class="text-right">' + (stock.price != null ? stock.price.toFixed(2) : '-') + '</td>' +
 '<td class="text-right ' + changeClass + '">' + (stock.change_pct != null ? (stock.change_pct > 0 ? '+' : '') + stock.change_pct.toFixed(2) + '%' : '-') + '</td>' +
 '<td class="text-right ' + t3Class + '">' + (stock.three_day_change != null ? (stock.three_day_change > 0 ? '+' : '') + stock.three_day_change.toFixed(2) + '%' : '-') + '</td>' +
 '<td class="text-right">' + (stock.volume_change_pct != null ? stock.volume_change_pct.toFixed(2) + '%' : '-') + '</td>' +
 '<td class="text-right">' + (stock.pe != null ? stock.pe.toFixed(2) : '-') + '</td>' +
 '<td class="text-right">' + (stock.pb != null ? stock.pb.toFixed(2) : '-') + '</td>' +
 '<td class="text-right">' + (stock.market_cap != null ? stock.market_cap.toFixed(0) : '-') + '</td>' +
 '</tr>';
 }).join('');
}

// ─────────────────────────────────────────────────────────────
// 表格列排序：点击表头在 默认排序 → 升序 → 降序 之间循环
// ─────────────────────────────────────────────────────────────
var _currentSortField = '';
var _currentSortState = 0; // 0=默认 1=升序 2=降序

function sortTable(field) {
 if (!window._currentTableData || !window._currentTableData.length) return;

 // 三态循环：默认 → 升序 → 降序 → 默认
 if (_currentSortField === field) {
  _currentSortState = (_currentSortState + 1) % 3;
 } else {
  _currentSortField = field;
  _currentSortState = 1; // 新列首次点击默认升序
 }

 // 第6列（N日涨幅）在不同榜单/模式下实际字段名不同
 var actualField = field;
 if (field === 'n_day_change') {
  actualField = window._currentTableChangeField || 'three_day_change';
 }

 var data = window._currentTableData.slice();
 if (_currentSortState === 1 || _currentSortState === 2) {
  var dir = _currentSortState === 1 ? 1 : -1;
  data.sort(function(a, b) {
   var va = a[actualField], vb = b[actualField];
   if (va == null || va === '') va = -Infinity;
   if (vb == null || vb === '') vb = -Infinity;
   if (typeof va === 'string') return va.localeCompare(vb) * dir;
   return (va - vb) * dir;
  });
 }

 // 重新渲染（渲染函数内部会重置排序状态，这里先记录再恢复）
 var savedField = _currentSortField;
 var savedState = _currentSortState;
 var isRank = window._currentTableIsRank;
 if (isRank) {
  renderRankTable(data, window._currentTableTitle || '', window._currentTableChangeField || '');
 } else {
  renderStockList(data, window._currentTableTotal || data.length);
 }
 _currentSortField = savedField;
 _currentSortState = savedState;

 // 更新箭头
 document.querySelectorAll('.sort-arrow').forEach(function(el) { el.className = 'sort-arrow'; });
 var arrow = document.getElementById('sort_' + field);
 if (arrow) {
  arrow.className = 'sort-arrow ' + (_currentSortState === 1 ? 'active-asc' : (_currentSortState === 2 ? 'active-desc' : ''));
 }
}

// 加载股票图表
async function loadStockByCode(code) {
 // 序号防竞态：仅接受最新一次请求的响应
 var seq = ++_loadSeq;
 currentStock = { code: code, name: '' };

 // 显示底部 K 线面板
 var panel = document.getElementById('chartPanel');
 panel.style.display = 'flex';

 document.getElementById('chartTitle').textContent = code + ' 加载中...';
 document.getElementById('chartContainer').innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100%;"><div class="progress-fill active" style="width:100px;height:4px;"></div></div>';

 try {
 const response = await fetch('/api/stock/' + encodeURIComponent(code) + '/kline?days=30');
 const data = await response.json();
 if (seq !== _loadSeq) return; // 过期响应，丢弃
 if (data.success) {
 currentStock.name = data.stock.name || '';
 // 标题显示最新收盘价与涨跌幅（类似股票软件）
 var kArr = (data.data && data.data.kline) || [];
 var titleSuffix = '（近30交易日）';
 if (kArr.length) {
  var lc = kArr[kArr.length - 1][1];
  var pc = kArr.length > 1 ? kArr[kArr.length - 2][1] : null;
  var chg = pc ? +(((lc - pc) / pc) * 100).toFixed(2) : null;
  titleSuffix = '　最新: ' + lc.toFixed(2) + (chg != null ? ' (' + (chg > 0 ? '+' : '') + chg.toFixed(2) + '%)' : '');
 }
 document.getElementById('chartTitle').textContent = code + ' ' + (data.stock.name || '') + titleSuffix;
 renderChart(data.data, code, data.stock.name || '');
 } else {
 document.getElementById('chartContainer').innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100%;color:#999;">加载失败: ' + escapeHtml(data.error) + '</div>';
 }
 } catch (e) {
 if (seq !== _loadSeq) return; // 过期响应，丢弃
 document.getElementById('chartContainer').innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100%;color:#999;">请求失败: ' + escapeHtml(e.message) + '</div>';
 }
}

// 关闭 K 线面板
function closeChartPanel() {
 document.getElementById('chartPanel').style.display = 'none';
}

// K线图面板：上下拖动调整高度（占屏幕比例）
function initChartDrag() {
 var bar = document.getElementById('chartDragBar');
 var panel = document.getElementById('chartPanel');
 if (!bar || !panel) return;
 bar.addEventListener('mousedown', function(e) {
  e.preventDefault();
  var startY = e.clientY;
  var startH = panel.offsetHeight;
  var maxH = Math.round(window.innerHeight * 0.85);
  function onMove(ev) {
   // 方向：鼠标上拖 → 面板变大（分隔线向上展开）；下拖 → 变小
   var h = startH - (ev.clientY - startY);
   h = Math.max(160, Math.min(maxH, h));
   panel.style.height = h + 'px';
  }
  function onUp() {
   bar.classList.remove('dragging');
   document.removeEventListener('mousemove', onMove);
   document.removeEventListener('mouseup', onUp);
   document.body.style.userSelect = '';
   if (window._currentChart) window._currentChart.resize();
  }
  bar.classList.add('dragging');
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
  document.body.style.userSelect = 'none';
 });
}

// 用 ECharts 渲染 K 线图
function renderChart(data, code, name) {
 // ECharts 未加载守卫（defer 加载失败时给出提示而不是抛异常）
 if (typeof echarts === 'undefined') {
 document.getElementById('chartContainer').innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100%;color:#999;">图表组件未加载，请刷新页面重试</div>';
 return;
 }

 // 释放旧实例与延迟 resize 定时器，避免重复 init 造成泄漏
 if (window._currentChart) {
 try { window._currentChart.dispose(); } catch (e) { /* 忽略释放异常 */ }
 window._currentChart = null;
 }
 if (_chartResizeTimer) {
 clearTimeout(_chartResizeTimer);
 _chartResizeTimer = null;
 }

 var container = document.getElementById('chartContainer');
 container.innerHTML = '<div id="echartsDiv" style="width:100%;height:100%;"></div>';

 var chart = echarts.init(document.getElementById('echartsDiv'));

 var dates = data.dates || [];
 var klines = data.kline || [];       // [open, close, low, high]
 var volumes = data.volume || [];
 var macd = data.macd || [];
 var macdSignal = data.macd_signal || [];
 var macdHist = data.macd_hist || [];

 // K 线数据：[open, close, low, high]
 var ohlc = klines.map(function(k) { return [k[0], k[1], k[2], k[3]]; });

 // MA 兜底计算
 function calcMA(data, n) {
  var result = [];
  for (var i = 0; i < data.length; i++) {
   if (i < n - 1) result.push(null);
   else {
    var sum = 0;
    for (var j = 0; j < n; j++) sum += data[i - j][1];
    result.push(+(sum / n).toFixed(2));
   }
  }
  return result;
 }

 // 每根 K 线的涨跌幅（用于 tooltip）
 var pctChanges = [];
 for (var i = 0; i < klines.length; i++) {
  var prev = i > 0 ? klines[i-1][1] : null;
  pctChanges.push(prev ? +(((klines[i][1] - prev) / prev) * 100).toFixed(2) : null);
 }

 // 最新一根：收盘价与涨跌
 var lastIdx = klines.length - 1;
 var lastClose = lastIdx >= 0 ? klines[lastIdx][1] : null;
 var lastPct = lastIdx >= 0 ? pctChanges[lastIdx] : null;

 // 主题配色：读取当前主题 style.css 中 :root 定义的 --chart-* 变量（缺省回退深色值）
 function chartVar(name, fallback) {
  var v = getComputedStyle(document.documentElement).getPropertyValue(name);
  v = v ? v.trim() : '';
  return v || fallback;
 }
 var upColor = chartVar('--chart-up', '#ef232a');
 var downColor = chartVar('--chart-down', '#14b143');
 var chartText = chartVar('--chart-text', '#8b949e');
 var chartBg = chartVar('--chart-bg', 'rgba(22,27,34,0.92)');
 var chartGrid = chartVar('--chart-grid', '#21262d');
 var chartBorder = chartVar('--chart-border', '#30363d');
 var chartAccent = chartVar('--chart-accent', '#58a6ff');
 var chartAccentSoft = chartVar('--chart-accent-soft', 'rgba(88,166,255,0.15)');
 var chartTooltipText = chartVar('--chart-tooltip-text', '#c9d1d9');

 // 成交量/量变柱颜色（红涨绿跌）
 function volColor(params) {
  var idx = params.dataIndex;
  return (klines[idx] && klines[idx][1] >= klines[idx][0]) ? upColor : downColor;
 }
 function macdColor(params) {
  var v = macdHist[params.dataIndex];
  return (v != null && v >= 0) ? upColor : downColor;
 }

 // 详细十字光标信息（类似股票软件的 OHLC + 量 + 指标）
 function fmtTooltip(params) {
  if (!params || !params.length) return '';
  var idx = params[0].dataIndex;
  var d = dates[idx] || '';
  var k = klines[idx];
  var lines = ['<div style="font-weight:600;margin-bottom:4px;">' + d + '</div>'];
  if (k) {
   var o = k[0], c = k[1], lo = k[2], h = k[3];
   var pct = pctChanges[idx];
   var pctTxt = pct == null ? '-' : (pct > 0 ? '+' : '') + pct.toFixed(2) + '%';
   var pctColor = pct > 0 ? upColor : (pct < 0 ? downColor : '#999');
   lines.push('<div>开 <b>' + o.toFixed(2) + '</b>　高 <b>' + h.toFixed(2) + '</b></div>');
   lines.push('<div>收 <b>' + c.toFixed(2) + '</b>　低 <b>' + lo.toFixed(2) + '</b></div>');
   lines.push('<div>涨跌幅 <b style="color:' + pctColor + '">' + pctTxt + '</b></div>');
   var v = volumes[idx];
   if (v != null) {
    lines.push('<div>成交量 <b>' + (v >= 1e8 ? (v/1e8).toFixed(2) + '亿股' : (v >= 1e4 ? (v/1e4).toFixed(0) + '万股' : v.toFixed(0) + '股')) + '</b></div>');
   }
  }
  // MA 值
  var maVals = [];
  [['MA5', data.ma5], ['MA10', data.ma10], ['MA20', data.ma20], ['MA60', data.ma60]].forEach(function(pair) {
   var arr = pair[1];
   if (arr && arr[idx] != null) maVals.push(pair[0] + ' ' + (+arr[idx]).toFixed(2));
  });
  if (maVals.length) lines.push('<div style="margin-top:2px;">' + maVals.join('　') + '</div>');
  // MACD
  var m = macd[idx], ms = macdSignal[idx], mh = macdHist[idx];
  if (m != null || ms != null || mh != null) {
   lines.push('<div style="margin-top:2px;">MACD ' + (m != null ? (+m).toFixed(3) : '-') + '　DIF ' + (ms != null ? (+ms).toFixed(3) : '-') + '　BAR ' + (mh != null ? (+mh).toFixed(3) : '-') + '</div>');
  }
  return lines.join('');
 }

 var option = {
  backgroundColor: 'transparent',
  animation: false,
  legend: {
   data: ['K线', 'MA5', 'MA10', 'MA20', 'MA60', 'DIF', 'DEA'],
   top: 2,
   textStyle: { fontSize: 11, color: chartText }
  },
  axisPointer: { link: [{ xAxisIndex: 'all' }] },
  tooltip: {
   trigger: 'axis',
   axisPointer: { type: 'cross', label: { backgroundColor: '#6e7981' } },
   backgroundColor: chartBg,
   borderColor: chartBorder,
   textStyle: { color: chartTooltipText, fontSize: 12 },
   formatter: fmtTooltip
  },
  grid: [
   { left: '9%', right: '8%', top: 28, height: '50%' },
   { left: '9%', right: '8%', top: '62%', height: '13%' },
   { left: '9%', right: '8%', top: '78%', height: '13%' }
  ],
  xAxis: [
   { type: 'category', data: dates, gridIndex: 0, boundaryGap: true, axisLine: { lineStyle: { color: chartBorder } }, axisLabel: { fontSize: 10, color: chartText }, axisTick: { show: false } },
   { type: 'category', data: dates, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false } },
   { type: 'category', data: dates, gridIndex: 2, axisLabel: { fontSize: 10, color: chartText }, axisTick: { show: false } }
  ],
  yAxis: [
   { scale: true, gridIndex: 0, position: 'right', axisLabel: { fontSize: 10, color: chartText }, splitLine: { lineStyle: { color: chartGrid } } },
   { gridIndex: 1, position: 'right', splitNumber: 2, axisLabel: { fontSize: 9, color: chartText }, splitLine: { show: false } },
   { gridIndex: 2, position: 'right', splitNumber: 2, axisLabel: { fontSize: 9, color: chartText }, splitLine: { show: false } }
  ],
  dataZoom: [
   { type: 'inside', xAxisIndex: [0, 1, 2], start: 0, end: 100 },
   // 上下拉动：按住主图垂直拖动可平移价格区间，滚轮缩放
   { type: 'inside', yAxisIndex: 0, start: 0, end: 100 },
   { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 4, height: 14, borderColor: chartBorder, backgroundColor: chartBg, fillerColor: chartAccentSoft, handleStyle: { color: chartAccent } }
  ],
  series: [
   {
    name: 'K线', type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
    itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor },
    markLine: lastClose != null ? {
     silent: true, symbol: 'none',
     label: { show: true, position: 'end', fontSize: 10, color: lastPct >= 0 ? upColor : downColor, formatter: lastClose.toFixed(2) },
     lineStyle: { color: lastPct >= 0 ? upColor : downColor, type: 'dashed', width: 1 },
     data: [{ yAxis: lastClose }]
    } : undefined
   },
   { name: 'MA5', type: 'line', data: data.ma5 || calcMA(ohlc, 5), xAxisIndex: 0, yAxisIndex: 0, smooth: true, symbol: 'none', lineStyle: { width: 1 }, z: 3 },
   { name: 'MA10', type: 'line', data: data.ma10 || calcMA(ohlc, 10), xAxisIndex: 0, yAxisIndex: 0, smooth: true, symbol: 'none', lineStyle: { width: 1 }, z: 3 },
   { name: 'MA20', type: 'line', data: data.ma20 || calcMA(ohlc, 20), xAxisIndex: 0, yAxisIndex: 0, smooth: true, symbol: 'none', lineStyle: { width: 1 }, z: 3 },
   { name: 'MA60', type: 'line', data: data.ma60 || calcMA(ohlc, 60), xAxisIndex: 0, yAxisIndex: 0, smooth: true, symbol: 'none', lineStyle: { width: 1 }, z: 3 },
   {
    name: '成交量', type: 'bar', data: volumes, xAxisIndex: 1, yAxisIndex: 1,
    itemStyle: { color: volColor }
   },
   {
    name: 'MACD', type: 'bar', data: macdHist, xAxisIndex: 2, yAxisIndex: 2,
    itemStyle: { color: macdColor }
   },
   { name: 'DIF', type: 'line', data: macd, xAxisIndex: 2, yAxisIndex: 2, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#f5a623' } },
   { name: 'DEA', type: 'line', data: macdSignal, xAxisIndex: 2, yAxisIndex: 2, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#4a9eff' } }
  ]
 };

 chart.setOption(option);

 // 延迟 resize，确保面板 flex 布局稳定后图表尺寸正确（定时器可被下次渲染取消）
 _chartResizeTimer = setTimeout(function() { chart.resize(); }, 100);

 // 记录当前实例，供下次渲染前 dispose
 window._currentChart = chart;
 window.onresize = function() { chart.resize(); };
}

// 根据标题识别榜单类型（供 TXT/Excel 导出与文件名使用）
function getRankingType(title) {
 if (title.indexOf('三日') >= 0) return '三日涨幅榜';
 if (title.indexOf('五日') >= 0) return '五日涨幅榜';
 if (title.indexOf('月度') >= 0) return '月度涨幅榜';
 if (title.indexOf('首次涨停') >= 0) return '首次涨停';
 if (title.indexOf('涨停') >= 0 && title.indexOf('放量') >= 0) return '涨停放量2.5倍';
 if (title.indexOf('放量') >= 0) return '换手率放量3倍';
 if (title.indexOf('连阳') >= 0) return '连阳5-7天';
 if (title.indexOf('潜力股') >= 0 || title.indexOf('ML') >= 0) return '潜力股筛选';
 if (title.indexOf('自定义') >= 0) return '自定义查询';
 if (title.indexOf('筛选') >= 0 || title.indexOf('指标') >= 0) return '指标筛选';
 if (title.indexOf('股票') >= 0) return '股票列表';
 if (title.indexOf('强势精选') >= 0) return '强势精选';
 if (title.indexOf('涨停天数排行') >= 0) return '涨停天数排行';
 if (title.indexOf('天数排行') >= 0) return '大涨天数排行';
 return title.replace(/[\\/:*?"<>|]/g, '').trim() || '榜单';
}

// 导出当前榜单为 Excel (.xlsx) 文件
async function exportToExcel() {
 // XLSX 组件未加载守卫
 if (typeof XLSX === 'undefined') {
 alert('导出组件未加载');
 return;
 }

 if (!window._currentTableData || window._currentTableData.length === 0) {
 alert('没有数据可导出');
 return;
 }

 var title = document.getElementById('resultTitle').textContent;
 var rankingType = getRankingType(title);

 // 表头
 var headers = [];
 document.querySelectorAll('.stock-table thead th').forEach(function(th) {
 headers.push(th.textContent.trim());
 });

 // 数据行
 var dataRows = [];
 document.querySelectorAll('.stock-table tbody tr').forEach(function(tr) {
 var cells = [];
 tr.querySelectorAll('td').forEach(function(td) {
 cells.push(td.textContent.trim());
 });
 if (cells.length > 1) dataRows.push(cells);
 });

 // 构建 SheetJS 工作表
 var wsData = [headers];
 dataRows.forEach(function(r) { wsData.push(r); });
 var ws = XLSX.utils.aoa_to_sheet(wsData);

 // 列宽
 ws['!cols'] = headers.map(function(h, i) {
 if (i === 0) return { wch: 5 };
 if (i === 1) return { wch: 10 };
 if (i === 2) return { wch: 12 };
 return { wch: 12 };
 });

 var wb = XLSX.utils.book_new();
 XLSX.utils.book_append_sheet(wb, ws, rankingType);

 var now = new Date();
 var dateStr = now.toISOString().slice(0, 10);
 var timeStr = String(now.getHours()).padStart(2, '0') + String(now.getMinutes()).padStart(2, '0') + String(now.getSeconds()).padStart(2, '0');
 var fileName = rankingType + '_' + dateStr + '_' + timeStr + '.xlsx';

 // 优先使用文件系统访问 API 弹出保存对话框
 if (window.showSaveFilePicker) {
 try {
 var wbout = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
 var blob = new Blob([wbout], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
 var handle = await window.showSaveFilePicker({
 suggestedName: fileName,
 types: [{
 description: 'Excel 文件',
 accept: { 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'] }
 }]
 });
 var writable = await handle.createWritable();
 await writable.write(blob);
 await writable.close();
 return;
 } catch (err) {
 if (err.name !== 'AbortError') {
 console.error('Excel 保存失败:', err);
 alert('保存失败: ' + err.message);
 }
 return;
 }
 }

 // 回退方案：自动下载
 var wbout2 = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
 var blob2 = new Blob([wbout2], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
 var url = URL.createObjectURL(blob2);
 var a = document.createElement('a');
 a.href = url;
 a.download = fileName;
 document.body.appendChild(a);
 a.click();
 document.body.removeChild(a);
 URL.revokeObjectURL(url);
}

// 导出当前榜单为 TXT 文件
async function exportToTxt() {
 if (!window._currentTableData || window._currentTableData.length === 0) {
 alert('没有数据可导出');
 return;
 }

 var title = document.getElementById('resultTitle').textContent;

 // 根据当前标题判断榜单类型（所有榜单均可导出）
 var rankingType = getRankingType(title);
 var lines = [];
 lines.push(title);
 lines.push('导出时间: ' + new Date().toLocaleString('zh-CN'));
 lines.push('='.repeat(60));

 // 表头
 var headers = [];
 document.querySelectorAll('.stock-table thead th').forEach(function(th) {
 headers.push(th.textContent.trim());
 });
 lines.push(headers.join('\t'));

 // 数据行
 document.querySelectorAll('.stock-table tbody tr').forEach(function(tr) {
 var cells = [];
 tr.querySelectorAll('td').forEach(function(td) {
 cells.push(td.textContent.trim());
 });
 if (cells.length > 1) {
 lines.push(cells.join('\t'));
 }
 });

 lines.push('');
 lines.push('数据仅供参考，不构成投资建议。');

 // 根据榜单类型生成文件名：文字在前，时间在后
 var now = new Date();
 var dateStr = now.toISOString().slice(0, 10);
 var timeStr = String(now.getHours()).padStart(2, '0') + String(now.getMinutes()).padStart(2, '0') + String(now.getSeconds()).padStart(2, '0');

 var suggestedName = rankingType + '_' + dateStr + '_' + timeStr + '.txt';
 var content = lines.join('\n');
 var blob = new Blob(['\uFEFF' + content], { type: 'text/plain;charset=utf-8' });

 // 优先使用文件系统访问 API 弹出保存对话框
 if (window.showSaveFilePicker) {
 try {
 var handle = await window.showSaveFilePicker({
 suggestedName: suggestedName,
 types: [{
 description: '文本文件',
 accept: { 'text/plain': ['.txt'] }
 }]
 });
 var writable = await handle.createWritable();
 await writable.write(blob);
 await writable.close();
 return;
 } catch (err) {
 if (err.name !== 'AbortError') {
 console.error('showSaveFilePicker failed:', err);
 alert('保存失败: ' + err.message);
 }
 return;
 }
 }

 // 回退方案：自动下载到浏览器默认位置
 var url = URL.createObjectURL(blob);
 var a = document.createElement('a');
 a.href = url;
 a.download = suggestedName;
 document.body.appendChild(a);
 a.click();
 document.body.removeChild(a);
 URL.revokeObjectURL(url);
}

// 技术分析预测
async function predictStock() {
 const codes = getCodes();
 const code = codes ? codes[0] : '';
 if (!code) {
 alert('请先输入股票代码');
 return;
 }

 const resultDiv = document.getElementById('mlPredictResult');
 resultDiv.style.display = 'block';
 resultDiv.innerHTML = '<p class="muted-text">分析中...</p>';

 try {
 const response = await fetch('/api/ml/predict/' + encodeURIComponent(code));
 const data = await response.json();
 if (data.success) {
 renderAnalysisResult(data);
 } else {
 resultDiv.innerHTML = '<p style="color:red;">分析失败: ' + escapeHtml(data.error || '未知错误') + '</p>';
 }
 } catch (e) {
 resultDiv.innerHTML = '<p style="color:red;">请求失败: ' + escapeHtml(e.message) + '</p>';
 }
}

// 渲染分析结果
function renderAnalysisResult(data) {
 const resultDiv = document.getElementById('mlPredictResult');
 resultDiv.style.display = 'block';

 const score = data.score || 0;
 const pred = data.prediction || '未知';
 const conf = data.confidence || 0;
 const color = score > 0 ? '#27ae60' : (score < 0 ? '#e74c3c' : '#666');

 let html = '<div class="analysis-result">' +
 '<div class="analysis-header">' +
 '<span class="analysis-title" style="color:' + color + '">' + escapeHtml(pred) + '</span>' +
 '<span class="analysis-score">置信度: ' + conf.toFixed(0) + '%</span>' +
 '</div>' +
 '<div style="margin:8px 0;"><b>' + escapeHtml(data.name || data.code) + '</b> (' + escapeHtml(data.code) + ')</div>' +
 '<div style="margin:8px 0;">得分: <b>' + score + '</b> | 价格: ' + (data.price != null ? data.price.toFixed(2) : '-') + '</div>' +
 '<div style="font-size:12px;color:#666;">涨跌: ' + (data.change_pct != null ? data.change_pct.toFixed(2) + '%' : '-') + ' | PE: ' + (data.pe != null ? data.pe.toFixed(1) : '-') + ' | PB: ' + (data.pb != null ? data.pb.toFixed(2) : '-') + '</div>' +
 '<div class="analysis-signals">';
 if (data.signals) {
 data.signals.forEach(function(s) {
 html += '<div class="signal-item">' + escapeHtml(s) + '</div>';
 });
 }
 html += '</div></div>';
 resultDiv.innerHTML = html;
}

// ML 筛选结果渲染（与旧版 HTML 结构一致，全部经 escapeHtml）
function renderMLScreenData(list) {
 const resultDiv = document.getElementById('mlScreenResult');
 if (list && list.length > 0) {
 let html = '';
 list.forEach(function(s, i) {
 const pred = s.prediction || '未知';
 const score = s.score || 0;
 const color = score >= 4 ? '#27ae60' : '#f39c12';
 html += '<div class="stock-item" data-code="' + escapeHtml(s.code) + '" tabindex="0" style="cursor:pointer;">' +
 '<div class="stock-item-header">' +
 '<span>' + (i + 1) + '. <b>' + escapeHtml(s.code) + '</b> ' + escapeHtml(s.name) + '</span>' +
 '<span style="color:' + color + '">' + escapeHtml(pred) + ' (' + score + '分)</span>' +
 '</div>' +
 '<div class="stock-item-meta">' +
 '<span>价格:' + (s.price != null ? s.price.toFixed(2) : '-') + ' ' + (s.change_pct != null ? s.change_pct.toFixed(2) + '%' : '') + '</span>' +
 '<span>PE:' + (s.pe != null ? s.pe.toFixed(1) : '-') + ' PB:' + (s.pb != null ? s.pb.toFixed(2) : '-') + '</span>' +
 '</div>' +
 '</div>';
 });
 resultDiv.innerHTML = html;
 } else {
 resultDiv.innerHTML = '<p style="color:#999;">没有找到符合条件的股票</p>';
 }
}

// 潜力股筛选（异步作业：POST start 启动，每 800ms 轮询 status）
async function runMLScreen() {
 const topN = parseInt(document.getElementById('mlTopN').value) || 20;
 const resultDiv = document.getElementById('mlScreenResult');
 const btn = document.querySelector('#mlMode .btn-success');
 const originalText = btn ? btn.textContent : '';
 const seq = ++_mlScreenPollSeq; // 递增序号：重复点击时丢弃旧轮询
 if (btn) btn.disabled = true;
 resultDiv.innerHTML = '<p class="muted-text loading-pulse">智能筛选中... 0%</p>';

 try {
 // 启动异步作业（409 = 已有作业在运行）
 const resp = await fetch('/api/ml/screen/start?top_n=' + encodeURIComponent(topN), {
  method: 'POST',
  headers: { 'X-CSRF-Token': await ensureCsrfToken() }
 });
 const start = await resp.json();
 if (seq !== _mlScreenPollSeq) return;
 if (!resp.ok || !start.success) {
  resultDiv.innerHTML = '<p style="color:red;">' + escapeHtml(start.error || ('启动失败 (HTTP ' + resp.status + ')')) + '</p>';
  return;
 }

 // 轮询进度
 while (true) {
  await new Promise(function(resolve) { setTimeout(resolve, 800); });
  if (seq !== _mlScreenPollSeq) return; // 旧轮询，丢弃
  const stResp = await fetch('/api/ml/screen/status?job_id=' + encodeURIComponent(start.job_id));
  const st = await stResp.json();
  if (seq !== _mlScreenPollSeq) return;
  if (!st.success) {
   resultDiv.innerHTML = '<p style="color:red;">' + escapeHtml(st.error || '状态查询失败') + '</p>';
   return;
  }
  if (st.status === 'running') {
   var pct = st.total > 0 ? Math.round((st.progress || 0) / st.total * 100) : 0;
   resultDiv.innerHTML = '<p class="muted-text loading-pulse">智能筛选中... ' + pct + '% (' + (st.progress || 0) + '/' + (st.total || 0) + ')</p>';
   continue;
  }
  if (st.status === 'error') {
   resultDiv.innerHTML = '<p style="color:red;">筛选失败: ' + escapeHtml(st.error || '未知错误') + '</p>';
   return;
  }
  // done：渲染结果
  renderMLScreenData(st.data);
  return;
 }
 } catch (e) {
 if (seq === _mlScreenPollSeq) {
  resultDiv.innerHTML = '<p style="color:red;">筛选失败: ' + escapeHtml(e.message) + '</p>';
 }
 } finally {
 // 仅当仍是最新一次点击时才恢复按钮（旧轮询不抢按钮状态）
 if (btn && seq === _mlScreenPollSeq) {
  btn.textContent = originalText;
  btn.disabled = false;
 }
 }
}

// 重置筛选
function resetFilters() {
 document.querySelectorAll('.sidebar input').forEach(function(input) {
 input.value = '';
 });
 const sortEl = document.getElementById('sortOrder');
 if (sortEl) sortEl.value = 'default';
 const tbody = document.getElementById('stockListBody');
 tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#999;padding:40px;">请选择模式并加载数据</td></tr>';
 document.getElementById('stockCount').textContent = '等待筛选';
 window._currentTableData = [];
}



// ═══ 排行榜功能 ═══

// 排行榜通用：带按钮加载态与请求序号的 fetch
function fetchRankingWithState(url, btn, onSuccess) {
 var seq = ++_rankSeq;
 var originalText = btn ? btn.textContent : '';
 if (btn) {
 btn.textContent = '分析中...';
 btn.disabled = true;
 }
 fetch(url)
 .then(function(r) { return r.json(); })
 .then(function(data) {
 if (seq !== _rankSeq) return; // 过期响应，丢弃
 if (data.success) {
 window._lastRankTotal = data.total || (data.data ? data.data.length : 0);
 onSuccess(data);
 } else {
 alert('加载失败: ' + (data.error || '未知错误'));
 }
 })
 .catch(function(e) {
 if (seq !== _rankSeq) return;
 alert('请求失败: ' + e.message);
 })
 .finally(function() {
 if (btn) {
 btn.textContent = originalText;
 btn.disabled = false;
 }
 });
}

// 加载三日涨幅前50
function loadTop3Day() {
 var btn = document.getElementById('btnTop3');
 fetchRankingWithState('/api/top-change?days=3', btn, function(data) {
 renderRankTable(data.data, ' 三日涨幅前五十', 'n_day_change');
 });
}

// 加载月度前50（约22个交易日）
function loadMonthlyTop() {
 var btn = document.getElementById('btnMonthlyTop');
 fetchRankingWithState('/api/top-change?days=22', btn, function(data) {
 renderRankTable(data.data, ' 月度涨幅前五十', 'n_day_change');
 });
}

// 自定义排名查询
async function loadCustomRank() {
 var maxPrice = document.getElementById('rankMaxPrice').value;
 var minChange = document.getElementById('rankMinChange').value;

 var btn = document.getElementById('btnCustomRank');
 var seq = ++_rankSeq;
 var originalText = btn ? btn.textContent : '';
 if (btn) {
 btn.textContent = '分析中...';
 btn.disabled = true;
 }

 try {
 const data = await postJson('/api/stocks', {
 price_max: maxPrice ? parseFloat(maxPrice) : null,
 change_pct_min: minChange ? parseFloat(minChange) : null,
 sort_by: 'change_pct',
 sort_order: 'desc'
 });
 if (seq !== _rankSeq) return;
 if (data.success) {
 renderRankTable(data.data, ' 自定义查询结果', 'change_pct');
 } else {
 alert('加载失败: ' + (data.error || '未知错误'));
 }
 } catch (e) {
 if (seq !== _rankSeq) return;
 alert('请求失败: ' + e.message);
 } finally {
 if (btn) {
 btn.textContent = originalText;
 btn.disabled = false;
 }
 }
}

// 渲染排行榜表格
function renderRankTable(stocks, title, changeField) {
 var tbody = document.getElementById('stockListBody');
 var resultTitle = document.getElementById('resultTitle');
 var countEl = document.getElementById('stockCount');

 // 缓存数据供列排序使用
 window._currentTableData = stocks || [];
 window._currentTableIsRank = true;
 window._currentTableTitle = title;
 window._currentTableChangeField = changeField;
 window._currentTableTotal = stocks ? stocks.length : 0;
 // 重置排序状态与箭头
 _currentSortField = '';
 _currentSortState = 0;
 document.querySelectorAll('.sort-arrow').forEach(function(el) { el.className = 'sort-arrow'; });

 resultTitle.textContent = title;
 updateCountBadge(window._lastRankTotal || (stocks ? stocks.length : 0), stocks ? stocks.length : 0);

 // 动态更新表头第6列（保留排序箭头）
 var th6 = document.querySelectorAll('.stock-table thead th');
 if (th6.length >= 6) {
 var label = 'N日涨幅';
 if (title.indexOf('三日') >= 0) label = '3日涨幅';
 else if (title.indexOf('五日') >= 0) label = '5日涨幅';
 else if (title.indexOf('月度') >= 0) label = '月度涨幅';
 else if (title.indexOf('自定义') >= 0) label = '涨跌幅';
 else if (title.indexOf('放量') >= 0) label = '换手率';
 else if (title.indexOf('强势精选') >= 0) label = '大涨天数';
 else if (title.indexOf('涨停天数排行') >= 0) label = '涨停天数';
 else if (title.indexOf('天数排行') >= 0) label = '大涨天数';
 th6[5].innerHTML = label + ' <span class="sort-arrow" id="sort_n_day_change"></span>';
 if (th6.length >= 7 && (title.indexOf('天数排行') >= 0 || title.indexOf('强势精选') >= 0)) {
 th6[6].innerHTML = (title.indexOf('涨停天数排行') >= 0 ? '大涨天数' : '占比') + ' <span class="sort-arrow" id="sort_volume_change_pct"></span>';
 }
 }

 if (!stocks || stocks.length === 0) {
 updateCountBadge(window._lastRankTotal || 0, 0);
 tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#999;padding:40px;">无数据</td></tr>';
 return;
 }

 updateCountBadge(window._lastRankTotal || stocks.length, stocks.length);

 tbody.innerHTML = stocks.map(function(s, i) {
 var changeClass = s.change_pct > 0 ? 'change-up' : (s.change_pct < 0 ? 'change-down' : '');
 // 支持 n_day_change / three_day_change / five_day_change / monthly_change
 var extraVal = null;
 if (changeField && s[changeField] != null) {
 extraVal = s[changeField];
 } else {
 extraVal = s.monthly_change != null ? s.monthly_change :
 (s.five_day_change != null ? s.five_day_change :
 (s.three_day_change != null ? s.three_day_change : null));
 }
 var extraChangeClass = extraVal > 0 ? 'change-up' : (extraVal < 0 ? 'change-down' : '');
 var isBigUpDays = changeField === 'big_up_days' || changeField === 'limit_up_days';
 var isLimitUpRank = changeField === 'limit_up_days';
 var mcap = s.market_cap != null ? (typeof s.market_cap === 'number' ? s.market_cap.toFixed(0) : String(s.market_cap)) : '-';
 return '<tr data-code="' + escapeHtml(s.code) + '" tabindex="0" style="cursor:pointer;">' +
 '<td>' + (i + 1) + '</td>' +
 '<td class="stock-code">' + escapeHtml(s.code) + '</td>' +
 '<td class="stock-name">' + escapeHtml(s.name) + '</td>' +
 '<td class="text-right">' + (s.price != null ? s.price.toFixed(2) : '-') + '</td>' +
 '<td class="text-right ' + changeClass + '">' + (s.change_pct != null ? (s.change_pct > 0 ? '+' : '') + s.change_pct.toFixed(2) + '%' : '-') + '</td>' +
 '<td class="text-right ' + (isBigUpDays ? '' : extraChangeClass) + '">' + (isBigUpDays ? (extraVal != null ? extraVal + '天' : '-') : (extraVal != null ? (extraVal > 0 ? '+' : '') + extraVal.toFixed(2) + '%' : '-')) + '</td>' +
 '<td class="text-right">' + (isLimitUpRank ? (s.big_up_days != null ? s.big_up_days + '天' : '-') : (isBigUpDays && s.big_up_ratio != null ? s.big_up_ratio.toFixed(1) + '%' : '-')) + '</td>' +
 '<td class="text-right">' + (s.pe != null ? s.pe.toFixed(1) : '-') + '</td>' +
 '<td class="text-right">' + (s.pb != null ? s.pb.toFixed(2) : '-') + '</td>' +
 '<td class="text-right">' + escapeHtml(mcap) + '</td>' +
 '</tr>';
 }).join('');
}

// 模式切换（覆盖版：支持排行榜模式）
function setMode(mode) {
 var filterMode = document.getElementById('filterMode');
 var codeMode = document.getElementById('codeMode');
 var mlMode = document.getElementById('mlMode');
 var rankMode = document.getElementById('rankMode');

 filterMode.style.display = 'none';
 codeMode.style.display = 'none';
 mlMode.style.display = 'none';
 rankMode.style.display = 'none';

 document.querySelectorAll('.nav-link').forEach(function(el) { el.classList.remove('active'); });

 if (mode === 'filter') {
 filterMode.style.display = 'block';
 document.querySelectorAll('.nav-link')[0].classList.add('active');
 } else if (mode === 'rank') {
 rankMode.style.display = 'block';
 document.querySelectorAll('.nav-link')[1].classList.add('active');
 } else if (mode === 'code') {
 codeMode.style.display = 'block';
 document.querySelectorAll('.nav-link')[2].classList.add('active');
 } else if (mode === 'ml') {
 mlMode.style.display = 'block';
 document.querySelectorAll('.nav-link')[3].classList.add('active');
 }
}

// 加载五日涨幅前50
function loadTop5Day() {
 var btn = document.getElementById('btnTop5');
 fetchRankingWithState('/api/top-change?days=5', btn, function(data) {
 renderRankTable(data.data, ' 五日涨幅前五十', 'n_day_change');
 });
}

// 首次涨停筛选
function screenFirstLimitUp() {
 var btn = document.getElementById('btnFirstLimitUp');
 fetchRankingWithState('/api/screen/first_limit_up?limit=100', btn, function(data) {
 renderRankTable(data.data, ' 首次涨停（主板10%/创业板20%，14天内首次）', 'change_pct');
 });
}

// 涨停+换手率放量2.5倍
function screenLimitUpVolume() {
 var btn = document.getElementById('btnLimitUpVolume');
 fetchRankingWithState('/api/screen/limit_up_volume?limit=50', btn, function(data) {
 renderRankTable(data.data, ' 涨停+放量2.5倍（主板10%/创业板20%）', 'turnover_ratio');
 });
}

// 连阳3-4天，涨幅<=20%
function screenConsecutiveUp() {
 var btn = document.getElementById('btnConsecutiveUp');
 fetchRankingWithState('/api/screen/consecutive_up?limit=100', btn, function(data) {
 renderRankTable(data.data, ' 连阳5-7天≤20%', 'total_gain');
 });
}

// 换手率放量≥3倍榜单（不限涨停，按放量倍数降序）
function screenVolumeSurge() {
 var btn = document.getElementById('btnVolumeSurge');
 fetchRankingWithState('/api/screen/volume_surge?limit=50', btn, function(data) {
 renderRankTable(data.data, ' 换手率放量≥3倍', 'turnover_ratio');
 });
}

// 涨停回调6-7%筛选
function screenLimitUpPullback() {
 var btn = document.getElementById('btnLimitUpPullback');
 fetchRankingWithState('/api/screen/limit_up_pullback?limit=50', btn, function(data) {
 renderRankTable(data.data, ' 涨停回调6-7%（后2~4交易日）', 'pullback_pct');
 });
}

// 20日涨幅超3.5%天数排行（从大到小；股价 6~40 元，20日区间涨幅≤50%）
function screenBigUpDays() {
 var btn = document.getElementById('btnBigUpDays');
 fetchRankingWithState('/api/screen/big_up_days?limit=100&min_count=1&days=20', btn, function(data) {
 renderRankTable(data.data, ' 最近20交易日涨幅超3.5%天数排行（股价6-40元·区间涨幅≤50%）', 'big_up_days');
 });
}

// 20日涨停天数排行（按板块规则：主板9.8%/创业板科创板19.5%；同时要求≥2天涨≥5%；排除ST）
function screenLimitUpDays() {
 var btn = document.getElementById('btnLimitUpDays');
 fetchRankingWithState('/api/screen/limit_up_days?limit=100&days=20&min_count=1', btn, function(data) {
  renderRankTable(data.data, ' 最近20交易日涨停天数排行（主板10%/创业板20%·非ST）', 'limit_up_days');
 });
}

// ── 高级筛选下拉：按选项 key 执行对应榜单 ──
function runAdvancedScreen(key) {
 var fns = {
  firstLimitUp: screenFirstLimitUp,
  limitUpVolume: screenLimitUpVolume,
  consecutiveUp: screenConsecutiveUp,
  volumeSurge: screenVolumeSurge,
  limitUpPullback: screenLimitUpPullback,
  bigUpDays: screenBigUpDays,
  limitUpDays: screenLimitUpDays,
  strongPick: screenStrongPick
 };
 if (fns[key]) fns[key]();
}

// 选中态：排行榜面板内任一按钮点击后高亮，其余恢复未选中
(function() {
 document.addEventListener('click', function(e) {
  var btn = e.target && e.target.closest ? e.target.closest('#rankMode button') : null;
  if (!btn || btn.id === 'rankModeSelect' || btn.tagName !== 'BUTTON') return;
  var all = document.querySelectorAll('#rankMode button');
  for (var i = 0; i < all.length; i++) all[i].classList.remove('rank-selected');
  btn.classList.add('rank-selected');
 });
})();

// 20日强势精选：≥2天大涨(≥3.5%) + 至少1次涨停 + 近10日无深跌(<-3%) + 非ST
function screenStrongPick() {
 var btn = document.getElementById('btnStrongPick');
 fetchRankingWithState('/api/screen/big_up_days?limit=100&days=20&min_count=2&min_limit_up=1&max_drop_pct=-3&exclude_st=1', btn, function(data) {
  renderRankTable(data.data, ' 20日强势精选排行（≥2天大涨·≥1次涨停·近10日无深跌·非ST）', 'big_up_days');
 });
}
