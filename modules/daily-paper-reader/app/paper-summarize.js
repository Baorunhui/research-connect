// 论文总结模块：把一篇（站外的）论文丢进来（arXiv/网页链接或 PDF），
// 调用本地后端 /api/paper/summarize 生成结构化中文总结。
// 依赖本地后端（local_server.py）；纯 GitHub Pages 静态部署下 PDF 功能不可用，会给出提示。
window.PaperSummarizer = (function () {
  'use strict';

  function apiUrl(path) {
    var base = String(window.DPR_LOCAL_API_BASE || '').trim().replace(/\/$/, '');
    return base + path;
  }

  var SUMMARIZE_ENDPOINT = apiUrl('/api/paper/summarize');
  var MAX_PDF_BYTES = 50 * 1024 * 1024; // 与后端默认一致（DPR_PDF_MAX_MB 默认 50MB）

  function isProbablyLocal() {
    var h = String(window.location && window.location.hostname || '').toLowerCase();
    return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h.indexOf('.local') >= 0;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function backendAvailable() {
    // 用轻量 health 端点探测，避免向 /api/paper/summarize 发空请求建废 job
    return fetch(apiUrl('/api/local/health'), { cache: 'no-store' })
      .then(function (r) { return r.ok; })
      .catch(function () { return false; });
  }

  // ---- 前端状态 ----
  var state = {
    file: null,   // 已选 PDF File
    busy: false,
    result: null,
    jobId: null,       // 当前异步 job_id
    seenEventIds: {},  // 已展示过的事件 event_id 集合（去重）
  };

  function renderResult(summary, meta, figures) {
    var out = document.querySelector('#paper-summarize-result');
    if (!out) return;
    out.textContent = '';
    out.classList.remove('is-error');

    var title = (meta && meta.title) || (summary && summary.title) || '';
    if (title) {
      var h = el('h3', 'paper-summarize-result-title', title);
      out.appendChild(h);
    }

    // _raw 表示结构化解析失败时后端返回的原始 markdown 正文
    if (summary && summary._raw) {
      var pre = el('pre', 'paper-summarize-raw', summary._raw);
      out.appendChild(pre);
      return;
    }

    var fields = [
      ['论文标题', 'title'],
      ['一句话总结 (TL;DR)', 'tl_dr'],
      ['研究问题', 'research_question'],
      ['核心方法', 'methodology'],
      ['主要结果', 'main_results'],
      ['创新点', 'innovation'],
      ['局限与不足', 'limitations'],
      ['阅读建议', 'reading_recommendation'],
    ];
    fields.forEach(function (pair) {
      var label = pair[0];
      var key = pair[1];
      var val = (summary && summary[key] != null) ? String(summary[key]) : '';
      if (!val.trim()) return;
      var card = el('div', 'paper-summarize-card');
      var lh = el('div', 'paper-summarize-card-label', label);
      var vh = el('div', 'paper-summarize-card-value', val);
      card.appendChild(lh);
      card.appendChild(vh);
      out.appendChild(card);
    });
    if (figures && figures.length) {
      renderFigureInterpretations(out, figures);
    }
    if (!out.childElementCount) {
      out.appendChild(el('p', '', '未能生成总结内容，请检查 LLM 配置或换用更清晰的论文来源。'));
    }
  }

  // 图表解读区块：复用日报的滚动图轮播（window.DPRMediaCarousel），逐图展示解读。
  function renderFigureInterpretations(out, figures) {
    var wrap = el('div', 'paper-summarize-figure-section');
    var heading = el('h3', 'paper-summarize-figure-heading', '📊 图表解读');
    wrap.appendChild(heading);

    // 把后端返回的 {index, caption(解读), image_b64} 转成轮播所需的 item。
    var items = figures
      .filter(function (f) { return f && f.image_b64; })
      .map(function (f, i) {
        return {
          url: 'data:image/webp;base64,' + f.image_b64,
          caption: (f.caption || '').trim(),
          index: Number(f.index || i + 1),
        };
      });
    if (!items.length) return;

    if (window.DPRMediaCarousel && typeof window.DPRMediaCarousel.renderFigures === 'function') {
      var html = window.DPRMediaCarousel.renderFigures(items);
      var host = el('div', 'paper-summarize-figure-carousel');
      host.innerHTML = html;
      wrap.appendChild(host);
      out.appendChild(wrap);
      // 激活轮播交互（上一张/下一张、缩略图）
      try { window.DPRMediaCarousel.bind(host); } catch (_err) { /* 忽略 */ }
    } else {
      // 兜底：无轮播组件时退化为简单的图+解读列表
      items.forEach(function (item) {
        var card = el('div', 'paper-summarize-figure-card');
        var img = el('img', 'paper-summarize-figure-img');
        img.src = item.url;
        img.alt = '论文图表';
        card.appendChild(img);
        if (item.caption) {
          var cap = el('div', 'paper-summarize-figure-caption', item.caption);
          card.appendChild(cap);
        }
        wrap.appendChild(card);
      });
      out.appendChild(wrap);
    }
  }

  function renderError(msg) {
    var out = document.querySelector('#paper-summarize-result');
    if (!out) return;
    out.textContent = '';
    out.classList.add('is-error');
    out.appendChild(el('p', '', '❌ ' + (msg || '总结失败')));
  }

  function setStatus(text, isError) {
    var s = document.querySelector('#paper-summarize-status');
    if (!s) return;
    s.textContent = text || '';
    s.classList.toggle('is-error', !!isError);
  }

  function setBusy(busy) {
    state.busy = busy;
    var btn = document.querySelector('#paper-summarize-submit');
    if (btn) btn.disabled = busy;
    var txt = busy ? '总结中…' : '开始总结';
    if (btn) {
      btn.textContent = txt;
      btn.classList.toggle('is-busy', busy);
    }
    var pause = document.querySelector('#paper-summarize-pause');
    if (pause) pause.style.display = busy ? 'block' : 'none';
  }

  function doSummarize(payload) {
    setBusy(true);
    document.querySelector('#paper-summarize-result').textContent = '';
    var progBox = document.querySelector('#paper-summarize-progress');
    if (progBox) progBox.textContent = '';
    state.jobId = null;
    state.seenEventIds = {};
    setStatus('');
    renderProgress([], 'queued');
    // 异步 job：POST 建job拿 job_id，然后轮询 GET /api/paper/summarize/<id> 拿进度事件
    return fetch(SUMMARIZE_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (resp) { return resp.json().catch(function () { return {}; }); })
      .then(function (data) {
        if (!data || !data.ok || !data.job_id) {
          state.busy = false;
          var msg = (data && (data.error || data.detail || data.message)) || '后端未返回 job_id';
          renderError(msg);
          setStatus('');
          return;
        }
        state.jobId = data.job_id;
        state.seenEventIds = {};
        setStatus('已创建任务 ' + data.job_id + '，正在处理…');
        pollJob(data.job_id);
      })
      .catch(function () {
        state.busy = false;
        renderError('无法连接本地后端。总结功能需要本地后端（python src/local_server.py）支持；纯 GitHub Pages 静态站点下不可用。');
        setStatus('');
      });
  }

  // ---- 轮询 job 状态 + 事件 ----
  var POLL_INTERVAL = 2000;

  function pollJob(jobId) {
    if (state.jobId !== jobId) return; // 已被新请求取代
    fetch(SUMMARIZE_ENDPOINT + '/' + encodeURIComponent(jobId), { cache: 'no-store' })
      .then(function (resp) { return resp.json().catch(function () { return {}; }); })
      .then(function (data) {
        if (!data || !data.ok || !data.job) {
          state.busy = false;
          renderError('轮询失败：未找到任务');
          setStatus('');
          return;
        }
        var job = data.job;
        var status = String(job.status || 'unknown').toLowerCase();
        var events = job.events || [];
        renderProgress(events, status, state.seenEventIds);
        if (status === 'completed') {
          state.busy = false;
          handleJobResult(job);
        } else if (status === 'failed') {
          state.busy = false;
          renderError(job.error || '总结失败');
          setStatus('');
        } else if (status === 'cancelled') {
          state.busy = false;
          setStatus('已取消');
        } else {
          // queued / running：继续轮询
          setTimeout(function () { pollJob(jobId); }, POLL_INTERVAL);
        }
      })
      .catch(function () {
        if (state.jobId !== jobId) return;
        state.busy = false;
        renderError('轮询中断，无法连接本地后端。');
        setStatus('');
      });
  }

  // 把新事件追加到进度区，已见过的按 event_id 去重
  function renderProgress(events, status, seenIds) {
    var box = document.querySelector('#paper-summarize-progress');
    if (!box) return;
    if (window.DPRTaskProgress && typeof window.DPRTaskProgress.render === 'function') {
      (events || []).forEach(function (ev) {
        if (ev && ev.event_id) seenIds[ev.event_id] = true;
      });
      window.DPRTaskProgress.render(box, {
        events: events,
        status: status,
        title: '⏳ 正在生成总结，请稍候…',
        doneTitle: '✅ 总结完成',
        failedTitle: '❌ 总结失败',
      });
      return;
    }
    var fresh = (events || []).filter(function (ev) {
      return ev && ev.event_id && !seenIds[ev.event_id];
    });
    fresh.forEach(function (ev) { seenIds[ev.event_id] = true; });
    if (!box.childElementCount) {
      var head = el('div', 'paper-summarize-progress-head', '⏳ 正在生成总结，请稍候…');
      box.appendChild(head);
      var bar = el('div', 'paper-summarize-progress-bar');
      var fill = el('div', 'paper-summarize-progress-fill');
      bar.appendChild(fill);
      box.appendChild(bar);
      var list = el('ul', 'paper-summarize-progress-list');
      list.id = 'paper-summarize-progress-list';
      box.appendChild(list);
    }
    var list = document.querySelector('#paper-summarize-progress-list');
    if (!list) return;
    fresh.forEach(function (ev) {
      var stage = ev.stage || ev.event_type || '';
      var msg = ev.message || '';
      var suffix = '';
      if (ev.current != null && ev.total != null) {
        suffix = ' (' + ev.current + '/' + ev.total + ')';
      }
      var li = el('li', 'paper-summarize-progress-item');
      var dot = el('span', 'paper-summarize-progress-dot');
      li.appendChild(dot);
      if (stage) li.appendChild(el('span', 'paper-summarize-progress-stage', stage));
      li.appendChild(el('span', 'paper-summarize-progress-msg', msg + suffix));
      list.appendChild(li);
    });
    // 更新进度条：按事件数粗略推进（不精确，仅视觉反馈）
    var fill = box.querySelector('.paper-summarize-progress-fill');
    if (fill) {
      var done = (status === 'completed' || status === 'failed' || status === 'cancelled');
      fill.style.width = done ? '100%' : Math.min(90, 10 + (events || []).length * 12) + '%';
    }
    if (status === 'completed') {
      var head = box.querySelector('.paper-summarize-progress-head');
      if (head) head.textContent = '✅ 总结完成';
    } else if (status === 'failed') {
      var head2 = box.querySelector('.paper-summarize-progress-head');
      if (head2) head2.textContent = '❌ 总结失败';
    }
    // 滚动到底
    try { box.scrollTop = box.scrollHeight; } catch (_e) {}
  }

  function handleJobResult(job) {
    var result = job.result || {};
    state.result = result.summary || {};
    // 方案 B：总结已由后端复用日报尾段落盘成 docs/<日期>/<id>-<slug>.md，
    // 其数据结构与日报纸张页同构。这里优先直接跳到该纸张页（用日报的展示层渲染），
    // 彻底复用 renderPaperFromMeta / 速览五段 / 图轮播；无 paper_id 时才退回卡片渲染。
    var meta = result.meta || {};
    var paperId = meta.paper_id || '';
    if (paperId) {
      setStatus('✅ 已生成纸张页，即将跳转…');
      var target = '#/' + paperId.replace(/^#?\//, '');
      try {
        if (typeof window.$docsify !== 'undefined') {
          window.$docsify.router.to(target);
        } else {
          window.location.hash = target;
        }
        setStatus('✅ 已打开纸张页「' + (meta.title || '') + '」');
      } catch (_e) {
        window.location.hash = target;
      }
      return;
    }
    renderResult(state.result, meta, (result.figures && result.figures.length ? result.figures : null));
    setStatus('✅ 完成');
  }

  function handleUrl() {
    var inp = document.querySelector('#paper-summarize-url');
    var url = (inp && inp.value || '').trim();
    if (!url) {
      renderError('请先输入论文链接（如 https://arxiv.org/abs/xxxx）');
      return;
    }
    return doSummarize({ source: 'url', url: url });
  }

  function handlePdf() {
    if (!state.file) {
      renderError('请先选择或拖入一个 PDF 文件');
      return;
    }
    var reader = new FileReader();
    var file = state.file;
    var promise = new Promise(function (resolve, reject) {
      reader.onload = function () {
        var b64 = String(reader.result).split(',')[1] || '';
        resolve(doSummarize({ source: 'pdf', filename: file.name, data_b64: b64 }).then(function () { return null; }));
      };
      reader.onerror = function () { reject(new Error('读取文件失败')); };
    });
    reader.readAsDataURL(file);
    return promise;
  }

  function setFileLabel(name) {
    var label = document.querySelector('#paper-summarize-file-name');
    if (label) label.textContent = name ? ('已选择：' + name) : '拖入 PDF，或点击选择文件';
  }

  function setupEvents(elems) {
    var urlBtn = elems && elems.urlBtn;
    if (urlBtn) urlBtn.addEventListener('click', function () { handleUrl(); });

    var fileInput = elems && elems.fileInput;
    var dropZone = elems && elems.dropZone;
    if (fileInput) {
      fileInput.addEventListener('change', function () {
        state.file = fileInput.files && fileInput.files[0] || null;
        setFileLabel(state.file ? state.file.name : '');
      });
    }
    if (dropZone) {
      dropZone.addEventListener('dragover', function (e) {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('is-dragover');
      });
      dropZone.addEventListener('dragleave', function () { dropZone.classList.remove('is-dragover'); });
      dropZone.addEventListener('drop', function (e) {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('is-dragover');
        var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f && /\.pdf$/i.test(f.name)) {
          state.file = f;
          if (fileInput) fileInput.value = '';
          setFileLabel(f.name);
        } else {
          renderError('请拖入 PDF 文件');
        }
      });
      dropZone.addEventListener('click', function () {
        if (fileInput) fileInput.click();
      });
    }

    var pdfBtn = elems && elems.pdfBtn;
    if (pdfBtn) pdfBtn.addEventListener('click', function () { handlePdf(); });
  }

  function buildUI() {
    var root = document.createElement('div');
    root.className = 'paper-summarize';
    root.id = 'paper-summarize';

    var title = el('h2', 'paper-summarize-heading', '📄 论文总结');
    root.appendChild(title);
    var subtitle = el('p', 'paper-summarize-sub', '贴一篇 arXiv/网页链接，或上传 PDF，即可得到结构化中文总结（本地后端驱动）。');
    root.appendChild(subtitle);

    var local = isProbablyLocal();

    // ---- 链接输入 ----
    var urlSection = el('div', 'paper-summarize-section');
    var urlLabel = el('label', 'paper-summarize-label', '论文链接');
    urlLabel.setAttribute('for', 'paper-summarize-url');
    urlSection.appendChild(urlLabel);
    var urlRow = el('div', 'paper-summarize-url-row');
    var urlInput = el('input', 'paper-summarize-input');
    urlInput.type = 'text';
    urlInput.id = 'paper-summarize-url';
    urlInput.placeholder = '例如 https://arxiv.org/abs/2301.12091 或其它论文网页';
    var urlBtn = el('button', 'paper-summarize-btn', '总结');
    urlBtn.id = 'paper-summarize-submit-url';
    urlBtn.type = 'button';
    urlRow.appendChild(urlInput);
    urlRow.appendChild(urlBtn);
    urlSection.appendChild(urlRow);
    root.appendChild(urlSection);

    // ---- PDF 输入 ----
    var pdfSection = el('div', 'paper-summarize-section');
    var pdfLabel = el('div', 'paper-summarize-label', '上传 PDF');
    pdfSection.appendChild(pdfLabel);
    if (local) {
      var drop = el('div', 'paper-summarize-drop');
      drop.id = 'paper-summarize-drop';
      drop.setAttribute('role', 'button');
      drop.setAttribute('tabindex', '0');
      drop.appendChild(el('div', 'paper-summarize-drop-icon', '⬆️'));
      var fileLabel = el('div', 'paper-summarize-file-name', '拖入 PDF，或点击选择文件');
      fileLabel.id = 'paper-summarize-file-name';
      drop.appendChild(fileLabel);
      drop.appendChild(el('div', 'paper-summarize-drop-hint', 'PDF 全文将在本地后端解析，上限 50MB'));
      var fileInput = el('input', 'paper-summarize-file-input');
      fileInput.type = 'file';
      fileInput.id = 'paper-summarize-file-input';
      fileInput.accept = 'application/pdf,.pdf';
      fileInput.style.display = 'none';
      var pdfBtn = el('button', 'paper-summarize-btn', '总结该 PDF');
      pdfBtn.id = 'paper-summarize-submit-pdf';
      pdfBtn.type = 'button';
      pdfSection.appendChild(drop);
      pdfSection.appendChild(fileInput);
      pdfSection.appendChild(pdfBtn);
    } else {
      var hint = el('p', 'paper-summarize-hint', '上传 PDF 需要本地后端。当前为静态部署：可使用上方链接总结（经后端代理），或通过本地 backend（python src/local_server.py）来获得 PDF 总结。');
      pdfSection.appendChild(hint);
    }
    root.appendChild(pdfSection);

    // ---- 统一提交 / 状态 / 结果 ----
    var submitRow = el('div', 'paper-summarize-submit-row');
    var submit = el('button', 'paper-summarize-btn paper-summarize-submit', '开始总结');
    submit.id = 'paper-summarize-submit';
    submit.type = 'button';
    submit.addEventListener('click', function () {
      if (state.busy) return;
      if (state.file) handlePdf(); else handleUrl();
    });
    submitRow.appendChild(submit);
    var status = el('div', 'paper-summarize-status');
    status.id = 'paper-summarize-status';
    submitRow.appendChild(status);
    root.appendChild(submitRow);

    var pause = el('div', 'paper-summarize-pause');
    pause.id = 'paper-summarize-pause';
    pause.textContent = '正在请求 LLM 总结，DeepSeek 结构化输出可能需要数秒~数十秒，请稍候…';
    root.appendChild(pause);

    var progress = el('div', 'paper-summarize-progress');
    progress.id = 'paper-summarize-progress';
    root.appendChild(progress);

    var result = el('div', 'paper-summarize-result');
    result.id = 'paper-summarize-result';
    root.appendChild(result);

    // 必须在元素已 append 到 root 之后、且用元素引用绑定（不能用 document.querySelector，
    // 因为 init() 调用本函数时 root 尚未插入 document，querySelector 会返回 null 导致事件丢失）。
    setupEvents({
      urlBtn: urlBtn,
      dropZone: typeof drop !== 'undefined' ? drop : null,
      fileInput: typeof fileInput !== 'undefined' ? fileInput : null,
      pdfBtn: typeof pdfBtn !== 'undefined' ? pdfBtn : null,
    });
    return root;
  }

  function init(container) {
    if (!container) return;
    // 幂等：重复路由不重复挂载
    if (container.querySelector('#paper-summarize')) return;
    container.appendChild(buildUI());
    // 非本地部署下，仅保留链接总结可用（链接也走本地后端代理，静态部署同样不可用，
    // 此时直接提示不可用）。
    if (!isProbablyLocal()) {
      backendAvailable().then(function (ok) {
        if (!ok) {
          var s = document.querySelector('#paper-summarize-status');
          if (s) {
            s.textContent = '⚠️ 当前未检测到本地后端，总结功能不可用。请运行 python src/local_server.py 后刷新。';
            s.classList.add('is-error');
          }
        }
      });
    }
    return true;
  }

  return {
    init: init,
  };
})();

// 模块加载完成：派发事件，通知 docsify-plugin 若当前正处于 summarize 路由则补挂载一次。
// 原因：本模块是延迟异步加载的，若首次路由到 summarize 时 JS 尚未就绪，mountPaperSummarizer
// 会跳过挂载且无补偿，导致入口 UI 一直缺失。docsify-plugin 监听 dpr-deferred-assets-ready 会重新挂载。
// 注意：dispatchEvent 同步执行监听器，必须在 window.PaperSummarizer 赋值完成之后派发，
// 否则挂载会被 undefined 守卫拦下（冷加载首路由一直缺失的根因）。
if (typeof document !== 'undefined') {
  document.dispatchEvent(new Event('dpr-deferred-assets-ready'));
}
