#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
PACKAGE_DIR="$REPO_ROOT/apps/ConstructionReadingDesk"
OUTPUT_APP="${1:-$REPO_ROOT/dist/建设读报台.app}"

if [[ "$OUTPUT_APP" != *.app || "$OUTPUT_APP" == "/" || "$OUTPUT_APP" == "$REPO_ROOT" ]]; then
  print -u2 "错误：输出路径必须是明确的 .app 路径。"
  exit 2
fi

INFO_PLIST="$PACKAGE_DIR/Resources/Info.plist"
plutil -lint "$INFO_PLIST" >/dev/null

swift build --package-path "$PACKAGE_DIR" -c release
BIN_DIR="$(swift build --package-path "$PACKAGE_DIR" -c release --show-bin-path)"
BINARY="$BIN_DIR/ConstructionReadingDesk"

if [[ ! -x "$BINARY" ]]; then
  print -u2 "错误：未找到 release 可执行文件：$BINARY"
  exit 3
fi

CONTENTS="$OUTPUT_APP/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES_DIR="$CONTENTS/Resources"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
install -m 755 "$BINARY" "$MACOS_DIR/ConstructionReadingDesk"
install -m 644 "$INFO_PLIST" "$CONTENTS/Info.plist"

if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - --timestamp=none "$OUTPUT_APP"
  codesign --verify --strict "$OUTPUT_APP"
fi

print "构建完成：$OUTPUT_APP"
