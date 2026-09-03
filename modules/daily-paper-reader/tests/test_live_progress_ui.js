const assert = require('node:assert/strict');

global.window = {
  location: { hash: '#/' },
  localStorage: { getItem: () => null, setItem: () => {} },
  addEventListener: () => {},
  setTimeout,
  clearTimeout,
  matchMedia: () => ({ matches: false }),
  CSS: { escape: (value) => String(value) },
};
global.document = {
  readyState: 'loading',
  addEventListener: () => {},
  dispatchEvent: () => {},
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => {
    let text = '';
    return {
      set innerHTML(value) { text = String(value || '').replace(/<[^>]*>/g, ''); },
      get innerHTML() { return text; },
      get textContent() { return text; },
      set textContent(value) { text = String(value == null ? '' : value); },
    };
  },
  body: { appendChild: () => {}, classList: { add: () => {} } },
  documentElement: { style: { setProperty: () => {} } },
};

require('../app/task-progress.js');
const suffix = window.DPRTaskProgress.formatSuffix({
  current: 3,
  total: 29,
  payload: { percent: 10.3, rate: 0.02, eta_seconds: 1140 },
});
assert.equal(suffix, '（3/29 · 10.3% · 50 秒/篇 · 预计剩余 19 分钟）');

const sidebar = require('../app/dpr-sidebar.js').api;
const changed = sidebar.syncLivePapers({
  id: 'run-live-1',
  events: [{
    event_type: 'run.progress',
    payload: {
      step: 'step_6_generate',
      paper_status: 'completed',
      paper_id: '20260823-20260901/2608.1v1-a-vlm-paper',
      paper_title: 'A VLM Paper',
      section: 'deep',
    },
  }],
});
assert.equal(changed, true);
assert.ok(sidebar.getPaperHrefs().includes(
  '#/api/local/runtime/docs/20260823-20260901/2608.1v1-a-vlm-paper',
));

console.log('live progress UI tests passed');
