'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');

const index = fs.readFileSync('index.html', 'utf8');
const sidebar = fs.readFileSync('app/dpr-sidebar.js', 'utf8');
const plugin = fs.readFileSync('app/docsify-plugin.js', 'utf8');
const runner = fs.readFileSync('app/workflows.runner.js', 'utf8');

assert.match(index, /window\.DPR_SIDEBAR_TEXT = fetch\('docs\/_sidebar\.md'/);
assert.doesNotMatch(index, /rel = 'preload'/);
assert.doesNotMatch(index, /docs\/README/);
assert.doesNotMatch(index, /'docs\/_sidebar\.md',\s*\n\s*'docs\/'/);
assert.match(sidebar, /window\.DPR_SIDEBAR_TEXT/);
assert.match(plugin, /authorsFromPaperText\(rawPaperContent, frontmatterPaperMeta\)/);
assert.match(plugin, /Math\.min\(authorParagraphs\.length, 6\)/);
assert.doesNotMatch(runner, /python src\/local_server\.py/);
assert.doesNotMatch(runner, /run_id=/);
assert.doesNotMatch(runner, /任务 #/);
assert.match(fs.readFileSync('app/paper-summarize.js', 'utf8'), /只用于这次总结，上限 50MB/);
assert.doesNotMatch(fs.readFileSync('app/chat.discussion.js', 'utf8'), /仅保存在本机/);

console.log('sidebar prefetch tests passed');
