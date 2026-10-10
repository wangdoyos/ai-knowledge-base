#!/usr/bin/env node
// 每日推送：群单条 markdown digest + 私聊成本报告
// 读取 /tmp/kb_push/(manifest.json + digest.md) 和 /tmp/kb_cost.txt
import fs from 'node:fs';
import path from 'node:path';
import {
  sendGroupMarkdown,
  sendC2CMessage,
} from '/home/ubuntu/.openclaw/workspace/qq-bot/src/api.js';

const GROUP_OPENID = '45C654585D9355E1D9A6D018B8489519'; // 测试群
const USER_OPENID = '7DFC63C774318C760F06E8721EA3CF59'; // 老王私聊
const PUSH_DIR = '/tmp/kb_push';
const COST_FILE = '/tmp/kb_cost.txt';

async function pushGroup() {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(PUSH_DIR, 'manifest.json'), 'utf8'),
  );
  const digestPath = path.join(PUSH_DIR, 'digest.md');
  let content;
  if (manifest.count === 0 || !fs.existsSync(digestPath)) {
    content = `📭 ${manifest.date} 暂无新增知识条目`;
  } else {
    content = fs.readFileSync(digestPath, 'utf8');
  }
  await sendGroupMarkdown(GROUP_OPENID, content);
  console.log(`群推送：单条 digest(${manifest.count} 篇, ${content.length} 字符)`);
}

async function pushCost() {
  let text = fs.readFileSync(COST_FILE, 'utf8');
  // 断更护栏：当日 0 篇入库时在成本报告前加预警，避免静默断更无人察觉
  try {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(PUSH_DIR, 'manifest.json'), 'utf8'),
    );
    if (manifest.count === 0) {
      text =
        `⚠️ 断更预警：${manifest.date} 当日 0 篇新增入库` +
        '(可能为审核未通过或采集异常)，请检查管线日志。\n\n' +
        text;
    }
  } catch { /* manifest 缺失时不影响成本报告推送 */ }
  await sendC2CMessage(USER_OPENID, text);
  console.log('私聊推送：成本报告');
}

pushGroup()
  .then(pushCost)
  .then(() => { console.log('全部推送完成'); process.exit(0); })
  .catch((e) => { console.error('推送失败:', e.message); process.exit(1); });
