#!/usr/bin/env node
// 把知识库文章渲染成 QQ 群推送文本
// 用法:
//   node scripts/kb-render.mjs              # 全部文章(按 index 顺序)
//   node scripts/kb-render.mjs --id 20260817-github-01   # 单篇
//   node scripts/kb-render.mjs --latest     # 仅最新一篇
// 输出:stdout(UTF-8 文本,已分片友好)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const KB_DIR = path.resolve(__dirname, '../v4-online-bot/knowledge/articles');

function loadArticles() {
  const index = JSON.parse(fs.readFileSync(path.join(KB_DIR, 'index.json'), 'utf8'));
  return index.articles.map((meta) => {
    const fp = path.join(KB_DIR, `${meta.id}.json`);
    const detail = fs.existsSync(fp) ? JSON.parse(fs.readFileSync(fp, 'utf8')) : {};
    return { ...meta, ...detail };
  });
}

function renderOne(a) {
  const lines = [];
  lines.push(`📌 ${a.title}`);
  if (a.category) lines.push(`🏷️ 分类:${a.category}`);
  if (a.tags && a.tags.length) lines.push(`🏷️ 标签:${a.tags.join(' / ')}`);
  if (a.summary) lines.push(`\n${a.summary}`);
  if (a.key_points && a.key_points.length) {
    lines.push('\n要点:');
    a.key_points.forEach((p, i) => lines.push(`${i + 1}. ${p}`));
  }
  if (a.url) lines.push(`\n🔗 ${a.url}`);
  return lines.join('\n');
}

const args = process.argv.slice(2);
const articles = loadArticles();

if (args.includes('--latest')) {
  // 按 id 排序取最新(id 前缀是日期)
  const sorted = [...articles].sort((a, b) => String(b.id).localeCompare(String(a.id)));
  console.log(renderOne(sorted[0]));
} else if (args.includes('--id')) {
  const id = args[args.indexOf('--id') + 1];
  const a = articles.find((x) => x.id === id);
  if (!a) { console.error(`未找到文章: ${id}`); process.exit(2); }
  console.log(renderOne(a));
} else {
  articles.forEach((a, i) => {
    if (i > 0) console.log('\n' + '─'.repeat(24) + '\n');
    console.log(renderOne(a));
  });
}
