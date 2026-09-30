// 论文总结模块：把一篇（站外的）论文丢进来（arXiv/网页链接或 PDF），
// 调用本地后端 /api/paper/summarize 生成结构化中文总结。
// 依赖本地后端（local_server.py）；纯 GitHub Pages 静态部署下 PDF 功能不可用，会给出提示。
window.PaperSummarizer = (function () {
  'use strict';

  function apiUrl(path) {
    var base = String(window.DPR_LOCAL_API_BASE || '').trim().replace(/\/$/, '');
    return base + path;
  }

  function summarizeEndpoint() {
    return apiUrl('/api/paper/summarize');
  }
  var MAX_PDF_BYTES = 50 * 1024 * 1024; // 与后端默认一致（DPR_PDF_MAX_MB 默认 50MB）

  function rememberSummarizeJob(jobId) {
    try { sessionStorage.setItem('dpr_summarize_job', jobId); } catch (e) { /* 浏览器禁了存储就只在当前页看进度 */ }
  }

  function forgetSummarizeJob() {
    try { sessionStorage.removeItem('dpr_summarize_job'); } catch (e) { /* 同上 */ }
  }

  function readSummarizeJob() {
    try { return sessionStorage.getItem('dpr_summarize_job') || ''; } catch (e) { return ''; }
  }

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
    progressKey: '',
    pollFailures: 0,
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
      out.appendChild(el('p', '', '没能写出总结。请到页面设置检查模型，或换一个论文链接。'));
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

  function visibleMessage(text) {
    var t = String(text || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 180) return '';
    if (/https?:\/\/|Traceback|\.py\b|Exception|timeout/i.test(t)) return '';
    if (/[A-Za-z]{3,}/.test(t) && !/\b(PDF|arXiv)\b/.test(t)) return '';
    return t;
  }

  function renderError(msg) {
    var out = document.querySelector('#paper-summarize-result');
    if (!out) return;
    out.textContent = '';
    out.classList.add('is-error');
    out.appendChild(el('p', '', '❌ ' + (visibleMessage(msg) || '这次总结没有完成，请稍后重试。')));
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
    state.progressKey = '';
    setStatus('');
    renderProgress([], 'queued');
    // 异步 job：POST 建job拿 job_id，然后轮询 GET /api/paper/summarize/<id> 拿进度事件
    return fetch(summarizeEndpoint(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          data = data || {};
          data._status = resp.status;
          return data;
        });
      })
      .then(function (data) {
        if (data && data._status === 409 && data.job_id) {
          state.jobId = data.job_id;
          state.pollFailures = 0;
          rememberSummarizeJob(data.job_id);
          setStatus('已经有一篇总结在写，接着看这一次。');
          pollJob(data.job_id);
          return;
        }
        if (!data || !data.ok || !data.job_id) {
          state.busy = false;
          var msg = visibleMessage(data && (data.error || data.detail || data.message));
          renderError(msg || '没有开始，请稍后重试。');
          setStatus('');
          return;
        }
        state.jobId = data.job_id;
        state.seenEventIds = {};
        state.pollFailures = 0;
        rememberSummarizeJob(data.job_id);
        setStatus('已开始，正在写总结。');
        pollJob(data.job_id);
      })
      .catch(function () {
        state.busy = false;
        renderError('总结没有提交成功，请稍后重试。');
        setStatus('');
      });
  }

  // ---- 轮询 job 状态 + 事件 ----
  var POLL_INTERVAL = 2000;

  function pollJob(jobId) {
    if (state.jobId !== jobId) return; // 已被新请求取代
    fetch(summarizeEndpoint() + '/' + encodeURIComponent(jobId), { cache: 'no-store' })
      .then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          return { ok: resp.ok, status: resp.status, data: data };
        });
      })
      .then(function (res) {
        if (state.jobId !== jobId) return;
        var data = res.data || {};
        if (!res.ok || !data.ok || !data.job) {
          if (res.status === 404) {
            forgetSummarizeJob();
            state.busy = false;
            renderError('没有找到这次总结，请重新提交。');
            setStatus('');
            return;
          }
          throw new Error('retry');
        }
        state.pollFailures = 0;
        var job = data.job;
        var status = String(job.status || 'unknown').toLowerCase();
        var events = job.events || [];
        renderProgress(events, status, state.seenEventIds);
        if (status === 'completed') {
          forgetSummarizeJob();
          state.busy = false;
          handleJobResult(job);
        } else if (status === 'failed') {
          forgetSummarizeJob();
          state.busy = false;
          renderError(job.error || '总结失败');
          setStatus('');
        } else if (status === 'cancelled') {
          forgetSummarizeJob();
          state.busy = false;
          setStatus('这次已停下。');
        } else {
          setTimeout(function () { pollJob(jobId); }, POLL_INTERVAL);
        }
      })
      .catch(function () {
        if (state.jobId !== jobId) return;
        state.pollFailures += 1;
        if (state.pollFailures < 5) {
          setStatus('进度暂时没刷新，正在重试。');
          setTimeout(function () { pollJob(jobId); }, POLL_INTERVAL);
          return;
        }
        state.busy = false;
        renderError('进度更新中断，请稍后刷新页面。这次总结还在继续。');
        setStatus('');
      });
  }

  var SUMMARIZE_STAGE_LABELS = {
    fetch_arxiv: '读取论文',
    fetch_pdf: '下载论文',
    fetch_web: '读取网页',
    extract_meta: '整理作者和来源',
    parse_pdf: '读取原文',
    cache_hit: '已有总结',
    daily_pipeline: '写总结',
    persist: '保存到列表',
    glance: '速览',
    figures: '看图',
    figure_interpretation: '看图表',
    translate: '翻译',
    deep_summary: '精读',
    deep: '精读',
    error: '出错',
  };

  function presentSummarizeEvents(events) {
    return (events || []).map(function (ev) {
      if (!ev) return ev;
      var copy = {};
      Object.keys(ev).forEach(function (key) { copy[key] = ev[key]; });
      if (copy.stage && SUMMARIZE_STAGE_LABELS[copy.stage]) copy.stage = SUMMARIZE_STAGE_LABELS[copy.stage];
      return copy;
    });
  }

  // 把新事件追加到进度区，已见过的按 event_id 去重
  function renderProgress(events, status, seenIds) {
    var box = document.querySelector('#paper-summarize-progress');
    if (!box) return;
    var viewKey = String(status || '') + '\n' + (events || []).map(function (ev) {
      return (ev && ev.event_id) || '';
    }).join(',');
    if (viewKey === state.progressKey && box.querySelector('.paper-summarize-progress-head')) return;
    state.progressKey = viewKey;
    var shown = presentSummarizeEvents(events);
    if (window.DPRTaskProgress && typeof window.DPRTaskProgress.render === 'function') {
      (events || []).forEach(function (ev) {
        if (ev && ev.event_id) seenIds[ev.event_id] = true;
      });
      window.DPRTaskProgress.render(box, {
        events: shown,
        status: status,
        title: '⏳ 正在生成总结，请稍候…',
        doneTitle: '✅ 总结完成',
        failedTitle: '❌ 总结失败',
      });
      return;
    }
    var fresh = (shown || []).filter(function (ev) {
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
      var msg = visibleMessage(ev.message);
      if (!msg) return;
      var suffix = '';
      if (ev.current != null && ev.total != null) {
        suffix = '（' + ev.current + '/' + ev.total + '）';
      }
      var li = el('li', 'paper-summarize-progress-item');
      var dot = el('span', 'paper-summarize-progress-dot');
      li.appendChild(dot);
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

  function looksLikeArxiv(text) {
    var value = String(text || '').trim();
    if (!value || value.length > 2000) return false;
    if (/arxiv\.org\/(?:abs|pdf)\//i.test(value)) return true;
    if (/(?:^|\s)arXiv:\s*\S+/i.test(value)) return true;
    return /^\d{4}\.\d{4,5}(v\d+)?$/.test(value);
  }

  function paperLinkProblem(url) {
    if (!url) return '请先填写论文链接。';
    if (url.length > 2000) return '这个链接太长了，请换一篇论文的链接。';
    if (looksLikeArxiv(url)) return '';
    var blocked = '这个链接不能打开。请换一篇论文的网页链接。';
    if (!/^https?:\/\//i.test(url)) return blocked;
    var host = '';
    try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return blocked; }
    if (!host || host === 'localhost' || host === '::1' ||
        host.endsWith('.localhost') || host.endsWith('.local') || host.endsWith('.internal') ||
        /^(127\.|10\.|192\.168\.|169\.254\.|0\.|172\.(1[6-9]|2\d|3[0-1])\.)/.test(host)) {
      return blocked;
    }
    return '';
  }

  function handleUrl() {
    var inp = document.querySelector('#paper-summarize-url');
    var url = (inp && inp.value || '').trim();
    var problem = paperLinkProblem(url);
    if (problem) {
      renderError(problem);
      return;
    }
    return doSummarize({ source: 'url', url: url });
  }

  var PDF_MAX_BYTES = 50 * 1024 * 1024;

  function pdfProblem(file) {
    if (!file) return '请先选择或拖入一个 PDF 文件';
    if (!/\.pdf$/i.test(file.name || '')) return '请选择 PDF 文件';
    if (file.size > PDF_MAX_BYTES) return 'PDF 不能超过 50MB，请换一个小一点的文件。';
    return '';
  }

  function handlePdf() {
    var problem = pdfProblem(state.file);
    if (problem) {
      renderError(problem);
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
        var picked = fileInput.files && fileInput.files[0] || null;
        var problem = pdfProblem(picked);
        if (picked && problem) {
          state.file = null;
          fileInput.value = '';
          setFileLabel('');
          renderError(problem);
          return;
        }
        state.file = picked;
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
        if (!f) return;
        var problem = pdfProblem(f);
        if (!problem) {
          state.file = f;
          if (fileInput) fileInput.value = '';
          setFileLabel(f.name);
        } else {
          state.file = null;
          setFileLabel('');
          renderError(problem);
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

    var title = el('h2', 'paper-summarize-heading', '论文总结');
    var subtitle = el('p', 'paper-summarize-sub', '贴一篇论文链接，或上传 PDF，写成中文总结。');
    root.appendChild(title);
    root.appendChild(subtitle);

    // 公网站点会通过 Report Hub 的受限 API 中继访问用户电脑上的本地后端。
    // 它不是 localhost，但同样具备 PDF 上传能力，不能按“静态站点”隐藏入口。
    var local = isProbablyLocal() || Boolean(String(window.DPR_LOCAL_API_BASE || '').trim());

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
      drop.appendChild(el('div', 'paper-summarize-drop-hint', 'PDF 将经当前安装的安全中继交给本机后端解析，上限 50MB'));
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
      var hint = el('p', 'paper-summarize-hint', '当前页面不能上传 PDF。可以改用上方的论文链接。');
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
    pause.textContent = '正在写总结，可能要几十秒。';
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
    var pending = readSummarizeJob();
    if (pending) {
      state.jobId = pending;
      state.seenEventIds = {};
      setBusy(true);
      setStatus('正在继续上次的总结。');
      pollJob(pending);
    } else if (!isProbablyLocal()) {
      backendAvailable().then(function (ok) {
        if (!ok) {
          var s = document.querySelector('#paper-summarize-status');
          if (s) {
            s.textContent = '总结服务暂时连不上，请稍后刷新。';
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
