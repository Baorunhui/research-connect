// 工作流触发面板：用于从前端触发 GitHub Actions workflow，并展示运行进度
// 依赖：GitHub Token（Classic PAT），需要 repo + workflow 权限

window.DPRWorkflowRunner = (function () {
  const WORKFLOWS = [
    {
      key: 'daily-now',
      id: 'daily-paper-reader.yml',
      name: '立即爬取并处理论文',
      desc: '触发 daily-paper-reader 工作流（抓取→召回→重排→生成 docs）。',
      dispatchInputs: {
        run_enrich: 'false',
      },
    },
    {
      key: 'sync',
      id: 'sync.yml',
      name: '同步上游代码',
      desc: '触发 Upstream Sync 工作流（合并上游 main 到当前仓库）。',
    },
    {
      key: 'reset-content',
      id: 'reset-content.yml',
      name: '重置 content（docs + archive）',
      desc: '将 docs 恢复为 docs_init 基线，并清空 archive。该操作为危险操作。',
    },
    {
      key: 'conference-retrieval',
      id: 'conference-paper-retrieval.yml',
      name: '会议论文检索',
      desc: '按会议和年份触发 Supabase BM25/Embedding 候选召回与 RRF 融合。',
      dispatchInputs: {
        top_k: '50',
        rrf_top_n: '200',
        run_rerank: 'true',
        reranker_profile: 'public-zwwen-rerank',
        run_llm_refine: 'true',
      },
    },
  ];

  const QUICK_FETCH_PRESETS = {
    '10': {
      key: 'daily-now',
      dispatchInputs: {
        run_enrich: 'false',
        fetch_days: '10',
      },
    },
    '30': {
      key: 'daily-now',
      dispatchInputs: {
        run_enrich: 'false',
        fetch_days: '30',
        fetch_mode: 'skims',
      },
    },
    '30-skims': {
      key: 'daily-now',
      dispatchInputs: {
        run_enrich: 'false',
        fetch_days: '30',
        fetch_mode: 'skims',
      },
    },
    '30-standard': {
      key: 'daily-now',
      dispatchInputs: {
        run_enrich: 'false',
        fetch_days: '30',
        fetch_mode: 'standard',
      },
    },
  };

  let overlay = null;
  let panel = null;
  let statusEl = null;
  let runsEl = null;
  let recentEl = null;
  let refreshTimer = null;
  let activeRun = null;
  let selectedRun = null;
  const lastRunStateById = {};
  let repoContextCache = null;

  const escapeHtml = (str) => {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  const loadGithubToken = () => {
    try {
      const raw = window.localStorage
        ? window.localStorage.getItem('github_token_data')
        : '';
      if (!raw) return '';
      const obj = JSON.parse(raw);
      return String((obj && obj.token) || '').trim();
    } catch {
      return '';
    }
  };
  const loadRerankerProfile = () => {
    if (isLocalDebugPage()) return 'public-zwwen-rerank';
    return '';
  };

  const resolveRepoFromUrl = async (token) => {
    const currentUrl = window.location.href || '';
    const githubPagesMatch = currentUrl.match(
      /https?:\/\/([^.]+)\.github\.io\/([^\/]+)/,
    );
    if (githubPagesMatch) {
      return { owner: githubPagesMatch[1], repo: githubPagesMatch[2] };
    }

    // 非 GitHub Pages URL：回退到「Token 对应的用户 + daily-paper-reader」作为默认目标仓库
    try {
      const userRes = await ghFetch(token, 'https://api.github.com/user');
      if (userRes.ok) {
        const user = await userRes.json();
        const login = (user && user.login) ? String(user.login) : '';
        if (login) {
          return { owner: login, repo: 'daily-paper-reader' };
        }
      }
    } catch {
      // ignore
    }

    return { owner: '', repo: '' };
  };

  const resolveRepoContext = async (token, options = {}) => {
    const { forceRefresh = false } = options || {};
    const { owner, repo } = await resolveRepoFromUrl(token);
    if (!owner || !repo) {
      return { owner: '', repo: '', isFork: null, defaultBranch: 'main' };
    }

    const cacheKey = `${owner}/${repo}`;
    if (!forceRefresh && repoContextCache && repoContextCache.key === cacheKey && repoContextCache.value) {
      return repoContextCache.value;
    }
    if (!forceRefresh && repoContextCache && repoContextCache.key === cacheKey && repoContextCache.promise) {
      return repoContextCache.promise;
    }

    const fetchPromise = (async () => {
      try {
        const repoUrl = `https://api.github.com/repos/${owner}/${repo}`;
        const res = await ghFetch(token, repoUrl);
        if (!res.ok) {
          return { owner, repo, isFork: null, defaultBranch: 'main' };
        }
        const data = await res.json().catch(() => null);
        return {
          owner,
          repo,
          isFork: !!(data && data.fork),
          defaultBranch: String((data && data.default_branch) || 'main'),
        };
      } catch {
        return { owner, repo, isFork: null, defaultBranch: 'main' };
      }
    })();

    repoContextCache = { key: cacheKey, promise: fetchPromise, value: null };
    const value = await fetchPromise;
    repoContextCache = { key: cacheKey, promise: null, value };
    return value;
  };

  const ghFetch = async (token, url, init) => {
    const res = await fetch(url, {
      ...(init || {}),
      headers: {
        Authorization: `token ${token}`,
        Accept: 'application/vnd.github.v3+json',
        ...(init && init.headers ? init.headers : {}),
      },
    });
    return res;
  };

  const isLocalDebugPage = () => {
    if (window.DPR_LOCAL_API_BASE) return true;
    const host = String((window.location && window.location.hostname) || '').toLowerCase();
    if (host === 'localhost' || host === '127.0.0.1' || host === '0.0.0.0') return true;
    if (/^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
    if (/^192\.168\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
    if (/^172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}$/.test(host)) return true;
    return false;
  };

  const isPublicReadOnlyPage = () => window.DPR_PUBLIC_READ_ONLY === true;

  const getLocalApiUrl = (path) => {
    const base = String(window.DPR_LOCAL_API_BASE || '').trim().replace(/\/$/, '');
    if (!base && isLocalDebugPage()) {
      const protocol = String((window.location && window.location.protocol) || 'http:');
      const hostname = String((window.location && window.location.hostname) || '127.0.0.1');
      return `${protocol}//${hostname}:8567${path}`;
    }
    if (!base) return path;
    return `${base}${path}`;
  };

  const localApiFetch = async (path, init) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const res = await fetch(getLocalApiUrl(path), {
        ...(init || {}),
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          ...(init && init.headers ? init.headers : {}),
        },
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || `本地调试后端请求失败：HTTP ${res.status}`);
      }
      return data;
    } catch (e) {
      if (e && e.name === 'AbortError') {
        throw new Error('本地调试后端请求超时，请确认 8567 端口服务正在运行。');
      }
      throw e;
    } finally {
      clearTimeout(timeout);
    }
  };

  const isWorkflowLogNearBottom = (logEl) => {
    if (!logEl) return true;
    const distance =
      Number(logEl.scrollHeight || 0) -
      Number(logEl.scrollTop || 0) -
      Number(logEl.clientHeight || 0);
    return distance <= 24;
  };

  const scrollWorkflowLogToBottom = (shouldScroll) => {
    if (!runsEl || !shouldScroll) return;
    requestAnimationFrame(() => {
      const logEl = runsEl.querySelector('[data-dpr-workflow-log]');
      if (logEl) {
        logEl.scrollTop = logEl.scrollHeight;
      }
    });
  };

  // 日报流水线步骤定义（与后端 src/local_server.py 的 PIPELINE_STEPS 保持一致）。
  const PIPELINE_STEPS = [
    { key: 'step_0_enrich', num: '0', label: 'LLM 扩充检索关键词' },
    { key: 'step_1_fetch', num: '1', label: '抓取 arXiv 论文' },
    { key: 'step_2_1_bm25', num: '2.1', label: 'BM25 关键词召回' },
    { key: 'step_2_2_embedding', num: '2.2', label: '向量语义召回' },
    { key: 'step_2_3_rrf', num: '2.3', label: 'RRF 融合候选池' },
    { key: 'step_3_rerank', num: '3', label: 'Reranker 重排' },
    { key: 'step_4_llm_refine', num: '4', label: 'LLM 精炼打分' },
    { key: 'step_5_select', num: '5', label: '选择论文（精读/速读）' },
    { key: 'step_6_generate', num: '6', label: '生成日报文档' },
  ];

  // 从 run.events 里提取各步骤最新状态（started/completed/skipped/failed）。
  const buildStepStateMap = (run) => {
    const map = {};
    (run && Array.isArray(run.events) ? run.events : []).forEach((ev) => {
      if (!ev || ev.event_type !== 'run.progress') return;
      const p = ev.payload || {};
      if (p.step && p.state) map[p.step] = p.state;
    });
    return map;
  };

  const renderStepPanel = (run) => {
    // 仅日报流水线显示步骤清单：已有进度事件，或正在运行的 daily-now 任务。
    // 其它 workflow（同步/重置/会议）没有步骤锚点，不渲染以免误导。
    const hasProgress = (run && Array.isArray(run.events) ? run.events : []).some(
      (ev) => ev && ev.event_type === 'run.progress',
    );
    const isDailyRunning = run && run.workflow_key === 'daily-now' && run.status !== 'completed';
    if (!hasProgress && !isDailyRunning) return '';
    const stateMap = buildStepStateMap(run);
    const finished = run.status === 'completed';
    const isSuccess = run.conclusion === 'success';
    const rows = PIPELINE_STEPS.map((s) => {
      let st = stateMap[s.key] || '';
      // 运行结束后仍未开始过的步骤视为「未执行」；仍在 running 的按结果收尾。
      if (finished && !st) st = 'skipped';
      if (finished && st === 'started') st = isSuccess ? 'completed' : 'failed';
      const icon = st === 'completed' ? '✓' : st === 'started' ? '▶' : st === 'failed' ? '✗' : st === 'skipped' ? '–' : '·';
      const color = st === 'completed' ? '#2e7d32' : st === 'started' ? '#1565c0' : st === 'failed' ? '#c00' : st === 'skipped' ? '#bbb' : '#999';
      const rowBg = st === 'started' ? 'rgba(21,101,192,0.08)' : 'transparent';
      const text = st === 'skipped' ? '#bbb' : '#333';
      return (
        '<div style="display:flex;align-items:center;gap:6px;padding:2px 6px;border-radius:4px;background:' + rowBg + ';">' +
        '<span style="width:14px;text-align:center;color:' + color + ';">' + icon + '</span>' +
        '<span style="color:' + text + ';">Step ' + s.num + ' ' + escapeHtml(s.label) + '</span>' +
        '</div>'
      );
    });
    return (
      '<div style="margin-bottom:8px;border:1px solid #e5e5e5;border-radius:6px;padding:6px 8px;font-size:12px;line-height:1.7;">' +
      rows.join('') +
      '</div>'
    );
  };

  // 原始日志默认折叠；跨重渲染保留用户展开状态。
  let rawLogExpanded = false;

  const renderLocalRun = (run, logText) => {
    if (!runsEl || !run) return;
    const previousDetails = runsEl.querySelector('[data-dpr-workflow-log-details]');
    if (previousDetails) {
      rawLogExpanded = previousDetails.open;
    }
    const previousLogEl = runsEl.querySelector('[data-dpr-workflow-log]');
    const shouldFollowLog = rawLogExpanded && isWorkflowLogNearBottom(previousLogEl);
    const status = run.status || '';
    const conclusion = run.conclusion || '';
    const badgeColor =
      conclusion === 'success'
        ? '#2e7d32'
        : conclusion === 'failure'
          ? '#c00'
          : status === 'in_progress'
            ? '#1565c0'
            : '#666';
    const command = Array.isArray(run.command) ? run.command.join(' ') : '';
    const logLines = logText ? logText.split('\n').length : 0;
    const logHtml = logText
      ? `<details data-dpr-workflow-log-details="1" ${rawLogExpanded ? 'open' : ''} style="margin-top:4px;">
           <summary style="cursor:pointer; font-size:12px; color:#666; user-select:none; outline:none;">
             原始日志（${logLines} 行，点击${rawLogExpanded ? '收起' : '展开'}）
           </summary>
           <pre data-dpr-workflow-log="1" style="white-space:pre-wrap; max-height:360px; overflow:auto; background:#111; color:#ddd; padding:10px; border-radius:6px; font-size:12px; margin-top:6px;">${escapeHtml(logText)}</pre>
         </details>`
      : '<div style="color:#999;">暂无日志。</div>';
    runsEl.innerHTML = `
      <div style="margin-bottom:8px;">
        <div style="font-weight:600;">本地运行 #${escapeHtml(run.run_number || run.id)}</div>
        <div style="color:#666; margin-top:2px;">
          <span style="display:inline-block; padding:1px 6px; border-radius:999px; background:rgba(0,0,0,0.06); color:${badgeColor};">
            ${escapeHtml(formatRunBadgeText(status, conclusion))}
          </span>
          <span style="margin-left:8px;">${escapeHtml(formatRunTime(run.created_at))}</span>
        </div>
      </div>
      <div style="font-size:12px; color:#666; margin-bottom:8px;">${escapeHtml(command)}</div>
      ${renderStepPanel(run)}
      ${logHtml}
    `;
    scrollWorkflowLogToBottom(shouldFollowLog);
  };

  const refreshLocalRun = async (runId) => {
    try {
      const data = await localApiFetch(`/api/local/runs/${encodeURIComponent(runId)}/log`);
      const run = data.run || {};
      renderLocalRun(run, data.log || '');
      if (run.status === 'completed') {
        stopPolling();
        setStatus(
          `本地运行已结束：${run.conclusion || 'completed'}`,
          run.conclusion === 'success' ? '#080' : '#c00',
        );
      } else {
        setStatus('本地运行中：每 5 秒自动刷新...', '#1565c0', { waiting: true });
      }
    } catch (e) {
      console.error(e);
      setStatus(`刷新本地运行失败：${e.message || e}`, '#c00');
    }
  };

  const dispatchLocalAndMonitor = async (wf, workflowFile, dispatchInputs) => {
    stopPolling();
    activeRun = null;
    rawLogExpanded = false;
    setStatus(`正在触发本地调试任务：${wf.name || workflowFile} ...`, '#666', { waiting: true });
    runsEl.innerHTML = '<div style="color:#999;">正在请求本地后端，请稍候...</div>';
    const data = await localApiFetch('/api/local/workflows/dispatch', {
      method: 'POST',
      body: JSON.stringify({
        workflowKey: wf.key || '',
        workflowFile,
        inputs: dispatchInputs || {},
      }),
    });
    const run = data.run || {};
    activeRun = { local: true, runId: run.id };
    selectedRun = activeRun;
    setStatus(`本地运行已创建：run_id=${run.id}`, '#080', { waiting: true });
    await refreshLocalRun(run.id);
    refreshTimer = setInterval(() => {
      const r = selectedRun || activeRun;
      if (!r || !r.local) return;
      refreshLocalRun(r.runId);
    }, 5000);
  };

  const resolveWorkflowRunInputs = async (owner, repo, token, runId) => {
    if (!owner || !repo || !runId || !token) return null;
    const runUrl = `https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}`;
    try {
      const res = await ghFetch(token, runUrl);
      if (!res.ok) return null;
      const data = await res.json().catch(() => null);
      if (!data || typeof data !== 'object') return null;
      if (data.inputs && typeof data.inputs === 'object') {
        return data.inputs;
      }
      return null;
    } catch {
      return null;
    }
  };

  const resolveRecentRunTag = async (owner, repo, token, run) => {
    if (!run) return 'daily-now';
    // 统一归类到 daily-now，触发面板不再单独展示一个月/一个月标准入口
    if (run.inputs && typeof run.inputs === 'object') return 'daily-now';
    await resolveWorkflowRunInputs(owner, repo, token, run.id);
    return 'daily-now';
  };

  const setStatus = (text, color, options = {}) => {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.style.color = color || '#666';
    statusEl.classList.toggle('is-waiting', !!(options && options.waiting));
  };

  const ensureOverlay = () => {
    if (overlay && panel) return;
    overlay = document.getElementById('dpr-workflow-overlay');
    if (overlay) {
      panel = document.getElementById('dpr-workflow-panel');
      statusEl = document.getElementById('dpr-workflow-status');
      runsEl = document.getElementById('dpr-workflow-runs');
      recentEl = document.getElementById('dpr-workflow-recent');
      return;
    }

    overlay = document.createElement('div');
    overlay.id = 'dpr-workflow-overlay';
    overlay.innerHTML = `
      <div id="dpr-workflow-panel">
        <div id="dpr-workflow-header">
          <div style="font-weight:600;">工作流触发</div>
          <div style="display:flex; gap:8px; align-items:center;">
            <button id="dpr-workflow-refresh-btn" class="arxiv-tool-btn" style="padding:2px 10px;">刷新</button>
            <button id="dpr-workflow-close-btn" class="arxiv-tool-btn" style="padding:2px 6px;">关闭</button>
          </div>
        </div>
        <div id="dpr-workflow-body">
          <div id="dpr-workflow-status" style="font-size:12px; color:#666; margin-bottom:10px;">准备就绪。</div>
          <div style="font-weight:600; font-size:13px; margin-bottom:6px;">最近运行（各取 3 条）</div>
          <div id="dpr-workflow-recent" style="font-size:12px; color:#333; border:1px solid #eee; border-radius:8px; background:#fff; padding:10px; margin-bottom:12px;">
            <div style="color:#999;">加载中...</div>
          </div>
          <div style="font-weight:600; font-size:13px; margin-bottom:6px;">执行过程</div>
          <div id="dpr-workflow-runs" style="font-size:12px; color:#333; border:1px solid #eee; border-radius:8px; background:#fff; padding:10px; min-height:120px;">
            <div style="color:#999;">尚未触发工作流。</div>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);

    panel = document.getElementById('dpr-workflow-panel');
    statusEl = document.getElementById('dpr-workflow-status');
    runsEl = document.getElementById('dpr-workflow-runs');
    recentEl = document.getElementById('dpr-workflow-recent');

    const closeBtn = document.getElementById('dpr-workflow-close-btn');
    if (closeBtn) {
      closeBtn.addEventListener('click', close);
    }
    overlay.addEventListener('mousedown', (e) => {
      if (e.target === overlay) close();
    });

    const refreshBtn = document.getElementById('dpr-workflow-refresh-btn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        const r = selectedRun || activeRun;
        if (r && r.local && r.runId) {
          refreshLocalRun(r.runId);
        } else if (r && r.owner && r.repo && r.runId) {
          refreshRun(r.owner, r.repo, r.runId);
        } else {
          setStatus('暂无可刷新的运行记录。', '#666');
        }
      });
    }

  };

  const open = () => {
    ensureOverlay();
    if (!overlay) return;
    overlay.style.display = 'flex';
    requestAnimationFrame(() => overlay.classList.add('show'));
    // 打开面板时尝试加载最近运行（不依赖触发）
    loadRecentRuns();
    return true;
  };

  const close = () => {
    if (!overlay) return;
    overlay.classList.remove('show');
    setTimeout(() => {
      overlay.style.display = 'none';
    }, 160);
    stopPolling();
  };

  const stopPolling = () => {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  };

  const badgeColorFor = (status, conclusion) => {
    if (conclusion === 'success') return '#2e7d32';
    if (conclusion === 'failure') return '#c00';
    if (conclusion === 'cancelled') return '#666';
    if (status === 'in_progress') return '#1565c0';
    return '#666';
  };

  const formatRunBadgeText = (status, conclusion) => {
    const s = String(status || '');
    const c = String(conclusion || '');
    // 用户希望 completed / success 这种冗余展示去掉：优先展示 conclusion，其次 status
    return c || s || '';
  };

  const formatRunTime = (isoTime) => {
    if (!isoTime) return '';
    try {
      const d = new Date(isoTime);
      if (Number.isNaN(d.getTime())) {
        return String(isoTime).replace('T', ' ').replace('Z', '');
      }
      return d.toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    } catch {
      return String(isoTime || '');
    }
  };

  const renderRecentRuns = (owner, repo, byWorkflow, errText, repoContext = null) => {
    if (!recentEl) return;
    recentEl.classList.remove('is-loading');
    if (errText) {
      recentEl.innerHTML = `<div style="color:#c00;">${escapeHtml(errText)}</div>`;
      return;
    }
    const blocks = WORKFLOWS.map((wf) => {
      if (wf.key === 'sync' && repoContext && repoContext.isFork === false) {
        return `
          <div class="dpr-wf-recent-block">
            <div class="dpr-wf-recent-block-title">${escapeHtml(wf.name)}</div>
            <div style="color:#c90;">当前仓库不是 GitHub Fork，已禁用上游同步。</div>
          </div>
        `;
      }
      const list = (byWorkflow && byWorkflow[String(wf.key || wf.id || '')]) || [];
      const items = Array.isArray(list) ? list : [];
      const lines = items
        .map((r) => {
          const status = r.status || '';
          const conclusion = r.conclusion || '';
          const color = badgeColorFor(status, conclusion);
          const isActive =
            selectedRun &&
            String(selectedRun.runId || '') === String(r.id || '');
          const createdAt = formatRunTime(r.created_at);
          const badge = formatRunBadgeText(status, conclusion);
          const title = `#${r.run_number || r.id}${badge ? ` ${badge}` : ''}`;
          return `
            <button class="dpr-wf-recent-item ${isActive ? 'is-active' : ''}" data-run-id="${escapeHtml(
              String(r.id || ''),
            )}" style="text-align:left;">
              <div class="dpr-wf-recent-title">
                <span class="dpr-wf-recent-badge" style="color:${color};">${escapeHtml(
                  title,
                )}</span>
                <span class="dpr-wf-recent-time">${escapeHtml(createdAt)}</span>
              </div>
              <div class="dpr-wf-recent-sub">${escapeHtml(wf.name)}</div>
            </button>
          `;
        })
        .join('');
      return `
        <div class="dpr-wf-recent-block">
          <div class="dpr-wf-recent-block-title">${escapeHtml(wf.name)}</div>
          ${lines || '<div style="color:#999;">暂无运行记录</div>'}
        </div>
      `;
    }).join('');

    recentEl.innerHTML = blocks;

    recentEl.querySelectorAll('.dpr-wf-recent-item').forEach((btn) => {
      if (btn._bound) return;
      btn._bound = true;
      btn.addEventListener('click', async () => {
        const runId = btn.getAttribute('data-run-id') || '';
        if (!runId) return;
        stopPolling();
        recentEl
          .querySelectorAll('.dpr-wf-recent-item.is-active')
          .forEach((n) => n.classList.remove('is-active'));
        btn.classList.add('is-active');
        selectedRun = { owner, repo, runId, token: loadGithubToken() };
        setStatus(`正在加载运行详情：run_id=${runId}`, '#666', { waiting: true });
        await refreshRun(owner, repo, runId);
        refreshTimer = setInterval(() => {
          if (!selectedRun) return;
          refreshRun(selectedRun.owner, selectedRun.repo, selectedRun.runId);
        }, 5000);
      });
    });
  };

  const loadRecentRuns = async () => {
    ensureOverlay();
    if (!recentEl) return;
    if (isLocalDebugPage()) {
      try {
        recentEl.classList.add('is-loading');
        const data = await localApiFetch('/api/local/runs');
        const runs = Array.isArray(data.runs) ? data.runs.slice(0, 12) : [];
        recentEl.classList.remove('is-loading');
        if (!runs.length) {
          recentEl.innerHTML = '<div style="color:#999;">暂无运行记录。</div>';
          return;
        }
        recentEl.innerHTML = runs.map((run) => {
          const status = formatRunBadgeText(run.status || '', run.conclusion || '');
          return `<button type="button" class="dpr-wf-recent-item" data-run-id="${escapeHtml(run.id || '')}" style="display:block;width:100%;margin:0 0 6px;padding:8px;text-align:left;border:1px solid #eee;border-radius:6px;background:#fff;cursor:pointer;">
            <strong>本地运行 #${escapeHtml(run.run_number || run.id || '')}</strong>
            <span style="margin-left:8px;color:#666;">${escapeHtml(status)}</span>
            <span style="float:right;color:#999;">${escapeHtml(formatRunTime(run.created_at))}</span>
          </button>`;
        }).join('');
        recentEl.querySelectorAll('.dpr-wf-recent-item').forEach((button) => {
          button.addEventListener('click', async () => {
            const runId = button.getAttribute('data-run-id') || '';
            if (!runId) return;
            stopPolling();
            selectedRun = { local: true, runId };
            await refreshLocalRun(runId);
            refreshTimer = setInterval(() => refreshLocalRun(runId), 5000);
          });
        });
      } catch (e) {
        recentEl.classList.remove('is-loading');
        recentEl.innerHTML = `<div style="color:#c00;">${escapeHtml(e.message || String(e))}</div>`;
      }
      return;
    }
    const token = loadGithubToken();
    if (!token) {
      recentEl.classList.remove('is-loading');
      recentEl.innerHTML =
        '<div style="color:#c00;">未检测到 GitHub Token，无法加载最近运行记录。</div>';
      return;
    }

    try {
      const repoContext = await resolveRepoContext(token);
      const { owner, repo } = repoContext;
      if (!owner || !repo) {
        renderRecentRuns(owner, repo, null, '无法推断目标仓库，无法加载最近运行记录。');
        return;
      }

      const hasRendered = !!recentEl.querySelector('.dpr-wf-recent-block');
      if (!hasRendered) {
        recentEl.innerHTML = '<div style="color:#999;">正在加载最近运行记录...</div>';
      } else {
        // 刷新时不要清空现有内容，避免“闪一下再出现”的观感
        recentEl.classList.add('is-loading');
      }
      const byWorkflow = {};
      const runsByWorkflowId = {};
      const uniqueWorkflowIds = Array.from(
        new Set(WORKFLOWS.map((wf) => String(wf.id || ''))),
      );

      for (const wfId of uniqueWorkflowIds) {
        const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(
          wfId,
        )}/runs?per_page=12`;
        const res = await ghFetch(token, url);
        if (!res.ok) {
          const txt = await res.text().catch(() => '');
          throw new Error(
            `读取最近运行失败(${wfId})：HTTP ${res.status} ${res.statusText} - ${txt}`,
          );
        }
        const data = await res.json();
        runsByWorkflowId[wfId] = Array.isArray(data.workflow_runs)
          ? data.workflow_runs
          : [];
      }

      const dailyFileRuns = runsByWorkflowId['daily-paper-reader.yml'] || [];
      const dailyNowRuns = [];
      if (dailyFileRuns.length > 0) {
        const tagged = await Promise.all(
          dailyFileRuns.map((run) =>
            resolveRecentRunTag(owner, repo, token, run).then((runTag) => ({ run, runTag })),
          ),
        );
        tagged.forEach(({ run }) => {
          dailyNowRuns.push(run);
        });
      }

      WORKFLOWS.forEach((wf) => {
        const wfId = String(wf.id || '');
        if (wf.id === 'daily-paper-reader.yml' && wf.key === 'daily-now') {
          byWorkflow[String(wf.key)] = dailyNowRuns.slice(0, 3);
          return;
        }
        byWorkflow[String(wf.key || wfId)] = (runsByWorkflowId[wfId] || []).slice(0, 3);
      });

      renderRecentRuns(owner, repo, byWorkflow, '', repoContext);
    } catch (e) {
      console.error(e);
      if (recentEl) recentEl.classList.remove('is-loading');
      renderRecentRuns('', '', null, e.message || String(e), null);
    }
  };

  const getWorkflowByKey = (workflowKey) =>
    WORKFLOWS.find((wf) => String(wf.key || '') === String(workflowKey || ''));

  const combineInputs = (baseInputs, extraInputs) => {
    const merged = {};
    const mergeOne = (source) => {
      if (!source || typeof source !== 'object') return;
      Object.keys(source).forEach((k) => {
        const v = source[k];
        if (typeof v === 'undefined' || v === null) return;
        const txt = String(v).trim();
        if (!txt) return;
        merged[String(k)] = txt;
      });
    };
    mergeOne(baseInputs);
    mergeOne(extraInputs);
    return merged;
  };

  const dispatchAndMonitor = async (workflow, extraInputs) => {
    const wf = workflow || {};
    const workflowFile = String(wf.id || '');
    if (!workflowFile) {
      setStatus('工作流配置缺失，无法触发。', '#c00');
      return;
    }
    const dynamicInputs = { ...(wf.dispatchInputs || {}) };
    const rerankerProfile = loadRerankerProfile();
    if (
      rerankerProfile &&
      (workflowFile === 'daily-paper-reader.yml' ||
        workflowFile === 'conference-paper-retrieval.yml')
    ) {
      dynamicInputs.reranker_profile = rerankerProfile;
    }
    const dispatchInputs = combineInputs(dynamicInputs, extraInputs);
    if (isLocalDebugPage()) {
      if (isPublicReadOnlyPage()) {
        setStatus('公网日报站点为只读模式，请在飞书中发起新任务。', '#666');
        runsEl.innerHTML = '<div style="color:#666;">此页面用于查看运行进度和历史日报；新任务请通过飞书机器人发起。</div>';
        return;
      }
      try {
        return await dispatchLocalAndMonitor(wf, workflowFile, dispatchInputs);
      } catch (e) {
        console.error(e);
        const msg = e.message || String(e);
        setStatus(`本地触发失败：${msg}`, '#c00');
        runsEl.innerHTML = `<div style="color:#c00;">${escapeHtml(msg)}<br/>请确认本地后端已启动：<code>scripts/local_debug.sh</code> 或 <code>python src/local_debug_server.py --port 8567</code></div>`;
        return;
      }
    }
    const token = loadGithubToken();
    if (!token) {
      setStatus('未检测到 GitHub Token：请在“密钥配置”或“GitHub Token”处完成配置。', '#c00');
      return;
    }
    const repoContext = await resolveRepoContext(token);
    const { owner, repo } = repoContext;
    if (!owner || !repo) {
      setStatus('无法推断目标仓库：请确认 GitHub Token 有效，或使用 xxx.github.io/仓库名/ 访问。', '#c00');
      return;
    }
    if (wf.key === 'sync' && repoContext.isFork === false) {
      setStatus('当前仓库不是 GitHub Fork，无法使用上游同步。', '#c00');
      runsEl.innerHTML =
        '<div style="color:#c00;">当前仓库不是 Fork 仓库，Upstream Sync 不会运行。</div>' +
        `<div style="margin-top:8px;"><a class="arxiv-tool-btn" style="padding:6px 10px; text-decoration:none;" target="_blank" href="https://github.com/${owner}/${repo}/fork">前往 Fork 当前仓库</a></div>`;
      return;
    }

    setStatus(`正在检查工作流状态：${wf.name || workflowFile} ...`, '#666', { waiting: true });
    runsEl.innerHTML = '<div style="color:#999;">正在检查是否有运行中的工作流...</div>';
    stopPolling();
    activeRun = null;

    try {
      // 检查是否有正在运行中的同名工作流（防止误触重复触发）
      const activeStatuses = new Set(['queued', 'in_progress', 'waiting']);
      const statusZhMap = { queued: '排队中', in_progress: '运行中', waiting: '等待中' };
      const checkUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(
        workflowFile,
      )}/runs?per_page=5`;
      const checkRes = await ghFetch(token, checkUrl);
      if (checkRes.ok) {
        const checkData = await checkRes.json();
        const runs = Array.isArray(checkData.workflow_runs) ? checkData.workflow_runs : [];
        const activeRuns = runs.filter((r) => activeStatuses.has(r.status));
        if (activeRuns.length > 0) {
          const r = activeRuns[0];
          const runUrl = `https://github.com/${owner}/${repo}/actions/runs/${r.id}`;
          const statusText = statusZhMap[r.status] || r.status;
          setStatus(
            `已有正在运行的工作流（#${r.run_number || r.id}，状态：${statusText}），请等待完成后再触发。`,
            '#c00',
          );
          runsEl.innerHTML =
            `<div style="color:#c00;">同一时间只允许运行一个该工作流实例，请等待当前运行结束。</div>` +
            `<div style="margin-top:8px;"><a class="arxiv-tool-btn" style="padding:6px 10px; text-decoration:none;" target="_blank" href="${runUrl}">查看当前运行</a></div>`;
          return;
        }
      }

      setStatus(`正在触发工作流：${wf.name || workflowFile} ...`, '#666', { waiting: true });
      runsEl.innerHTML = '<div style="color:#999;">正在触发，请稍候...</div>';

      const createdAt = new Date();

      // 触发 dispatch
      const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(
        workflowFile,
      )}/dispatches`;
      const dispatchBody = {
        ref: String(repoContext.defaultBranch || 'main'),
      };
      if (Object.keys(dispatchInputs).length > 0) {
        dispatchBody.inputs = dispatchInputs;
      }

      const res = await ghFetch(token, dispatchUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(dispatchBody),
      });
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        if (res.status === 422 && txt.includes('disabled workflow')) {
          const err = new Error('触发失败：该 Workflow 当前处于禁用状态，请先前往 Actions 页面启用该工作流。');
          err.workflowEnableUrl = `https://github.com/${owner}/${repo}/actions/workflows/${encodeURIComponent(workflowFile)}`;
          throw err;
        }
        throw new Error(`触发失败：HTTP ${res.status} ${res.statusText} - ${txt}`);
      }

      setStatus('已触发，正在等待运行记录创建...', '#666', { waiting: true });

      // 轮询找到本次 dispatch 对应的 run
      const lookup = async () => {
        const runsUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(
          workflowFile,
        )}/runs?event=workflow_dispatch&per_page=10`;
        const runsRes = await ghFetch(token, runsUrl);
        if (!runsRes.ok) {
          const txt = await runsRes.text().catch(() => '');
          throw new Error(`读取 workflow runs 失败：HTTP ${runsRes.status} ${runsRes.statusText} - ${txt}`);
        }
        const data = await runsRes.json();
        const list = Array.isArray(data.workflow_runs) ? data.workflow_runs : [];
        const found = list.find((r) => {
          try {
            const t = new Date(r.created_at);
            return t.getTime() >= createdAt.getTime() - 5000;
          } catch {
            return false;
          }
        });
        return found || null;
      };

      let run = null;
      for (let i = 0; i < 18; i += 1) {
        // 最多等 ~90 秒
        // eslint-disable-next-line no-await-in-loop
        run = await lookup();
        if (run) break;
        // eslint-disable-next-line no-await-in-loop
        await new Promise((r) => setTimeout(r, 5000));
      }

      if (!run || !run.id) {
        setStatus('已触发，但未能在短时间内找到对应的运行记录。建议打开 Actions 页面查看。', '#c00');
        runsEl.innerHTML = `<div style="color:#666;">请在 GitHub Actions 查看：<a target="_blank" href="https://github.com/${owner}/${repo}/actions">打开 Actions</a></div>`;
        return;
      }

      activeRun = { owner, repo, runId: run.id, token };
      selectedRun = activeRun;
      setStatus(`运行已创建：run_id=${run.id}，开始拉取进度...`, '#080', { waiting: true });
      await refreshRun(owner, repo, run.id);

      refreshTimer = setInterval(() => {
        const r = selectedRun || activeRun;
        if (!r) return;
        refreshRun(r.owner, r.repo, r.runId);
      }, 5000);

      // 触发后刷新最近运行列表
      loadRecentRuns();
    } catch (e) {
      console.error(e);
      const msg = e.message || String(e);
      setStatus(`触发失败：${msg}`, '#c00');
      if (e.workflowEnableUrl) {
        runsEl.innerHTML =
          `<div style="color:#c00;">${escapeHtml(msg)}<br/>` +
          `👉 <a href="${e.workflowEnableUrl}" target="_blank" style="color:#1a73e8;">前往 Actions 页面启用工作流</a></div>`;
      } else {
        runsEl.innerHTML = `<div style="color:#c00;">${escapeHtml(msg)}</div>`;
      }
    }
  };

  const renderRun = (owner, repo, run, jobs) => {
    const runUrl = `https://github.com/${owner}/${repo}/actions/runs/${run.id}`;
    const status = run.status || '';
    const conclusion = run.conclusion || '';

    const badgeColor =
      conclusion === 'success'
        ? '#2e7d32'
        : conclusion === 'failure'
          ? '#c00'
          : status === 'in_progress'
            ? '#1565c0'
            : '#666';
    const badgeText = formatRunBadgeText(status, conclusion);

    const jobList = Array.isArray(jobs) ? jobs : [];
    const jobHtml = jobList
      .map((j) => {
        const steps = Array.isArray(j.steps) ? j.steps : [];
        const stepLines = steps
          .map((s) => {
            const c = s.conclusion || s.status || '';
            const icon =
              c === 'success'
                ? '✅'
                : c === 'failure'
                  ? '❌'
                  : c === 'skipped'
                    ? '⏭'
                    : c === 'in_progress'
                      ? '⏳'
                      : '•';
            return `<div class="dpr-wf-step">${icon} ${escapeHtml(
              s.name || '',
            )}</div>`;
          })
          .join('');
        const jobId = j.id ? String(j.id) : '';
        return `
          <div class="dpr-wf-job">
            <div class="dpr-wf-job-title">${escapeHtml(j.name || '')}</div>
            <div class="dpr-wf-job-meta">
              <span class="dpr-wf-job-meta-text">${escapeHtml(j.status || '')}${j.conclusion ? ` / ${escapeHtml(j.conclusion)}` : ''}</span>
            </div>
            <div class="dpr-wf-steps">${stepLines || '<div style="color:#999;">暂无步骤信息</div>'}</div>
          </div>
        `;
      })
      .join('');

    runsEl.innerHTML = `
      <div style="display:flex; justify-content:space-between; gap:10px; align-items:center; margin-bottom:8px;">
        <div style="min-width:0;">
          <div style="font-weight:600;">Run #${run.run_number || run.id}</div>
          <div style="color:#666; margin-top:2px;">
            <span style="display:inline-block; padding:1px 6px; border-radius:999px; background:rgba(0,0,0,0.06); color:${badgeColor};">
              ${escapeHtml(badgeText)}
            </span>
            <span style="margin-left:8px;">${escapeHtml(
              formatRunTime(run.created_at),
            )}</span>
          </div>
        </div>
        <div style="flex-shrink:0; display:flex; gap:8px;">
          <a class="arxiv-tool-btn" style="padding:6px 10px; text-decoration:none;" target="_blank" href="${runUrl}">打开 Actions</a>
        </div>
      </div>
      ${jobHtml || '<div style="color:#999;">暂无 Job 信息</div>'}
    `;
  };

  const refreshRun = async (owner, repo, runId) => {
    const token = activeRun && activeRun.token ? activeRun.token : loadGithubToken();
    if (!token) return;

    try {
      const runUrl = `https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}`;
      const res = await ghFetch(token, runUrl);
      if (!res.ok) {
        const txt = await res.text().catch(() => '');
        throw new Error(`读取 run 失败：HTTP ${res.status} ${res.statusText} - ${txt}`);
      }
      const run = await res.json();
      const stateKey = `${run.status || ''}/${run.conclusion || ''}`;
      const prevStateKey = lastRunStateById[String(runId)];
      lastRunStateById[String(runId)] = stateKey;

      const jobsUrl = `https://api.github.com/repos/${owner}/${repo}/actions/runs/${runId}/jobs?per_page=100`;
      const jobsRes = await ghFetch(token, jobsUrl);
      let jobs = [];
      if (jobsRes.ok) {
        const jobsData = await jobsRes.json();
        jobs = Array.isArray(jobsData.jobs) ? jobsData.jobs : [];
      }

      renderRun(owner, repo, run, jobs);

      if (run.status === 'completed') {
        stopPolling();
        setStatus(
          `运行已结束：${run.conclusion || 'completed'}`,
          run.conclusion === 'success' ? '#080' : '#c00',
        );
        // run 状态结束后，刷新“最近运行”列表，确保 completed/success 等状态能及时反映
        if (prevStateKey !== stateKey) {
          loadRecentRuns();
        }
      } else {
        setStatus('运行中：每 5 秒自动刷新...', '#1565c0', { waiting: true });
      }
    } catch (e) {
      console.error(e);
      setStatus(`刷新失败：${e.message || e}`, '#c00');
    }
  };

  const runWorkflowByKey = async (workflowKey, extraInputs) => {
    const wf = getWorkflowByKey(workflowKey);
    if (!wf) {
      setStatus('未找到对应的工作流配置。', '#c00');
      return;
    }
    open();
    return dispatchAndMonitor(wf, extraInputs);
  };

  // 「Step 0 LLM 扩充检索关键词」本地偏好（设置面板与快速抓取弹窗共用同一存储键）
  const loadRunEnrichPref = () => {
    try {
      return window.localStorage
        ? window.localStorage.getItem('dpr_run_enrich') === '1'
        : false;
    } catch (e) {
      return false;
    }
  };

  const runQuickFetchByDays = async (days, extra) => {
    const parsed = parseInt(days, 10);
    const normalized = Number.isFinite(parsed) && parsed > 0 ? String(Math.max(1, parsed)) : '10';
    const options = extra && typeof extra === 'object' ? extra : {};
    const fetchMode = (typeof options.fetchMode === 'string' ? options.fetchMode : '').trim().toLowerCase();
    const presetKey = fetchMode ? `${normalized}-${fetchMode}` : normalized;
    const preset = QUICK_FETCH_PRESETS[presetKey] || QUICK_FETCH_PRESETS[normalized] || null;
    // 天数 + 模式任意组合：以预设打底，天数始终取用户所选；
    // standard/skims 显式透传，auto 交由后端按阈值（≥11 天转速览）决定。
    const baseInputs = Object.assign({}, preset ? preset.dispatchInputs : {}, { fetch_days: normalized });
    if (fetchMode === 'standard' || fetchMode === 'skims') {
      baseInputs.fetch_mode = fetchMode;
    }
    const mergedInputs = combineInputs(baseInputs, options.dispatchInputs);
    if (!Object.prototype.hasOwnProperty.call(options.dispatchInputs || {}, 'run_enrich')) {
      // 调用方未显式指定时，应用本地偏好，覆盖预设里硬编码的默认值
      mergedInputs.run_enrich = loadRunEnrichPref() ? 'true' : 'false';
    }
    return runWorkflowByKey((preset && preset.key) || 'daily-now', mergedInputs);
  };

  const normalizeConferenceName = (value) => {
    const text = String(value || '').trim();
    const lower = text.toLowerCase();
    const MAP = {
      nips: 'NeurIPS', neurips: 'NeurIPS',
      icml: 'ICML', iclr: 'ICLR', aaai: 'AAAI',
      cvpr: 'CVPR', eccv: 'ECCV', ijcai: 'IJCAI',
      acl: 'ACL', emnlp: 'EMNLP',
      unified: 'unified',
      osdi: 'OSDI', sosp: 'SOSP', ndss: 'NDSS',
      sp: 'IEEE S&P', 's&p': 'IEEE S&P', ieeesp: 'IEEE S&P',
      'ieee-sp': 'IEEE S&P', ieee_sp: 'IEEE S&P', 'ieee s&p': 'IEEE S&P',
    };
    return MAP[lower] || '';
  };

  const normalizeConferenceYears = (values) => {
    const raw = Array.isArray(values) ? values : [values];
    const out = [];
    const seen = new Set();
    raw.forEach((item) => {
      const year = parseInt(item, 10);
      if (!Number.isFinite(year) || year <= 0 || seen.has(year)) return;
      seen.add(year);
      out.push(String(year));
    });
    return out;
  };

  const normalizeConferencePairKey = (value) => {
    const text = String(value || '').trim();
    if (!text) return '';
    const compact = text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '');
    if (['ieee_s_p', 'ieee_sp', 's_p', 'sp'].includes(compact)) return 'ieee_sp';
    if (compact === 'nips') return 'neurips';
    return compact;
  };

  const normalizeConferencePairs = (values) => {
    const raw = Array.isArray(values) ? values : String(values || '').split(',');
    const out = [];
    const seen = new Set();
    raw.forEach((item) => {
      const text = String(item || '').trim();
      if (!text || !text.includes(':')) return;
      const [confRaw, yearRaw] = text.split(':');
      const conf = normalizeConferencePairKey(confRaw);
      const year = parseInt(yearRaw, 10);
      if (!conf || !Number.isFinite(year) || year <= 0) return;
      const pair = `${conf}:${year}`;
      if (seen.has(pair)) return;
      seen.add(pair);
      out.push(pair);
    });
    return out;
  };

  const runConferenceRetrieval = async (conference, years, options = {}) => {
    const extraInputs =
      options && typeof options === 'object' && options.dispatchInputs
        ? options.dispatchInputs
        : {};
    const normalizedPairs = normalizeConferencePairs(extraInputs.conference_pairs);
    const normalizedConference = normalizedPairs.length ? 'unified' : normalizeConferenceName(conference);
    const normalizedYears = normalizeConferenceYears(years);
    const pairYears = normalizedPairs.map((item) => item.split(':')[1]);
    const workflowYears = normalizedYears.length ? normalizedYears : normalizeConferenceYears(pairYears);
    if (!normalizedConference || !workflowYears.length) {
      open();
      setStatus('请先选择支持的会议和年份。', '#c00');
      return false;
    }
    const inputs = {
      conference: normalizedConference,
      years: workflowYears.join(','),
      ...extraInputs,
    };
    if (normalizedPairs.length) inputs.conference_pairs = normalizedPairs.join(',');
    return runWorkflowByKey('conference-retrieval', inputs);
  };

  const runConferenceMaintain = async (conference, years) =>
    runConferenceRetrieval(conference, years);

  return {
    open,
    runWorkflowByKey,
    runQuickFetchByDays,
    runConferenceRetrieval,
    runConferenceMaintain,
  };
})();
