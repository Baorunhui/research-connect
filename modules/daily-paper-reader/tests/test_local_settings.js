'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function loadModule() {
  const src = fs.readFileSync('app/local-settings.js', 'utf8');
  const sandbox = {
    console,
    window: {},
    document: {
      getElementById: () => null,
      createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {} }),
    },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) }),
  };
  vm.runInNewContext(src, sandbox, { filename: 'app/local-settings.js' });
  // UMD 挂载在 sandbox 全局（root = globalThis）上
  return sandbox.DPRLocalSettings;
}

function testBuildLocalPayloadMapsFormToApiShape() {
  const api = loadModule();
  const local = api.buildLocalPayload({
    chatBaseUrl: 'https://api.sinksilk.com:58443',
    chatModel: 'deepseek-v4-flash',
    chatApiKey: '',
    schedEnabled: true,
    schedTime: '18:30',
    rerankProfile: 'public-zwwen-rerank',
    recallMode: 'local',
  });
  // 用 JSON 比较以规避 vm 跨 realm 的原型差异
  assert.equal(JSON.stringify(local), JSON.stringify({
    local: {
      chat: {
        base_url: 'https://api.sinksilk.com:58443',
        model: 'deepseek-v4-flash',
        api_key: '',
      },
      schedule: {
        enabled: true,
        time: '18:30',
      },
      rerank: {
        profile: 'public-zwwen-rerank',
      },
      recall: {
        mode: 'local',
      },
    },
  }));
}

function testBuildLocalPayloadTrimsWhitespaceAndKeepsEmptyKey() {
  const api = loadModule();
  const local = api.buildLocalPayload({
    chatBaseUrl: '  https://example.com/v1  ',
    chatModel: ' some-model ',
    chatApiKey: '',
    schedEnabled: false,
    schedTime: ' 09:15 ',
    rerankProfile: ' auto ',
  });
  assert.equal(local.local.chat.base_url, 'https://example.com/v1');
  assert.equal(local.local.chat.model, 'some-model');
  assert.equal(local.local.chat.api_key, '');
  assert.equal(local.local.schedule.enabled, false);
  assert.equal(local.local.schedule.time, '09:15');
  assert.equal(local.local.rerank.profile, 'auto');  // trim 后保留合法值
  assert.equal(local.local.recall.mode, '');  // 未选时为空 → 后端视为 supabase
}

function testBuildLocalPayloadKeepsProvidedApiKey() {
  const api = loadModule();
  const local = api.buildLocalPayload({
    chatBaseUrl: 'https://x',
    chatModel: 'm',
    chatApiKey: ' sk-secret ',
    schedEnabled: true,
    schedTime: '18:30',
  });
  assert.equal(local.local.chat.api_key, 'sk-secret');
}

