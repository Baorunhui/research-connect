const assert = require('node:assert/strict');
const fs = require('node:fs');

// ---- browser stub (mirrors test_dpr_sidebar_v2.js pattern) ----

function setupBrowserStub() {
  global.window = {
    location: { hash: '#/' },
    localStorage: {
      _store: {},
      getItem(k) { return this._store[k] || null; },
      setItem(k, v) { this._store[k] = String(v); },
    },
    addEventListener: () => {},
    setTimeout,
    clearTimeout,
    matchMedia: () => ({ matches: false }),
    CSS: { escape: (s) => String(s) },
  };
  global.document = {
    readyState: 'loading',
    addEventListener: () => {},
    dispatchEvent: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    createEvent: () => ({ initCustomEvent(name) { this.type = name; } }),
    createElement: () => {
      let text = '';
      return {
        set innerHTML(v) { text = String(v).replace(/<[^>]*>/g, ''); },
        get innerHTML() { return text; },
        get textContent() { return text; },
        set textContent(v) { text = String(v == null ? '' : v); },
      };
    },
    body: { appendChild: () => {}, classList: { add: () => {} } },
    documentElement: { style: { setProperty: () => {} } },
  };
}

function loadTools() {
  setupBrowserStub();
  delete require.cache[require.resolve('../app/dpr-sidebar.js')];
  return require('../app/dpr-sidebar.js').__test;
}

// ---- v3 fixture ----
const V3_FIELDS = [
  { sub: 'DS', name: '计算机体系结构/并行与分布计算/存储系统', name_en: 'Computer Architecture' },
  { sub: 'NW', name: '计算机网络', name_en: 'Computer Networks' },
  { sub: 'SC', name: '安全', name_en: 'Security' },
  { sub: 'SE', name: '软件工程/系统软件/程序设计语言', name_en: 'Software Engineering' },
  { sub: 'DB', name: '数据库/数据挖掘/内容检索', name_en: 'Database' },
  { sub: 'CT', name: '计算机科学理论', name_en: 'Theory' },
  { sub: 'CG', name: '计算机图形学', name_en: 'Graphics' },
  { sub: 'AI', name: '人工智能', name_en: 'Artificial Intelligence' },
  { sub: 'HI', name: '人机交互与普适计算', name_en: 'HCI' },
  { sub: 'MX', name: '交叉/综合/新兴', name_en: 'Interdisciplinary' },
];

function makeV3Schedule(conferences) {
  return {
    schema_version: 3,
    generated_at: '2026-08-17T00:00:00Z',
    source: 'ccfddl/ccf-deadlines',
    license: 'MIT',
    fields: V3_FIELDS,
    conferences: conferences || [],
  };
}

function makeV3Conference(key, label, sub, rank, milestones) {
  return {
    key: key,
    label: label,
    description: label + ' conference',
    sub: sub || 'AI',
    rank: rank || { ccf: 'A', core: 'A*', thcpl: 'A' },
    dblp: key,
    years: [{
      year: 2025,
      id: key + '25',
      link: 'https://example.com/' + key,
      place: 'Test City',
      date_text: 'Dec 1-5, 2025',
      timezone: 'AoE',
      is_tbd: false,
      milestones: milestones || [],
    }],
  };
}

function makeV3Milestone(type, date, ts, label, opts) {
  return Object.assign({
    type: type,
    date: date,
    ts: ts,
    label: label || type,
    time_text: '',
    is_tbd: false,
    round: null,
    source: 'ccfddl',
  }, opts || {});
}

// ---- v2 fixture (hardcoded — do NOT read the live file, which may be v3) ----
function makeV2Schedule() {
  return {
    schema_version: 2,
    generated_at: '2026-08-17T00:00:00Z',
    conferences: [{
      key: 'neurips',
      label: 'NeurIPS',
      canonical: { submission: 'abstract', notification: 'september', conference: 'december' },
      years: [{
        year: 2026,
        milestones: [
          { type: 'full_paper', date: '2026-05-27', label: '全文投稿截止' },
          { type: 'notification', date: '2026-09-23', label: '录取通知' },
          { type: 'conference', date: '2026-12-06', label: '会议召开 12/6-12/12' },
        ],
      }],
    }],
  };
}

