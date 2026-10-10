#!/bin/bash
# 清理知识库原始数据(raw/)中超过保留期的快照文件
# 保留最近 7 天,更早的删除;入库后 raw 即冗余,删掉控制体积
# 用法: ./cleanup_raw.sh [保留天数,默认7]
set -euo pipefail

PROJECT_DIR="/home/ubuntu/.openclaw/workspace/v4-online-bot"
RAW_DIR="$PROJECT_DIR/knowledge/raw"
LOG_DIR="$PROJECT_DIR/logs"
KEEP_DAYS="${1:-7}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/cleanup-$(date +%Y%m%d).log"

if [ ! -d "$RAW_DIR" ]; then
  echo "[$(date '+%F %T')] raw 目录不存在,跳过" | tee -a "$LOG"
  exit 0
fi

# 统计待删文件(仅常规文件,含子目录)
OLD_FILES=$(find "$RAW_DIR" -type f -mtime +"$KEEP_DAYS" | wc -l)
OLD_SIZE=$(find "$RAW_DIR" -type f -mtime +"$KEEP_DAYS" -printf '%s\n' 2>/dev/null | awk '{s+=$1} END {print s+0}')

if [ "$OLD_FILES" -eq 0 ]; then
  echo "[$(date '+%F %T')] 无超过 ${KEEP_DAYS} 天的 raw 文件,跳过" | tee -a "$LOG"
else
  # 先记清单再删(可回溯)
  find "$RAW_DIR" -type f -mtime +"$KEEP_DAYS" > "$LOG_DIR/cleanup-$(date +%Y%m%d).list"
  find "$RAW_DIR" -type f -mtime +"$KEEP_DAYS" -delete
  echo "[$(date '+%F %T')] 删除 ${OLD_FILES} 个文件,释放 $((OLD_SIZE/1024)) KB(保留 ${KEEP_DAYS} 天)" | tee -a "$LOG"
fi

# 顺带统计知识库文章数
ARTICLE_COUNT=$(ls "$PROJECT_DIR/knowledge/articles/"*.json 2>/dev/null | grep -v index.json | wc -l)
echo "[$(date '+%F %T')] 知识库当前文章数: ${ARTICLE_COUNT}" | tee -a "$LOG"
echo "[$(date '+%F %T')] raw 目录当前大小: $(du -sh "$RAW_DIR" | cut -f1)" | tee -a "$LOG"
