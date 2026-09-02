#!/usr/bin/env bash
# readdaily install.sh —— 幂等安装：技能软链（多端）→ OCR 构建 → launchd/Cron → 数据与 Vault 初始化
# 用法: ./install.sh [--dry-run] [--all-hosts] [--force-replace] [--vault <path>]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_ROOT="${READDAILY_DATA:-$HOME/Library/Application Support/readdaily}"
ARCHIVE="$DATA_ROOT/news-archive"
VAULT="${READDAILY_VAULT:-$DATA_ROOT/vault}"
HOSTS=(agents codex)   # 默认含 Codex；--all-hosts 覆盖 Claude/WorkBuddy
DRY=0
FORCE=0
VOCR_DEST="$REPO_DIR/skills/newspaper-fetch/bin/vocr"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force-replace) FORCE=1 ;;
    --all-hosts) HOSTS=(agents codex claude workbuddy) ;;
    --vault) VAULT="$2"; shift ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
  shift
done

log() { if [[ $DRY -eq 1 ]]; then echo "[dry-run] $*"; else echo "[install] $*"; fi; }
run()  { if [[ $DRY -eq 1 ]]; then log "run: $*"; else "$@"; fi; }

echo "== readdaily 安装 =="
echo "  源仓库: $REPO_DIR"

# 1) 技能软链（Claude/Codex/通用 Agents/WorkBuddy 共用同一源，改仓库即同步）
for host in "${HOSTS[@]}"; do
  hdir="$HOME/.$host/skills"; [[ "$host" == "agents" ]] && hdir="$HOME/.agents/skills"
  for skill in newspaper-fetch newspaper-reader read-daily; do
    dst="$hdir/$skill"
    src="$REPO_DIR/skills/$skill"
    if [[ -L "$dst" ]]; then
      log "软链已存在 (更新): $dst"
      run rm -f "$dst"; run ln -s "$src" "$dst"
    elif [[ -e "$dst" ]]; then
      if [[ $FORCE -eq 1 ]]; then
        log "真实目录 -> 备份: $dst -> $dst.bak"
        run mv "$dst" "$dst.bak"; run ln -s "$src" "$dst"
      else
        echo "!! $dst 已是真实目录，跳过（用 --force-replace 备份后接管）"
      fi
    else
      run mkdir -p "$hdir"
      run ln -s "$src" "$dst"
    fi
  done
done

# 2) OCR 二进制（macOS + swiftc；非 macOS 或无 swiftc 则跳过，微信渠道届时不可用）
if command -v swiftc >/dev/null 2>&1; then
  log "构建 Vision OCR (swiftc)..."
  run mkdir -p "$REPO_DIR/skills/newspaper-fetch/bin"
  run swiftc -O -o "$VOCR_DEST" "$REPO_DIR/scripts/vocr.swift"
else
  echo "!! 未找到 swiftc（仅 macOS）；跳过 OCR 构建。微信读报渠道不可用，方正/API 渠道不受影响。"
fi

# 3) 定时任务（macOS launchd；其他平台给出 cron 建议）
if [[ "$(uname)" == "Darwin" ]]; then
  for spec in com.guopeijun.daily-reader com.guopeijun.jianshebao-daily com.guopeijun.readdaily-llm; do
    src="$REPO_DIR/skills/newspaper-fetch/plists/$spec.plist"
    dst="$HOME/Library/LaunchAgents/$spec.plist"
    if [[ -f "$src" ]]; then
      run sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO_DIR|g" "$src" > "$dst"
      run launchctl bootout "gui/$(id -u)/$spec" 2>/dev/null || true
      run launchctl bootstrap "gui/$(id -u)" "$dst" || log "bootstrap 跳过（launchd 状态异常时手动执行）"
    fi
  done
else
  echo "!! 非 macOS：请用 cron 添加："
  echo "   0 10 * * *  python3 $REPO_DIR/scripts/readdaily.py fetch"
fi

# 4) 数据与 Vault 初始化
run mkdir -p "$ARCHIVE" "$VAULT" "$VAULT/报纸原文" "$VAULT/每日摘要" "$VAULT/主体跟踪" "$VAULT/看板" "$VAULT/_templates"
for t in "$REPO_DIR"/config/templates/*.md; do
  [[ -f "$t" ]] && run cp -n "$t" "$VAULT/_templates/"
done
run touch "$ARCHIVE/.keep"

cat <<EOF

✅ 安装完成
  技能（多端软链）: ~/.agents/skills / ~/.codex/skills / …
  数据根: $ARCHIVE
  Vault : $VAULT  （Obsidian 打开此目录；可改为你自己的 vault: --vault <path>）
  每日任务: macOS launchd 已加载（10:25/20:00 抓取；归纳需 Agent 会话或 API）
  CLI: python3 $REPO_DIR/scripts/readdaily.py status

  发布到 GitHub 后：git clone → ./install.sh 即用。
EOF
