import { getToken, getGatewayUrl, sendGroupMessage, sendC2CMessage } from './api.js';

// 意图:群聊 + 私聊(C2C)消息事件
const INTENTS = 1 << 25; // GROUP_AND_C2C_EVENT
const RECONNECT_DELAY = 5000;

function log(...a) {
  console.log(new Date().toISOString(), ...a);
}

async function run() {
  const token = await getToken();
  const { url } = await getGatewayUrl();
  log('已连接 gateway,建立 WebSocket...');

  const ws = new WebSocket(url);
  let seq = 0;
  let heartbeatTimer = null;

  const identify = () =>
    ws.send(JSON.stringify({ op: 2, d: { token: `QQBot ${token}`, intents: INTENTS, shard: [0, 1] } }));

  const stopHeartbeat = () => {
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }
  };

  ws.onmessage = async (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.s) seq = msg.s;
    switch (msg.op) {
      case 10: // Hello
        stopHeartbeat();
        heartbeatTimer = setInterval(() => {
          try { ws.send(JSON.stringify({ op: 1, d: seq })); } catch { /* ignore */ }
        }, msg.d.heartbeat_interval);
        identify();
        break;
      case 11: // Heartbeat ACK
        break;
      case 7: // 要求重连:等 close 后自动重连
        log('收到 op7,准备重连');
        break;
      case 9: // Invalid session:重新 identify
        log('收到 op9,重新 identify');
        identify();
        break;
      case 0: // Dispatch
        await handleEvent(msg.t, msg.d);
        break;
      default:
        break;
    }
  };

  ws.onclose = (e) => {
    log('连接关闭', e.code, e.reason || '');
    stopHeartbeat();
    setTimeout(run, RECONNECT_DELAY);
  };

  ws.onerror = (e) => {
    log('WebSocket 错误', e.message || e);
  };
}

async function handleEvent(t, d) {
  if (t === 'READY') {
    log('READY: 机器人上线,user=', d.user?.username || d.user?.id);
    return;
  }
  if (t === 'GROUP_AT_MESSAGE_CREATE') {
    const groupOpenId = d.group_openid;
    const userId = d.author?.member_openid;
    const content = (d.content || '').replace(/<@!?\w+>/g, '').trim();
    log('群@消息:', userId, 'in', groupOpenId, '=>', content);
    try { await onGroupAt(groupOpenId, userId, content, d.id); } catch (e) { log('群消息处理失败:', e.message); }
    return;
  }
  if (t === 'C2C_MESSAGE_CREATE') {
    const userId = d.author?.user_openid;
    const content = (d.content || '').trim();
    log('私聊消息:', userId, '=>', content);
    try { await onC2C(userId, content, d.id); } catch (e) { log('私聊处理失败:', e.message); }
    return;
  }
}

// ===== 业务逻辑(后续接知识库时在此扩展) =====

async function onGroupAt(groupOpenId, _userId, content, msgId) {
  if (!content) return;
  const reply = await handleCommand(content);
  if (reply) await sendGroupMessage(groupOpenId, reply, msgId);
}

async function onC2C(userId, content, msgId) {
  if (!content) return;
  const reply = await handleCommand(content);
  if (reply) await sendC2CMessage(userId, reply, msgId);
}

async function handleCommand(content) {
  const c = content.trim();
  if (/^(帮助|help|菜单)/i.test(c)) {
    return [
      '📚 知识库推送机器人「AI知识库」',
      '可用指令:',
      '  /help  显示帮助',
      '  /ping  连通测试',
      '(知识库查询/推送指令待接入)',
    ].join('\n');
  }
  if (/^\/ping/i.test(c)) return 'pong 🦀';
  // 普通消息也回应(私聊/群@),避免“没反应”的错觉
  return '收到 ✅ 我是「AI知识库」机器人。发送 /help 查看可用指令。';
}

run().catch((e) => {
  log('启动失败:', e.message);
  process.exit(1);
});
