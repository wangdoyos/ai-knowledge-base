#!/bin/bash
# 每日知识管线 + 推送总控
# 流程：采集→分析→整理→入库 → 群推 markdown digest → 私推成本报告
set -euo pipefail

PROJECT_DIR="/home/ubuntu/.openclaw/workspace/v4-online-bot"
QQBOT_DIR="/home/ubuntu/.openclaw/workspace/qq-bot"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/daily-$RUN_ID.log"

export PATH="$HOME/.local/bin:$PATH"
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

cd "$PROJECT_DIR"
set -a
source .env
set +a

echo "=== [$(date '+%F %T')] 每日管线开始 ===" | tee -a "$LOG"

# 1. 跑管线(采集→分析→审核→入库),graph.py 自带成本报告落盘
.venv/bin/python workflows/graph.py >> "$LOG" 2>&1 || {
  echo "管线失败,日志: $LOG" >&2
  exit 1
}
echo "=== [$(date '+%F %T')] 管线完成 ===" | tee -a "$LOG"

# 2. 生成推送内容
.venv/bin/python prepare_push.py >> "$LOG" 2>&1 || {
  echo "推送内容准备失败,日志: $LOG" >&2
  exit 1
}

# 3. 推送(群 markdown + 私聊成本)
node "$QQBOT_DIR/src/push_daily.js" >> "$LOG" 2>&1 || {
  echo "推送失败,日志: $LOG" >&2
  exit 1
}

echo "=== [$(date '+%F %T')] 全部完成,日志: $LOG ===" | tee -a "$LOG"
