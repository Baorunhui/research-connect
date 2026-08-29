(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
    return;
  }

  var api = factory();
  var hostWindow = root && root.window ? root.window : root;
  if (hostWindow) {
    hostWindow.DPRAcademicNews = api;
    if (typeof api.autoInit === 'function') {
      api.autoInit(hostWindow);
    }
  }
})(
  typeof globalThis !== 'undefined'
    ? globalThis
    : typeof self !== 'undefined'
      ? self
      : this,
  function () {
    'use strict';

    var SCHEDULE_JSON_PATH = 'app/conference-schedule.json';

    var SELECTORS = {
      container: '[data-dpr-news]',
    };

    // ---- 工具函数 ----

    /** 日期起始时间戳 (UTC 00:00) */
    function dayStartTs(ts) {
      var d = new Date(ts);
      return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())).getTime();
    }

    /** 剩余天数文字 */
    function formatCountdownText(ts, nowTs) {
      if (ts == null) return '';
      nowTs = nowTs || Date.now();
      var diff = ts - nowTs;
      if (diff <= 0) return '已截止';
      var days = Math.floor(diff / 86400000);
      if (days === 0) return '今天截止';
      return '剩余 ' + days + ' 天';
    }

    /** 颜色分级 class */
    function colorTier(daysUntil) {
      if (daysUntil <= 7) return 'is-urgent';
      if (daysUntil <= 30) return 'is-soon';
      return 'is-normal';
    }

    /** CCF rank → class */
    function rankClass(rank) {
      if (!rank || !rank.ccf) return 'rank-n';
      var c = String(rank.ccf).toUpperCase();
      if (c === 'A') return 'rank-a';
      if (c === 'B') return 'rank-b';
      if (c === 'C') return 'rank-c';
      return 'rank-n';
    }

    /** CCF rank → 文字 */
    function rankLabel(rank) {
      if (!rank || !rank.ccf) return '';
      return 'CCF ' + rank.ccf;
    }

    /** HTML 转义 */
    function esc(str) {
      var div = document.createElement('div');
      div.appendChild(document.createTextNode(str || ''));
      return div.innerHTML;
    }

    /** 日期格式化 MM.DD */
    function fmtDate(dateStr) {
      if (!dateStr) return '';
      var parts = String(dateStr).split('-');
      if (parts.length < 2) return dateStr;
      return parts[1] + '.' + parts[2];
    }

    // ---- 核心：展平 + 过滤 ----

    function flattenAndFilter(schedule, topN) {
      topN = topN || 8;
      var todayStart = dayStartTs(Date.now());
      var out = [];

      (schedule && schedule.conferences || []).forEach(function (conf) {
        (conf.years || []).forEach(function (y) {
          (y.milestones || []).forEach(function (m) {
            if (m.is_tbd) return;
            if (m.ts < todayStart) return;
            out.push({
              conferenceLabel: conf.label,
              rank: conf.rank || null,
              milestoneLabel: m.label,
              date: m.date,
              ts: m.ts,
              daysUntil: Math.round((m.ts - todayStart) / 86400000),
              type: m.type,
              link: y.link || '',
            });
          });
        });
      });

      out.sort(function (a, b) { return a.ts - b.ts; });
      return out.slice(0, topN);
    }

    // ---- 渲染 ----

    function renderItems(items) {
      if (!items || items.length === 0) {
        return '<p class="dpr-home-news-empty">近期暂无会议节点</p>';
      }
      var html = '<ul class="dpr-home-news-list">';
      items.forEach(function (item) {
        var tier = colorTier(item.daysUntil);
        var countdown = formatCountdownText(item.ts);
        var rankCls = rankClass(item.rank);
        var rankLbl = rankLabel(item.rank);
        var dateFmt = fmtDate(item.date);

        html += '<li class="dpr-home-news-item">';
        html += '<div class="dpr-home-news-item-main">';
        html += '<span class="dpr-home-news-conf">' + esc(item.conferenceLabel) + '</span>';
        if (rankLbl) {
          html += ' <span class="dpr-home-news-rank ' + esc(rankCls) + '">' + esc(rankLbl) + '</span>';
        }
        html += '<span class="dpr-home-news-milestone">' + esc(item.milestoneLabel) + '</span>';
        html += '</div>';
        html += '<div class="dpr-home-news-item-meta">';
        html += '<span class="dpr-home-news-date">' + esc(dateFmt) + '</span>';
        html += '<span class="dpr-home-news-countdown ' + esc(tier) + '">' + esc(countdown) + '</span>';
        html += '</div>';
        html += '</li>';
      });
      html += '</ul>';
      return html;
    }

    function renderPanel(items) {
      var html = '';
      html += '<div class="dpr-home-news-header">';
      html += '<span class="dpr-home-news-entries">' + renderItems(items) + '</span>';
      html += '<a class="dpr-home-news-viewall" data-dpr-news-viewall href="javascript:void(0)">查看完整日历 ›</a>';
      html += '</div>';
      return html;
    }

    // ---- 入口链接交互 ----

    function bindViewAllLink(container) {
      var link = container.querySelector('[data-dpr-news-viewall]');
      if (!link) return;

      link.addEventListener('click', function (e) {
        e.preventDefault();
        try {
          var win = window;
          // 优先通过 DPRSidebar API 打开侧栏
          if (win.DPRSidebar && typeof win.DPRSidebar.openMobile === 'function') {
            win.DPRSidebar.openMobile();
          }
          // 尝试点击日程面板 toggle
          setTimeout(function () {
            var toggle = document.querySelector('[data-schedule-toggle]');
            if (toggle) toggle.click();
          }, 100);
        } catch (_err) {
          // 降级：无操作（链接本身无 href）
        }
      });
    }

    // ---- 初始化 ----

    function init(win) {
      win = win || window;
      var doc = win.document;
      if (!doc) return;

      var container = doc.querySelector(SELECTORS.container);
      if (!container) return;

      var fetchFn = win.fetch && win.fetch.bind
        ? win.fetch.bind(win)
        : win.fetch;

      fetchFn(SCHEDULE_JSON_PATH, { cache: 'no-store' })
        .then(function (res) {
          if (!res || !res.ok) throw new Error('fetch schedule failed');
          return res.json();
        })
        .then(function (schedule) {
          var items = flattenAndFilter(schedule, 8);
          container.innerHTML = renderPanel(items);
          container.hidden = false;
          bindViewAllLink(container);
        })
        .catch(function () {
          // 静默降级：容器保持 hidden，不干扰页面
        });
    }

    function autoInit(hostWindow) {
      hostWindow = hostWindow || (typeof window !== 'undefined' ? window : null);
      if (!hostWindow || hostWindow.__DPR_ACADEMIC_NEWS_AUTO_INIT__) {
        return;
      }

      var doc = hostWindow.document;

      function triggerInit() {
        try {
          init(hostWindow);
        } catch (_err) {
          // ignore
        }
      }

      if (doc && typeof doc.addEventListener === 'function') {
        doc.addEventListener('dpr-docsify-ready', triggerInit);
        if (doc.readyState === 'loading') {
          doc.addEventListener('DOMContentLoaded', triggerInit, { once: true });
        } else {
          triggerInit();
        }
      } else {
        triggerInit();
      }

      hostWindow.__DPR_ACADEMIC_NEWS_AUTO_INIT__ = true;
    }

    return {
      autoInit: autoInit,
      init: init,
      flattenAndFilter: flattenAndFilter,
      formatCountdownText: formatCountdownText,
      colorTier: colorTier,
    };
  },
);
