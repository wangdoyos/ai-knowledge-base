import { APP_ID, APP_SECRET, BASE } from './config.js';

let token = null;
let tokenExpAt = 0;

// 获取 access_token(缓存,提前 5 分钟刷新)
export async function getToken(force = false) {
  if (!force && token && Date.now() < tokenExpAt - 5 * 60 * 1000) return token;
  const res = await fetch('https://bots.qq.com/app/getAppAccessToken', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ appId: APP_ID, clientSecret: APP_SECRET }),
  });
  if (!res.ok) throw new Error(`getAppAccessToken ${res.status}: ${await res.text()}`);
  const j = await res.json();
  if (!j.access_token) throw new Error(`getAppAccessToken 无 token: ${JSON.stringify(j)}`);
  token = j.access_token;
  tokenExpAt = Date.now() + (j.expires_in ?? 7200) * 1000;
  return token;
}

async function qq(pathname, opts = {}) {
  const t = await getToken();
  const res = await fetch(`${BASE}${pathname}`, {
    ...opts,
    headers: {
      Authorization: `QQBot ${t}`,
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  });
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try { detail = JSON.stringify(JSON.parse(text)); } catch { /* keep raw */ }
    throw new Error(`${opts.method || 'GET'} ${pathname} -> ${res.status}: ${detail}`);
  }
  return text ? JSON.parse(text) : {};
}

export function getGatewayUrl() {
  return qq('/gateway');
}

// 群消息:msgId 传了就按被动回复(带上下文窗口),不传即主动推送
export function sendGroupMessage(groupOpenId, content, msgId) {
  const body = { msg_type: 0, content };
  if (msgId) body.msg_id = msgId;
  return qq(`/v2/groups/${encodeURIComponent(groupOpenId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// 私聊(C2C)消息
export function sendC2CMessage(userOpenId, content, msgId) {
  const body = { msg_type: 0, content };
  if (msgId) body.msg_id = msgId;
  return qq(`/v2/users/${encodeURIComponent(userOpenId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// 私聊(C2C) Markdown 消息(msg_type=2,2026/04/23 起自定义 markdown 全量开放无需模板)
export function sendC2CMarkdown(userOpenId, markdownContent, msgId) {
  const body = { msg_type: 2, markdown: { content: markdownContent } };
  if (msgId) body.msg_id = msgId;
  return qq(`/v2/users/${encodeURIComponent(userOpenId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// 群聊 Markdown 消息(msg_type=2)
export function sendGroupMarkdown(groupOpenId, markdownContent, msgId) {
  const body = { msg_type: 2, markdown: { content: markdownContent } };
  if (msgId) body.msg_id = msgId;
  return qq(`/v2/groups/${encodeURIComponent(groupOpenId)}/messages`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// 超长内容分片发送(群消息单条 content 上限约 4000 字符)
export function splitContent(content, max = 4000) {
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

export async function sendToGroupChunked(groupOpenId, content, msgId) {
  const chunks = splitContent(content);
  const results = [];
  for (let i = 0; i < chunks.length; i++) {
    // 仅第一条带 msg_id(被动回复场景),其余走主动消息
    results.push(await sendGroupMessage(groupOpenId, chunks[i], i === 0 ? msgId : undefined));
  }
  return results;
}