// ---- 1. v3 parser ----
function testV3Parser() {
  const tools = loadTools();
  const now = Date.now();
  const futureTs = now + 10 * 86400000; // 10 days from now
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', futureTs, '全文投稿截止', { time_text: '23:59 AoE', source: 'ccfddl' }),
      makeV3Milestone('notification', '2025-09-25', futureTs + 100 * 86400000, '录取通知'),
    ]),
  ]);
  const parsed = tools.parseSchedule(schedule);
  assert.equal(parsed.schema_version, 3, 'v3 schema version parsed');
  assert.ok(parsed.fields.length === 10, 'fields parsed');
  assert.equal(parsed.conferences.length, 1, 'one conference parsed');
  const conf = parsed.conferences[0];
  assert.equal(conf.key, 'neurips');
  assert.equal(conf.sub, 'AI');
  assert.deepEqual(conf.rank, { ccf: 'A' });
  assert.equal(conf.description, 'NeurIPS conference');
  const m0 = conf.years[0].milestones[0];
  assert.equal(typeof m0.ts, 'number', 'milestone ts is number');
  assert.equal(m0.time_text, '23:59 AoE', 'time_text carried');
  assert.equal(m0.source, 'ccfddl', 'source carried');
  assert.equal(m0.is_tbd, false, 'is_tbd carried');
  // year-level fields
  assert.equal(conf.years[0].link, 'https://example.com/neurips');
  assert.equal(conf.years[0].place, 'Test City');
  assert.equal(conf.years[0].date_text, 'Dec 1-5, 2025');
}

// ---- 2. v2 parser backward compat ----
function testV2ParserBackwardCompat() {
  const tools = loadTools();
  const seed = makeV2Schedule();
  const parsed = tools.parseSchedule(seed);
  assert.ok(parsed.conferences.length > 0, 'v2 conferences parsed');
  assert.equal(parsed.schema_version, 2, 'v2 schema version');
  parsed.conferences.forEach(function (conf) {
    assert.ok(conf.key && conf.label, 'key/label present');
    assert.ok(!conf.rank, 'v2 has no rank');
    assert.equal(conf.sub || '', '', 'v2 has no sub');
    conf.years.forEach(function (y) {
      y.milestones.forEach(function (m) {
        assert.equal(typeof m.ts, 'number', 'ts computed from Date.UTC');
        assert.equal(m.time_text || '', '', 'v2 has no time_text');
        assert.equal(m.source || '', '', 'v2 has no source');
      });
    });
  });
}

// ---- 3. countdown formatter ----
function testCountdownFormatter() {
  const tools = loadTools();
  const now = Date.now();
  // future: 10 days + 2 hours + 30 minutes
  const future = now + 10 * 86400000 + 2 * 3600000 + 30 * 60000;
  const text = tools.formatCountdownText(future, now);
  assert.ok(text.includes('10天'), 'contains days: ' + text);
  assert.ok(text.includes('2时'), 'contains hours: ' + text);
  assert.ok(text.includes('30分'), 'contains minutes: ' + text);
  // past
  const past = now - 86400000;
  assert.equal(tools.formatCountdownText(past, now), '已截止', 'past shows 已截止');
  // TBD (null ts)
  assert.equal(tools.formatCountdownText(null, now), 'TBD', 'null shows TBD');
  // small: 2 hours
  const soon = now + 2 * 3600000;
  const sText = tools.formatCountdownText(soon, now);
  assert.ok(sText.includes('2时'), 'hours-only: ' + sText);
}

