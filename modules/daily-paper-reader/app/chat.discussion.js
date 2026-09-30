// 私人研讨区模块：负责聊天 UI、LLM 配置与本地记忆（IndexedDB）
window.PrivateDiscussionChat = (function () {
  const apiUrl = (path) => {
    const base = String(window.DPR_LOCAL_API_BASE || '').trim().replace(/\/$/, '');
    return base + path;
  };
  const CHAT_HISTORY_KEY = 'dpr_chat_history_v1'; // 仅用于旧版本迁移
  const CHAT_DB_NAME = 'dpr_chat_db_v1';
  const CHAT_STORE_NAME = 'paper_chats';
  const CHAT_MODEL_PREF_KEY = 'dpr_chat_model_preference_v1';

  // 最近提问记录（仅本机 localStorage，从现在开始记录，不回溯历史聊天内容）
  const QUESTION_RECENT_KEY = 'dpr_chat_recent_questions_v1';
  const QUESTION_PINNED_KEY = 'dpr_chat_pinned_questions_v1';
  const MAX_RECENT_QUESTIONS = 10; // 展示与保存都只保留最近 10 个（用户诉求）
  const MAX_PINNED_QUESTIONS = 50; // 防止无限增长
  const paperTextCache = new Map();

  function shortenPaperText(text) {
    const limit = 60000;
    const head = 45000;
    const tail = 12000;
    if (!text || text.length <= limit) return text || '';
    return text.slice(0, head) + '\n\n（中间有一段这次没有带上。）\n\n' + text.slice(-tail);
  }

  function rememberPaperText(id, text) {
    if (!id || !text) return;
    if (!paperTextCache.has(id) && paperTextCache.size >= 2) {
      paperTextCache.delete(paperTextCache.keys().next().value);
    }
    paperTextCache.set(id, text);
  }

  const resizeChatInput = (input) => {
    if (!input) return;
    const style = window.getComputedStyle ? window.getComputedStyle(input) : null;
    const maxHeight = style ? parseFloat(style.maxHeight || '0') || 160 : 160;
    input.style.height = 'auto';
    const nextHeight = Math.min(input.scrollHeight, maxHeight);
    input.style.height = `${nextHeight}px`;
    input.style.overflowY = input.scrollHeight > maxHeight ? 'auto' : 'hidden';
  };

  // 读取用户偏好的 Chat 模型名称（跨页面生效）
  const loadPreferredModelName = () => {
    try {
      if (!window.localStorage) return '';
      const v = window.localStorage.getItem(CHAT_MODEL_PREF_KEY);
      return typeof v === 'string' ? v : '';
    } catch {
      return '';
    }
  };

  // 保存用户偏好的 Chat 模型名称
  const savePreferredModelName = (name) => {
    try {
      if (!window.localStorage) return;
      const v = (name || '').trim();
      if (!v) return;
      window.localStorage.setItem(CHAT_MODEL_PREF_KEY, v);
    } catch {
      // ignore
    }
  };

  // 每次提问都读当前配置，避免改完页面设置后还用旧模型。
  const resolveChatConfig = async () => {
    try {
      const resp = await fetch(apiUrl('/api/chat/config'));
      if (!resp.ok) return null;
      const data = await resp.json();
      return (data && data.model) ? data.model : null;
    } catch {
      return null;
    }
  };
  const inferChatApiProfile = (baseUrl, model) => {
    const utils = window.DPRLLMConfigUtils || {};
    if (typeof utils.inferChatApiProfile === 'function') {
      return utils.inferChatApiProfile(baseUrl, model);
    }
    const normalizedBaseUrl = String(baseUrl || '').trim().toLowerCase();
    const normalizedModel = String(model || '').trim().toLowerCase();
    if (
      /(^|\/\/)(api\.)?deepseek\.com(?:$|\/)/i.test(normalizedBaseUrl)
      || normalizedModel.startsWith('deepseek-')
    ) {
      return 'deepseek';
    }
    return 'unsupported';
  };
	  const buildStreamingChatPayload = (baseUrl, model, messages) => {
	    const utils = window.DPRLLMConfigUtils || {};
	    if (typeof utils.buildStreamingChatPayload === 'function') {
	      return utils.buildStreamingChatPayload({ baseUrl, model, messages });
	    }
	    const payload = {
	      model,
	      messages,
	      stream: true,
	    };
	    const normalizedModel = String(model || '').trim().toLowerCase();
	    const normalizedBaseUrl = String(baseUrl || '').trim().toLowerCase();
	    if (
	      (normalizedModel === 'deepseek-v4-flash' || normalizedModel === 'deepseek-v4-pro')
	      && /(^|\/\/)(api\.)?deepseek\.com(?:$|\/)/i.test(normalizedBaseUrl)
	    ) {
	      payload.max_tokens = 393216;
	    }
	    return payload;
	  };

  let chatDbPromise = null;

  const openChatDB = () => {
    if (chatDbPromise) return chatDbPromise;
    if (typeof indexedDB === 'undefined') {
      chatDbPromise = Promise.resolve(null);
      return chatDbPromise;
    }
    chatDbPromise = new Promise((resolve) => {
      try {
        const req = indexedDB.open(CHAT_DB_NAME, 1);
        req.onupgradeneeded = (event) => {
          const db = event.target.result;
          if (!db.objectStoreNames.contains(CHAT_STORE_NAME)) {
            db.createObjectStore(CHAT_STORE_NAME, { keyPath: 'paperId' });
          }
        };
        req.onsuccess = (event) => {
          const db = event.target.result;
          // 迁移旧版 localStorage 聊天记录
          try {
            if (window.localStorage) {
              const raw = window.localStorage.getItem(CHAT_HISTORY_KEY);
              if (raw) {
                const obj = JSON.parse(raw) || {};
                const tx = db.transaction(CHAT_STORE_NAME, 'readwrite');
                const store = tx.objectStore(CHAT_STORE_NAME);
                Object.keys(obj).forEach((pid) => {
                  const list = obj[pid];
                  if (pid && Array.isArray(list)) {
                    store.put({ paperId: pid, messages: list });
                  }
                });
                tx.oncomplete = () => {
                  window.localStorage.removeItem(CHAT_HISTORY_KEY);
                };
              }
            }
          } catch {
            // ignore
          }
          resolve(db);
        };
        req.onerror = () => resolve(null);
      } catch {
        resolve(null);
      }
    });
    return chatDbPromise;
  };

  const loadChatHistory = async (paperId) => {
    if (!paperId) return [];
    const db = await openChatDB();
    if (!db) {
      try {
        if (!window.localStorage) return [];
        const raw = window.localStorage.getItem(CHAT_HISTORY_KEY);
        if (!raw) return [];
        const obj = JSON.parse(raw);
        if (!obj || typeof obj !== 'object') return [];
        const list = obj[paperId];
        return Array.isArray(list) ? list : [];
      } catch {
        return [];
      }
    }
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(CHAT_STORE_NAME, 'readonly');
        const store = tx.objectStore(CHAT_STORE_NAME);
        const req = store.get(paperId);
        req.onsuccess = () => {
          const record = req.result;
          if (record && Array.isArray(record.messages)) {
            resolve(record.messages);
          } else {
            resolve([]);
          }
        };
        req.onerror = () => resolve([]);
      } catch {
        resolve([]);
      }
    });
  };

  const CHAT_HISTORY_KEEP = 60;

  const saveChatHistory = async (paperId, list) => {
    if (!paperId) return;
    if (Array.isArray(list) && list.length > CHAT_HISTORY_KEEP) {
      list = list.slice(-CHAT_HISTORY_KEEP);
    }
    const db = await openChatDB();
    if (!db) {
      try {
        if (!window.localStorage) return;
        const raw = window.localStorage.getItem(CHAT_HISTORY_KEY);
        const obj = raw ? JSON.parse(raw) || {} : {};
        obj[paperId] = list;
        window.localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(obj));
      } catch {
        // ignore
      }
      return;
    }
    try {
      const tx = db.transaction(CHAT_STORE_NAME, 'readwrite');
      const store = tx.objectStore(CHAT_STORE_NAME);
      store.put({ paperId, messages: list });
    } catch {
      // ignore
    }
  };

  const renderChatUI = () => {
    return `
      <div id="paper-chat-container">
        <div id="chat-history">
            <div style="text-align:center; color:#999">暂无讨论，输入你的想法开始对话（只留在这个浏览器里）</div>
        </div>
        <div class="input-area">
          <textarea id="user-input" rows="3" placeholder="针对这篇论文提问，仅自己可见..."></textarea>
          <div class="chat-input-actions">
            <button id="chat-questions-toggle-btn" class="chat-questions-toggle-btn" type="button" title="最近提问">🕘</button>
            <button id="send-btn">发送</button>
          </div>
        </div>
        <div id="chat-questions-panel" class="chat-questions-panel" style="display:none"></div>
        <div class="chat-footer">
          <div class="chat-footer-controls">
            <button id="chat-sidebar-toggle-btn" class="chat-footer-icon-btn" type="button">☰</button>
            <button id="chat-settings-toggle-btn" class="chat-footer-icon-btn" type="button">⚙️</button>
            <button id="chat-quick-run-btn" class="chat-footer-icon-btn" type="button" title="快速抓取">🚀</button>
            <div id="chat-quick-run-modal" class="chat-quick-run-modal" aria-hidden="true">
              <div class="chat-quick-run-modal-panel">
                <div class="chat-quick-run-modal-head">
                  <div class="chat-quick-run-title">快速抓取</div>
                  <button id="chat-quick-run-close-btn" class="chat-quick-run-close-btn" type="button" aria-label="关闭">✕</button>
                </div>
                <div class="chat-quick-run-row">
                  <label for="chat-quick-run-mode-select">运行模式</label>
                  <select id="chat-quick-run-mode-select">
                    <option value="auto">自动：十天及以内精读，再长就速览</option>
                    <option value="standard">标准精读</option>
                    <option value="skims">速览（全部进速读）</option>
                  </select>
                </div>
                <button id="chat-quick-run-10d-btn" class="chat-quick-run-item" type="button">立即搜寻十天内论文</button>
                <button id="chat-quick-run-30d-btn" class="chat-quick-run-item" type="button">立即搜寻三十天内论文</button>
                <label class="chat-quick-run-enrich" for="chat-quick-run-enrich-checkbox" title="打开后，这次抓取会先把订阅词扩成更多检索词。默认关闭。">
                  <input id="chat-quick-run-enrich-checkbox" type="checkbox" />
                  <span>抓取前先扩充检索词</span>
                </label>
              </div>
            </div>
          </div>
          <div id="chat-model-picker" class="chat-model-picker">
            <button
              id="chat-model-picker-btn"
              class="chat-model-picker-btn"
              type="button"
              aria-haspopup="listbox"
              aria-expanded="false"
            >
              <span class="chat-model-picker-kicker">模型</span>
              <span id="chat-model-picker-label" class="chat-model-picker-label">选择模型</span>
              <span class="chat-model-picker-chevron" aria-hidden="true">⌄</span>
            </button>
            <div
              id="chat-model-picker-menu"
              class="chat-model-picker-menu"
              role="listbox"
              aria-label="选择 Chat 模型"
            ></div>
            <select
              id="chat-llm-model-select"
              class="chat-model-select"
              aria-hidden="true"
              tabindex="-1"
            ></select>
          </div>
          <span id="chat-status" class="chat-status"></span>
        </div>
      </div>
    `;
  };

  const QUICK_RUN_CONFERENCES = [
    'ACL',
    'AAAI',
    'COLING',
    'EMNLP',
    'ICCV',
    'ICLR',
    'ICML',
    'IJCAI',
    'NeurIPS',
    'OSDI',
    'SOSP',
    'S&P',
    'NDSS',
    'SIGIR',
  ];

  const fillQuickRunOptions = (yearSelectEl, confSelectEl) => {
    if (yearSelectEl && !yearSelectEl._dprQuickRunOptionsFilled) {
      yearSelectEl._dprQuickRunOptionsFilled = true;
      const currentYear = new Date().getFullYear();
      for (let y = currentYear; y >= currentYear - 8; y -= 1) {
        const opt = document.createElement('option');
        opt.value = String(y);
        opt.textContent = String(y);
        yearSelectEl.appendChild(opt);
      }
    }

    if (confSelectEl && !confSelectEl._dprQuickRunOptionsFilled) {
      confSelectEl._dprQuickRunOptionsFilled = true;
      QUICK_RUN_CONFERENCES.forEach((name) => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        confSelectEl.appendChild(opt);
      });
    }
  };

  const resolveQuickRunYear = (value) => {
    const y = parseInt(value, 10);
    if (!Number.isFinite(y) || y <= 0) {
      return '';
    }
    return String(y);
  };

  const runQuickFetch = (days, statusEl, showToast = () => {}) => {
    if (!window.DPRWorkflowRunner || typeof window.DPRWorkflowRunner.runQuickFetchByDays !== 'function') {
      if (statusEl) {
        statusEl.textContent = '工作流触发器未加载到当前页面。';
        statusEl.style.color = '#c00';
      }
      return;
    }
    // run_enrich 偏好由 workflows.runner 统一读取本地存储（设置面板与弹窗共用），
    // 这里不显式传值，避免弹窗内勾选状态过期时覆盖设置面板的最新偏好
    const modeEl = document.getElementById('chat-quick-run-mode-select');
    const fetchMode = modeEl ? String(modeEl.value || '').trim() : '';
    const options = {};
    if (fetchMode === 'standard' || fetchMode === 'skims') {
      options.fetchMode = fetchMode;
    }
    window.DPRWorkflowRunner.runQuickFetchByDays(days, options);
    showToast();
  };

  const runQuickConferencePlaceholder = (yearSelectEl, confSelectEl, msgEl, statusEl) => {
    const year = resolveQuickRunYear(yearSelectEl ? yearSelectEl.value : '');
    const conf = confSelectEl ? String(confSelectEl.value || '').trim() : '';
    if (!year || !conf) {
      if (msgEl) {
        msgEl.textContent = '请先选择年份和会议名。';
        msgEl.style.color = '#c00';
      }
      return;
    }
    if (msgEl) {
      msgEl.textContent = `${year} ${conf} 的会议论文抓取功能暂未接入。`;
      msgEl.style.color = '#c90';
    }
    if (statusEl) {
      statusEl.textContent = `${year} ${conf} 的会议论文抓取入口先保留。`;
      statusEl.style.color = '#c90';
    }
  };

  const getQuickRunModal = () => document.getElementById('chat-quick-run-modal');

  const safeLoadList = (key) => {
    try {
      if (!window.localStorage) return [];
      const raw = window.localStorage.getItem(key);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : [];
    } catch {
      return [];
    }
  };

  const safeSaveList = (key, list) => {
    try {
      if (!window.localStorage) return;
      window.localStorage.setItem(key, JSON.stringify(list || []));
    } catch {
      // ignore
    }
  };

  const normalizeQuestion = (text) => {
    const s = String(text || '')
      .replace(/\s+/g, ' ')
      .trim();
    if (!s) return '';
    // 防止异常超长内容把 UI 撑爆
    if (s.length > 500) return s.slice(0, 500);
    return s;
  };

  const getPinnedQuestions = () => safeLoadList(QUESTION_PINNED_KEY);
  const setPinnedQuestions = (list) =>
    safeSaveList(QUESTION_PINNED_KEY, (list || []).slice(0, MAX_PINNED_QUESTIONS));

  const getRecentQuestions = () => safeLoadList(QUESTION_RECENT_KEY);
  const setRecentQuestions = (list) =>
    safeSaveList(QUESTION_RECENT_KEY, (list || []).slice(0, MAX_RECENT_QUESTIONS));

  let quickRunPanelController = null;

  const recordRecentQuestion = (question) => {
    const q = normalizeQuestion(question);
    if (!q) return;

    const pinned = getPinnedQuestions();
    // 已钉住的就不再重复进入 recent（避免重复）
    if (pinned.includes(q)) return;

    const recent = getRecentQuestions().filter((x) => x !== q);
    recent.unshift(q);
    setRecentQuestions(recent);
  };

  const togglePinQuestion = (question) => {
    const q = normalizeQuestion(question);
    if (!q) return;
    const pinned = getPinnedQuestions();
    const idx = pinned.indexOf(q);
    if (idx >= 0) {
      pinned.splice(idx, 1);
      setPinnedQuestions(pinned);
      return;
    }

    pinned.unshift(q);
    setPinnedQuestions(pinned);
    // 钉住后从 recent 移除（保证“置顶 + recent 仍展示 10 个其它问题”）
    const recent = getRecentQuestions().filter((x) => x !== q);
    setRecentQuestions(recent);
  };

  const getChatRoot = () => {
    const el = document.getElementById('paper-chat-container');
    return el || null;
  };

  const getQuestionsPanel = (root) => {
    const r = root || getChatRoot();
    if (!r) return null;
    return r.querySelector('#chat-questions-panel');
  };

  const closeQuestionsPanelElement = (panel) => {
    if (!panel) return;
    if (panel.style.display === 'none') return;
    if (panel._closingTimer) {
      clearTimeout(panel._closingTimer);
    }
    panel.classList.add('is-closing');
    panel._closingTimer = setTimeout(() => {
      panel.style.display = 'none';
      panel.classList.remove('is-closing');
      panel._closingTimer = null;
    }, 170);
  };

  const closeQuestionsPanel = (root) => {
    closeQuestionsPanelElement(getQuestionsPanel(root));
  };

  const isQuestionsPanelOpen = (root) => {
    const panel = getQuestionsPanel(root);
    if (!panel) return false;
    return panel.style.display !== 'none';
  };

  const renderQuestionsPanel = (root) => {
    const panel = getQuestionsPanel(root);
    if (!panel) return;
    panel.innerHTML = '';

    const pinned = getPinnedQuestions();
    const recent = getRecentQuestions().filter((q) => !pinned.includes(q));

    const header = document.createElement('div');
    header.className = 'chat-q-header';

    const title = document.createElement('div');
    title.className = 'chat-q-title';
    title.textContent = '最近提问';

    const closeBtn = document.createElement('button');
    closeBtn.id = 'chat-q-close';
    closeBtn.className = 'chat-q-close';
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', '关闭');
    closeBtn.textContent = '✕';

    header.appendChild(title);
    header.appendChild(closeBtn);
    panel.appendChild(header);

    const buildSection = (label, items, pinnedFlag) => {
      const sec = document.createElement('div');
      sec.className = 'chat-q-section';

      const secTitle = document.createElement('div');
      secTitle.className = 'chat-q-section-title';
      secTitle.textContent = label;
      sec.appendChild(secTitle);

      const list = document.createElement('div');
      list.className = 'chat-q-list';

      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'chat-q-empty';
        empty.textContent = pinnedFlag
          ? '暂无钉住的问题'
          : '暂无最近问题（从现在开始记录）';
        list.appendChild(empty);
      } else {
        items.forEach((q) => {
          const item = document.createElement('div');
          item.className = `chat-q-item${pinnedFlag ? ' is-pinned' : ''}`;
          item.dataset.q = q;

          const useBtn = document.createElement('button');
          useBtn.className = 'chat-q-use';
          useBtn.type = 'button';
          useBtn.title = '填入输入框';
          useBtn.textContent = q;

          const pinBtn = document.createElement('button');
          pinBtn.className = 'chat-q-pin';
          pinBtn.type = 'button';
          pinBtn.title = pinnedFlag ? '取消钉住' : '钉住';
          pinBtn.textContent = pinnedFlag ? '📌' : '📍';

          item.appendChild(useBtn);
          item.appendChild(pinBtn);
          list.appendChild(item);
        });
      }

      sec.appendChild(list);
      panel.appendChild(sec);
    };

    buildSection('📌 已钉住', pinned, true);
    buildSection('🕘 最近 10 条', recent.slice(0, MAX_RECENT_QUESTIONS), false);
  };

  const openQuestionsPanel = (root) => {
    const panel = getQuestionsPanel(root);
    if (!panel) return;
    if (panel._closingTimer) {
      clearTimeout(panel._closingTimer);
      panel._closingTimer = null;
    }
    renderQuestionsPanel(root);
    panel.classList.remove('is-closing');
    panel.style.display = 'block';
  };

  const toggleQuestionsPanel = (root) => {
    if (isQuestionsPanelOpen(root)) closeQuestionsPanel(root);
    else openQuestionsPanel(root);
  };

  let questionsGlobalBound = false;
  const bindQuestionsPanelEventsOnce = () => {
    const root = getChatRoot();
    if (!root) return;

    const btn = root.querySelector('#chat-questions-toggle-btn');
    if (btn && !btn._boundQToggle) {
      btn._boundQToggle = true;
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleQuestionsPanel(root);
      });
    }

    // 面板内部事件委托
    if (!root._boundQPanelClick) {
      root._boundQPanelClick = true;
      root.addEventListener('click', (e) => {
        const panel = getQuestionsPanel(root);
        if (!panel || panel.style.display === 'none') return;

        const closeBtn =
          e.target && e.target.closest ? e.target.closest('#chat-q-close') : null;
        if (closeBtn) {
          e.preventDefault();
          closeQuestionsPanel(root);
          return;
        }

        const pinBtn =
          e.target && e.target.closest ? e.target.closest('.chat-q-pin') : null;
        if (pinBtn) {
          const item =
            e.target && e.target.closest ? e.target.closest('.chat-q-item') : null;
          const q = item ? item.dataset.q : '';
          togglePinQuestion(q);
          renderQuestionsPanel(root);
          e.preventDefault();
          e.stopPropagation();
          return;
        }

        const useBtn =
          e.target && e.target.closest ? e.target.closest('.chat-q-use') : null;
        if (useBtn) {
          const item =
            e.target && e.target.closest ? e.target.closest('.chat-q-item') : null;
          const q = item ? item.dataset.q : '';
          const input = root.querySelector('#user-input');
          if (input && q) {
            input.value = q;
            resizeChatInput(input);
            input.focus();
          }
          // 选择某一项后自动关闭面板
          closeQuestionsPanel(root);
          e.preventDefault();
          e.stopPropagation();
          return;
        }
      });
    }

    if (questionsGlobalBound) return;
    questionsGlobalBound = true;

    // 面板外关闭：用 pointerdown（鼠标左键按下就关闭；触摸也会关闭）
    document.addEventListener(
      'pointerdown',
      (e) => {
        // 可能存在重复渲染导致的多个 chat 容器，这里对“所有打开的面板”做统一处理
        const panels = Array.from(
          document.querySelectorAll('#paper-chat-container .chat-questions-panel'),
        );
        const openPanels = panels.filter((p) => p && p.style.display !== 'none');
        if (!openPanels.length) return;

        // 仅鼠标左键触发（右键/中键不处理）
        if (e && e.pointerType === 'mouse' && typeof e.button === 'number') {
          if (e.button !== 0) return;
        }

        const insideChat =
          e.target && e.target.closest
            ? e.target.closest('#paper-chat-container')
            : null;
        if (!insideChat) {
          openPanels.forEach((p) => {
            try {
              closeQuestionsPanelElement(p);
            } catch {
              // ignore
            }
          });
        }
      },
      true,
    );

    // ESC 关闭
    document.addEventListener('keydown', (e) => {
      if (e && e.key === 'Escape') closeQuestionsPanel(null);
    });
  };

  const renderHistoryMessage = (msg, renderMarkdownWithTables, renderMathInEl) => {
    const item = document.createElement('div');
    item.className = 'msg-item';

    const role = (msg.role || '').toLowerCase();
    const isThinking = role === 'thinking';
    const isAi = role === 'ai' || role === 'assistant' || isThinking;
    const isUser = role === 'user';

    if (!isThinking) {
      if (isUser && msg.time) {
        const timeSpan = document.createElement('span');
        timeSpan.className = 'msg-time msg-time-user';
        timeSpan.textContent = msg.time;
        item.appendChild(timeSpan);
      }

      const contentDiv = document.createElement('div');
      contentDiv.className =
        'msg-content ' + (isAi ? 'msg-content-ai' : 'msg-content-user');
      const markdown = msg.content || '';

      if (isUser || !renderMarkdownWithTables) {
        contentDiv.textContent = markdown;
      } else {
        contentDiv.innerHTML = renderMarkdownWithTables(markdown);
        if (renderMathInEl) renderMathInEl(contentDiv);
      }

      item.appendChild(contentDiv);
      return item;
    }

    if (msg.time) {
      const timeSpan = document.createElement('span');
      timeSpan.className = 'msg-time msg-time-ai';
      timeSpan.textContent = msg.time;
      item.appendChild(timeSpan);
    }

    const thinkingContainer = document.createElement('div');
    thinkingContainer.className = 'thinking-history-container';

    const thinkingHeader = document.createElement('div');
    thinkingHeader.className = 'thinking-history-header';
    const titleSpan = document.createElement('span');
    titleSpan.textContent = '思考过程';
    const toggleBtn = document.createElement('button');
    toggleBtn.className = 'thinking-history-toggle';
    toggleBtn.textContent = '展开';
    thinkingHeader.appendChild(titleSpan);
    thinkingHeader.appendChild(toggleBtn);

    const thinkingContent = document.createElement('div');
    thinkingContent.className =
      'msg-content thinking-history-content thinking-collapsed';
    const markdown = msg.content || '';
    if (renderMarkdownWithTables) {
      thinkingContent.innerHTML = renderMarkdownWithTables(markdown);
    } else {
      thinkingContent.textContent = markdown;
    }

    thinkingContainer.appendChild(thinkingHeader);
    thinkingContainer.appendChild(thinkingContent);

    toggleBtn.addEventListener('click', () => {
      const collapsed = thinkingContent.classList.toggle('thinking-collapsed');
      toggleBtn.textContent = collapsed ? '展开' : '折叠';
      if (!collapsed && renderMathInEl && !thinkingContent.dataset.mathReady) {
        thinkingContent.dataset.mathReady = '1';
        renderMathInEl(thinkingContent);
      }
    });

    item.appendChild(thinkingContainer);
    return item;
  };

  const renderHistory = async (paperId) => {
    const historyDiv = document.getElementById('chat-history');
    if (!historyDiv) return;

    const data = await loadChatHistory(paperId);
    if (!data || !data.length) {
      historyDiv.innerHTML =
        '<div style="text-align:center; color:#999">暂无讨论，输入上方问题开始提问。</div>';
      return;
    }

    const { renderMarkdownWithTables, renderMathInEl } = window.DPRMarkdown || {};
    historyDiv.innerHTML = '';
    const RECENT_MESSAGES = 12;
    const older = data.length > RECENT_MESSAGES ? data.slice(0, -RECENT_MESSAGES) : [];
    const visible = older.length ? data.slice(-RECENT_MESSAGES) : data;
    if (older.length) {
      const earlier = document.createElement('button');
      earlier.type = 'button';
      earlier.className = 'thinking-history-toggle';
      earlier.textContent = `更早的 ${older.length} 条`;
      earlier.addEventListener('click', () => {
        const spot = document.createElement('div');
        earlier.replaceWith(spot);
        let index = 0;
        const step = () => {
          const end = Math.min(index + 4, older.length);
          const fragment = document.createDocumentFragment();
          for (; index < end; index += 1) {
            fragment.appendChild(renderHistoryMessage(older[index], renderMarkdownWithTables, renderMathInEl));
          }
          spot.before(fragment);
          if (index < older.length) {
            setTimeout(step, 0);
          } else {
            spot.remove();
          }
        };
        step();
      });
      historyDiv.appendChild(earlier);
    }
    visible.forEach((msg) => {
      historyDiv.appendChild(renderHistoryMessage(msg, renderMarkdownWithTables, renderMathInEl));
    });

    historyDiv.scrollTop = historyDiv.scrollHeight;

    // 同时更新问题导航
    ensureQuestionNavContainer();
    renderQuestionNav(paperId);

    // 聊天历史渲染完成后，通知 Zotero 元数据刷新一次（包含最新对话）
    try {
      if (window.DPRZoteroMeta && window.DPRZoteroMeta.updateFromPage) {
        // vm.route.file 在前端不可见，这里只传 paperId，后端函数会使用当前路由
        window.DPRZoteroMeta.updateFromPage(paperId);
      }
    } catch {
      // 忽略刷新失败
    }
  };

  const ensureQuestionNavContainer = () => {};

  const renderQuestionNav = () => {};

  const sendMessage = async (paperId) => {
    // 游客模式或尚未解锁密钥时，禁止直接调用大模型
    if (window.DPR_ACCESS_MODE === 'guest' || window.DPR_ACCESS_MODE === 'locked') {
      const statusEl = document.getElementById('chat-status');
      if (statusEl) {
        statusEl.textContent =
          '当前为游客模式或尚未解锁密钥，无法直接与大模型对话。';
        statusEl.style.color = '#c00';
      }
      const historyDiv = document.getElementById('chat-history');
      if (historyDiv && !historyDiv._guestHintShown) {
        historyDiv._guestHintShown = true;
        historyDiv.innerHTML =
          '<div style="text-align:center; color:#999; padding:8px 0;">当前为游客模式，解锁密钥后可启用大模型对话。</div>';
      }
      return;
    }
    const input = document.getElementById('user-input');
    const btn = document.getElementById('send-btn');
    const statusEl = document.getElementById('chat-status');

    if (!input || !btn) {
      if (statusEl) {
        statusEl.textContent = '聊天输入框未就绪，请刷新页面重试。';
        statusEl.style.color = '#c00';
      }
      return;
    }

    const question = input.value.trim();
    let paperContent = paperId ? paperTextCache.get(paperId) || '' : '';

    if (!question) {
      if (statusEl) {
        statusEl.textContent = '请输入问题后再发送。';
        statusEl.style.color = '#c00';
      }
      return;
    }

    // 同一篇论文的正文只取一次。换一篇会用新的 paperId。
    if (!paperContent && paperId && paperId.indexOf('..') < 0 && paperId.indexOf('\\') < 0) {
      try {
        const txtUrl = 'docs/' + paperId.split('/').filter(Boolean).map(encodeURIComponent).join('/') + '.txt';
        const resp = await fetch(txtUrl);
        if (resp.ok) {
          const txt = await resp.text();
          if (txt && txt.trim()) paperContent = txt;
        }
      } catch {
        paperContent = '';
      }
    }

    // 回退策略：如果 .txt 不存在，就用页面正文纯文本
    if (!paperContent) {
      const section = document.querySelector('.markdown-section');
      if (section) {
        const clone = section.cloneNode(true);
        const chat = clone.querySelector('#paper-chat-container');
        if (chat) chat.remove();
        // 副本不在页面上，innerText 会是空的，问答就读不到这篇论文。
        paperContent = String(clone.textContent || '').trim();
      }
    }
    paperContent = shortenPaperText(paperContent);
    rememberPaperText(paperId, paperContent);

    if (!question) return;

    // 从现在开始记录“最近提问”（只记录用户输入；不回溯旧聊天）
    recordRecentQuestion(question);
    // 如果面板开着，顺手刷新一下列表（体验更顺滑）
    if (isQuestionsPanelOpen(null)) {
      renderQuestionsPanel(null);
    }

    const stopSending = (message) => {
      if (statusEl && message) {
        statusEl.textContent = message;
        statusEl.style.color = '#c00';
      }
      input.disabled = false;
      btn.disabled = false;
      btn.innerText = '发送';
    };

    input.disabled = true;
    btn.disabled = true;
    btn.innerText = '思考中...';

    const historyDiv = document.getElementById('chat-history');
    if (!historyDiv) {
      stopSending('这次没有发出去，请稍后重试。');
      return;
    }
    const nowStr = new Date().toLocaleString();
    // 立刻用“气泡样式”渲染用户消息（避免等刷新后才套上 msg-content-user）
    try {
      const userItem = document.createElement('div');
      userItem.className = 'msg-item';

      const time = document.createElement('span');
      time.className = 'msg-time msg-time-user';
      time.textContent = nowStr;

      const content = document.createElement('div');
      content.className = 'msg-content msg-content-user';
      content.textContent = question;

      userItem.appendChild(time);
      userItem.appendChild(content);
      historyDiv.appendChild(userItem);
    } catch {
      // 回退：至少不要把用户输入当作 HTML 注入
      const userItem = document.createElement('div');
      userItem.className = 'msg-item';
      const content = document.createElement('div');
      content.className = 'msg-content msg-content-user';
      content.textContent = question;
      userItem.appendChild(content);
      historyDiv.appendChild(userItem);
    }
    historyDiv.scrollTop = historyDiv.scrollHeight;

    const aiItem = document.createElement('div');
    aiItem.className = 'msg-item';
    aiItem.innerHTML = `
        <span class="msg-time msg-time-ai">${nowStr}</span>
        <div class="ai-response-header">
          <span class="ai-thinking-indicator">
            <span class="dot"></span>
            <span class="dot"></span>
            <span class="dot"></span>
          </span>
        </div>
        <div class="thinking-container" style="margin-top:8px; border-left:3px solid #ddd; padding-left:8px; font-size:0.85rem; color:#666; display:none;">
          <div style="display:flex; align-items:center; justify-content:space-between;">
            <span>思考过程</span>
            <button class="thinking-toggle" style="margin-left:8px; font-size:0.75rem; padding:2px 6px;">展开</button>
          </div>
          <div class="thinking-content" style="white-space:pre-wrap; margin-top:4px;"></div>
        </div>
        <div class="msg-content msg-content-ai"></div>
    `;
    historyDiv.appendChild(aiItem);

    // 判断用户是否在页面底部（允许 50px 误差）
    let userAtBottom = true;
    const checkIfAtBottom = () => {
      const threshold = 50;
      const scrollTop = window.pageYOffset || document.documentElement.scrollTop;
      const windowHeight = window.innerHeight;
      const docHeight = document.documentElement.scrollHeight;
      return docHeight - scrollTop - windowHeight <= threshold;
    };
    userAtBottom = checkIfAtBottom();

    // 监听用户滚动，更新 userAtBottom 状态
    const onUserScroll = () => {
      userAtBottom = checkIfAtBottom();
    };
    if (window.__dprChatScroll) window.removeEventListener('scroll', window.__dprChatScroll);
    window.__dprChatScroll = onUserScroll;
    window.addEventListener('scroll', onUserScroll, { passive: true });

    // 跟着最新一句走。不用平滑滚动，否则每个字都会排一次动画。
    const onThisPage = () => aiItem.isConnected;
    const scrollToBottomIfNeeded = () => {
      if (onThisPage() && userAtBottom) window.scrollTo(0, document.documentElement.scrollHeight);
    };

    // 发送消息后立即滚动到底部。已经换到别的论文时，不再带动当前页面。
    if (onThisPage()) window.scrollTo(0, document.documentElement.scrollHeight);

    const thinkingContainer = aiItem.querySelector('.thinking-container');
    const thinkingContent = aiItem.querySelector('.thinking-content');
    const toggleBtn = aiItem.querySelector('.thinking-toggle');
    const aiAnswerDiv = aiItem.querySelector('.msg-content');

    let history;
    try {
      history = await loadChatHistory(paperId);
      history.push({
        role: 'user',
        content: question,
        time: nowStr,
      });
      await saveChatHistory(paperId, history);
    } catch (sendErr) {
      console.error(sendErr);
      stopSending('这次没有发出去，请稍后重试。');
      return;
    }

    // 更新问题导航（新增了用户提问）
    renderQuestionNav(paperId);

    // 给刚添加的用户消息设置 ID（用于问题导航定位）
    const userMessages = historyDiv.querySelectorAll('.msg-content-user');
    if (userMessages.length > 0) {
      const lastUserItem = userMessages[userMessages.length - 1].closest('.msg-item');
      if (lastUserItem && !lastUserItem.id) {
        const userQuestionCount = history.filter(m => m.role === 'user').length;
        lastUserItem.id = `user-question-${userQuestionCount - 1}`;
      }
    }

    // 用户发起提问后，立即刷新一次 Zotero 摘要（包含最新提问）
    try {
      if (window.DPRZoteroMeta && window.DPRZoteroMeta.updateFromPage) {
        window.DPRZoteroMeta.updateFromPage(paperId);
      }
    } catch {
      // 忽略刷新失败
    }

    let model;
    try {
      model = await resolveChatConfig();
    } catch (sendErr) {
      console.error(sendErr);
      stopSending('这次没有发出去，请稍后重试。');
      return;
    }
    const modelSelect = document.getElementById('chat-llm-model-select');
    if (model && modelSelect && onThisPage()) {
      const known = Array.from(modelSelect.options).some((opt) => opt.value === model);
      if (!known) {
        const opt = document.createElement('option');
        opt.value = model;
        opt.textContent = model;
        modelSelect.appendChild(opt);
      }
      modelSelect.value = model;
      syncChatModelPicker([model]);
    }

    if (!model) {
      aiAnswerDiv.textContent =
        '还没有可用的对话模型。请打开页面设置，填写模型后再试。';
      if (statusEl) {
        statusEl.textContent =
          '还没有可用的对话模型。';
        statusEl.style.color = '#c00';
      }
      stopSending();
      return;
    }

    // 记录当前使用的模型为用户偏好，供后续页面复用
    try { savePreferredModelName(model); } catch (prefErr) { console.error(prefErr); }

    if (statusEl) {
      statusEl.textContent = `正在用 ${model} 回答`;
      statusEl.style.color = '#666';
    }
    let thinkingBuffer = '';
    let answerBuffer = '';
    // 默认以折叠模式展示思考过程，仅显示前若干行
    let thinkingCollapsed = true;
    let renderTimer = null;

    const { renderMarkdownWithTables, renderMathInEl } =
      window.DPRMarkdown || {};

    const applyThinkingView = (withMath) => {
      if (!thinkingBuffer || !thinkingContent) return;
      const source = thinkingBuffer;
      const maxLines = 6;
      let toRender = source;

      if (thinkingCollapsed) {
        const lines = source.split('\n');
        if (lines.length > maxLines) {
          toRender =
            lines.slice(0, maxLines).join('\n') +
            '\n...（已折叠，点击展开查看更多思考过程）';
        }
      }

      if (renderMarkdownWithTables) {
        thinkingContent.innerHTML = renderMarkdownWithTables(toRender);
      } else {
        thinkingContent.textContent = toRender;
      }
      if (withMath !== false && renderMathInEl) {
        renderMathInEl(thinkingContent);
      }
    };

    const applyAnswerView = (withMath) => {
      if (!aiAnswerDiv) return;
      const content = answerBuffer || '（空响应）';
      if (renderMarkdownWithTables) {
        aiAnswerDiv.innerHTML = renderMarkdownWithTables(content);
      } else {
        aiAnswerDiv.textContent = content;
      }
      if (withMath !== false && renderMathInEl) {
        renderMathInEl(aiAnswerDiv);
      }
    };

    if (toggleBtn && thinkingContainer) {
      toggleBtn.addEventListener('click', () => {
        thinkingCollapsed = !thinkingCollapsed;
        toggleBtn.textContent = thinkingCollapsed ? '展开' : '折叠';
        applyThinkingView();
      });
    }

    const paintChat = (withMath) => {
      if (!onThisPage()) return;
      if (thinkingBuffer && thinkingContainer) {
        thinkingContainer.style.display = 'block';
        applyThinkingView(withMath);
      }
      if (answerBuffer) applyAnswerView(withMath);
      scrollToBottomIfNeeded();
    };

    const scheduleRender = () => {
      if (renderTimer) return;
      // 出字时先排文字。每个字都重排公式会把页面卡住。
      renderTimer = setTimeout(() => {
        renderTimer = null;
        paintChat(false);
      }, 250);
    };

    const finishChatRender = () => {
      if (renderTimer) {
        clearTimeout(renderTimer);
        renderTimer = null;
      }
      paintChat(true);
    };

    let stopChatTimers = function () {};
    try {
      const messages = [];
      messages.push({
        role: 'system',
        content:
          '你是学术讨论助手，负责围绕当前论文内容进行深入分析与讨论。请使用中文回答，并使用 Markdown + LaTeX 表达公式。',
      });
      // 使用全文上下文（优先 .txt 抽取结果），不再做 8000 字截断
      if (paperContent) {
        messages.push({
          role: 'user',
          content: `下面是当前论文的完整纯文本内容（可能包含自动抽取噪声，仅供参考）：\n\n${paperContent}`,
        });
      }

      const prev = await loadChatHistory(paperId);
      const conversational = [];
      prev.forEach((m) => {
        if (m.role === 'user' || m.role === 'ai') {
          conversational.push({
            role: m.role === 'ai' ? 'assistant' : 'user',
            content: m.content || '',
          });
        }
      });
      // 页面上仍保留更早的问答。发给模型的只留最近几轮，和服务器一致。
      const recent = conversational.slice(-8);
      const includedQuestion = recent.some((m) => m.role === 'user' && m.content === question);
      recent.forEach((m) => messages.push(m));
      if (!includedQuestion) {
        messages.push({
          role: 'user',
          content: question,
        });
      }

      const controller = new AbortController();
      const idleMs = 90000;
      const hardMs = 360000;
      const startedAt = Date.now();
      let idleTimer = null;
      const armIdle = () => {
        if (idleTimer) clearTimeout(idleTimer);
        const left = hardMs - (Date.now() - startedAt);
        if (left <= 0) {
          controller.abort();
          return;
        }
        idleTimer = setTimeout(() => controller.abort(), Math.min(idleMs, left));
      };
      stopChatTimers = () => {
        if (idleTimer) {
          clearTimeout(idleTimer);
          idleTimer = null;
        }
      };
      armIdle();
      let resp = null;

      const primaryPayload = buildStreamingChatPayload('', model, messages);
      const fallbackPayload = {
        model,
        messages,
        stream: true,
      };
      if (primaryPayload && primaryPayload.max_tokens) {
        fallbackPayload.max_tokens = primaryPayload.max_tokens;
      }

      const doChatFetch = async (payload) => fetch(apiUrl('/api/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify(payload),
      });

      resp = await doChatFetch(primaryPayload);
      if (
        resp
        && !resp.ok
        && (
          JSON.stringify(primaryPayload).includes('"reasoning"')
          || JSON.stringify(primaryPayload).includes('"extra_body"')
          || JSON.stringify(primaryPayload).includes('"thinking"')
        )
      ) {
        let retryText = '';
        try {
          retryText = await resp.text();
        } catch {
          retryText = '';
        }
        if (
          resp.status === 400
          && /reasoning|extra_body|return_reasoning|thinking/i.test(retryText)
        ) {
          resp = await doChatFetch(fallbackPayload);
        } else {
          resp._dprErrorPreview = retryText;
        }
      }

      if (!resp) {
        throw new Error('chat response missing');
      }

      if (!resp.ok) {
        let errorText = '';
        try {
          errorText = resp._dprErrorPreview || await resp.text();
        } catch {
          errorText = '';
        }
        const preview = (errorText || '').slice(0, 300).replace(/\s+/g, ' ');
        console.error(
          '[DPR CHAT] Chat API 调用失败：',
          `HTTP ${resp.status} ${resp.statusText || ''}`,
          preview ? `| 响应内容片段: ${preview}` : '',
        );
        const chatError = resp.status === 401 || resp.status === 403
          ? '模型没有接受这次请求。可以换一个模型名，或稍后再试。'
          : resp.status === 429
            ? '模型暂时忙，请稍后再问。'
            : '这次没有答上来，请稍后重试。';
        let shown = chatError;
        try {
          const parsed = JSON.parse(errorText || '');
          const serverMsg = parsed && typeof parsed.error === 'string' ? parsed.error : '';
          if (serverMsg && serverMsg.length <= 180 && !/https?:\/\/|Traceback|\.py\b/.test(serverMsg)) {
            shown = serverMsg;
          }
        } catch (parseErr) { /* 不是 JSON 就用上面的短句 */ }
        aiAnswerDiv.textContent = shown;
        if (statusEl) {
          statusEl.textContent = shown;
          statusEl.style.color = '#c00';
        }
        return;
      }

      if (!resp.body) {
        // 回退：如果不支持流，则按一次性响应处理
        const data = await resp.json();
        const answer =
          data &&
          data.choices &&
          data.choices[0] &&
          data.choices[0].message &&
          data.choices[0].message.content
            ? data.choices[0].message.content
            : '这次没有答上来，请稍后重试。';
        answerBuffer = answer;
        finishChatRender();
      } else {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          armIdle();
          buffer += decoder.decode(value, { stream: true });

          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';

          for (const part of parts) {
            const line = part.trim();
            if (!line || !line.startsWith('data:')) continue;
            const jsonStr = line.replace(/^data:\s*/, '');
            if (jsonStr === '[DONE]') continue;
            let payload;
            try {
              payload = JSON.parse(jsonStr);
            } catch {
              continue;
            }
            const choice =
              payload.choices && payload.choices[0]
                ? payload.choices[0]
                : null;
            const delta = choice ? choice.delta || {} : {};
            const reasoning =
              delta.reasoning_content || delta.thinking || '';
            const contentPiece = delta.content || '';

            if (reasoning) {
              thinkingBuffer += reasoning;
            }
            if (contentPiece) {
              answerBuffer += contentPiece;
            }
            if (reasoning || contentPiece) {
              scheduleRender();
            }
          }
        }
        finishChatRender();
      }

      // 回复完成，移除思考动画及其容器
      const responseHeader = aiItem.querySelector('.ai-response-header');
      if (responseHeader) {
        responseHeader.remove();
      }

      const nowStrAnswer = new Date().toLocaleString();
      const visibleAnswer = answerBuffer.trim() || thinkingBuffer.trim();
      if (!visibleAnswer) {
        aiAnswerDiv.textContent = '这次没有答上来，请稍后重试。';
        if (statusEl) {
          statusEl.textContent = '这次没有答上来，请稍后重试。';
          statusEl.style.color = '#c00';
        }
        return;
      }
      if (!answerBuffer.trim()) {
        answerBuffer = visibleAnswer;
        applyAnswerView();
      }
      const updated = await loadChatHistory(paperId);
      if (thinkingBuffer.trim() && answerBuffer.trim() && thinkingBuffer.trim() !== answerBuffer.trim()) {
        updated.push({
          role: 'thinking',
          content: thinkingBuffer,
          time: nowStrAnswer,
        });
      }
      updated.push({
        role: 'ai',
        content: answerBuffer.trim(),
        time: nowStrAnswer,
      });
      await saveChatHistory(paperId, updated);

      // 新一轮对话完成后，再次刷新 Zotero 元数据
      try {
        if (window.DPRZoteroMeta && window.DPRZoteroMeta.updateFromPage) {
          window.DPRZoteroMeta.updateFromPage(paperId);
        }
      } catch {
        // 忽略刷新失败
      }

      if (statusEl) {
        statusEl.textContent = '已回答。';
        statusEl.style.color = '#4caf50';
      }

      input.value = '';
      resizeChatInput(input);
    } catch (e) {
      console.error(e);
      const isTimeout =
        e &&
        (e.name === 'AbortError' ||
          e.name === 'TimeoutError' ||
          /timed out|timed_out/i.test((e.message || '')));
      if (isTimeout) {
        const kept = (answerBuffer || thinkingBuffer || '').trim();
        if (kept) {
          if (!answerBuffer.trim()) answerBuffer = kept;
          finishChatRender();
          if (statusEl) {
            statusEl.textContent = '后面的回答没有继续，已显示前面收到的部分。';
            statusEl.style.color = '#c90';
          }
        } else {
          aiAnswerDiv.textContent = '这次等太久了，请稍后重试。';
          if (statusEl) {
            statusEl.textContent = '回答超时，请稍后重试。';
            statusEl.style.color = '#c00';
          }
        }
      } else if (e && e.name === 'TypeError') {
        aiAnswerDiv.textContent = '没有连上问答服务，请稍后重试。';
        if (statusEl) {
          statusEl.textContent = '没有连上问答服务，请稍后重试。';
          statusEl.style.color = '#c00';
        }
      } else {
        aiAnswerDiv.textContent = '这次没有发出去，请稍后重试。';
        if (statusEl) {
          statusEl.textContent = '这次没有发出去，请稍后重试。';
          statusEl.style.color = '#c00';
        }
      }
    } finally {
      stopChatTimers();
      if (renderTimer) {
        clearTimeout(renderTimer);
        renderTimer = null;
      }
      // 确保思考动画及其容器被移除
      const responseHeader = aiItem.querySelector('.ai-response-header');
      if (responseHeader) {
        responseHeader.remove();
      }
      window.removeEventListener('scroll', onUserScroll);
      if (input.isConnected) {
        input.disabled = false;
        input.focus();
      }
      if (btn.isConnected) {
        btn.disabled = false;
        btn.innerText = '发送';
      }
    }
  };

  const getChatModelPickerElements = () => ({
    picker: document.getElementById('chat-model-picker'),
    button: document.getElementById('chat-model-picker-btn'),
    label: document.getElementById('chat-model-picker-label'),
    menu: document.getElementById('chat-model-picker-menu'),
    select: document.getElementById('chat-llm-model-select'),
  });

  const setChatModelPickerOpen = (open) => {
    const { picker, button } = getChatModelPickerElements();
    if (!picker || !button || picker.classList.contains('is-disabled')) return;
    picker.classList.toggle('is-open', Boolean(open));
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
  };

  const closeChatModelPicker = () => {
    const { picker, button } = getChatModelPickerElements();
    if (!picker || !button) return;
    picker.classList.remove('is-open');
    button.setAttribute('aria-expanded', 'false');
  };

  const syncChatModelPicker = (names = []) => {
    const { picker, button, label, menu, select } = getChatModelPickerElements();
    if (!picker || !button || !label || !menu || !select) return;

    const cleanNames = Array.from(
      new Set(names.map((name) => (name || '').trim()).filter(Boolean)),
    );
    const current = (select.value || cleanNames[0] || '').trim();
    const disabled = select.disabled || !cleanNames.length;

    label.textContent = current || '选择模型';
    picker.classList.toggle('is-disabled', disabled);
    button.disabled = disabled;
    button.title = disabled ? select.title || '暂无可用 Chat 模型' : '切换 Chat 模型';

    if (disabled) {
      closeChatModelPicker();
    }

    menu.innerHTML = '';
    cleanNames.forEach((name) => {
      const item = document.createElement('button');
      const selected = name === current;
      item.type = 'button';
      item.className = `chat-model-picker-option${selected ? ' is-selected' : ''}`;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', selected ? 'true' : 'false');
      item.dataset.value = name;
      item.innerHTML = `
        <span class="chat-model-option-main">${name}</span>
        <span class="chat-model-option-check" aria-hidden="true">✓</span>
      `;
      item.addEventListener('click', () => {
        if (select.disabled) return;
        select.value = name;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        closeChatModelPicker();
        syncChatModelPicker(cleanNames);
      });
      menu.appendChild(item);
    });
  };

  const bindChatModelPickerOnce = () => {
    const { picker, button, select } = getChatModelPickerElements();
    if (!picker || !button || !select || picker._boundPicker) return;
    picker._boundPicker = true;

    button.addEventListener('click', () => {
      if (button.disabled) return;
      const shouldOpen = !picker.classList.contains('is-open');
      setChatModelPickerOpen(shouldOpen);
    });

    button.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        setChatModelPickerOpen(true);
        const first = picker.querySelector('.chat-model-picker-option');
        if (first) first.focus();
      }
    });

    if (!document._dprChatModelPickerBound) {
      document._dprChatModelPickerBound = true;
      document.addEventListener('click', (e) => {
        const { picker: currentPicker } = getChatModelPickerElements();
        if (!currentPicker || currentPicker.contains(e.target)) return;
        closeChatModelPicker();
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          closeChatModelPicker();
        }
      });
    }
  };

  const initForPage = (paperId) => {
    const mainContent = document.querySelector('.markdown-section');
    if (!mainContent || !paperId) return;
    // 总结和综述脚本各补挂一次。同一页再挂会叠出两个问答框。
    if (mainContent.querySelector('#paper-chat-container')) return;

    const container = document.createElement('div');
    container.innerHTML = renderChatUI();
    mainContent.appendChild(container);

    // 最近提问按钮/面板
    bindQuestionsPanelEventsOnce();

    const sendBtnEl = document.getElementById('send-btn');
    const inputEl = document.getElementById('user-input');
    const statusEl = document.getElementById('chat-status');
    const modelSelect = document.getElementById('chat-llm-model-select');
    const chatSidebarBtn = document.getElementById('chat-sidebar-toggle-btn');
    const chatSettingsBtn = document.getElementById('chat-settings-toggle-btn');
    const chatQuickRunBtn = document.getElementById('chat-quick-run-btn');
    const chatQuickRunCloseBtn = document.getElementById('chat-quick-run-close-btn');
    const chatQuickRun10dBtn = document.getElementById('chat-quick-run-10d-btn');
    const chatQuickRun30dBtn = document.getElementById('chat-quick-run-30d-btn');
    const chatQuickRunConferenceBtn = document.getElementById(
      'chat-quick-run-conference-run-btn',
    );
    const chatQuickRunYearSelect = document.getElementById('chat-quick-run-year-select');
    const chatQuickRunConferenceSelect = document.getElementById(
      'chat-quick-run-conference-select',
    );
    const chatQuickRunConferenceMsg = document.getElementById(
      'chat-quick-run-conference-msg',
    );
    const modal = getQuickRunModal();
    if (modal && modal.parentElement !== document.body) {
      document.body.appendChild(modal);
    }
    fillQuickRunOptions(chatQuickRunYearSelect, chatQuickRunConferenceSelect);
    bindChatModelPickerOnce();

    const enrichEl = document.getElementById('chat-quick-run-enrich-checkbox');
    if (enrichEl && !enrichEl._boundEnrichNav) {
      enrichEl._boundEnrichNav = true;
      try {
        enrichEl.checked = localStorage.getItem('dpr_run_enrich') === '1';
      } catch (e) { /* localStorage 不可用时保持默认 */ }
      enrichEl.addEventListener('change', () => {
        try {
          localStorage.setItem('dpr_run_enrich', enrichEl.checked ? '1' : '0');
        } catch (e) { /* 忽略持久化失败 */ }
      });
    }

    const modeSelectEl = document.getElementById('chat-quick-run-mode-select');
    if (modeSelectEl && !modeSelectEl._boundModeNav) {
      modeSelectEl._boundModeNav = true;
      try {
        const savedMode = String(localStorage.getItem('dpr_fetch_mode') || '').trim();
        if (['auto', 'standard', 'skims'].includes(savedMode)) {
          modeSelectEl.value = savedMode;
        }
      } catch (e) { /* localStorage 不可用时保持默认 */ }
      modeSelectEl.addEventListener('change', () => {
        try {
          localStorage.setItem('dpr_fetch_mode', modeSelectEl.value);
        } catch (e) { /* 忽略持久化失败 */ }
      });
    }

    const inGuestMode =
      window.DPR_ACCESS_MODE === 'guest' || window.DPR_ACCESS_MODE === 'locked';

    const enableChatControls = async () => {
      const sendBtn = document.getElementById('send-btn');
      const input = document.getElementById('user-input');
      const status = document.getElementById('chat-status');
      const select = document.getElementById('chat-llm-model-select');

      if (sendBtn && !sendBtn._boundSend) {
        sendBtn._boundSend = true;
        sendBtn.disabled = false;
        sendBtn.title = '';
        sendBtn.addEventListener('click', () => {
          sendMessage(paperId);
        });
      }

      if (input && !input._boundKey) {
        input._boundKey = true;
        input.disabled = false;
        input.placeholder = '针对这篇论文提问，仅自己可见...';
        resizeChatInput(input);
        input.addEventListener('input', () => {
          resizeChatInput(input);
        });
        input.addEventListener('compositionstart', () => { input._imeLock = true; });
        input.addEventListener('compositionend', () => {
          input._imeLock = true;
          setTimeout(() => { input._imeLock = false; }, 0);
        });
        input.addEventListener('keydown', (e) => {
          if (e.isComposing || e.keyCode === 229 || input._imeLock) return;
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage(paperId);
          }
        });
      }

      if (select) {
        const chatModel = await resolveChatConfig();
        // 解锁后重新启用下拉框
        select.disabled = false;
        select.title = '';
        select.innerHTML = '';
        const names = chatModel ? [chatModel] : [];
        names.forEach((name) => {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          select.appendChild(opt);
        });
        // 选择模型默认值：
        // 1. 若存在用户偏好（localStorage），优先使用偏好；
        // 2. 否则退回第一个可用模型。
        const prefName = loadPreferredModelName();
        let defaultName = '';
        if (prefName && names.includes(prefName)) {
          defaultName = prefName;
        } else if (names.length) {
          defaultName = names[0];
        }
        if (defaultName) {
          select.value = defaultName;
        }
        if (!names.length && status) {
          status.textContent =
            '还没有可用的对话模型。请打开页面设置填写。';
          status.style.color = '#c00';
        }
        syncChatModelPicker(names);

        // 用户手动切换模型时，更新偏好，跨页面复用
        if (!select._boundChange) {
          select._boundChange = true;
          select.addEventListener('change', () => {
            const v = (select.value || '').trim();
            if (v) {
              savePreferredModelName(v);
            }
            syncChatModelPicker(names);
          });
        }
      }
    };

    if (sendBtnEl) {
      if (inGuestMode) {
        sendBtnEl.disabled = true;
        sendBtnEl.title = '当前为游客模式或未解锁密钥，无法直接提问。';
      } else {
        enableChatControls().catch(() => {});
      }
    }
    if (inputEl) {
      if (inGuestMode) {
        inputEl.disabled = true;
        inputEl.placeholder = '当前为游客模式，解锁密钥后才能向大模型提问。';
      } else {
        // 已在 enableChatControls 中绑定
      }
    }
    if (modelSelect) {
      if (inGuestMode) {
        modelSelect.disabled = true;
        modelSelect.title = '当前为游客模式或未解锁密钥，无法选择大模型。';
        syncChatModelPicker([]);
      }
    }

    // 如果当前是 locked/guest，则等待密钥解锁事件，再启用聊天控件
    if (inGuestMode) {
      const handler = (e) => {
        const mode = e && e.detail && e.detail.mode;
        if (mode === 'full') {
          document.removeEventListener('dpr-access-mode-changed', handler);
          enableChatControls().catch(() => {});
        }
      };
      document.addEventListener('dpr-access-mode-changed', handler);
    }

    // 小屏幕下聊天区侧边栏开关与后台管理按钮
    if (chatSidebarBtn && !chatSidebarBtn._bound) {
      chatSidebarBtn._bound = true;
      chatSidebarBtn.addEventListener('click', () => {
        if (window.DPRSidebar && typeof window.DPRSidebar.toggleMobile === 'function') {
          window.DPRSidebar.toggleMobile();
          return;
        }
        const dprSidebar = document.getElementById('dpr-sidebar-v2');
        if (dprSidebar && dprSidebar.classList) {
          dprSidebar.classList.toggle('is-open');
          return;
        }
        // 优先复用 Docsify 自带的 sidebar-toggle 行为
        const toggle = document.querySelector('.sidebar-toggle');
        if (toggle) {
          toggle.click();
          return;
        }
        // 兜底：直接切换 body.close，用于控制侧边栏展开/收起
        // const body = document.body;
        // if (!body) return;
        // body.classList.toggle('close');
      });
    }

    if (chatSettingsBtn && !chatSettingsBtn._bound) {
      chatSettingsBtn._bound = true;
      chatSettingsBtn.addEventListener('click', () => {
        if (window.DPRLocalSettings && window.DPRLocalSettings.open) {
          window.DPRLocalSettings.open();
          return;
        }
        // 兜底：打开旧的订阅设置面板（local-settings 未加载时不启用）
        const ensureEvent = new CustomEvent('ensure-arxiv-ui');
        document.dispatchEvent(ensureEvent);

        setTimeout(() => {
          const loadEvent = new CustomEvent('load-arxiv-subscriptions');
          document.dispatchEvent(loadEvent);

          const overlay = document.getElementById('arxiv-search-overlay');
          if (overlay) {
            overlay.style.display = 'flex';
            requestAnimationFrame(() => {
              requestAnimationFrame(() => {
                overlay.classList.add('show');
              });
            });
          }
        }, 100);
      });
    }

    const closeQuickRunPopover = () => {
      const modal = getQuickRunModal();
      if (!modal) return;
      modal.classList.remove('is-open');
      modal.setAttribute('aria-hidden', 'true');

      setTimeout(() => {
        if (modal.classList.contains('is-open')) return;
        modal.style.display = 'none';
      }, 300);
    };

    const openQuickRunPopover = () => {
      const modal = getQuickRunModal();
      if (!modal) return;
      // 打开时与本地偏好对齐（设置面板可能已修改）
      const enrichEl = document.getElementById('chat-quick-run-enrich-checkbox');
      if (enrichEl) {
        try {
          enrichEl.checked = localStorage.getItem('dpr_run_enrich') === '1';
        } catch (e) { /* 忽略 */ }
      }
      modal.setAttribute('aria-hidden', 'false');
      modal.style.display = 'flex';
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          modal.classList.add('is-open');
        });
      });
    };

    const openQuickRunPanelInner = () => {
      const modal = getQuickRunModal();
      if (!modal) {
        if (chatQuickRunConferenceMsg) {
          chatQuickRunConferenceMsg.textContent = '当前页面未完成快速抓取入口初始化。';
          chatQuickRunConferenceMsg.style.color = '#c90';
        }
        return false;
      }
      toggleQuickRunPopover();
      return true;
    };

    const flushQuickRunOpenRequest = () => {
      if (window.__dprQuickRunOpenRequested) {
        window.__dprQuickRunOpenRequested = false;
        openQuickRunPanelInner();
      }
    };

    const toggleQuickRunPopover = () => {
      const modal = getQuickRunModal();
      if (!modal) return;
      if (modal.classList.contains('is-open')) {
        closeQuickRunPopover();
        return;
      }
      if (chatQuickRunConferenceMsg) {
        chatQuickRunConferenceMsg.textContent = '';
        chatQuickRunConferenceMsg.style.color = '#999';
      }
      openQuickRunPopover();
    };

    if (chatQuickRunBtn && !chatQuickRunBtn._bound) {
      chatQuickRunBtn._bound = true;
      chatQuickRunBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        toggleQuickRunPopover();
      });
    }

    if (chatQuickRunCloseBtn && !chatQuickRunCloseBtn._bound) {
      chatQuickRunCloseBtn._bound = true;
      chatQuickRunCloseBtn.addEventListener('click', (e) => {
        e.preventDefault();
        closeQuickRunPopover();
      });
    }

    if (chatQuickRun10dBtn && !chatQuickRun10dBtn._bound) {
      chatQuickRun10dBtn._bound = true;
      chatQuickRun10dBtn.addEventListener('click', () => {
        runQuickFetch(10, statusEl, closeQuickRunPopover);
      });
    }

    if (chatQuickRun30dBtn && !chatQuickRun30dBtn._bound) {
      chatQuickRun30dBtn._bound = true;
      chatQuickRun30dBtn.addEventListener('click', () => {
        runQuickFetch(30, statusEl, closeQuickRunPopover);
      });
    }

    if (chatQuickRunConferenceBtn && !chatQuickRunConferenceBtn._bound) {
      chatQuickRunConferenceBtn._bound = true;
      chatQuickRunConferenceBtn.addEventListener('click', () => {
        runQuickConferencePlaceholder(
          chatQuickRunYearSelect,
          chatQuickRunConferenceSelect,
          chatQuickRunConferenceMsg,
          statusEl,
        );
      });
    }

    if (!document._dprQuickRunPopoverBound) {
      document._dprQuickRunPopoverBound = true;
      document.addEventListener('click', (e) => {
        const modal = getQuickRunModal();
        if (!modal || !modal.classList.contains('is-open')) {
          return;
        }
        if (e.target === modal) {
          closeQuickRunPopover();
          return;
        }
        if (!modal.contains(e.target)) {
          closeQuickRunPopover();
        }
      });
    }

    if (!document._dprQuickRunOpenEventBound) {
      document._dprQuickRunOpenEventBound = true;
      document.addEventListener('dpr-open-quick-run', () => {
        window.__dprQuickRunOpenRequested = false;
        openQuickRunPanelInner();
      });
    }

    flushQuickRunOpenRequest();

    if (!document._dprQuickRunEscBound) {
      document._dprQuickRunEscBound = true;
      document.addEventListener('keydown', (e) => {
        if (e && e.key === 'Escape') {
          closeQuickRunPopover();
        }
      });
    }

    renderHistory(paperId).catch(() => {});

    quickRunPanelController = openQuickRunPanelInner;
  };

  return {
    initForPage,
    openQuickRunPanel: () => {
      if (typeof quickRunPanelController === 'function') {
        const ok = quickRunPanelController();
        if (ok === true) return true;
      }
      if (
        window.DPRWorkflowRunner &&
        typeof window.DPRWorkflowRunner.open === 'function'
      ) {
        window.DPRWorkflowRunner.open();
        return true;
      }
      return false;
    },
  };
})();
