#!/usr/bin/env node
// 手动把内容推送到指定群
// 用法:
//   node src/push.js <group_openid> "文本内容"
//   node src/push.js <group_openid> --file 文章.md
import fs from 'node:fs';
import { sendToGroupChunked } from './api.js';

const [, , groupOpenId, modeOrText, ...rest] = process.argv;

if (!groupOpenId) {
  console.error('用法:\n  node src/push.js <group_openid> "<文本>"\n  node src/push.js <group_openid> --file <文件路径>');
  process.exit(2);
}

let content;
if (modeOrText === '--file') {
  const p = rest[0];
  if (!p || !fs.existsSync(p)) {
    console.error('文件不存在:', p);
    process.exit(2);
  }
  content = fs.readFileSync(p, 'utf8');
} else {
  content = modeOrText || '';
}
if (!content.trim()) {
  console.error('内容为空');
  process.exit(2);
}

sendToGroupChunked(groupOpenId, content)
  .then((rs) => { console.log(`已发送 ${rs.length} 条到群 ${groupOpenId}`); process.exit(0); })
  .catch((e) => { console.error('发送失败:', e.message); process.exit(1); });