// ---- 4. color tier ----
function testColorTier() {
  const tools = loadTools();
  assert.equal(tools.scheduleColorTier(3), 'is-urgent', '3 days is urgent');
  assert.equal(tools.scheduleColorTier(7), 'is-urgent', '7 days is urgent');
  assert.equal(tools.scheduleColorTier(8), 'is-soon', '8 days is soon');
  assert.equal(tools.scheduleColorTier(30), 'is-soon', '30 days is soon');
  assert.equal(tools.scheduleColorTier(31), 'is-normal', '31 days is normal');
  assert.equal(tools.scheduleColorTier(365), 'is-normal', '365 days is normal');
}

// ---- 5. field filter ----
function testFieldFilter() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('sigmod', 'SIGMOD', 'DB', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-01-15', now + 20 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('cvpr', 'CVPR', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-02-01', now + 15 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('ccs', 'CCS', 'SEC', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-03-01', now + 18 * 86400000, '全文投稿截止'),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  // filter AI：严格匹配，SEC 等其它领域不得混入（组合筛选边界）
  const aiOnly = tools.filterScheduleMilestones(all, { field: 'AI', rankFilter: { A: true, B: true, C: true, N: true } });
  assert.ok(aiOnly.every(function (m) { return m.sub === 'AI'; }), 'all AI');
  assert.equal(aiOnly.length, 2, 'two AI conferences');
  // filter all
  const allFiltered = tools.filterScheduleMilestones(all, { field: 'all', rankFilter: { A: true, B: true, C: true, N: true } });
  assert.equal(allFiltered.length, 4, 'all keeps all');
}

// ---- 6. rank filter ----
function testRankFilter() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('aaai', 'AAAI', 'AI', { ccf: 'B' }, [
      makeV3Milestone('full_paper', '2025-08-15', now + 20 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('colt', 'COLT', 'CT', { ccf: 'C' }, [
      makeV3Milestone('full_paper', '2025-02-01', now + 15 * 86400000, '全文投稿截止'),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  // turn off B
  const noB = tools.filterScheduleMilestones(all, { rankFilter: { A: true, B: false, C: true, N: true } });
  assert.ok(noB.every(function (m) { return m.rank && m.rank.ccf !== 'B'; }), 'B hidden');
  // turn off B and C
  const onlyA = tools.filterScheduleMilestones(all, { rankFilter: { A: true, B: false, C: false, N: true } });
  assert.ok(onlyA.every(function (m) { return m.rank && m.rank.ccf === 'A'; }), 'only A');
  // 领域 × 等级组合：AI 且仅 A 档
  const aiAndA = tools.filterScheduleMilestones(all, { field: 'AI', rankFilter: { A: true, B: false, C: false, N: true } });
  assert.ok(aiAndA.every(function (m) { return m.sub === 'AI' && m.rank && m.rank.ccf === 'A'; }), 'AI intersects rank A');
  // 一个等级都没勾 = 不限等级（新默认态）
  const noneSelected = tools.filterScheduleMilestones(all, { rankFilter: { A: false, B: false, C: false, N: false } });
  assert.equal(noneSelected.length, all.length, 'empty tier selection means unlimited');
}

// ---- 7. state filter ----
function testStateFilter() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('aaai', 'AAAI', 'AI', { ccf: 'B' }, [
      makeV3Milestone('full_paper', '2025-08-15', now + 40 * 86400000, '全文投稿截止'),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  // upcoming (≤30d)
  const upcoming = tools.filterScheduleMilestones(all, { stateFilter: 'upcoming', rankFilter: { A: true, B: true, C: true, N: true } });
  assert.ok(upcoming.every(function (m) { return m.ts >= now && m.ts <= now + 30 * 86400000; }), 'all within 30d');
  // all
  const allState = tools.filterScheduleMilestones(all, { stateFilter: 'all', rankFilter: { A: true, B: true, C: true, N: true } });
  assert.equal(allState.length, 2, 'all state keeps all');
}

// ---- 8. search ----
function testSearch() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A', core: 'A*', thcpl: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('sigmod', 'SIGMOD', 'DB', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-01-15', now + 20 * 86400000, '全文投稿截止'),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  // search "neur" matches NeurIPS
  const neur = tools.filterScheduleMilestones(all, { search: 'neur', rankFilter: { A: true, B: true, C: true, N: true } });
  assert.ok(neur.length > 0, 'neur matches');
  assert.ok(neur.every(function (m) { return m.conferenceLabel.toLowerCase().indexOf('neur') !== -1; }), 'all contain neur');
  // search "管理" should not match these
  const none = tools.filterScheduleMilestones(all, { search: '管理', rankFilter: { A: true, B: true, C: true, N: true } });
  assert.equal(none.length, 0, '管理 matches none');
}

// ---- 9. favorites ----
function testFavorites() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止'),
    ]),
    makeV3Conference('sigmod', 'SIGMOD', 'DB', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-01-15', now + 20 * 86400000, '全文投稿截止'),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  const favs = new Set(['neurips']);
  const favOnly = tools.filterScheduleMilestones(all, { favOnly: true, favorites: favs, rankFilter: { A: true, B: true, C: true, N: true } });
  assert.equal(favOnly.length, 1, 'only favorite');
  assert.equal(favOnly[0].conferenceKey, 'neurips', 'neurips is the favorite');
  // no favs
  const noFav = tools.filterScheduleMilestones(all, { favOnly: true, favorites: new Set(), rankFilter: { A: true, B: true, C: true, N: true } });
  assert.equal(noFav.length, 0, 'empty favs returns nothing');
}

// ---- 10. ICS generator ----
function testIcsGenerator() {
  const tools = loadTools();
  const milestones = [
    {
      conferenceKey: 'neurips', conferenceLabel: 'NeurIPS', year: 2025,
      type: 'full_paper', date: '2025-05-22', ts: 1747900800000, label: '全文投稿截止',
      time_text: '23:59 AoE', place: 'San Diego', link: 'https://neurips.cc',
    },
    {
      conferenceKey: 'sigmod', conferenceLabel: 'SIGMOD', year: 2025,
      type: 'full_paper', date: '2025-01-15', ts: 1736976000000, label: '全文投稿截止',
    },
  ];
  const ics = tools.generateIcsContent(milestones);
  assert.ok(ics.includes('BEGIN:VCALENDAR'), 'has VCALENDAR start');
  assert.ok(ics.includes('END:VCALENDAR'), 'has VCALENDAR end');
  // count VEVENT blocks
  const veventCount = (ics.match(/BEGIN:VEVENT/g) || []).length;
  assert.equal(veventCount, 2, 'two VEVENT blocks');
  // check DTSTART format
  assert.ok(ics.includes('DTSTART;VALUE=DATE:20250522'), 'DTSTART correct for neurips');
  assert.ok(ics.includes('DTEND;VALUE=DATE:20250523'), 'DTEND is next day');
  assert.ok(ics.includes('SUMMARY:'), 'SUMMARY present');
  // parse back DTSTART
  const match = /DTSTART;VALUE=DATE:(\d{8})/.exec(ics);
  assert.ok(match, 'DTSTART parseable');
  const dt = match[1];
  assert.equal(dt.length, 8, 'DTSTART is 8 digits');
  // empty milestones
  assert.equal(tools.generateIcsContent([]), '', 'empty list returns empty');
  assert.equal(tools.generateIcsContent(null), '', 'null returns empty');
}

// ---- 11. override source milestone ----
function testOverrideSourceMilestone() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止', { source: 'override' }),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  assert.equal(all.length, 1, 'override milestone present');
  assert.equal(all[0].source, 'override', 'source is override');
  // render should include 手工 marker
  const html = tools.renderSchedulePanelHtml(schedule, true, now);
  assert.ok(html.includes('手工'), '手工 marker in HTML');
}

// ---- 12. schema version guard (v2 doesn't crash v3 code) ----
function testSchemaVersionGuard() {
  const tools = loadTools();
  const seed = makeV2Schedule();
  // v2 schedule should not crash v3 code paths
  const parsed = tools.parseSchedule(seed);
  assert.equal(parsed.schema_version, 2);
  const all = tools.computeAllMilestones(parsed, Date.now());
  assert.ok(Array.isArray(all), 'computeAllMilestones works on v2');
  // filter should work (rank will be null → N)
  const filtered = tools.filterScheduleMilestones(all, { rankFilter: { A: true, B: true, C: true, N: true } });
  assert.ok(Array.isArray(filtered), 'filterScheduleMilestones works on v2');
  // render should work
  const html = tools.renderSchedulePanelHtml(parsed, true, Date.now());
  assert.ok(typeof html === 'string', 'render works on v2');
  assert.ok(html.includes('会议日程'), 'panel title present in v2 render');
}

// ---- 13. groupMilestonesByConference ----
function testGroupMilestonesByConference() {
  const tools = loadTools();
  const now = Date.now();
  const schedule = makeV3Schedule([
    makeV3Conference('neurips', 'NeurIPS', 'AI', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-05-22', now + 10 * 86400000, '全文投稿截止'),
      makeV3Milestone('notification', '2025-09-25', now + 130 * 86400000, '录取通知'),
    ]),
    makeV3Conference('sigmod', 'SIGMOD', 'DB', { ccf: 'A' }, [
      makeV3Milestone('full_paper', '2025-01-15', now + 20 * 86400000, '全文投稿截止'),
    ]),
  ]);
  const all = tools.computeAllMilestones(schedule, now);
  const grouped = tools.groupMilestonesByConference(all);
  assert.equal(grouped.length, 2, 'two groups');
  // sorted by next milestone ts
  assert.ok(grouped[0].nextMilestone.ts <= grouped[1].nextMilestone.ts, 'sorted by earliest');
  assert.equal(grouped[0].label, 'NeurIPS', 'NeurIPS first (earlier deadline)');
  assert.equal(grouped[0].allMilestones.length, 2, 'NeurIPS has 2 milestones');
  assert.equal(grouped[1].allMilestones.length, 1, 'SIGMOD has 1 milestone');
}

