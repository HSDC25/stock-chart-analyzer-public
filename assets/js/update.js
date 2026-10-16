/*
 * StockChart 自动更新的前端部分：导航栏右侧的版本徽章。
 * 五个界面（win7 + 四个 Win11 主题）共用这一份，由 /assets/js/update.js 提供。
 *
 * 后端只做两件事：报告 updater 的进度、接受手动检查。真正的下载、换皮（UI 覆盖层）
 * 与本体替换都在 common/updater.py 里，启动时自动跑，这里只负责让人看见。
 * 本体下载完成后不等用户点确认，直接自动重启换体——Win7 侧的用户是老人，弹窗只会卡住流程。
 */
(function () {
 'use strict';

 var STYLE = [
  '.sc-update-badge{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;',
  'border-radius:10px;font-size:11px;line-height:16px;cursor:pointer;opacity:.75;',
  'background:rgba(128,128,128,.14);border:1px solid rgba(128,128,128,.25);',
  'color:inherit;font-family:inherit;white-space:nowrap}',
  '.sc-update-badge:hover{opacity:1}',
  '.sc-update-badge[data-phase="downloading"],.sc-update-badge[data-phase="installing"],',
  '.sc-update-badge[data-phase="checking"]{opacity:1;border-color:currentColor}',
  '.sc-update-badge[data-phase="failed"]{color:#d13438;opacity:1}',
  '.sc-update-badge .sc-spin{width:9px;height:9px;border-radius:50%;',
  'border:2px solid currentColor;border-top-color:transparent;animation:sc-rot 1s linear infinite}',
  '@keyframes sc-rot{to{transform:rotate(360deg)}}'
 ].join('');

 var ACTIVE = { checking: 1, downloading: 1, installing: 1 };
 var RELOAD_KEY = 'scUiReloadedVersion';
 var el = null;
 var timer = null;
 var installArmed = false;

 function reloadedAt(v) {
  try { return sessionStorage.getItem(RELOAD_KEY) === v; } catch (e) { return false; }
 }

 function markReloaded(v) {
  try { sessionStorage.setItem(RELOAD_KEY, v); } catch (e) { /* 禁用存储时只是少一次自动刷新 */ }
 }

 function cssInject() {
  var s = document.createElement('style');
  s.textContent = STYLE;
  (document.head || document.documentElement).appendChild(s);
 }

 function mount() {
  var host = document.querySelector('.navbar-status') || document.querySelector('nav');
  el = document.createElement('span');
  el.className = 'sc-update-badge';
  el.id = 'scUpdateBadge';
  el.setAttribute('role', 'button');
  el.tabIndex = 0;
  el.textContent = '·';
  el.addEventListener('click', onActivate);
  el.addEventListener('keydown', function (e) {
   if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onActivate(); }
  });
  if (host) {
   host.appendChild(el);
  } else {
   el.style.cssText += ';position:fixed;right:8px;bottom:8px;z-index:9999';
   document.body.appendChild(el);
  }
 }

 function csrf() {
  if (window._csrfToken) return Promise.resolve(window._csrfToken);
  if (typeof window.ensureCsrfToken === 'function') return window.ensureCsrfToken();
  return fetch('/api/csrf-token', { cache: 'no-store' })
   .then(function (r) { return r.json(); })
   .then(function (d) { return (d && d.csrf_token) || ''; });
 }

 function render(s) {
  if (!el) return;
  var phase = s.phase || 'idle';
  el.setAttribute('data-phase', phase);
  var busy = ACTIVE[phase];
  var text;
  if (busy || phase === 'done' || phase === 'failed') {
   text = s.message || phase;
  } else {
   text = 'v' + (s.local_version || '?');
  }
  var pct = (typeof s.progress === 'number' && s.progress > 0 && s.progress < 100)
   ? ' ' + Math.round(s.progress) + '%' : '';
  el.textContent = text + pct;          // textContent: 服务器文案按纯文本处理
  if (busy) {
   var dot = document.createElement('i');
   dot.className = 'sc-spin';
   el.insertBefore(dot, el.firstChild);
  }
  el.title = [
   '版本 v' + (s.local_version || '?'),
   '变体 ' + (s.variant || '-'),
   s.remote_version ? '服务器版本 v' + s.remote_version : '',
   s.overlay_version ? '界面覆盖层 v' + s.overlay_version : '',
   s.notes ? '更新说明：' + s.notes : '',
   busy ? '' : '点击检查更新'
  ].filter(Boolean).join('\n');
  el.dataset.restart = s.restart_required ? '1' : '';
 }

 function install() {
  csrf().then(function (token) {
   return fetch('/api/update/install', {
    method: 'POST', headers: { 'X-CSRF-Token': token }, cache: 'no-store',
   });
  }).catch(function () { /* 进程正在退出，忽略 */ });
 }

 function poll() {
  fetch('/api/update/status', { cache: 'no-store' })
   .then(function (r) { return r.ok ? r.json() : null; })
   .then(function (s) {
    if (!s || !s.success) { stop(); return; }
    render(s);
    var hot = s.overlay_active && s.phase === 'done' && s.restart_required === false &&
              /界面已更新/.test(s.message || '');
    if (hot) {
     // 后端进程不重启，_state 会一直停在这条文案上。刷新过一次就必须记住，否则新页面
     // 加载完又命中同一条件，标签页会无限刷新下去。
     var key = (s.overlay_version || s.remote_version || '?') + '|' + s.message;
     if (!reloadedAt(key)) {
      markReloaded(key);
      setTimeout(function () { location.reload(); }, 1200);
      return;
     }
    }
    // 本体已下载就绪：留几秒让徽章显示完整，然后自动换体重启，不弹确认框
    if (s.restart_required && !installArmed) {
     installArmed = true;
     stop();
     setTimeout(install, 4000);
     return;
    }
    schedule(ACTIVE[s.phase] ? 1200 : 30000);
   })
   .catch(function () { schedule(15000); });
 }

 function schedule(ms) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(poll, ms);
 }

 function stop() {
  if (timer) clearTimeout(timer);
  timer = null;
 }

 function onActivate() {
  if (el && el.dataset.restart === '1') { install(); return; }   // 徽章被点到时立刻换体
  csrf().then(function (token) {
   return fetch('/api/update/check', {
    method: 'POST', headers: { 'X-CSRF-Token': token }, cache: 'no-store',
   });
  }).then(function () { poll(); }).catch(function () { poll(); });
 }

 function start() {
  cssInject();
  mount();
  poll();
 }

 if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', start);
 } else {
  start();
 }
})();