function testBuildSubscriptionsPayloadPreservesSchemaAndCache() {
  const api = loadModule();
  const prev = {
    schema_migration: { stage: 'A' },
    keyword_recall_mode: 'or',
    intent_profiles: [
      {
        tag: 'RAG',
        description: 'Retrieval-Augmented Generation',
        enabled: true,
        keywords: [
          { query: 'retrieval augmented generation', keyword: 'retrieval augmented generation', embedding_cache: { version: 1 } },
          { query: 'old query', keyword: 'old keyword', embedding_cache: { version: 1 } },
        ],
        intent_queries: [
          { query: 'Find recent papers on RAG', enabled: true, embedding_cache: { version: 1 } },
          { query: 'second query', enabled: true, embedding_cache: { version: 1 } },
        ],
        paper_sources: ['arxiv'],
      },
    ],
  };
  const form = [
    {
      tag: 'RAG',
      description: 'Retrieval-Augmented Gen',
      enabled: true,
      keywordsText: 'retrieval augmented generation\nnew keyword',
      intentQueriesText: 'Find recent papers on RAG',
    },
  ];
  const out = api.buildSubscriptionsPayload(form, prev);

  // 未编辑的顶层字段保留
  assert.equal(out.schema_migration.stage, 'A');
  assert.equal(out.keyword_recall_mode, 'or');

  const rag = out.intent_profiles[0];
  assert.equal(rag.tag, 'RAG');
  assert.equal(rag.description, 'Retrieval-Augmented Gen');
  assert.equal(JSON.stringify(rag.paper_sources), JSON.stringify(['arxiv']));

  // 未改动文本的 keyword 保留 embedding_cache；新增文本丢弃缓存
  const kw0 = rag.keywords.find((k) => k.keyword === 'retrieval augmented generation');
  assert.ok(kw0.embedding_cache, 'unchanged keyword should keep embedding_cache');
  const kwNew = rag.keywords.find((k) => k.keyword === 'new keyword');
  assert.equal(kwNew.embedding_cache, undefined, 'new keyword should drop embedding_cache');
  // 被删掉的 keyword 不再出现
  assert.equal(rag.keywords.find((k) => k.keyword === 'old keyword'), undefined);

  // 未改动文本的 intent_query 保留 embedding_cache；被删的移除
  const iq0 = rag.intent_queries.find((q) => q.query === 'Find recent papers on RAG');
  assert.ok(iq0.embedding_cache, 'unchanged intent_query should keep embedding_cache');
  assert.equal(rag.intent_queries.find((q) => q.query === 'second query'), undefined);
}

function testBuildSubscriptionsPayloadAddsNewProfile() {
  const api = loadModule();
  const prev = {
    schema_migration: { stage: 'A' },
    keyword_recall_mode: 'or',
    intent_profiles: [{ tag: 'RAG', enabled: true, keywords: [], intent_queries: [], paper_sources: ['arxiv'] }],
  };
  const form = [
    { tag: 'RAG', description: '', enabled: true, keywordsText: 'rag', intentQueriesText: 'rag papers' },
    { tag: 'LLM', description: 'New', enabled: true, keywordsText: 'llm', intentQueriesText: 'llm papers' },
  ];
  const out = api.buildSubscriptionsPayload(form, prev);
  assert.equal(out.intent_profiles.length, 2);
  assert.equal(out.intent_profiles[1].tag, 'LLM');
  assert.equal(JSON.stringify(out.intent_profiles[1].paper_sources), JSON.stringify(['arxiv']), 'brand-new profile defaults to arxiv source');
}

// 回归：结构化接口把 subscriptions 嵌在 data.local.subscriptions（而非顶层 data.subscriptions）。
// open() 必须从该路径加载并按原样回写，否则改词条保存会清空整个 intent_profiles。
function testPanelRoundTripPreservesLoadedSubscriptions() {
  const api = loadModule();
  // 模拟 /api/local/config/structured 的返回结构
  const data = {
    ok: true,
    local: {
      chat: { model: 'm', base_url: 'u', api_key: '' },
      schedule: { enabled: true, time: '18:30' },
      subscriptions: {
        schema_migration: { stage: 'A', diff_threshold_pct: 15 },
        keyword_recall_mode: 'or',
        intent_profiles: [
          {
            tag: 'RAG',
            description: 'Retrieval-Augmented Generation',
            enabled: true,
            keywords: [{ query: 'retrieval augmented generation', keyword: 'retrieval augmented generation', embedding_cache: { version: 1 } }],
            intent_queries: [{ query: 'Find recent papers on RAG', enabled: true, embedding_cache: { version: 1 } }],
            paper_sources: ['arxiv'],
          },
        ],
      },
    },
  };
  // open() 语义：取 data.local.subscriptions 作为 lastSubscriptions
  const subs = (data.local && data.local.subscriptions) || {};
  // 模拟用户未改动字段：从加载的词条重建 formState
  const profiles = subs.intent_profiles || [];
  const formState = profiles.map((p) => ({
    tag: p.tag,
    description: p.description,
    enabled: p.enabled,
    keywordsText: (p.keywords || []).map((k) => k.keyword || k.query || '').join('\n'),
    intentQueriesText: (p.intent_queries || []).map((q) => q.query || '').join('\n'),
  }));
  const out = api.buildSubscriptionsPayload(formState, subs);

  // 关键断言：词条、schema、缓存都必须原样保留，不能被清空
  assert.equal(out.schema_migration.stage, 'A');
  assert.equal(out.keyword_recall_mode, 'or');
  assert.equal(out.intent_profiles.length, 1);
  assert.equal(out.intent_profiles[0].tag, 'RAG');
  assert.equal(out.intent_profiles[0].keywords.length, 1);
  assert.equal(out.intent_profiles[0].intent_queries.length, 1);
  assert.ok(out.intent_profiles[0].keywords[0].embedding_cache, 'unchanged keyword keeps cache');
}

