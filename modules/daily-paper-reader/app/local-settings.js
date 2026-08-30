(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.DPRLocalSettings = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const OVERLAY_ID = 'dpr-local-settings-overlay';
  const apiUrl = (path) => {
    const base = String(window.DPR_LOCAL_API_BASE || '').trim().replace(/\/$/, '');
    return base ? `${base}${path}` : path;
  };
  const STRUCTURED_ENDPOINT = () => apiUrl('/api/local/config/structured');
  const PARTIAL_ENDPOINT = () => apiUrl('/api/local/config/partial');

  const trimText = (value) => String(value == null ? '' : value).trim();

  // 最近一次从本地服务读取的完整 subscriptions 对象，保存时用于保留 schema/embedding_cache。
  let lastSubscriptions = null;

  function splitLines(text) {
    return String(text || '')
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  // 按每行文本匹配旧的 keyword/query 条目：文本未变则保留原对象（含 embedding_cache），
  // 文本变化（新增/修改）则丢弃缓存，流水线下次运行会重算。
  function preserveEntriesWithCache(lines, prevEntries, textKey, makeNew) {
    const prev = Array.isArray(prevEntries) ? prevEntries : [];
    return lines.map((line) => {
      const match = prev.find((e) => String(e && e[textKey] || '').trim() === line);
      if (match) return { ...match };
      return makeNew(line);
    });
  }

  function buildLocalPayload(formValues) {
    const v = formValues || {};
    return {
      local: {
        chat: {
          base_url: trimText(v.chatBaseUrl),
          model: trimText(v.chatModel),
          api_key: trimText(v.chatApiKey),
        },
        schedule: {
          enabled: Boolean(v.schedEnabled),
          time: trimText(v.schedTime),
        },
        rerank: {
          profile: trimText(v.rerankProfile),
        },
        recall: {
          mode: trimText(v.recallMode),
        },
      },
    };
  }

  // 推荐名额：空值表示跟随运行模式默认，后端会剔除非法值。
  // formValues: { recommendDeep, recommendQuick, deepUnlimited }
  function buildRecommendPayload(formValues) {
    const v = formValues || {};
    return {
      deep_dive_base: trimText(v.recommendDeep),
      quick_skim_base: trimText(v.recommendQuick),
      deep_dive_unlimited: Boolean(v.deepUnlimited),
    };
  }

  // formState: 面板里每个词条的编辑态数组
  //   { tag, description, enabled, keywordsText, intentQueriesText }
  // prevSubscriptions: 上一版完整 subscriptions 对象（含 schema_migration / keyword_recall_mode）
  // 返回可直接回写 /api/local/config/partial 的 subscriptions 对象。
  function buildSubscriptionsPayload(formState, prevSubscriptions) {
    const subs = prevSubscriptions && typeof prevSubscriptions === 'object' ? prevSubscriptions : {};
    const prevProfiles = Array.isArray(subs.intent_profiles) ? subs.intent_profiles : [];
    const rows = Array.isArray(formState) ? formState : [];

    const intentProfiles = rows.map((row) => {
      const tag = trimText(row.tag);
      const prev = prevProfiles.find(
        (p) => String((p && p.tag) || '').trim() === tag,
      ) || {};

      const keywords = preserveEntriesWithCache(
        splitLines(row.keywordsText),
        prev.keywords,
        'keyword',
        (line) => ({ query: line, keyword: line }),
      );

      const intentQueries = preserveEntriesWithCache(
        splitLines(row.intentQueriesText),
        prev.intent_queries,
        'query',
        (line) => ({ query: line, enabled: true }),
      );

      const profile = {
        tag,
        description: trimText(row.description),
        enabled: Boolean(row.enabled),
        keywords,
        intent_queries: intentQueries,
      };
      if (Array.isArray(prev.paper_sources)) {
        profile.paper_sources = prev.paper_sources.slice();
      } else {
        profile.paper_sources = ['arxiv'];
      }
      return profile;
    });

    return {
      ...subs,
      intent_profiles: intentProfiles,
    };
  }

  // 保存前校验：避免把订阅词条写成“空”导致后续日报召回 0 篇。
  // 只要有非空标签即视为可用（关键词/意图查询为可选增强，不强制）。
  // 仅当原本有可用词条、新版却一个标签都不剩时，才阻止保存。
  function profileIsUsable(p) {
    return !!(p && typeof p === 'object' && (p.tag || '').trim());
  }

  function validateSubscriptionsPayload(prevSubs, nextSubs) {
    const prevProfiles = Array.isArray(prevSubs && prevSubs.intent_profiles) ? prevSubs.intent_profiles : [];
    const nextSub = nextSubs && typeof nextSubs === 'object' ? nextSubs : {};
    const nextProfiles = Array.isArray(nextSub.intent_profiles) ? nextSub.intent_profiles : [];

    const hadUsable = prevProfiles.some(profileIsUsable);
    if (!hadUsable) return null; // 原本就没可用词条，不拦截

    const hasUsable = nextProfiles.some(profileIsUsable);
    if (hasUsable) return null;

    return '保存会清空所有订阅词条（一个带标签的词条都不剩），日报将无法召回论文。请检查订阅标签区块，或刷新页面后重试。';
  }

  function escapeAttr(text) {
    return String(text == null ? '' : text).replace(/"/g, '&quot;').replace(/</g, '&lt;');
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // 清洗 /api/local/smart-query 返回的候选：丢空 en、截断上限，保证前端渲染安全。
  function normalizeCandidates(data) {
    const d = data && typeof data === 'object' ? data : {};
    const pairs = (raw, limit) => (Array.isArray(raw) ? raw : [])
      .map((item) => ({
        en: String(item && item.en || '').trim(),
        zh: String(item && item.zh || '').trim(),
      }))
      .filter((p) => p.en)
      .slice(0, limit);
    return {
      tag: String(d.tag || '').trim(),
      description: String(d.description || '').trim(),
      keywords: pairs(d.keywords, 15),
      queries: pairs(d.queries, 12),
    };
  }

  // 把候选（取 key 字段）合并进 textarea 现有行：忽略大小写去重，返回新文本与新增/跳过计数。
  function mergeCandidateLines(existingText, candidates, key) {
    const existing = splitLines(existingText);
    const seen = new Set(existing.map((s) => s.toLowerCase()));
    const fresh = [];
    (Array.isArray(candidates) ? candidates : []).forEach((c) => {
      const value = String(c && c[key] || '').trim();
      if (!value) return;
      const lower = value.toLowerCase();
      if (seen.has(lower)) return;
      seen.add(lower);
      fresh.push(value);
    });
    return {
      text: existing.concat(fresh).join('\n'),
      added: fresh.length,
      skipped: (Array.isArray(candidates) ? candidates.filter((c) => String(c && c[key] || '').trim()) : []).length - fresh.length,
    };
  }

  // 候选卡片列表 HTML（英中成对，默认全选）；kind 用于区分关键词/意图查询两组勾选。
  function renderCandidateCards(kind, items) {
    return (Array.isArray(items) ? items : []).map((c) =>
      '<label class="dpr-smart-cand">' +
      '<input type="checkbox" checked data-dpr-smart-check="' + kind + '" value="' + escapeAttr(c.en) + '" />' +
      '<span class="dpr-smart-cand-body">' +
      '<span class="dpr-smart-cand-en">' + escapeHtml(c.en) + '</span>' +
      (c.zh ? '<span class="dpr-smart-cand-zh">' + escapeHtml(c.zh) + '</span>' : '') +
      '</span>' +
      '</label>'
    ).join('');
  }

  function createProfileEditorHtml(profile, index) {
    const keywordsText = (profile.keywords || [])
      .map((k) => k.keyword || k.query || '')
      .join('\n');
    const intentQueriesText = (profile.intent_queries || [])
      .map((iq) => iq.query || '')
      .join('\n');
    return (
      '<fieldset class="dpr-sub-profile" data-profile-index="' + index + '">' +
      '  <div class="dpr-settings-field">' +
      '    <label>订阅标签（如 RAG）</label>' +
      '    <input type="text" class="dpr-sub-profile-tag" value="' + escapeAttr(profile.tag) + '" placeholder="RAG" />' +
      '  </div>' +
      '  <div class="dpr-settings-field">' +
      '    <label>描述</label>' +
      '    <input type="text" class="dpr-sub-profile-desc" value="' + escapeAttr(profile.description) + '" placeholder="Retrieval-Augmented Generation" />' +
      '  </div>' +
      '  <div class="dpr-settings-field">' +
      '    <label><input type="checkbox" class="dpr-sub-profile-enabled"' + (profile.enabled ? ' checked' : '') + ' /> 启用该订阅词条</label>' +
      '  </div>' +
      '  <fieldset class="dpr-smart-query">' +
      '    <label class="dpr-smart-title">✨ AI 生成候选</label>' +
      '    <p class="dpr-smart-hint">输入一句检索需求，LLM 解析成候选关键词/意图查询，勾选后应用到下面两个输入框。</p>' +
      '    <div class="dpr-smart-row">' +
      '      <input type="text" class="dpr-smart-intent" placeholder="例：追踪用强化学习做神经组合优化的最新论文" />' +
      '      <button type="button" class="secret-gate-btn secondary" data-dpr-smart-generate>生成候选</button>' +
      '    </div>' +
      '    <div class="dpr-smart-status" aria-live="polite"></div>' +
      '    <div class="dpr-smart-cands"></div>' +
      '    <button type="button" class="secret-gate-btn primary dpr-smart-apply" data-dpr-smart-apply style="display:none;">应用所选到关键词 / 意图查询</button>' +
      '  </fieldset>' +
      '  <div class="dpr-settings-field">' +
      '    <label>关键词（BM25 召回，每行一个）</label>' +
      '    <textarea class="dpr-sub-profile-keywords" rows="3" placeholder="retrieval augmented generation">' + escapeAttr(keywordsText) + '</textarea>' +
      '  </div>' +
      '  <div class="dpr-settings-field">' +
      '    <label>意图查询（向量语义召回，每行一条）</label>' +
      '    <textarea class="dpr-sub-profile-queries" rows="3" placeholder="Find recent papers on retrieval-augmented generation and LLM grounding">' + escapeAttr(intentQueriesText) + '</textarea>' +
      '  </div>' +
      '  <div class="dpr-sub-profile-actions">' +
      '    <button type="button" class="secret-gate-btn secondary" data-dpr-sub-remove>删除词条</button>' +
      '  </div>' +
      '</fieldset>'
    );
  }

  function renderSubscriptions(container, profiles) {
    const list = (profiles || []).filter((p) => p && typeof p === 'object');
    container.innerHTML =
      '<div class="dpr-sub-section">' +
      '  <h3 style="margin:16px 0 4px;">📚 订阅标签（intent_profiles）</h3>' +
      '  <p style="font-size:12px;color:#666;margin:0 0 10px;">每个词条至少填一个标签即可；关键词/意图查询为可选增强（留空时会自动用标签本身召回）。保存后写回 config.yaml，改动过的关键词/查询会在下次运行重算向量缓存。</p>' +
      '  <div id="dpr-sub-profile-list"></div>' +
      '  <button type="button" class="secret-gate-btn secondary" id="dpr-sub-add-profile" data-dpr-sub-add>+ 新增词条</button>' +
      '</div>';
    const listEl = container.querySelector('#dpr-sub-profile-list');
    listEl.innerHTML = list.map((p, i) => createProfileEditorHtml(p, i)).join('');

    const addBtn = container.querySelector('#dpr-sub-add-profile');
    addBtn.addEventListener('click', () => {
      list.push({ tag: '', description: '', enabled: true, keywords: [], intent_queries: [], paper_sources: ['arxiv'] });
      renderSubscriptions(container, list);
    });

    listEl.querySelectorAll('[data-dpr-sub-remove]').forEach((btn, i) => {
      btn.addEventListener('click', () => {
        list.splice(i, 1);
        renderSubscriptions(container, list);
      });
    });

    // AI 生成候选的事件用委托挂在 listEl 上；listEl 本身不随重渲染替换，只绑一次。
    if (!listEl.dataset.dprSmartWired) {
      listEl.dataset.dprSmartWired = '1';
      listEl.addEventListener('click', onSmartQueryClick);
    }
  }

  function smartBlockRefs(el) {
    const block = el.closest('[data-profile-index]');
    if (!block) return null;
    return {
      block,
      intentEl: block.querySelector('.dpr-smart-intent'),
      statusEl: block.querySelector('.dpr-smart-status'),
      candsEl: block.querySelector('.dpr-smart-cands'),
      applyEl: block.querySelector('[data-dpr-smart-apply]'),
      genBtn: block.querySelector('[data-dpr-smart-generate]'),
      tagEl: block.querySelector('.dpr-sub-profile-tag'),
      descEl: block.querySelector('.dpr-sub-profile-desc'),
      kwEl: block.querySelector('.dpr-sub-profile-keywords'),
      qEl: block.querySelector('.dpr-sub-profile-queries'),
    };
  }

  function onSmartQueryClick(e) {
    const genBtn = e.target.closest('[data-dpr-smart-generate]');
    if (genBtn) {
      handleSmartGenerate(genBtn);
      return;
    }
    const applyBtn = e.target.closest('[data-dpr-smart-apply]');
    if (applyBtn) handleSmartApply(applyBtn);
  }

  async function handleSmartGenerate(btn) {
    const refs = smartBlockRefs(btn);
    if (!refs) return;
    const intent = refs.intentEl ? refs.intentEl.value.trim() : '';
    if (!intent) {
      refs.statusEl.style.color = '#c00';
      refs.statusEl.textContent = '请先输入一句检索需求。';
      return;
    }
    refs.genBtn.disabled = true;
    refs.statusEl.style.color = '';
    refs.statusEl.textContent = '正在让 LLM 解析检索需求（约 5-20 秒）...';
    refs.candsEl.innerHTML = '';
    refs.applyEl.style.display = 'none';
    try {
      const resp = await fetch('/api/local/smart-query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ intent: intent, tag: refs.tagEl ? refs.tagEl.value.trim() : '' }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        throw new Error((data && data.error) || ('HTTP ' + resp.status));
      }
      const cands = normalizeCandidates(data);
      if (!cands.keywords.length && !cands.queries.length) {
        throw new Error('LLM 没有给出可用候选，请换一句更具体的描述重试。');
      }
      refs.candsEl.innerHTML =
        (cands.keywords.length
          ? '<div class="dpr-smart-group"><div class="dpr-smart-group-title">关键词候选（BM25 召回）</div><div class="dpr-smart-grid">' + renderCandidateCards('keywords', cands.keywords) + '</div></div>'
          : '') +
        (cands.queries.length
          ? '<div class="dpr-smart-group"><div class="dpr-smart-group-title">意图查询候选（向量语义召回）</div><div class="dpr-smart-grid">' + renderCandidateCards('queries', cands.queries) + '</div></div>'
          : '');
      // 词条标签/描述还空着时，顺手用 LLM 的建议回填
      if (cands.tag && refs.tagEl && !refs.tagEl.value.trim()) refs.tagEl.value = cands.tag;
      if (cands.description && refs.descEl && !refs.descEl.value.trim()) refs.descEl.value = cands.description;
      refs.applyEl.style.display = '';
      refs.statusEl.textContent = '已生成 ' + cands.keywords.length + ' 个关键词、' + cands.queries.length +
        ' 条意图查询候选；取消勾选不需要的项，再点「应用所选」。';
    } catch (err) {
      refs.statusEl.style.color = '#c00';
      refs.statusEl.textContent = '候选生成失败：' + (err && err.message ? err.message : err);
    } finally {
      refs.genBtn.disabled = false;
    }
  }

  function handleSmartApply(btn) {
    const refs = smartBlockRefs(btn);
    if (!refs) return;
    const checkedOf = (kind) => Array.from(
      refs.block.querySelectorAll('input[data-dpr-smart-check="' + kind + '"]:checked'),
    ).map((el) => ({ en: el.value }));
    const kwCands = checkedOf('keywords');
    const qCands = checkedOf('queries');
    if (!kwCands.length && !qCands.length) {
      refs.statusEl.style.color = '#c00';
      refs.statusEl.textContent = '没有勾选任何候选。';
      return;
    }
    const kwResult = refs.kwEl ? mergeCandidateLines(refs.kwEl.value, kwCands, 'en') : { added: 0, skipped: 0 };
    const qResult = refs.qEl ? mergeCandidateLines(refs.qEl.value, qCands, 'en') : { added: 0, skipped: 0 };
    if (refs.kwEl) refs.kwEl.value = kwResult.text;
    if (refs.qEl) refs.qEl.value = qResult.text;
    const skipped = (kwResult.skipped || 0) + (qResult.skipped || 0);
    refs.statusEl.style.color = '#080';
    refs.statusEl.textContent = '已应用：新增 ' + kwResult.added + ' 个关键词、' + qResult.added + ' 条意图查询' +
      (skipped > 0 ? '；重复跳过 ' + skipped + ' 条' : '') +
      '。点「保存」写回 config.yaml 后，下次运行生效。';
    refs.candsEl.innerHTML = '';
    refs.applyEl.style.display = 'none';
  }

  function collectSubscriptionsFormState(container) {
    const listEl = container.querySelector('#dpr-sub-profile-list');
    if (!listEl) return [];
    return Array.from(listEl.querySelectorAll('[data-profile-index]')).map((block) => {
      const tagEl = block.querySelector('.dpr-sub-profile-tag');
      const descEl = block.querySelector('.dpr-sub-profile-desc');
      const enabledEl = block.querySelector('.dpr-sub-profile-enabled');
      const kwEl = block.querySelector('.dpr-sub-profile-keywords');
      const qEl = block.querySelector('.dpr-sub-profile-queries');
      return {
        tag: tagEl ? tagEl.value : '',
        description: descEl ? descEl.value : '',
        enabled: enabledEl ? enabledEl.checked : true,
        keywordsText: kwEl ? kwEl.value : '',
        intentQueriesText: qEl ? qEl.value : '',
      };
    });
  }

  function createOverlay() {
    let overlay = document.getElementById(OVERLAY_ID);
    if (overlay) return overlay;
    overlay = document.createElement('div');
    overlay.id = OVERLAY_ID;
    overlay.className = 'secret-gate-overlay dpr-local-settings-overlay';
    overlay.style.display = 'none';
    overlay.innerHTML =
      '<div class="secret-gate-modal" role="dialog" aria-modal="true" aria-label="本地服务设置">' +
      '  <h2 style="margin-top:0;">⚙️ 本地服务设置</h2>' +
      '  <p style="font-size:13px;color:#555;margin:0 0 14px;">修改会直接写回本机 <code>config.yaml</code>，重启后对本地服务生效。</p>' +
      '  <div class="dpr-settings-field"><label>API 端点（OpenAI 兼容）</label><input type="text" id="dpr-settings-chat-baseurl" placeholder="https://api.sinksilk.com:58443" /></div>' +
      '  <div class="dpr-settings-field"><label>API Key（留空表示沿用 .env，不修改）</label><input type="password" id="dpr-settings-chat-apikey" placeholder="sk-..." autocomplete="off" /></div>' +
      '  <div class="dpr-settings-field"><label>AI 问答模型</label>' +
      '    <div style="display:flex;gap:6px;align-items:center;">' +
      '      <input type="text" id="dpr-settings-chat-model" placeholder="deepseek-v4-flash" style="flex:1;min-width:0;" />' +
      '      <button type="button" class="secret-gate-btn secondary" id="dpr-settings-chat-fetch-models" style="white-space:nowrap;padding:4px 10px;font-size:12px;">获取模型列表</button>' +
      '    </div>' +
      '    <select id="dpr-settings-chat-model-select" style="margin-top:6px;width:100%;display:none;"></select>' +
      '    <p style="font-size:12px;color:#666;margin:4px 0 0;">填好端点和密钥后点「获取模型列表」，从下拉里选择会自动填入；也可以直接手输模型名。密钥留空时按已保存/.env 的密钥拉取。</p></div>' +
      '  <div class="dpr-settings-field"><label>连通性测试</label>' +
      '    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' +
      '      <button type="button" class="secret-gate-btn secondary" id="dpr-settings-chat-test" style="padding:4px 10px;font-size:12px;">测试连通性</button>' +
      '      <span id="dpr-settings-chat-test-status" style="font-size:12px;color:#666;"></span>' +
      '    </div>' +
      '    <p style="font-size:12px;color:#666;margin:4px 0 0;">向当前端点+模型发一次最小对话请求（max_tokens=8），成功会显示耗时。</p></div>' +
      '  <div class="dpr-settings-field"><label>召回模式（日报数据来源）</label>' +
      '    <select id="dpr-settings-recall-mode">' +
      '      <option value="supabase">云端（Supabase RPC，默认）</option>' +
      '      <option value="local">本地（arXiv API 增量 + 本地 FTS/embedding，不依赖 Supabase）</option>' +
      '    </select>' +
      '    <p style="font-size:12px;color:#666;margin:4px 0 0;">云端模式快但依赖共享数据库（偶发时段性超时）；本地模式每次运行先从 arXiv API 抓取窗口增量（约 3-5 分钟），再用本地索引 + embedding 精排（默认 qwen3-embedding，失败自动降级本地 bge-small），共享库故障时零影响。保存后下一次流水线生效。</p></div>' +
      '  <div class="dpr-settings-field"><label>Reranker 精选后端（日报与综述共用）</label>' +
      '    <select id="dpr-settings-rerank-profile">' +
      '      <option value="auto">自动（跟随 .env，缺省远程）</option>' +
      '      <option value="public-zwwen-rerank">远程 · zwwen 公益端点（推荐，免配置）</option>' +
      '      <option value="public-sinksilk-rerank">远程 · sinksilk 中转</option>' +
      '      <option value="siliconflow-qwen3-0.6b">远程 · SiliconFlow</option>' +
      '      <option value="local-qwen3-0.6b">本地 GPU（Qwen3-Reranker-0.6B）</option>' +
      '    </select>' +
      '    <p style="font-size:12px;color:#666;margin:4px 0 0;">默认远程优先：不用本机 GPU、无需下载模型；公益端点实测速度与本地 GPU 打平。选本地需已安装 requirements-local-models.txt。保存后下一次流水线/综述即生效，无需重启服务。</p></div>' +
      '  <div class="dpr-settings-field"><label><input type="checkbox" id="dpr-settings-sched-enabled" /> 每天定时自动跑流水线</label></div>' +
      '  <div class="dpr-settings-field"><label>定时时间（本地 24 小时制）</label><input type="text" id="dpr-settings-sched-time" placeholder="18:30" /></div>' +
      '  <div class="dpr-settings-field"><label><input type="checkbox" id="dpr-settings-run-enrich" /> 触发日报时启用 Step 0：LLM 扩充检索关键词</label>' +
      '    <p style="font-size:12px;color:#666;margin:4px 0 0;">开启后每次手动触发（含快速抓取、保存并生成日报）会先让 LLM 根据订阅关键词扩充检索词并写回 config.yaml；默认关闭。该偏好在本地浏览器记忆，两个入口共用。</p></div>' +
      '  <div class="dpr-settings-field"><label>默认运行模式（快速抓取弹窗与本面板共用）</label>' +
      '    <select id="dpr-settings-fetch-mode">' +
      '      <option value="auto">自动（10 天走标准精读、30 天走速览）</option>' +
      '      <option value="standard">标准精读</option>' +
      '      <option value="skims">速览（全部进速读）</option>' +
      '    </select>' +
      '    <p style="font-size:12px;color:#666;margin:4px 0 0;">决定触发流水线时的分层策略，选择会记住。「速览」忽略历史已读记录；≥10 天窗口自动按区间日期归档。</p></div>' +
      '  <fieldset class="dpr-sub-profile" style="margin-top:14px;">' +
      '    <label style="font-weight:600;display:block;margin-bottom:8px;">📊 每日推荐名额</label>' +
      '    <div class="dpr-settings-field"><label>精读区保底数量（标准模式默认 5，下限 3；留空跟随所选运行模式）</label>' +
      '      <input type="number" min="0" id="dpr-settings-recommend-deep" placeholder="5" /></div>' +
      '    <div class="dpr-settings-field"><label>速读区基数（标准模式默认 10；留空跟随所选运行模式）</label>' +
      '      <input type="number" min="0" id="dpr-settings-recommend-quick" placeholder="10" /></div>' +
      '    <div class="dpr-settings-field"><label><input type="checkbox" id="dpr-settings-recommend-deep-unlimited" /> 精读不限量（已默认生效，勾选保留兼容）</label>' +
      '      <p style="font-size:12px;color:#666;margin:4px 0 0;">≥8 分论文自动全部进入精读区（无上限）；不足保底数量时从 6 分以上按分数从高到低回填，不会把 6 分以下的论文硬凑进精读。保底数量越大，长总结的 LLM 耗时与成本越高。保底下限为 3，防止篇数太少影响生成质量。</p></div>' +
      '  </fieldset>' +
      '  <div id="dpr-settings-subscriptions"></div>' +
      '  <div id="dpr-settings-status" style="min-height:18px;font-size:12px;color:#666;margin:8px 0;"></div>' +
      '  <div class="secret-gate-actions">' +
      '    <button type="button" class="secret-gate-btn secondary" id="dpr-settings-cancel">取消</button>' +
      '    <button type="button" class="secret-gate-btn secondary" id="dpr-settings-save" title="仅保存配置到 config.yaml，不跑流水线">保存</button>' +
      '    <button type="button" class="secret-gate-btn primary" id="dpr-settings-save-run" title="保存配置后触发每日流水线，生成新的论文日报">🚀 保存并生成日报</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(overlay);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close();
    });
    document.getElementById('dpr-settings-cancel').addEventListener('click', close);
    document.getElementById('dpr-settings-save').addEventListener('click', save);
    document.getElementById('dpr-settings-save-run').addEventListener('click', saveAndRun);

    const enrichPrefEl = document.getElementById('dpr-settings-run-enrich');
    if (enrichPrefEl) {
      enrichPrefEl.addEventListener('change', () => {
        try {
          localStorage.setItem('dpr_run_enrich', enrichPrefEl.checked ? '1' : '0');
        } catch (err) { /* 忽略持久化失败 */ }
      });
    }

    // 默认运行模式：与快速抓取弹窗共用 localStorage 键 dpr_fetch_mode，改动立即生效。
    const fetchModeEl = document.getElementById('dpr-settings-fetch-mode');
    if (fetchModeEl) {
      try {
        const savedMode = String(localStorage.getItem('dpr_fetch_mode') || '').trim();
        if (['auto', 'standard', 'skims'].includes(savedMode)) {
          fetchModeEl.value = savedMode;
        }
      } catch (err) { /* 忽略 */ }
      fetchModeEl.addEventListener('change', () => {
        try {
          localStorage.setItem('dpr_fetch_mode', fetchModeEl.value);
        } catch (err) { /* 忽略持久化失败 */ }
      });
    }

    // AI 问答模型：拉取模型列表 / 下拉回填 / 连通性测试（走本地后端，密钥留空回退已保存/.env 的密钥）。
    const fetchModelsBtn = document.getElementById('dpr-settings-chat-fetch-models');
    const modelSelect = document.getElementById('dpr-settings-chat-model-select');
    if (fetchModelsBtn && modelSelect) {
      fetchModelsBtn.addEventListener('click', async () => {
        const baseUrl = String((document.getElementById('dpr-settings-chat-baseurl') || {}).value || '').trim();
        const apiKey = String((document.getElementById('dpr-settings-chat-apikey') || {}).value || '').trim();
        if (!baseUrl) {
          setChatToolStatus('请先填写 API 端点', '#c00');
          return;
        }
        const originalText = fetchModelsBtn.textContent;
        fetchModelsBtn.disabled = true;
        setChatToolStatus('正在获取模型列表...');
        let resp;
        try {
          resp = await fetch(apiUrl('/api/local/chat/models'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok || !data.ok) throw new Error((data && data.error) || ('HTTP ' + resp.status));
          const models = Array.isArray(data.models) ? data.models : [];
          if (!models.length) throw new Error('端点返回了空模型列表');
          modelSelect.innerHTML = '';
          modelSelect.appendChild(new Option('共 ' + models.length + ' 个模型，点选自动填入 ↓', ''));
          models.forEach((m) => modelSelect.appendChild(new Option(m, m)));
          modelSelect.style.display = '';
          setChatToolStatus('已获取 ' + models.length + ' 个模型 ✓', '#080');
        } catch (err) {
          setChatToolStatus('获取失败：' + describeChatToolError(err, resp), '#c00');
        } finally {
          fetchModelsBtn.disabled = false;
          fetchModelsBtn.textContent = originalText;
        }
      });
      modelSelect.addEventListener('change', () => {
        const chosen = String(modelSelect.value || '');
        if (!chosen) return;
        const modelInput = document.getElementById('dpr-settings-chat-model');
        if (modelInput) modelInput.value = chosen;
        setChatToolStatus('已选择模型：' + chosen, '#080');
      });
    }

    const chatTestBtn = document.getElementById('dpr-settings-chat-test');
    if (chatTestBtn) {
      chatTestBtn.addEventListener('click', async () => {
        const baseUrl = String((document.getElementById('dpr-settings-chat-baseurl') || {}).value || '').trim();
        const apiKey = String((document.getElementById('dpr-settings-chat-apikey') || {}).value || '').trim();
        const model = String((document.getElementById('dpr-settings-chat-model') || {}).value || '').trim();
        if (!baseUrl) {
          setChatToolStatus('请先填写 API 端点', '#c00');
          return;
        }
        if (!model) {
          setChatToolStatus('请先填写或从下拉选择模型名称', '#c00');
          return;
        }
        const originalText = chatTestBtn.textContent;
        chatTestBtn.disabled = true;
        setChatToolStatus('正在发测试请求（最长约 30 秒）...');
        let resp;
        try {
          resp = await fetch(apiUrl('/api/local/chat/test'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, model: model }),
          });
          const data = await resp.json().catch(() => ({}));
          if (!resp.ok || !data.ok) throw new Error((data && data.error) || ('HTTP ' + resp.status));
          const latency = typeof data.latency_ms === 'number' ? '（' + data.latency_ms + 'ms）' : '';
          setChatToolStatus('连接成功 ✓ ' + latency + (data.reply ? ' 回复：' + data.reply : ''), '#080');
        } catch (err) {
          setChatToolStatus('连接失败：' + describeChatToolError(err, resp), '#c00');
        } finally {
          chatTestBtn.disabled = false;
          chatTestBtn.textContent = originalText;
        }
      });
    }
    return overlay;
  }

  function setStatus(text, color) {
    const el = document.getElementById('dpr-settings-status');
    if (el) {
      el.textContent = text || '';
      if (color) el.style.color = color;
    }
  }

  function setChatToolStatus(text, color) {
    const el = document.getElementById('dpr-settings-chat-test-status');
    if (el) {
      el.textContent = text || '';
      if (color) el.style.color = color;
    }
  }

  // 本地服务进程早于新代码启动时，新端点不存在，后端统一返回 404 not found——
  // 必须明确提示重启，否则用户只看到一句 not found 无从下手。
  function describeChatToolError(err, resp) {
    const msg = err && err.message ? err.message : String(err || '');
    if ((resp && resp.status === 404) || /not found/i.test(msg)) {
      return '本地服务进程是旧版本（不含该接口），请重启本地服务后重试';
    }
    return msg;
  }

  async function open() {
    const overlay = createOverlay();
    overlay.style.display = 'flex';
    requestAnimationFrame(() => requestAnimationFrame(() => overlay.classList.add('show')));
    const enrichPrefEl = document.getElementById('dpr-settings-run-enrich');
    if (enrichPrefEl) {
      try {
        enrichPrefEl.checked = localStorage.getItem('dpr_run_enrich') === '1';
      } catch (err) { /* 忽略 */ }
    }
    const fetchModeEl = document.getElementById('dpr-settings-fetch-mode');
    if (fetchModeEl) {
      try {
        const savedMode = String(localStorage.getItem('dpr_fetch_mode') || '').trim();
        if (['auto', 'standard', 'skims'].includes(savedMode)) {
          fetchModeEl.value = savedMode;
        }
      } catch (err) { /* 忽略 */ }
    }
    setStatus('正在读取配置...');
    try {
      const resp = await fetch(STRUCTURED_ENDPOINT());
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      const local = (data && data.local) || {};
      const chat = local.chat || {};
      const sched = local.schedule || {};
      const modelEl = document.getElementById('dpr-settings-chat-model');
      const baseEl = document.getElementById('dpr-settings-chat-baseurl');
      const keyEl = document.getElementById('dpr-settings-chat-apikey');
      const enabledEl = document.getElementById('dpr-settings-sched-enabled');
      const timeEl = document.getElementById('dpr-settings-sched-time');
      if (modelEl) modelEl.value = chat.model || '';
      if (baseEl) baseEl.value = chat.base_url || '';
      if (keyEl) keyEl.value = ''; // 不预填密钥，留空表示沿用 .env
      if (enabledEl) enabledEl.checked = Boolean(sched.enabled);
      if (timeEl) timeEl.value = sched.time || '';
      const rerankEl = document.getElementById('dpr-settings-rerank-profile');
      if (rerankEl) {
        const savedProfile = String((local.rerank && local.rerank.profile) || '').trim();
        rerankEl.value = Array.prototype.some.call(rerankEl.options, (o) => o.value === savedProfile)
          ? savedProfile
          : 'auto';
      }
      const recallEl = document.getElementById('dpr-settings-recall-mode');
      if (recallEl) {
        const savedMode = String((local.recall && local.recall.mode) || '').trim().toLowerCase();
        recallEl.value = savedMode === 'local' ? 'local' : 'supabase';
      }

      const recommend = (local && local.recommend_setting) || {};
      const deepEl = document.getElementById('dpr-settings-recommend-deep');
      const quickEl = document.getElementById('dpr-settings-recommend-quick');
      const unlimitedEl = document.getElementById('dpr-settings-recommend-deep-unlimited');
      if (deepEl) deepEl.value = String(recommend.deep_dive_base || '');
      if (quickEl) quickEl.value = String(recommend.quick_skim_base || '');
      if (unlimitedEl) unlimitedEl.checked = Boolean(recommend.deep_dive_unlimited);

      const subsContainer = document.getElementById('dpr-settings-subscriptions');
      if (subsContainer) {
        // 结构化接口把 subscriptions 嵌在 local 下（local.chat/schedule/subscriptions）
        const subs = ((data && data.local) && data.local.subscriptions) || {};
        lastSubscriptions = subs && typeof subs === 'object' ? subs : null;
        renderSubscriptions(subsContainer, (subs && subs.intent_profiles) || []);
      }
      setStatus('');
    } catch (err) {
      setStatus('读取配置失败：' + (err && err.message ? err.message : err), '#c00');
    }
  }

  function close() {
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) {
      overlay.classList.remove('show');
      overlay.style.display = 'none';
    }
  }

  function buildCurrentPayload() {
    const values = {
      chatBaseUrl: document.getElementById('dpr-settings-chat-baseurl').value,
      chatModel: document.getElementById('dpr-settings-chat-model').value,
      chatApiKey: document.getElementById('dpr-settings-chat-apikey').value,
      schedEnabled: document.getElementById('dpr-settings-sched-enabled').checked,
      schedTime: document.getElementById('dpr-settings-sched-time').value,
      rerankProfile: (document.getElementById('dpr-settings-rerank-profile') || {}).value || 'auto',
      recallMode: (document.getElementById('dpr-settings-recall-mode') || {}).value || 'supabase',
      recommendDeep: (document.getElementById('dpr-settings-recommend-deep') || {}).value || '',
      recommendQuick: (document.getElementById('dpr-settings-recommend-quick') || {}).value || '',
      deepUnlimited: document.getElementById('dpr-settings-recommend-deep-unlimited')
        ? document.getElementById('dpr-settings-recommend-deep-unlimited').checked
        : false,
    };
    const payload = buildLocalPayload(values);
    payload.recommend_setting = buildRecommendPayload(values);

    const subsContainer = document.getElementById('dpr-settings-subscriptions');
    if (subsContainer) {
      const prevSubs = lastSubscriptions || {};
      const formState = collectSubscriptionsFormState(subsContainer);
      payload.subscriptions = buildSubscriptionsPayload(formState, prevSubs);
    }
    return payload;
  }

  // 把当前面板内容写回 config.yaml；成功返回 true，失败返回 false。
  async function persist() {
    const payload = buildCurrentPayload();

    const subsContainer = document.getElementById('dpr-settings-subscriptions');
    if (subsContainer) {
      const prevSubs = lastSubscriptions || {};
      const problem = validateSubscriptionsPayload(prevSubs, payload.subscriptions);
      if (problem) {
        setStatus(problem, '#c00');
        return false;
      }
    }

    setStatus('保存中...');
    try {
      const resp = await fetch(PARTIAL_ENDPOINT(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || !data.ok) {
        throw new Error((data && data.error) || ('HTTP ' + resp.status));
      }
      return true;
    } catch (err) {
      setStatus('保存失败：' + (err && err.message ? err.message : err), '#c00');
      return false;
    }
  }

  async function save() {
    const ok = await persist();
    if (!ok) return;
    setStatus('已保存 ✓', '#080');
    setTimeout(close, 700);
  }

  // 保存配置后触发每日流水线，生成新的论文日报。
  async function saveAndRun() {
    const ok = await persist();
    if (!ok) return;
    if (window.DPR_LOCAL_API_BASE) {
      setStatus('已保存 ✓，请返回飞书或 CLI 重新发起任务', '#080');
      setTimeout(close, 1200);
      return;
    }
    setStatus('已保存，正在触发日报生成…', '#086');
    try {
      const runner = window.DPRWorkflowRunner;
      if (!runner || typeof runner.runQuickFetchByDays !== 'function') {
        setStatus('未找到流水线触发器（workflows.runner.js 未加载），请刷新后重试。', '#c00');
        return;
      }
      await runner.runQuickFetchByDays('10');
      setStatus('已保存并触发日报生成 ✓，可在运行面板查看进度', '#080');
      setTimeout(close, 1200);
    } catch (err) {
      setStatus('保存成功，但触发日报失败：' + (err && err.message ? err.message : err), '#c00');
    }
  }

  return {
    open,
    close,
    save,
    saveAndRun,
    buildLocalPayload,
    buildRecommendPayload,
    buildSubscriptionsPayload,
    validateSubscriptionsPayload,
    normalizeCandidates,
    mergeCandidateLines,
    renderCandidateCards,
  };
});
