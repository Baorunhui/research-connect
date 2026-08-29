const assert = require('node:assert/strict');

const { getRawPaperSections } = require('../app/zotero-meta-utils.js');

const sample = `---
title: Attention Is All You Need
---

## 摘要
这是中文摘要。

## 📝 TLDR
这是 tldr。

## Abstract
This is the original abstract in English.

## 论文详细总结（自动生成）
This is ai summary.
`;

const sections = getRawPaperSections(sample);

assert.equal(sections.chineseAbstractText, '这是中文摘要。');
assert.equal(sections.tldrText, '这是 tldr。');
assert.equal(sections.originalAbstractText, 'This is the original abstract in English.');
assert.equal(sections.aiSummaryText, 'This is ai summary.');

console.log('zotero meta utils tests passed');