function testValidateBlocksEmptyClearing() {
  const api = loadModule();
  const prev = {
    schema_migration: { stage: 'A' },
    intent_profiles: [
      { tag: 'RAG', enabled: true, keywords: [{ keyword: 'rag', query: 'rag' }], intent_queries: [{ query: 'q', enabled: true }] },
    ],
  };
  // 原本有可用词条，新版却清空 → 应拦截
  const empty = { schema_migration: { stage: 'A' }, intent_profiles: [] };
  const problem = api.validateSubscriptionsPayload(prev, empty);
  assert.ok(problem, 'should block saving to zero profiles');
  assert.ok(problem.indexOf('清空') >= 0, 'problem mentions clearing: ' + problem);
  // 原样保存可用词条 → 不拦截
  assert.equal(api.validateSubscriptionsPayload(prev, prev), null, 'unchanged usable profiles allowed');
  // 原本就空 → 不拦截
  assert.equal(api.validateSubscriptionsPayload({ intent_profiles: [] }, { intent_profiles: [] }), null);
}

// 用户只填一个标签（不填关键词/意图查询）也应允许保存——标签是最小可用单元。
function testTagOnlyProfileAllowed() {
  const api = loadModule();
  const prev = {
    intent_profiles: [{ tag: 'RAG', enabled: true, keywords: [], intent_queries: [] }],
  };
  // 一个只有 tag、无关键词/查询的词条 → 不算清空，允许保存
  const tagOnly = { intent_profiles: [{ tag: 'NEW_TOPIC', enabled: true, keywords: [], intent_queries: [] }] };
  assert.equal(api.validateSubscriptionsPayload(prev, tagOnly), null, 'tag-only profile counts as usable');
  // 所有词条标签都清空 → 仍应拦截
  const allBlankTag = { intent_profiles: [{ tag: '', enabled: true, keywords: [], intent_queries: [] }] };
  assert.ok(api.validateSubscriptionsPayload(prev, allBlankTag), 'blanking all tags should block');
}

// 推荐名额 payload：数字/留空/不限量三种形态都能映射到后端约定结构。
function testBuildRecommendPayloadShapes() {
  const api = loadModule();
  // 常规：两个基数都填
  const full = api.buildRecommendPayload({ recommendDeep: ' 7 ', recommendQuick: '12', deepUnlimited: false });
  assert.deepEqual(JSON.parse(JSON.stringify(full)), {
    deep_dive_base: '7',
    quick_skim_base: '12',
    deep_dive_unlimited: false,
  });
  // 留空 = 跟随运行模式默认，仅 unlimited 传递
  const empty = api.buildRecommendPayload({ recommendDeep: '', recommendQuick: '', deepUnlimited: true });
  assert.equal(empty.deep_dive_base, '');
  assert.equal(empty.quick_skim_base, '');
  assert.equal(empty.deep_dive_unlimited, true);
}

// ===== AI 生成候选（smart-query）=====

