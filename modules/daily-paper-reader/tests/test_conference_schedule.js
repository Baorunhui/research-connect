const assert = require('node:assert/strict');
const fs = require('node:fs');

// 与 test_dpr_sidebar_v2.js 相同的浏览 stub，让 dpr-sidebar.js 可被 require
function setupBrowserStub() {
  global.window = {
    location: { hash: '#/' },
    localStorage: {
      getItem: () => null,
      setItem: () => {},
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

function loadSeed() {
  const raw = fs.readFileSync('app/conference-schedule.json', 'utf8');
  return JSON.parse(raw);
}

function testParseSchedule() {
  const tools = loadTools();
  const seed = loadSeed();
  const parsed = tools.parseSchedule(seed);
  assert.ok(parsed.conferences.length > 0, 'should parse conferences');
  parsed.conferences.forEach((conf) => {
    assert.ok(conf.key && conf.label, 'conference key/label present');
    (conf.years || []).forEach((y) => {
      assert.ok(Number.isInteger(y.year), 'year is integer');
      y.milestones.forEach((m) => {
        assert.equal(typeof m.ts, 'number', 'milestone ts computed');
        assert.ok(/^\d{4}-\d{2}-\d{2}$/.test(m.date), 'date format YYYY-MM-DD');
        assert.ok(m.type && m.label, 'milestone type and label present');
      });
    });
  });
}

// 无效输入优雅降级，不抛错
function testParseScheduleGraceful() {
  const tools = loadTools();
  const emptyResult = { schema_version: 2, fields: [], conferences: [] };
  assert.deepEqual(tools.parseSchedule(null), emptyResult);
  assert.deepEqual(tools.parseSchedule({}), emptyResult);
  assert.deepEqual(tools.parseSchedule({ conferences: 'bad' }), emptyResult);
}

function testComputeScheduleViewStates() {
  const tools = loadTools();
  const seed = loadSeed();
  const parsed = tools.parseSchedule(seed);
  // 参考今天 2026-08-17
  const ref = new Date(Date.UTC(2026, 7, 17));
  const view = tools.computeScheduleView(parsed, ref.getTime());
  assert.ok(Array.isArray(view), 'computeScheduleView returns array');
  assert.ok(view.length > 0, 'there are schedule items');
  const validStates = ['upcoming', 'soon', 'in-progress', 'overdue', 'done'];
  view.forEach((item) => {
    assert.ok(validStates.includes(item.state), 'valid state: ' + item.state);
    assert.ok(typeof item.daysUntil === 'number', 'daysUntil is number');
    assert.ok(item.conferenceKey && item.conferenceLabel, 'conference fields present');
  });
  // 排序按日期升序
  for (let i = 1; i < view.length; i++) {
    assert.ok(view[i - 1].milestone.ts <= view[i].milestone.ts, 'sorted by milestone ts ascending');
  }
}

function testComputeAllMilestones() {
  // artificially determinable today
  const ref = new Date(Date.UTC(2026, 7, 17)).getTime();
  const tools = loadTools();
  const seed = loadSeed();
  const parsed = tools.parseSchedule(seed);
  const all = tools.computeAllMilestones(parsed, ref, 14);
  assert.ok(Array.isArray(all) && all.length > 0, 'returns flat milestone list');
  // future filter in render panel happens inside renderSchedulePanelHtml; here we just assert states
  const todayStart = new Date(Date.UTC(2026, 7, 17)).getTime();
  all.forEach(function (m) {
    assert.ok(m.year > 0, 'year present');
    assert.ok(typeof m.daysUntil === 'number');
  });
  // ensure the earliest future milestone after today is marked quickly — sanity only
  console.log('schedule states sample:', all.map(function (m) { return m.state; }).slice(0, 10).join(','));
}

function testRenderSchedulePanelShownWithoutConferenceData() {
  const tools = loadTools();
  const seed = loadSeed();
  const parsed = tools.parseSchedule(seed);
  const ref = new Date(Date.UTC(2026, 7, 17)).getTime();
  const html = tools.renderSchedulePanelHtml(parsed, true, ref);
  assert.ok(html.includes('会议日程'), 'contains panel title');
  // v2 data: no filter controls, but list + footer present
  assert.ok(html.includes('dpr-sidebar-schedule-list'), 'contains schedule list');
  assert.ok(html.includes('dpr-sidebar-schedule-footer'), 'contains footer');
  // HTML/CSS 类名合同：渲染出的里程碑必须使用 CSS 中定义的 dpr-sidebar-schedule-* 前缀
  assert.ok(html.includes('dpr-sidebar-schedule-conf-row'), 'conf row uses dpr-sidebar-schedule-conf-row class');
  assert.ok(!html.includes('dpr-schedule-item '), 'no legacy dpr-schedule-item class in HTML');
  // 展开机制必须用 is-expanded class（与其他面板一致），不能用内联 display:none
  assert.ok(html.includes('dpr-sidebar-schedule-panel is-expanded'), 'expanded panel carries is-expanded class');
  assert.ok(!html.includes('style="display:none"'), 'no inline display:none on schedule content');
  // 折叠态则不应带 is-expanded
  const collapsedHtml = tools.renderSchedulePanelHtml(parsed, false, ref);
  assert.ok(!collapsedHtml.includes('dpr-sidebar-schedule-panel is-expanded'), 'collapsed panel omits is-expanded class');
  // 空 schedule → 不崩；null → 空串
  assert.equal(tools.renderSchedulePanelHtml(null, true, ref), '');
  assert.ok(tools.renderSchedulePanelHtml({ conferences: [] }, true, ref).includes('暂无近期会议日程'));
}

function testCSSContract() {
  const css = fs.readFileSync('app/app.css', 'utf8');
  function matching(sel) {
    const escaped = sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp('(^|\\n)\\s*' + escaped + '\\s*\\{').test(css);
  }
  assert.ok(matching('.dpr-sidebar-schedule-panel'), 'schedule panel CSS rule exists');
  // v3 new classes
  assert.ok(matching('.dpr-sidebar-schedule-conf-row'), 'conf row CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-header'), 'conf header CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-label'), 'conf label CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-rank'), 'conf rank CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-countdown'), 'countdown CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-fav'), 'fav star CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-field-chip'), 'field chip CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-rank-btn'), 'rank btn CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-search'), 'search CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-show-all'), 'show-all CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-footer'), 'footer CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-detail'), 'detail CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-override'), 'override marker CSS exists');
  // existing classes still present
  assert.ok(matching('.dpr-sidebar-schedule-badge'), 'schedule badge CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-chip'), 'schedule chip CSS exists');
  // color tier classes
  assert.ok(matching('.dpr-sidebar-schedule-conf-countdown.is-urgent'), 'urgent countdown CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-countdown.is-soon'), 'soon countdown CSS exists');
  assert.ok(matching('.dpr-sidebar-schedule-conf-countdown.is-normal'), 'normal countdown CSS exists');
  // 不得引入 sticky 定位或负 margin，避免破坏 sticky 堆叠合同
  const panelRule = cssRule(css, '.dpr-sidebar-schedule-panel');
  assert.ok(!/position:\s*sticky/.test(panelRule), 'no sticky on schedule panel');
}

function cssRule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp('(^|\\n)\\s*' + escaped + '\\s*\\{').exec(css);
  const index = match ? match.index + match[1].length : -1;
  assert.notEqual(index, -1, selector + ' CSS rule should exist');
  const start = css.indexOf('{', index);
  const end = css.indexOf('}', start);
  return css.slice(start + 1, end);
}

testParseSchedule();
testParseScheduleGraceful();
testComputeScheduleViewStates();
testComputeAllMilestones();
testRenderSchedulePanelShownWithoutConferenceData();
testCSSContract();

console.log('conference schedule tests passed');
