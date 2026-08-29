const assert = require('node:assert/strict');

const { inferSpeaker, buildChatLinesFromMessages } = require('../app/zotero-chat-utils.js');

assert.equal(inferSpeaker({ roleText: '你', className: '' }), 'User');
assert.equal(inferSpeaker({ roleText: '助手', className: '' }), 'AI');
assert.equal(inferSpeaker({ roleText: '', className: 'msg-content msg-content-user' }), 'User');
assert.equal(inferSpeaker({ roleText: '', className: 'msg-content msg-content-ai' }), 'AI');
assert.equal(inferSpeaker({ roleText: '思考过程', className: 'msg-content thinking-history-content' }), '');
assert.equal(inferSpeaker({ roleText: '', className: 'thinking-history-content' }), '');

assert.deepEqual(
  buildChatLinesFromMessages([
    { role: 'user', content: '你好' },
    { role: 'ai', content: '公式 $a=b$' },
    { role: 'thinking', content: 'ignore me' },
  ]),
  ['👤 User: 你好', '🤖 AI: 公式 $a=b$'],
);

console.log('zotero chat utils tests passed');
