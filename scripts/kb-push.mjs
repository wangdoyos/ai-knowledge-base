#!/usr/bin/env node
// 定时推送:渲染知识库最新文章 -> 推送到 QQ 群
// 由 cron 任务调用;依赖 qq-bot 目录的 .env(APP_ID/APP_SECRET)
// 用法: node scripts/kb-push.mjs [group_openid] [--all | --latest | --id <id>]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const KB_DIR = path.join(ROOT, 'v4-online-bot/knowledge/articles');
const QQ_BOT_DIR = path.join(ROOT, 'qq-bot');

// 默认目标群(测试群);可用第一个参数覆盖
const DEFAULT_GROUP = '45C654585D9355E1D9A6D018B8489519';

// ---- 渲染(与 kb-render.mjs 保持一致) ----
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

// ---- 推送(复用 qq-bot 的 API 逻辑) ----
function loadEnv() {
  const env = {};
  const envPath = path.join(QQ_BOT_DIR, '.env');
  if (fs.existsSync(envPath)) {
    for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  }
  return env;
}

let tokenCache = null;
let tokenExpAt = 0;

async function getToken(env) {
  if (tokenCache && Date.now() < tokenExpAt - 5 * 60 * 1000) return tokenCache;
  const res = await fetch('https://bots.qq.com/app/getAppAccessToken', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ appId: env.APP_ID, clientSecret: env.APP_SECRET }),
  });
  if (!res.ok) throw new Error(`getAppAccessToken ${res.status}: ${await res.text()}`);
  const j = await res.json();
  if (!j.access_token) throw new Error(`getAppAccessToken 无 token: ${JSON.stringify(j)}`);
  tokenCache = j.access_token;
  tokenExpAt = Date.now() + (j.expires_in ?? 7200) * 1000;
  return tokenCache;
}

const BASE = 'https://api.sgroup.qq.com';

async function sendGroupMessage(env, groupOpenId, content) {
  const t = await getToken(env);
  const res = await fetch(`${BASE}/v2/groups/${encodeURIComponent(groupOpenId)}/messages`, {
    method: 'POST',
    headers: { Authorization: `QQBot ${t}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ msg_type: 0, content }),
  });
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try { detail = JSON.stringify(JSON.parse(text)); } catch { /* keep raw */ }
    throw new Error(`POST /v2/groups/... -> ${res.status}: ${detail}`);
  }
  return JSON.parse(text);
}

function splitContent(content, max = 4000) {
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

// ---- main ----
const args = process.argv.slice(2);
const groupOpenId = (args[0] && !args[0].startsWith('--')) ? args.shift() : DEFAULT_GROUP;

const articles = pickArticles(args);
if (!articles.length) { console.error('知识库为空,无文章可推送'); process.exit(2); }

const env = loadEnv();
if (!env.APP_ID || !env.APP_SECRET) { console.error('缺少 APP_ID/APP_SECRET(qq-bot/.env)'); process.exit(2); }

const header = `📚 知识库推送 · ${new Date().toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}`;
let body = articles.map(renderOne).join('\n\n' + '─'.repeat(24) + '\n\n');
const full = `${header}\n${body}`;

const chunks = splitContent(full);
for (let i = 0; i < chunks.length; i++) {
  await sendGroupMessage(env, groupOpenId, chunks[i]);
}
console.log(`OK 已推送 ${articles.length} 篇(${chunks.length} 条)到群 ${groupOpenId}`);