// 候选清洗：空 en 丢弃、截断上限、畸形输入不抛错。
function testNormalizeCandidatesDropsEmptyEnAndCaps() {
  const api = loadModule();
  const cands = api.normalizeCandidates({
    tag: ' RAG ',
    description: ' 检索增强生成 ',
    keywords: [
      { en: 'retrieval augmented generation', zh: '检索增强生成' },
      { en: '   ', zh: '空 en 应丢弃' },
      null,
      { zh: '没有 en 也丢弃' },
    ],
    queries: [{ en: 'Find recent papers on RAG', zh: '查找 RAG 新论文' }],
  });
  assert.equal(cands.tag, 'RAG');
  assert.equal(cands.description, '检索增强生成');
  assert.equal(cands.keywords.length, 1);
  assert.equal(cands.keywords[0].zh, '检索增强生成');
  assert.equal(cands.queries.length, 1);
  // 畸形输入兜底（vm 跨 realm，用 JSON 比较原型差异）
  assert.equal(
    JSON.stringify(api.normalizeCandidates(null)),
    JSON.stringify({ tag: '', description: '', keywords: [], queries: [] }),
  );
}

// 应用所选：忽略大小写去重、追加到现有行之后、统计新增/跳过。
function testMergeCandidateLinesDedupesAndAppends() {
  const api = loadModule();
  const r = api.mergeCandidateLines(
    'retrieval augmented generation\nGraph RAG',
    [{ en: 'graph rag', zh: '' }, { en: 'Hybrid Search', zh: '' }, { en: '', zh: '空' }],
    'en',
  );
  assert.equal(r.text, 'retrieval augmented generation\nGraph RAG\nHybrid Search');
  assert.equal(r.added, 1);
  assert.equal(r.skipped, 1);
  // 空现有文本也能合并
  const empty = api.mergeCandidateLines('', [{ en: 'A' }, { en: 'a' }], 'en');
  assert.equal(empty.text, 'A');
  assert.equal(empty.added, 1);
}

// 候选卡片：默认勾选、kind 标记、HTML 转义防注入。
function testRenderCandidateCardsChecksDefaultAndEscapes() {
  const api = loadModule();
  const html = api.renderCandidateCards('keywords', [{ en: '<b>x</b>&"y', zh: '中文' }]);
  assert.ok(html.indexOf('checked') >= 0, 'candidates default checked');
  assert.ok(html.indexOf('data-dpr-smart-check="keywords"') >= 0);
  assert.ok(html.indexOf('&lt;b&gt;x&lt;/b&gt;&amp;"y') >= 0, 'display text escaped');
  assert.equal(api.renderCandidateCards('queries', []), '');
}

function testSmartQueryProgressIsCappedAndExplainsLongWait() {
  const api = loadModule();
  const start = api.smartQueryProgressState(0);
  const normal = api.smartQueryProgressState(20);
  const slow = api.smartQueryProgressState(60);
  const verySlow = api.smartQueryProgressState(600);
  assert.ok(start.percent >= 0 && start.percent < normal.percent);
  assert.ok(normal.percent < slow.percent);
  assert.equal(verySlow.percent, 92, 'estimated progress must wait for the real response');
  assert.ok(slow.label.includes('仍在处理'));
  assert.ok(slow.label.includes('60 秒'));
}

Promise.resolve()
  .then(testBuildLocalPayloadMapsFormToApiShape)
  .then(testBuildLocalPayloadTrimsWhitespaceAndKeepsEmptyKey)
  .then(testBuildLocalPayloadKeepsProvidedApiKey)
  .then(testBuildRecommendPayloadShapes)
  .then(testBuildSubscriptionsPayloadPreservesSchemaAndCache)
  .then(testBuildSubscriptionsPayloadAddsNewProfile)
  .then(testPanelRoundTripPreservesLoadedSubscriptions)
  .then(testValidateBlocksEmptyClearing)
  .then(testTagOnlyProfileAllowed)
  .then(testNormalizeCandidatesDropsEmptyEnAndCaps)
  .then(testMergeCandidateLinesDedupesAndAppends)
  .then(testRenderCandidateCardsChecksDefaultAndEscapes)
  .then(testSmartQueryProgressIsCappedAndExplainsLongWait)
  .then(() => {
    console.log('local settings tests passed');
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
