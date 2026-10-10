# QQ 知识库推送机器人

QQ 开放平台官方机器人:支持**群聊**与**私聊**,用于把知识库文章推送到群里。

## 运行

```bash
# 机器人(长连接,群@回复 + 私聊回复)
node src/bot.js

# 手动推送内容到群(测试用)
node src/push.js <group_openid> "文本"
node src/push.js <group_openid> --file 文章.md
```

后台运行(本机无 systemd user,用 nohup):

```bash
nohup node src/bot.js >> bot.log 2>&1 &
```

## 配置

- `.env`: `APP_ID` / `APP_SECRET`(已 gitignore,勿提交)
- `src/config.js` 中 `BASE`:正式环境 `https://api.sgroup.qq.com`;
  若机器人未过审(沙箱模式),改为 `https://sandbox.api.sgroup.qq.com`

## 结构

- `src/api.js` — token 获取/缓存、群消息、私聊消息、分片发送
- `src/bot.js` — WebSocket 长连接、心跳、重连、事件分发、指令处理
- `src/push.js` — 手动推送 CLI

## 待接入/待确认

- [ ] 知识库位置与格式(路径、文章目录结构)
- [ ] 目标群:机器人需先被拉进群;群 `group_openid` 可从群内 @ 机器人的消息事件中获取
- [ ] 机器人审核状态(未过审只能走沙箱 + 测试群)
- [ ] 主动推送频控需实测(官方对主动消息有限额)
- [ ] 定时推送(确认知识库与群后可用 cron 接上)
