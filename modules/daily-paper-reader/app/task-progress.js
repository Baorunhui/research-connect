// 统一任务进度组件：单篇论文总结、论文日报等异步任务共用。
(function () {
  'use strict';

  function textNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text == null ? '' : String(text);
    return node;
  }

  function terminal(status) {
    return ['completed', 'success', 'failed', 'failure', 'cancelled', 'interrupted'].indexOf(String(status || '').toLowerCase()) >= 0;
  }

  function stateMap(events) {
    var map = {};
    (events || []).forEach(function (ev) {
      var payload = (ev && ev.payload) || {};
      if (payload.step && payload.state) map[payload.step] = payload;
    });
    return map;
  }

  function calculatePercent(events, status, steps) {
    if (terminal(status)) return 100;
    if (!steps || !steps.length) return Math.min(90, 10 + (events || []).length * 12);
    var map = stateMap(events);
    var units = 0;
    steps.forEach(function (step) {
      var payload = map[step.key] || {};
      var state = payload.state || '';
      if (state === 'completed' || state === 'skipped') {
        units += 1;
      } else if ((state === 'started' || state === 'running') && Number(payload.total) > 0) {
        units += Math.max(0, Math.min(0.98, Number(payload.current || 0) / Number(payload.total)));
      } else if (state === 'started' || state === 'running') {
        units += 0.12;
      }
    });
    return Math.max(2, Math.min(98, (units / steps.length) * 100));
  }

  function formatSuffix(ev) {
    var payload = (ev && ev.payload) || {};
    var current = ev && ev.current != null ? ev.current : payload.current;
    var total = ev && ev.total != null ? ev.total : payload.total;
    var parts = [];
    if (current != null && total != null) parts.push(current + '/' + total);
    if (payload.percent != null) parts.push(Number(payload.percent).toFixed(1) + '%');
    if (payload.rate != null) parts.push(Number(payload.rate).toFixed(2) + ' 篇/秒');
    if (payload.eta_seconds != null && Number(payload.eta_seconds) >= 0) {
      var seconds = Math.round(Number(payload.eta_seconds));
      parts.push('预计剩余 ' + (seconds >= 60 ? Math.ceil(seconds / 60) + ' 分钟' : seconds + ' 秒'));
    }
    return parts.length ? '（' + parts.join(' · ') + '）' : '';
  }

  function render(container, options) {
    if (!container) return;
    var opts = options || {};
    var status = String(opts.status || 'queued').toLowerCase();
    var events = Array.isArray(opts.events) ? opts.events : [];
    var steps = Array.isArray(opts.steps) ? opts.steps : [];
    var failed = status === 'failed' || status === 'failure';
    var cancelled = status === 'cancelled';
    var interrupted = status === 'interrupted';
    var completed = status === 'completed' || status === 'success';
    var title = interrupted
      ? (opts.interruptedTitle || '⚠️ 任务已中断')
      : failed
      ? (opts.failedTitle || '❌ 任务失败')
      : cancelled
        ? (opts.cancelledTitle || '⏹ 任务已取消')
        : completed
          ? (opts.doneTitle || '✅ 任务完成')
          : (opts.title || '⏳ 任务正在执行');

    var previousScroll = container.scrollTop;
    var wasNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 36;
    container.textContent = '';
    container.appendChild(textNode('div', 'paper-summarize-progress-head', title));

    var bar = textNode('div', 'paper-summarize-progress-bar', '');
    var fill = textNode('div', 'paper-summarize-progress-fill', '');
    fill.style.width = calculatePercent(events, status, steps).toFixed(1) + '%';
    bar.appendChild(fill);
    container.appendChild(bar);

    if (steps.length) {
      var states = stateMap(events);
      var strip = textNode('div', 'dpr-task-progress-steps', '');
      steps.forEach(function (step) {
        var payload = states[step.key] || {};
        var state = payload.state || '';
        var chip = textNode('span', 'dpr-task-progress-step is-' + (state || 'pending'), step.label || step.key);
        chip.title = state || 'pending';
        strip.appendChild(chip);
      });
      container.appendChild(strip);
    }

    var list = textNode('ul', 'paper-summarize-progress-list', '');
    var visibleEvents = events.filter(function (ev) {
      return ev && (ev.message || ev.stage || ev.event_type);
    }).slice(-30);
    visibleEvents.forEach(function (ev) {
      var payload = ev.payload || {};
      var li = textNode('li', 'paper-summarize-progress-item', '');
      li.appendChild(textNode('span', 'paper-summarize-progress-dot', ''));
      var stage = payload.step_label || ev.stage || ev.event_type || '';
      if (stage) li.appendChild(textNode('span', 'paper-summarize-progress-stage', stage));
      li.appendChild(textNode('span', 'paper-summarize-progress-msg', (ev.message || '') + formatSuffix(ev)));
      list.appendChild(li);
    });
    if (!visibleEvents.length) {
      var waiting = textNode('li', 'paper-summarize-progress-item', '');
      waiting.appendChild(textNode('span', 'paper-summarize-progress-dot', ''));
      waiting.appendChild(textNode('span', 'paper-summarize-progress-msg', '等待第一条进度事件…'));
      list.appendChild(waiting);
    }
    container.appendChild(list);
    if (wasNearBottom) container.scrollTop = container.scrollHeight;
    else container.scrollTop = previousScroll;
  }

  window.DPRTaskProgress = { render: render, calculatePercent: calculatePercent };
})();
