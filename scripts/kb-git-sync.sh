#!/bin/bash
# 每日增量同步:把 AI 知识库(管线 + 文章 + 推送脚本)提交并推送到 GitHub
# 由 cron 每天 8:30 调用(管线 8:00 跑完后)
# 仓库:ssh://git@ssh.github.com:443/wangdoyos/ai-knowledge-base.git
# 注意:github.com:443 在本机被墙,必须走 ssh.github.com:443
set -euo pipefail

export GIT_DIR=/home/ubuntu/.kb-git.git
export GIT_WORK_TREE=/home/ubuntu/.openclaw/workspace

cd "$GIT_WORK_TREE"

# 只跟踪这三个目录;.env / .venv / logs / raw 已由 .gitignore 排除
git add v4-online-bot qq-bot scripts

if git diff --cached --quiet; then
  echo "[$(date '+%F %T')] 无变更,跳过推送"
  exit 0
fi

git commit -q -m "sync: knowledge base update $(date '+%F %T')"
if git push -q origin main; then
  echo "[$(date '+%F %T')] 已推送 $(git rev-parse --short HEAD)"
else
  echo "[$(date '+%F %T')] ❌ 推送失败" >&2
  exit 1
fi
