const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function extractAssetLoaderScript(html) {
  const scripts = [...String(html).matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const script = scripts.find((body) => body.includes('window.DPRLoadAssets'));
  assert.ok(script, 'index.html should contain DPRLoadAssets bootstrap script');
  return script;
}

async function runAssetLoader(hostname, assets, windowOverrides = {}, appendElement) {
  const appended = [];
  const fetches = [];
  const timers = new Set();
  const sandbox = {
    console,
    location: { hostname },
    window: { ...windowOverrides },
    fetch(url, options) {
      fetches.push({ url, options: options || {} });
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ ok: true }),
      });
    },
    setTimeout() {
      const id = Symbol('timer');
      timers.add(id);
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    document: {
      createElement(tagName) {
        return {
          tagName: String(tagName || '').toLowerCase(),
          remove() {},
          setAttribute(key, value) {
            this[key] = value;
          },
        };
      },
      head: {
        appendChild(el) {
          appended.push(el);
          if (typeof appendElement === 'function' && appendElement(el, appended)) return;
          if (typeof el.onload === 'function') setImmediate(el.onload);
        },
      },
    },
  };
  sandbox.window.fetch = sandbox.fetch;
  sandbox.window.setTimeout = sandbox.setTimeout;
  sandbox.window.clearTimeout = sandbox.clearTimeout;

  const script = extractAssetLoaderScript(fs.readFileSync('index.html', 'utf8'));
  vm.runInNewContext(script, sandbox, { filename: 'index.html' });
  await sandbox.window.DPRLoadAssets(assets);
  appended.fetches = fetches;
  appended.jsonPromises = sandbox.window.DPR_ASSET_JSON_PROMISES || {};
  return appended;
}

async function testLocalScriptRetriesAfterTransientFailure() {
  let scriptAttempts = 0;
  await runAssetLoader(
    'localhost',
    [{ type: 'script', path: 'app/chat.discussion.js' }],
    {},
    (element) => {
      if (element.tagName !== 'script') return false;
      scriptAttempts += 1;
      if (scriptAttempts === 1) {
        setImmediate(element.onerror);
      } else {
        setImmediate(element.onload);
      }
      return true;
    },
  );

  assert.equal(scriptAttempts, 2, 'local scripts should retry once after a transient failure');
}

function testInitialLoadFailureCannotLeavePendingBlankScreen() {
  const html = fs.readFileSync('index.html', 'utf8');
  assert.match(html, /window\.DPRShowInitialLoadError\s*=\s*function/);
  assert.match(html, /app\.removeAttribute\(['"]data-dpr-pending['"]\)/);
  // secret-gate-overlay and its secret-gate-hidden class no longer exist in index.html
  assert.ok(!html.includes('secret-gate-hidden'), 'index.html should not contain secret-gate-hidden class');
  assert.match(html, /window\.DPRShowInitialLoadError\(err\)/);
  assert.match(
    html,
    /if\s*\(app\s*&&\s*app\.hasAttribute\(['"]data-dpr-pending['"]\)\)\s*return/,
    'window load fallback must not hide the splash while the app is still pending',
  );
}

async function testAllAssetsLoadFromLocalPathsOnly() {
  const hostnames = ['localhost', '127.0.0.1', 'example.github.io'];
  for (const hostname of hostnames) {
    const appended = await runAssetLoader(hostname, [
      { type: 'style', path: 'app/app.css' },
      { type: 'style', path: 'app/vendor/docsify/4/lib/themes/vue.css' },
      { type: 'script', path: 'app/dpr-sidebar.js' },
      { type: 'script', path: 'app/vendor/docsify/4/lib/docsify.min.js' },
    ]);
    const urls = appended.map((el) => el.href || el.src || '');

    for (const expected of ['app/app.css', 'app/vendor/docsify/4/lib/themes/vue.css', 'app/dpr-sidebar.js', 'app/vendor/docsify/4/lib/docsify.min.js']) {
      assert.ok(urls.includes(expected), `"${expected}" should be loaded locally on ${hostname}`);
    }

    const cdnUrls = urls.filter((u) => u.startsWith('https://'));
    assert.equal(cdnUrls.length, 0, `no CDN URLs should be used on ${hostname}`);
  }
}

function testIndexHtmlDoesNotContainRemovedModules() {
  const html = fs.readFileSync('index.html', 'utf8');

  // secret-gate-overlay element removed
  assert.ok(!html.includes('secret-gate-overlay'), 'index.html should not contain secret-gate-overlay');

  // removed scripts
  assert.ok(!html.includes('secret.session.js'), 'index.html should not reference app/secret.session.js');
  assert.ok(!html.includes("'app/feedback.issue.js'"), 'index.html should not reference app/feedback.issue.js');
  assert.ok(!html.includes("'app/gist-share-utils.js'"), 'index.html should not reference app/gist-share-utils.js');
  assert.ok(!html.includes('subscriptions.github-token.js'), 'index.html should not reference subscriptions.github-token.js');
  assert.ok(!html.includes('subscriptions.manager.js'), 'index.html should not reference subscriptions.manager.js');
  assert.ok(!html.includes('subscriptions.smart-query.js'), 'index.html should not reference subscriptions.smart-query.js');
  assert.ok(!html.includes('subscriptions.keywords.js'), 'index.html should not reference subscriptions.keywords.js');
  assert.ok(!html.includes('subscriptions.zotero.js'), 'index.html should not reference subscriptions.zotero.js');
  assert.ok(!html.includes('subscriptions.tracked-papers.js'), 'index.html should not reference subscriptions.tracked-papers.js');

  // CDN flags removed
  assert.ok(!html.includes('DPR_CDN_ACTIVE'), 'index.html should not set DPR_CDN_ACTIVE');
  assert.ok(!html.includes('DPR_ASSET_BASE'), 'index.html should not set DPR_ASSET_BASE');
}

Promise.resolve()
  .then(testLocalScriptRetriesAfterTransientFailure)
  .then(testAllAssetsLoadFromLocalPathsOnly)
  .then(testIndexHtmlDoesNotContainRemovedModules)
  .then(testInitialLoadFailureCannotLeavePendingBlankScreen)
  .then(() => {
    console.log('index asset loader tests passed');
  })
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });