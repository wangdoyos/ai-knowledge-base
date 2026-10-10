#!/usr/bin/env node
// 微信版知识库推送:渲染知识库文章 -> openclaw message send 发到微信大号(ClawBot 私聊)
// 由 cron 任务调用;发送走 openclaw CLI(渠道 openclaw-weixin)
// 用法: node scripts/kb-push-wechat.mjs [--all | --latest | --id <id>]
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const KB_DIR = path.join(ROOT, 'v4-online-bot/knowledge/articles');

// 微信大号 peer id(老王,扫码绑定后由 ClawBot 会话生成)
const TARGET = 'o9cq807CaS66i9xYFj4Q5jNBX1ZM@im.wechat';

// ---- 渲染(与 kb-push.mjs 保持一致) ----
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

function pickArticles(args) {
  const all = loadArticles();
  if (args.includes('--latest')) {
    return [[...all].sort((a, b) => String(b.id).localeCompare(String(a.id)))[0]];
  }
  if (args.includes('--id')) {
    const id = args[args.indexOf('--id') + 1];
    const a = all.find((x) => x.id === id);
    if (!a) throw new Error(`未找到文章: ${id}`);
    return [a];
  }
  return all;
}

function splitContent(content, max = 1800) {
  const out = [];
  let rest = String(content ?? '').trim();
  while (rest.length > max) {
    let cut = rest.lastIndexOf('\n', max);
    if (cut <= 0) cut = max;
    out.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) out.push(rest);
  return out;
}

// ---- 发送(走 openclaw CLI,渠道 openclaw-weixin) ----
function sendWechat(text) {
  const r = spawnSync(
    'openclaw',
    ['message', 'send', '--channel', 'openclaw-weixin', '--target', TARGET, '--message', text],
    { encoding: 'utf8', timeout: 60000 },
  );
  if (r.status !== 0) {
    throw new Error(`openclaw message send 失败: ${(r.stderr || r.stdout || '').trim().slice(0, 300)}`);
  }
  return r.stdout;
}

// ---- main ----
const args = process.argv.slice(2);
const articles = pickArticles(args);
if (!articles.length) { console.error('知识库为空,无文章可推送'); process.exit(2); }

const header = `📚 知识库推送 · ${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}`;
let body = articles.map(renderOne).join('\n\n' + '─'.repeat(24) + '\n\n');
const full = `${header}\n${body}`;

const chunks = splitContent(full);
for (let i = 0; i < chunks.length; i++) {
  const out = sendWechat(chunks[i]);
  if (chunks.length > 1) console.log(`第 ${i + 1}/${chunks.length} 条: ${out.trim().slice(0, 120)}`);
}
console.log(`OK 已推送 ${articles.length} 篇(${chunks.length} 条)到微信 ${TARGET}`);
