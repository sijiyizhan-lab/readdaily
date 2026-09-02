#!/usr/bin/env bash
# readdaily uninstall.sh —— 移除技能软链与定时任务（不删除数据/Vault）
set -uo pipefail
for host in agents codex claude workbuddy; do
  hdir="$HOME/.$host/skills"; [[ "$host" == "agents" ]] && hdir="$HOME/.agents/skills"
  for skill in newspaper-fetch newspaper-reader read-daily; do
    [[ -L "$hdir/$skill" ]] && rm -f "$hdir/$skill" && echo "removed $hdir/$skill"
  done
done
for spec in com.guopeijun.daily-reader com.guopeijun.jianshebao-daily; do
  launchctl bootout "gui/$(id -u)/$spec" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$spec.plist" && echo "removed $spec"
done
echo "数据与 Vault 保留：$HOME/Library/Application Support/readdaily"
