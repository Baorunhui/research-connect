"""Single-page browser UI for the XHS package API.

All URLs are relative so the page also works behind a path-prefix proxy.
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小红书内容包 · Research Connect</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: #f6f7fb; color: #1d2433; }
  main { max-width: 1100px; margin: 0 auto; padding: 28px 18px 48px; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  .muted { color: #5b6475; font-size: 14px; }
  .layout { display: grid; grid-template-columns: minmax(300px, 420px) 1fr; gap: 20px; margin-top: 20px; }
  @media (max-width: 860px) { .layout { grid-template-columns: 1fr; } }
  .panel { background: #fff; border: 1px solid #e3e7ef; border-radius: 12px; padding: 18px; }
  label { display: block; font-size: 13px; font-weight: 600; margin: 12px 0 4px; }
  input, select, textarea { width: 100%; padding: 8px 10px; border: 1px solid #cfd5e1; border-radius: 8px; font: inherit; font-size: 14px; }
  textarea { min-height: 90px; resize: vertical; }
  button { margin-top: 16px; width: 100%; padding: 10px; border: 0; border-radius: 8px; background: #e2324b; color: #fff; font-size: 15px; cursor: pointer; }
  button:disabled { background: #d6a0a8; cursor: wait; }
  .status { margin-top: 10px; font-size: 13px; color: #5b6475; white-space: pre-wrap; }
  .error { color: #c62828; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; margin-top: 12px; }
  .cards img { width: 100%; border-radius: 8px; border: 1px solid #e3e7ef; }
  pre { white-space: pre-wrap; background: #fafbfd; border: 1px solid #eef0f5; border-radius: 8px; padding: 12px; font-size: 14px; line-height: 1.6; }
  .tags span { display: inline-block; margin: 2px 6px 2px 0; color: #2f5bd3; font-size: 13px; }
  .history a { display: block; font-size: 13px; color: #2f5bd3; margin: 4px 0; cursor: pointer; }
</style>
</head>
<body>
<main>
  <h1>小红书内容包生成</h1>
  <div class="muted">填写论文或项目材料，生成标题、正文、标签和 1080×1440 卡片图（不会自动发布）。生成通常需要 1–3 分钟。</div>
  <div class="layout">
    <form class="panel" id="xhs-form">
      <label for="intent">内容类型</label>
      <select id="intent">
        <option value="paper_promo">论文推广</option>
        <option value="daily_paper">论文日报</option>
        <option value="project_promo">项目推广</option>
        <option value="lab_recruit">实验室招生</option>
      </select>
      <label for="title">标题 *</label>
      <input id="title" required placeholder="论文或项目标题">
      <label for="summary">一句话摘要 *</label>
      <textarea id="summary" required placeholder="核心问题、方法和结论"></textarea>
      <label for="materials">要点材料（每行一条）</label>
      <textarea id="materials" placeholder="例如：提出了……&#10;在……数据集上提升……"></textarea>
      <label for="links">链接（每行一个）</label>
      <textarea id="links" style="min-height:60px" placeholder="https://arxiv.org/abs/..."></textarea>
      <label for="audience">目标读者</label>
      <input id="audience" placeholder="AI研究生/青椒/博士生">
      <label for="cards">卡片页数</label>
      <input id="cards" type="number" min="1" max="8" value="5">
      <button type="submit" id="submit">生成内容包</button>
      <div class="status" id="status"></div>
    </form>
    <section class="panel">
      <div id="result"><div class="muted">生成结果会显示在这里。</div></div>
      <div class="history" id="history"></div>
    </section>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const lines = (id) => $(id).value.split('\\n').map(s => s.trim()).filter(Boolean);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function fileUrl(packageId, absPath) {
  const marker = '/' + packageId + '/';
  const idx = String(absPath).lastIndexOf(marker);
  if (idx < 0) return '';
  const rel = String(absPath).slice(idx + marker.length);
  if (!rel || rel.split('/').some(part => !part || part === '.' || part === '..')) return '';
  return 'v1/xhs/packages/' + encodeURIComponent(packageId) + '/files/' + rel.split('/').map(encodeURIComponent).join('/');
}

function readableError(resp) {
  const detail = resp && typeof resp.error === 'string' ? resp.error : '';
  if (detail && detail.length <= 180 && !/https?:\\/\\/|Traceback|\\.py\\b/.test(detail)) return detail;
  return '这份结果打不开，请重新生成。';
}

function render(resp) {
  const box = $('result');
  if (!resp || resp.status !== 'completed' || !resp.data) {
    box.innerHTML = '<div class="error">' + esc(readableError(resp)) + '</div>';
    return;
  }
  const d = resp.data || {};
  const p = d.xhs_payload || {};
  const artifacts = d.artifacts || {};
  const quality = d.quality || {};
  if (!p.title && !p.content) {
    box.innerHTML = '<div class="error">这份结果不完整，请重新生成。</div>';
    return;
  }
  const cards = (artifacts.cards || []).map(c => {
    const href = fileUrl(d.package_id, c);
    return href ? '<a href="' + href + '" target="_blank"><img src="' + href + '"></a>' : '';
  }).join('');
  const human = (quality.needs_human_check || []).map(s => '<li>' + esc(s) + '</li>').join('');
  const noteHref = artifacts.note_md ? fileUrl(d.package_id, artifacts.note_md) : '';
  const note = noteHref ? ' · <a href="' + noteHref + '" target="_blank">正文文件</a>' : '';
  box.innerHTML =
    '<h2 style="margin-top:0">' + esc(p.title) + '</h2>' +
    '<div class="tags">' + (p.tags || []).map(t => '<span>#' + esc(t) + '</span>').join('') + '</div>' +
    '<pre>' + esc(p.content) + '</pre>' +
    '<div class="muted">事实是否稳妥：' + esc(riskText(quality.fact_risk)) + ' · 语气是否合适：' + esc(riskText(quality.style_risk)) +
    note + '</div>' +
    (human ? '<div class="muted" style="margin-top:8px">发布前请人工核对：<ul>' + human + '</ul></div>' : '') +
    '<div class="cards">' + cards + '</div>';
}

function riskText(value) {
  if (value === 'low') return '低';
  if (value === 'medium') return '中';
  if (value === 'high') return '高';
  return value || '';
}

async function loadHistory() {
  try {
    const res = await fetch('v1/xhs/packages');
    const data = await res.json();
    const items = (data.packages || []).slice(0, 20);
    if (!items.length) return;
    $('history').innerHTML = '<div class="muted" style="margin-top:14px">以前生成的：</div>' +
      items.map(it => '<a data-id="' + esc(it.package_id) + '">' + esc(it.title || it.package_id) + '</a>').join('');
    $('history').querySelectorAll('a').forEach(a => a.addEventListener('click', async () => {
      try {
        const r = await fetch('v1/xhs/packages/' + encodeURIComponent(a.dataset.id));
        const data = await r.json();
        if (!r.ok) {
          $('result').innerHTML = '<div class="error">这份结果打不开，请重新生成。</div>';
          return;
        }
        render(data);
      } catch (e) {
        $('result').innerHTML = '<div class="error">这份结果打不开，请重新生成。</div>';
      }
    }));
  } catch (e) { /* history is optional */ }
}

function shownError(err) {
  var text = String(err && err.message || '').trim();
  if (!text || /failed to fetch|networkerror|load failed|traceback/i.test(text)) {
    return '没有连上服务器，请稍后重试。';
  }
  if (text.length > 180) return '生成没有完成，请稍后重试。';
  return text;
}

function formProblem(title, summary, materialLines, linkLines, audience) {
  if (!title || !summary) return '请先填写标题和一句话摘要。';
  if (title.length > 200) return '标题太长了，请缩短到 200 字以内。';
  if (summary.length > 4000) return '摘要太长了，请缩短到 4000 字以内。';
  if (materialLines.length > 30) return '要点太多了，请留在 30 条以内。';
  var materialLength = 0;
  materialLines.forEach(function (line) { materialLength += line.length; });
  if (materialLength > 12000) return '要点太长了，请缩短后再生成。';
  if (linkLines.length > 20) return '链接太多了，请留在 20 条以内。';
  var linkLength = 0;
  for (var i = 0; i < linkLines.length; i++) {
    if (linkLines[i].length > 2000) return '有一条链接太长了，请缩短后再生成。';
    linkLength += linkLines[i].length;
  }
  if (linkLength > 8000) return '链接太长了，请缩短后再生成。';
  if (audience.length > 400) return '读者说明太长了，请缩短后再生成。';
  return '';
}

$('xhs-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const title = $('title').value.trim();
  const summary = $('summary').value.trim();
  const materialLines = lines('materials');
  const linkLines = lines('links');
  const audience = $('audience').value.trim();
  const problem = formProblem(title, summary, materialLines, linkLines, audience);
  if (problem) {
    $('status').className = 'status error';
    $('status').textContent = problem;
    return;
  }
  const materials = materialLines.map((text, i) => ({ id: 'M' + (i + 1), type: 'fact', text }));
  const links = linkLines.map(url => ({ type: 'source', url }));
  const body = {
    schema_version: 'xhs_agent.request.v1',
    request_id: 'web-' + Date.now().toString(36),
    intent: $('intent').value,
    mode: 'generate_package',
    source: { kind: $('intent').value === 'paper_promo' || $('intent').value === 'daily_paper' ? 'paper' : 'project',
              title: title, summary: summary, materials, links, entities: {} },
    requirements: { card_count: (function () {
      var n = parseInt($('cards').value || '5', 10);
      return Number.isFinite(n) ? Math.max(1, Math.min(8, n)) : 5;
    })() },
  };
  if (audience) body.audience = { who: audience };
  $('submit').disabled = true;
  $('status').className = 'status';
  $('status').textContent = '正在整理论文要点';
  try {
    const res = await fetch('v1/xhs/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const started = await res.json();
    if (!res.ok) throw new Error(typeof started.detail === 'string' ? started.detail : '没有开始生成');
    sessionStorage.setItem('xhs_job', started.job_id);
    const data = await waitForJob(started.job_id);
    sessionStorage.removeItem('xhs_job');
    render(data);
    $('status').textContent = data.status === 'completed' ? '已完成。' : (data.error || '生成结束，请看上面的提示。');
    loadHistory();
  } catch (e) {
    if (e && e.forget) sessionStorage.removeItem('xhs_job');
    $('status').className = 'status error';
    $('status').textContent = shownError(e);
  } finally {
    $('submit').disabled = false;
  }
});

function stopError(message) {
  const err = new Error(message);
  err.forget = true;
  return err;
}

async function waitForJob(jobId) {
  let failures = 0;
  for (;;) {
    let res;
    try {
      res = await fetch('v1/xhs/jobs/' + encodeURIComponent(jobId));
    } catch (e) {
      failures += 1;
      if (failures >= 5) throw new Error('进度暂时没刷新。这次生成还在继续，请稍后刷新页面。');
      $('status').textContent = '进度暂时没刷新，正在重试。';
      await new Promise((resolve) => setTimeout(resolve, 1200));
      continue;
    }
    if (res.status === 404) throw stopError('上次生成已中断，请重新提交。');
    let job = null;
    try { job = await res.json(); } catch (e) { job = null; }
    if (!res.ok || !job) {
      failures += 1;
      if (failures >= 5) throw new Error('进度暂时没刷新。这次生成还在继续，请稍后刷新页面。');
      $('status').textContent = '进度暂时没刷新，正在重试。';
      await new Promise((resolve) => setTimeout(resolve, 1200));
      continue;
    }
    failures = 0;
    if (job.message) $('status').textContent = job.message;
    if (job.status === 'running') {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      continue;
    }
    if (!job.response) throw stopError(job.message || '生成失败');
    if (job.response.status === 'failed') throw stopError(job.response.error || '生成失败');
    return job.response;
  }
}

const pendingJob = sessionStorage.getItem('xhs_job');
if (pendingJob) {
  $('submit').disabled = true;
  $('status').textContent = '正在继续上次的生成';
  waitForJob(pendingJob).then((data) => {
    sessionStorage.removeItem('xhs_job');
    render(data);
    $('status').textContent = '已完成。';
    loadHistory();
  }).catch((e) => {
    if (e && e.forget) sessionStorage.removeItem('xhs_job');
    $('status').className = 'status error';
    $('status').textContent = e.message || '生成失败';
  }).finally(() => {
    $('submit').disabled = false;
  });
}
loadHistory();
</script>
</body>
</html>
"""
