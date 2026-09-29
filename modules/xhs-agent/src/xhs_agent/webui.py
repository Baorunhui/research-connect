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
  const rel = idx >= 0 ? String(absPath).slice(idx + marker.length) : String(absPath);
  return 'v1/xhs/packages/' + encodeURIComponent(packageId) + '/files/' + rel.split('/').map(encodeURIComponent).join('/');
}

function render(resp) {
  const box = $('result');
  if (!resp || resp.status !== 'completed' || !resp.data) {
    box.innerHTML = '<div class="error">生成失败：' + esc(resp && resp.error || '未知错误') + '</div>';
    return;
  }
  const d = resp.data, p = d.xhs_payload;
  const cards = (d.artifacts.cards || []).map(c => '<a href="' + fileUrl(d.package_id, c) + '" target="_blank"><img src="' + fileUrl(d.package_id, c) + '"></a>').join('');
  const human = (d.quality.needs_human_check || []).map(s => '<li>' + esc(s) + '</li>').join('');
  box.innerHTML =
    '<h2 style="margin-top:0">' + esc(p.title) + '</h2>' +
    '<div class="tags">' + (p.tags || []).map(t => '<span>#' + esc(t) + '</span>').join('') + '</div>' +
    '<pre>' + esc(p.content) + '</pre>' +
    '<div class="muted">事实是否稳妥：' + esc(riskText(d.quality.fact_risk)) + ' · 语气是否合适：' + esc(riskText(d.quality.style_risk)) +
    ' · <a href="' + fileUrl(d.package_id, d.artifacts.note_md) + '" target="_blank">正文文件</a></div>' +
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
      const r = await fetch('v1/xhs/packages/' + encodeURIComponent(a.dataset.id));
      render(await r.json());
    }));
  } catch (e) { /* history is optional */ }
}

$('xhs-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const materials = lines('materials').map((text, i) => ({ id: 'M' + (i + 1), type: 'fact', text }));
  const links = lines('links').map(url => ({ type: 'source', url }));
  const body = {
    schema_version: 'xhs_agent.request.v1',
    request_id: 'web-' + Date.now().toString(36),
    intent: $('intent').value,
    mode: 'generate_package',
    source: { kind: $('intent').value === 'paper_promo' || $('intent').value === 'daily_paper' ? 'paper' : 'project',
              title: $('title').value.trim(), summary: $('summary').value.trim(), materials, links, entities: {} },
    requirements: { card_count: Math.max(1, Math.min(8, parseInt($('cards').value || '5', 10))) },
  };
  if ($('audience').value.trim()) body.audience = { who: $('audience').value.trim() };
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
    $('status').className = 'status error';
    $('status').textContent = e.message || '请求失败';
  } finally {
    $('submit').disabled = false;
  }
});

async function waitForJob(jobId) {
  for (;;) {
    const res = await fetch('v1/xhs/jobs/' + encodeURIComponent(jobId));
    if (res.status === 404) throw new Error('上次生成已中断，请重新提交。');
    const job = await res.json();
    if (!res.ok) throw new Error('进度查询失败');
    if (job.message) $('status').textContent = job.message;
    if (job.status === 'running') {
      await new Promise((resolve) => setTimeout(resolve, 1200));
      continue;
    }
    if (!job.response) throw new Error(job.message || '生成失败');
    if (job.response.status === 'failed') throw new Error(job.response.error || '生成失败');
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
    sessionStorage.removeItem('xhs_job');
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