// ---- 14. rank label / css class ----
function testRankHelpers() {
  const tools = loadTools();
  assert.equal(tools.rankLabel({ ccf: 'A' }), 'CCF A');
  assert.equal(tools.rankLabel({ ccf: 'B' }), 'CCF B');
  assert.equal(tools.rankLabel(null), '');
  assert.equal(tools.rankLabel({}), '');
  assert.equal(tools.rankCssClass({ ccf: 'A' }), 'rank-a');
  assert.equal(tools.rankCssClass({ ccf: 'B' }), 'rank-b');
  assert.equal(tools.rankCssClass({ ccf: 'C' }), 'rank-c');
  assert.equal(tools.rankCssClass(null), 'rank-n');
  assert.equal(tools.rankCssClass({ ccf: 'X' }), 'rank-n');
}

// ---- 15. fieldSubToName ----
function testFieldSubToName() {
  const tools = loadTools();
  assert.equal(tools.fieldSubToName('AI', V3_FIELDS), '人工智能');
  assert.equal(tools.fieldSubToName('DB', V3_FIELDS), '数据库/数据挖掘/内容检索');
  assert.equal(tools.fieldSubToName('ZZ', V3_FIELDS), '');
  assert.equal(tools.fieldSubToName('', V3_FIELDS), '');
}

// ---- run all ----
testV3Parser();
testV2ParserBackwardCompat();
testCountdownFormatter();
testColorTier();
testFieldFilter();
testRankFilter();
testStateFilter();
testSearch();
testFavorites();
testIcsGenerator();
testOverrideSourceMilestone();
testSchemaVersionGuard();
testGroupMilestonesByConference();
testRankHelpers();
testFieldSubToName();

console.log('dpr-sidebar schedule tests passed');
