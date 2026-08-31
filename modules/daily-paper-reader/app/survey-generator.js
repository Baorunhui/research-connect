// 综述生成模块：输入研究主题，经本地后端 /api/survey 异步 Job 完成全链生成
// （召回 → 精选 → 抽取 → 聚类 → PDF 深读 → 分析 → 大纲 → 写作 → 审校），
// 报告落盘为站点正式页面 docs/survey/<slug>-<hash>.md 并注册侧栏。
// 依赖本地后端（local_server.py，默认 8567，同源相对路径）；纯 GitHub Pages 静态部署不可用。
window.SurveyGenerator = (function () {
  'use strict';

  var SURVEY_ENDPOINT = '/api/survey';
  var POLL_INTERVAL = 2500;

  // 阶段序与进度条权重（累计到该阶段完成时的比例；带 current/total 的事件在区间内线性推进）
  var STAGE_WEIGHTS = {
    seed: 0.1,
    recall: 0.26,
    coarse: 0.32,
    rerank: 0.38,
    extract: 0.52,
    cluster: 0.58,
    deepread: 0.7,
    analyse: 0.83,
    outline: 0.87,
    write: 0.96,
    review: 0.98,
    render: 1.0,
  };
  var STAGE_PREV = {
    seed: 0.03,
    recall: 0.1,
    coarse: 0.26,
    rerank: 0.32,
    extract: 0.38,
    cluster: 0.52,
    deepread: 0.58,
    analyse: 0.7,
    outline: 0.83,
    write: 0.87,
    review: 0.96,
    render: 0.98,
  };
  var STAGE_LABELS = {
    seed: '种子分析',
    recall: '召回',
    coarse: '语义粗排',
    rerank: '精选',
    extract: '抽取',
    cluster: '聚类',
    deepread: '全文深读',
    analyse: '分析',
    outline: '大纲',
    write: '写作',
    review: '审校',
    render: '落盘',
  };

  function isProbablyLocal() {
    var h = String((window.location && window.location.hostname) || '').toLowerCase();
    return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h.indexOf('.local') >= 0;
  }

  function backendAvailable() {
    // 轻量 health 探测，避免向后端发空请求建废 job
    return fetch('/api/local/health', { cache: 'no-store' })
      .then(function (r) { return r.ok; })
      .catch(function () { return false; });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function getEl(id) {
    return document.getElementById(id);
  }

  // ---- 前端状态 ----
  var state = {
    busy: false,
    polling: false,
    jobId: null,
    seenEventIds: {},
    timer: null,
    seedFile: null, // 已选种子 PDF File（与链接互斥：有文件优先）
  };

  function setStatus(text, isError) {
    var s = getEl('survey-status');
    if (!s) return;
    s.textContent = text || '';
    s.classList.toggle('is-error', !!isError);
  }

  function setBusy(busy) {
    state.busy = busy;
    var btn = getEl('survey-submit');
    if (btn) {
      btn.disabled = busy;
      btn.textContent = busy ? '生成中…' : '开始综述';
      btn.classList.toggle('is-busy', busy);
    }
    var cancelBtn = getEl('survey-cancel');
    if (cancelBtn) cancelBtn.style.display = busy ? '' : 'none';
  }

  function stopPolling() {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    state.polling = false;
  }

  function markedHtml(md) {
    try {
      if (window.marked && typeof window.marked.parse === 'function') return window.marked.parse(md);
      if (window.marked && typeof window.marked === 'function') return window.marked(md);
    } catch (_err) { /* ignore */ }
    return String(md || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\n/g, '<br/>');
  }

  function renderResult(md) {
    var out = getEl('survey-result');
    if (!out) return;
    out.textContent = '';
    out.classList.remove('is-error');
    var box = el('div', 'survey-result-box');
    box.innerHTML = markedHtml(md);
    out.appendChild(box);
  }

  function renderError(msg) {
    var out = getEl('survey-result');
    if (!out) return;
    out.textContent = '';
    out.classList.add('is-error');
    out.appendChild(el('p', 'survey-error-text', '❌ ' + (msg || '综述生成失败')));
  }

  // ---- 进度渲染（事件按 event_id 去重，进度条按阶段权重推进） ----
  function progressPercent(events, status) {
    if (status === 'completed') return 100;
    if (status === 'failed' || status === 'cancelled') return 100;
    var latest = null;
    (events || []).forEach(function (ev) {
      if (ev && ev.stage && STAGE_WEIGHTS[ev.stage] != null) latest = ev;
    });
    if (!latest) return 6;
    var hi = STAGE_WEIGHTS[latest.stage];
    var lo = STAGE_PREV[latest.stage] != null ? STAGE_PREV[latest.stage] : hi * 0.8;
    if (latest.current != null && latest.total != null && latest.total > 0) {
      var frac = Math.min(Math.max(latest.current / latest.total, 0), 1);
      return Math.round((lo + (hi - lo) * frac) * 100);
    }
    return Math.round(((lo + hi) / 2) * 100);
  }

  function renderProgressCard(job) {
    var out = getEl('survey-result');
    if (!out) return;
    var status = String(job.status || 'unknown').toLowerCase();
    var events = job.events || [];
    out.textContent = '';
    out.classList.remove('is-error');

    var card = el('div', 'survey-progress-card');
    var headline = {
      running: '⏳ 综述生成中',
      queued: '📥 已排队，等待开始',
      completed: '✅ 综述完成',
      failed: '❌ 综述失败',
      cancelled: '🚫 已取消',
    }[status] || '⏳ 综述生成中';
    card.appendChild(el('div', 'survey-progress-title', headline));
    card.appendChild(el('div', 'survey-progress-run', 'job_id: ' + (job.job_id || '')));

    var bar = el('div', 'survey-progress-bar');
    var fill = el('div', 'survey-progress-fill');
    fill.style.width = progressPercent(events, status) + '%';
    bar.appendChild(fill);
    card.appendChild(bar);

    var latest = null;
    (events || []).forEach(function (ev) {
      if (ev && ev.stage) latest = ev;
    });
    if (latest) {
      var label = STAGE_LABELS[latest.stage] || latest.stage;
      var suffix = (latest.current != null && latest.total != null) ? ('（' + latest.current + '/' + latest.total + '）') : '';
      card.appendChild(el('div', 'survey-progress-msg', '「' + label + '」' + suffix + (latest.message || '')));
    } else if (status === 'queued') {
      card.appendChild(el('div', 'survey-progress-msg', '任务已排队，正在等待流水线启动。'));
    }

    // 阶段事件流水（去重追加）
    var logBody = el('pre', 'survey-log-body survey-progress-log');
    var lines = [];
    (events || []).forEach(function (ev) {
      if (!ev || !ev.stage) return;
      var tag = STAGE_LABELS[ev.stage] || ev.stage;
      var suffix2 = (ev.current != null && ev.total != null) ? (' ' + ev.current + '/' + ev.total) : '';
      lines.push('[' + tag + suffix2 + '] ' + (ev.message || ''));
    });
    logBody.textContent = lines.slice(-80).join('\n');
    card.appendChild(logBody);

    var actions = el('div', 'survey-progress-actions');
    if (status === 'running' || status === 'queued') {
      var cancelBtn = el('button', 'survey-btn survey-btn-ghost', '取消任务');
      cancelBtn.id = 'survey-cancel';
      cancelBtn.type = 'button';
      cancelBtn.addEventListener('click', function () { requestCancel(job.job_id); });
      actions.appendChild(cancelBtn);
    }
    var logBtn = el('button', 'survey-btn survey-btn-ghost', '查看运行日志');
    logBtn.type = 'button';
    logBtn.addEventListener('click', function () { loadLog(job.job_id); });
    actions.appendChild(logBtn);
    card.appendChild(actions);

    out.appendChild(card);
    try { out.scrollTop = out.scrollHeight; } catch (_e) { /* ignore */ }
  }

  function requestCancel(jobId) {
    fetch(SURVEY_ENDPOINT + '/' + encodeURIComponent(jobId) + '/cancel', { method: 'POST' })
      .then(function () { setStatus('已请求取消，等待流水线在阶段边界停止…'); })
      .catch(function () { setStatus('取消请求发送失败', true); });
  }

  function loadLog(jobId) {
    fetch(SURVEY_ENDPOINT + '/' + encodeURIComponent(jobId) + '/log', { cache: 'no-store' })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        var out = getEl('survey-result');
        if (!out) return;
        out.textContent = '';
        out.classList.remove('is-error');
        var title = el('h3', 'survey-log-title', '📜 运行日志');
        var pre = el('pre', 'survey-log-body', String((data && data.log) || '(空)'));
        var back = el('button', 'survey-btn survey-btn-ghost', '← 返回结果');
        back.type = 'button';
        back.addEventListener('click', function () {
          if (state.jobId) pollJob(state.jobId);
        });
        var backRow = el('div', 'survey-log-back');
        backRow.appendChild(back);
        out.appendChild(backRow);
        out.appendChild(title);
        out.appendChild(pre);
      })
      .catch(function () { renderError('读取日志失败。'); });
  }

  // ---- 轮询 ----
  function pollJob(jobId) {
    if (state.polling || !jobId) return;
    state.jobId = jobId;
    state.seenEventIds = {};
    state.polling = true;
    schedulePoll(jobId);
  }

  function schedulePoll(jobId) {
    fetch(SURVEY_ENDPOINT + '/' + encodeURIComponent(jobId), { cache: 'no-store' })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        state.polling = false;
        if (state.jobId !== jobId) return;
        var job = data && data.job;
        if (!job) {
          renderError('未找到综述任务（job not found）。');
          setStatus('');
          setBusy(false);
          return;
        }
        var status = String(job.status || 'unknown').toLowerCase();
        renderProgressCard(job);
        if (status === 'completed') {
          setBusy(false);
          handleJobResult(job);
        } else if (status === 'failed') {
          setBusy(false);
          setStatus('综述失败：' + (job.error || ''), true);
          renderError(job.error || '综述生成失败');
          listRuns();
        } else if (status === 'cancelled') {
          setBusy(false);
          setStatus('任务已取消。');
          listRuns();
        } else {
          state.timer = setTimeout(function () {
            state.polling = true;
            schedulePoll(jobId);
          }, POLL_INTERVAL);
        }
      })
      .catch(function () {
        state.polling = false;
        if (state.jobId !== jobId) return;
        renderError('轮询中断，无法连接本地后端（8567）。');
        setStatus('');
        setBusy(false);
      });
  }

  function handleJobResult(job) {
    var result = job.result || {};
    var report = result.report || {};
    var paperId = report.paper_id || report.route || '';
    if (paperId) {
      setStatus('✅ 综述已生成，正在打开报告页…');
      var target = '#/' + String(paperId).replace(/^#?\//, '');
      try {
        if (typeof window.$docsify !== 'undefined') window.$docsify.router.to(target);
        else window.location.hash = target;
      } catch (_e) { window.location.hash = target; }
      listRuns();
      return;
    }
    setStatus('✅ 任务完成，但未取得报告路由，请从历史列表打开。');
    listRuns();
  }

  // ---- 运行历史 ----
  function listRuns() {
    fetch(SURVEY_ENDPOINT, { cache: 'no-store' })
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (data) {
        var wrap = document.getElementById('survey-history');
        if (!wrap) return;
        wrap.textContent = '';
        var jobs = (data && data.jobs) || [];
        if (!jobs.length) {
          wrap.appendChild(el('p', 'survey-history-empty', '暂无综述记录。提交一个主题后，这里会自动出现。'));
          return;
        }
        wrap.appendChild(el('div', 'survey-history-heading', '历史综述'));
        var list = el('div', 'survey-history-list');
        jobs.slice(0, 15).forEach(function (job) {
          var item = el('div', 'survey-history-item');
          var row = el('div', 'survey-history-row');
          var input = job.input || {};
          var report = (job.result && job.result.report) || {};
          var status = String(job.status || '').toLowerCase();
          var label = report.title || input.query || job.job_id || '';
          row.appendChild(el('span', 'survey-history-id', label + '（' + (job.job_id || '') + '）'));
          var btn = el('button', 'survey-history-view',
            status === 'completed' ? '打开报告' : '查看 / 继续轮询');
          btn.onclick = function () {
            if (status === 'completed' && report.route) {
              var target = '#/' + String(report.route).replace(/^#?\//, '');
              if (typeof window.$docsify !== 'undefined') window.$docsify.router.to(target);
              else window.location.hash = target;
            } else {
              setBusy(status === 'running' || status === 'queued');
              pollJob(job.job_id);
            }
          };
          row.appendChild(btn);
          item.appendChild(row);
          if (status === 'failed') {
            item.appendChild(el('div', 'survey-history-empty', '失败：' + (job.error || '')));
          }
          list.appendChild(item);
        });
        wrap.appendChild(list);
      })
      .catch(function () { /* 静默：历史列表非关键 */ });
  }

  // ---- 提交 ----
  function clampRange(n, min, max, fallback) {
    if (!isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, Math.round(n)));
  }

  function handleLaunch() {
    var q = getEl('survey-query');
    var query = (q && q.value || '').trim();
    if (!query) {
      renderError('请先输入研究主题。');
      return;
    }
    var maxP = getEl('survey-max-papers');
    var rerankEl = getEl('survey-use-rerank');
    var deepEl = getEl('survey-deep-read');
    var deepxivEl = getEl('survey-use-deepxiv');
    var kaggleEl = getEl('survey-use-kaggle');
    var payload = {
      query: query,
      max_papers: clampRange(maxP ? Number(maxP.value) : NaN, 5, 200, 30),
      fetch_days: lookbackDaysFromUI(),
      use_rerank: rerankEl ? !!rerankEl.checked : true,
      deep_read: deepEl ? !!deepEl.checked : true,
      use_deepxiv: deepxivEl ? !!deepxivEl.checked : false,
      use_kaggle: kaggleEl ? !!kaggleEl.checked : false,
      coarse_top_k: coarseTopFromUI(),
    };
    // 种子论文：PDF 文件优先，否则读 arXiv 链接
    if (state.seedFile) {
      var reader = new FileReader();
      var file = state.seedFile;
      reader.onload = function () {
        var b64 = String(reader.result).split(',')[1] || '';
        payload.seed = { source: 'pdf', filename: file.name, data_b64: b64 };
        doLaunch(payload);
      };
      reader.onerror = function () {
        renderError('读取种子 PDF 失败，请重试或改用 arXiv 链接。');
      };
      reader.readAsDataURL(file);
      return;
    }
    var seedUrlEl = getEl('survey-seed-url');
    var seedUrl = (seedUrlEl && seedUrlEl.value || '').trim();
    if (seedUrl) payload.seed = { source: 'url', url: seedUrl };
    doLaunch(payload);
  }

  function doLaunch(payload) {
    setBusy(true);
    setStatus('');
    var out = getEl('survey-result');
    if (out) out.textContent = '';
    stopPolling();
    return fetch(SURVEY_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (resp) { return resp.json().catch(function () { return {}; }); })
      .then(function (data) {
        if (data && data.ok && data.job_id) {
          setStatus('📮 任务已提交，job_id: ' + data.job_id);
          listRuns();
          pollJob(data.job_id);
        } else {
          setBusy(false);
          renderError('提交失败：' + ((data && data.error) || '未知错误'));
        }
      })
      .catch(function () {
        setBusy(false);
        renderError('无法连接本地后端。综述功能需要本地后端（python src/local_server.py，默认 8567）支持；纯 GitHub Pages 静态站点下不可用。');
      });
  }

  // ---- UI ----
  function buildNumberField(id, labelText, defaultValue, hintText, minV, maxV) {
    var wrap = el('div', 'survey-param');
    var lab = el('label', 'survey-label survey-param-label', labelText);
    lab.setAttribute('for', id);
    var input = el('input', 'survey-input survey-param-input');
    input.type = 'number';
    input.id = id;
    input.value = defaultValue;
    input.min = minV;
    input.max = maxV;
    if (hintText) lab.appendChild(el('span', 'survey-param-hint', hintText));
    wrap.appendChild(lab);
    wrap.appendChild(input);
    return wrap;
  }

  function buildCheckbox(id, labelText, checked) {
    var wrap = el('label', 'survey-param survey-param-check');
    wrap.setAttribute('for', id);
    var input = el('input', 'survey-input survey-param-checkbox');
    input.type = 'checkbox';
    input.id = id;
    input.checked = checked;
    var lab = el('span', 'survey-label survey-param-label', labelText);
    wrap.appendChild(input);
    wrap.appendChild(lab);
    return wrap;
  }

  // 回溯时长：综述以年为单位回溯（同一研究方向跨年的工作才够成综述），
  // 数值 + 单位（天/月/年），发送时统一换算为天数，后端上限 3 年。
  function buildLookbackField() {
    var wrap = el('div', 'survey-param');
    var lab = el('label', 'survey-label survey-param-label', '回溯时长');
    lab.setAttribute('for', 'survey-fetch-value');
    var row = el('span', 'survey-param-row');
    var input = el('input', 'survey-input survey-param-input survey-lookback-value');
    input.type = 'number';
    input.id = 'survey-fetch-value';
    input.value = '1';
    input.min = 1;
    input.max = 3;
    var unit = document.createElement('select');
    unit.className = 'survey-input survey-param-unit';
    unit.id = 'survey-fetch-unit';
    [
      ['day', '天'],
      ['month', '月'],
      ['year', '年'],
    ].forEach(function (pair) {
      var opt = document.createElement('option');
      opt.value = pair[0];
      opt.textContent = pair[1];
      if (pair[0] === 'year') opt.selected = true;
      unit.appendChild(opt);
    });
    row.appendChild(input);
    row.appendChild(unit);
    lab.appendChild(el('span', 'survey-param-hint', '按年回溯，最长 3 年'));
    wrap.appendChild(lab);
    wrap.appendChild(row);
    return wrap;
  }

  function lookbackDaysFromUI() {
    var valueEl = getEl('survey-fetch-value');
    var unitEl = getEl('survey-fetch-unit');
    var n = clampRange(valueEl ? Number(valueEl.value) : NaN, 1, 1095, 365);
    var factor = { day: 1, month: 30, year: 365 }[(unitEl && unitEl.value) || 'year'] || 365;
    return clampRange(n * factor, 1, 1095, 365);
  }

  // Kaggle 快照词法粗筛规模（三档）：3千轻量 / 1万标准 / 3万深挖，
  // 万级候选随后由本地语义粗排收窄到数百再进 rerank
  function buildCoarseTopField() {
    var wrap = el('div', 'survey-param');
    var lab = el('label', 'survey-label survey-param-label', '粗筛规模');
    lab.setAttribute('for', 'survey-coarse-top');
    var row = el('span', 'survey-param-row');
    var select = document.createElement('select');
    select.className = 'survey-input survey-param-unit';
    select.id = 'survey-coarse-top';
    [
      ['3000', '3 千篇'],
      ['10000', '1 万篇'],
      ['30000', '3 万篇'],
    ].forEach(function (pair) {
      var opt = document.createElement('option');
      opt.value = pair[0];
      opt.textContent = pair[1];
      if (pair[0] === '10000') opt.selected = true;
      select.appendChild(opt);
    });
    row.appendChild(select);
    lab.appendChild(el('span', 'survey-param-hint', 'Kaggle 快照候选量'));
    wrap.appendChild(lab);
    wrap.appendChild(row);
    return wrap;
  }

  function coarseTopFromUI() {
    var sel = getEl('survey-coarse-top');
    var n = sel ? Number(sel.value) : NaN;
    return clampRange(n, 500, 30000, 10000);
  }

  function bindEvents(submitBtn, topicInput, seedDropEl, seedFileInputEl) {
    // 必须用元素引用直接绑定：buildUI 阶段 UI 尚未插入 document，
    // document.getElementById 此时返回 null，监听器会静默丢失、点击无任何反应
    // （paper-summarize 同款教训）。点击时 handleLaunch 内部再用 getElementById 读值是安全的，
    // 那时元素已在文档中。
    if (submitBtn) submitBtn.addEventListener('click', function () { if (!state.busy) handleLaunch(); });
    if (topicInput) topicInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !state.busy) {
        e.preventDefault();
        handleLaunch();
      }
    });
    if (seedFileInputEl) {
      seedFileInputEl.addEventListener('change', function () {
        state.seedFile = seedFileInputEl.files && seedFileInputEl.files[0] || null;
        setSeedFileLabel(state.seedFile ? state.seedFile.name : '');
      });
    }
    if (seedDropEl) {
      seedDropEl.addEventListener('dragover', function (e) {
        e.preventDefault();
        e.stopPropagation();
        seedDropEl.classList.add('is-dragover');
      });
      seedDropEl.addEventListener('dragleave', function () { seedDropEl.classList.remove('is-dragover'); });
      seedDropEl.addEventListener('drop', function (e) {
        e.preventDefault();
        e.stopPropagation();
        seedDropEl.classList.remove('is-dragover');
        var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f && /\.pdf$/i.test(f.name)) {
          state.seedFile = f;
          if (seedFileInputEl) seedFileInputEl.value = '';
          setSeedFileLabel(f.name);
        } else if (f) {
          renderError('种子论文请拖入 PDF 文件，或使用上方 arXiv 链接');
        }
      });
      seedDropEl.addEventListener('click', function () {
        if (seedFileInputEl) seedFileInputEl.click();
      });
    }
  }

  function setSeedFileLabel(name) {
    var label = getEl('survey-seed-file-name');
    if (label) {
      label.textContent = name ? ('已选择种子：' + name) : '或拖入 / 点击上传种子 PDF';
    }
  }

  function buildUI() {
    var root = document.createElement('div');
    root.className = 'survey';
    root.id = 'survey';

    root.appendChild(el('h2', 'survey-heading', '📋 综述生成'));
    root.appendChild(el('p', 'survey-sub', '输入研究主题，流水线将从论文库召回候选、精选、聚类分析并生成一篇带引用的领域综述报告；报告会保存为站点页面并出现在左侧栏「Survey Reports」分组。'));

    var section = el('div', 'survey-section');
    var label = el('label', 'survey-label', '研究主题');
    label.setAttribute('for', 'survey-query');
    section.appendChild(label);
    var textarea = el('textarea', 'survey-input survey-input-query');
    textarea.id = 'survey-query';
    textarea.rows = 3;
    textarea.placeholder = '例如：多模态大模型的安全与对齐，或者：基于扩散模型的图像生成';
    section.appendChild(textarea);
    root.appendChild(section);

    // ---- 种子论文（可选）：锚定任务范式 + 自动追踪其参考文献 ----
    var seedSection = el('div', 'survey-section');
    var seedLabel = el('label', 'survey-label', '种子论文（可选，推荐）');
    seedLabel.setAttribute('for', 'survey-seed-url');
    seedLabel.appendChild(el('span', 'survey-param-hint', '锚定任务范式并自动追踪其参考文献'));
    seedSection.appendChild(seedLabel);
    var seedUrlInput = el('input', 'survey-input survey-seed-url');
    seedUrlInput.type = 'text';
    seedUrlInput.id = 'survey-seed-url';
    seedUrlInput.placeholder = 'arXiv 链接，如 https://arxiv.org/abs/2411.18011';
    seedSection.appendChild(seedUrlInput);
    var seedDrop = el('div', 'survey-seed-drop');
    seedDrop.id = 'survey-seed-drop';
    seedDrop.setAttribute('role', 'button');
    seedDrop.setAttribute('tabindex', '0');
    seedDrop.appendChild(el('span', 'survey-seed-drop-icon', '📄'));
    var seedFileName = el('span', 'survey-seed-file-name', '或拖入 / 点击上传种子 PDF');
    seedFileName.id = 'survey-seed-file-name';
    seedDrop.appendChild(seedFileName);
    var seedFileInput = el('input', 'survey-seed-file-input');
    seedFileInput.type = 'file';
    seedFileInput.id = 'survey-seed-file-input';
    seedFileInput.accept = 'application/pdf,.pdf';
    seedFileInput.style.display = 'none';
    seedSection.appendChild(seedDrop);
    seedSection.appendChild(seedFileInput);
    root.appendChild(seedSection);

    var params = el('div', 'survey-section survey-params');
    params.appendChild(buildNumberField('survey-max-papers', '候选论文数', '30', '5–200', 5, 200));
    params.appendChild(buildLookbackField());
    params.appendChild(buildCheckbox('survey-use-rerank', 'Reranker 精选', true));
    params.appendChild(buildCheckbox('survey-deep-read', '核心论文 PDF 深读', true));
    // 召回路开关（独立可关，便于 A/B 对比两条外部路的耗时与质量）：
    // Kaggle = 本地全量快照词法粗筛（万级、零限流、默认主路）；
    // DeepXiv = 语义检索（被引数 + 周级新鲜度，默认关——外部服务有 token 限额/波动，需要时勾选）
    params.appendChild(buildCheckbox('survey-use-deepxiv', 'DeepXiv 外部检索（可选）', false));
    params.appendChild(buildCheckbox('survey-use-kaggle', 'Kaggle 本地快照粗筛', true));
    params.appendChild(buildCoarseTopField());
    root.appendChild(params);

    var submitRow = el('div', 'survey-submit-row');
    var submit = el('button', 'survey-btn survey-submit', '开始综述');
    submit.id = 'survey-submit';
    submit.type = 'button';
    submitRow.appendChild(submit);
    var status = el('div', 'survey-status');
    status.id = 'survey-status';
    submitRow.appendChild(status);
    root.appendChild(submitRow);

    var result = el('div', 'survey-result');
    result.id = 'survey-result';
    root.appendChild(result);

    var history = el('div', 'survey-history');
    history.id = 'survey-history';
    root.appendChild(history);

    bindEvents(submit, textarea, seedDrop, seedFileInput);
    return root;
  }

  function init(container) {
    if (!container) return;
    if (container.querySelector('#survey')) return; // 幂等
    container.appendChild(buildUI());
    backendAvailable().then(function (ok) {
      if (ok) {
        setStatus('✅ 本地后端已连接，可提交综述。');
      } else {
        setStatus('⚠️ 未检测到本地后端。综述功能需要运行 python src/local_server.py（默认 8567）。', true);
      }
    });
    listRuns();
    return true;
  }

  return {
    init: init,
  };
})();

// 模块加载完成：派发事件，通知 docsify-plugin 若当前正处于 survey 路由则补挂载一次
// （本模块延迟异步加载，首次路由时可能尚未就绪）。
// 必须在 IIFE 赋值完成之后派发：dispatchEvent 同步执行监听器，
// 写在 IIFE 内部 return 之前时 window.SurveyGenerator 尚未赋值，挂载会被 undefined 守卫拦下。
if (typeof document !== 'undefined') {
  document.dispatchEvent(new Event('dpr-deferred-assets-ready'));
}
