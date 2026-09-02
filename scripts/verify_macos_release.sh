#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
APP_PATH="${1:-$REPO_ROOT/dist/Read Daily.app}"
INFO_PLIST="$APP_PATH/Contents/Info.plist"
VERSION="$(plutil -extract CFBundleShortVersionString raw "$INFO_PLIST")"
ARCHIVE_PATH="${2:-$REPO_ROOT/dist/Read-Daily-v${VERSION}-macOS-arm64.zip}"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
RUNTIME="$APP_PATH/Contents/Resources/readdaily"
VERIFY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/readdaily-release-verify.XXXXXX")"

cleanup() {
  if [[ -d "$VERIFY_DIR" ]]; then /bin/rm -rf -- "$VERIFY_DIR"; fi
}
trap cleanup EXIT INT TERM

fail() {
  print -u2 "验证失败：$1"
  exit 1
}

assert_same_file() {
  local source_file="$1"
  local bundled_file="$2"
  [[ -f "$source_file" ]] || fail "源码文件缺失：$source_file"
  [[ -f "$bundled_file" ]] || fail "包内运行时文件缺失：$bundled_file"
  cmp -s -- "$source_file" "$bundled_file" \
    || fail "包内文件不是当前源码版本：$bundled_file"
}

[[ -d "$APP_PATH" ]] || fail "未找到应用：$APP_PATH"
[[ -f "$ARCHIVE_PATH" ]] || fail "未找到发布压缩包：$ARCHIVE_PATH"
[[ -f "$CHECKSUM_PATH" ]] || fail "未找到 SHA-256 文件：$CHECKSUM_PATH"
plutil -lint "$INFO_PLIST" >/dev/null
[[ "$(plutil -extract CFBundleDisplayName raw "$INFO_PLIST")" == "Read Daily" ]] \
  || fail "CFBundleDisplayName 不是 Read Daily"
[[ "$(plutil -extract LSMinimumSystemVersion raw "$INFO_PLIST")" == "13.0" ]] \
  || fail "最低系统版本不是 13.0"

[[ -f "$APP_PATH/Contents/Resources/ReadDaily.icns" ]] || fail "缺少 ReadDaily.icns"
cmp "$REPO_ROOT/assets/logo/readdaily-icon.svg" \
  "$APP_PATH/Contents/Resources/readdaily-icon.svg" >/dev/null \
  || fail "包内 SVG 与指定品牌源不一致"

required_runtime=(
  "$RUNTIME/scripts/readdaily.py"
  "$RUNTIME/skills/newspaper-fetch/sources.json"
  "$RUNTIME/skills/newspaper-fetch/scripts/fetch.py"
  "$RUNTIME/skills/newspaper-fetch/scripts/local_pdf.py"
  "$RUNTIME/skills/newspaper-fetch/scripts/wechat_engine.py"
  "$RUNTIME/skills/newspaper-reader/scripts/workbench_api.py"
  "$RUNTIME/skills/newspaper-reader/scripts/vault_publisher.py"
  "$RUNTIME/third_party/wechat-article-pdf/scripts/download_article.py"
  "$RUNTIME/third_party/wechat-article-pdf/UPSTREAM.md"
  "$APP_PATH/Contents/Resources/readdaily-source-manifest.sha256"
  "$APP_PATH/Contents/Resources/licenses/readdaily-LICENSE.txt"
  "$APP_PATH/Contents/Resources/licenses/wechat-article-pdf-LICENSE.txt"
)
for required in "${required_runtime[@]}"; do
  [[ -f "$required" ]] || fail "缺少运行时文件：$required"
done

# 防止源码修复后忘记重建，却让陈旧 App/ZIP 误通过发布验收。
assert_same_file \
  "$REPO_ROOT/scripts/readdaily.py" \
  "$RUNTIME/scripts/readdaily.py"
assert_same_file \
  "$REPO_ROOT/skills/newspaper-fetch/sources.json" \
  "$RUNTIME/skills/newspaper-fetch/sources.json"
for script in fetch.py lib.py local_pdf.py wechat_engine.py; do
  assert_same_file \
    "$REPO_ROOT/skills/newspaper-fetch/scripts/$script" \
    "$RUNTIME/skills/newspaper-fetch/scripts/$script"
done
for adapter in "$REPO_ROOT"/skills/newspaper-fetch/scripts/adapters/*.py; do
  assert_same_file \
    "$adapter" \
    "$RUNTIME/skills/newspaper-fetch/scripts/adapters/${adapter:t}"
done
for script in workbench_api.py vault_publisher.py; do
  assert_same_file \
    "$REPO_ROOT/skills/newspaper-reader/scripts/$script" \
    "$RUNTIME/skills/newspaper-reader/scripts/$script"
done
for banner in banner-morning-city.svg banner-reading-desk.svg banner-weather.svg; do
  assert_same_file \
    "$REPO_ROOT/apps/ConstructionReadingDesk/Sources/ConstructionReadingDesk/Resources/$banner" \
    "$APP_PATH/Contents/Resources/$banner"
done
assert_same_file \
  "$REPO_ROOT/LICENSE" \
  "$APP_PATH/Contents/Resources/licenses/readdaily-LICENSE.txt"
assert_same_file \
  "$REPO_ROOT/third_party/wechat-article-pdf/scripts/download_article.py" \
  "$RUNTIME/third_party/wechat-article-pdf/scripts/download_article.py"
assert_same_file \
  "$REPO_ROOT/third_party/wechat-article-pdf/UPSTREAM.md" \
  "$RUNTIME/third_party/wechat-article-pdf/UPSTREAM.md"
assert_same_file \
  "$REPO_ROOT/third_party/wechat-article-pdf/LICENSE" \
  "$APP_PATH/Contents/Resources/licenses/wechat-article-pdf-LICENSE.txt"

EXPECTED_SOURCE_MANIFEST="$VERIFY_DIR/readdaily-source-manifest.sha256"
source_manifest_files=(
  "$REPO_ROOT/apps/ConstructionReadingDesk/Package.swift"
  "$REPO_ROOT/apps/ConstructionReadingDesk/Resources/Info.plist"
  "$REPO_ROOT/scripts/build_macos_app.sh"
  "$REPO_ROOT/scripts/vocr.swift"
  "$REPO_ROOT/scripts/pdfocr.swift"
)
while IFS= read -r swift_source; do
  source_manifest_files+=("$swift_source")
done < <(find "$REPO_ROOT/apps/ConstructionReadingDesk/Sources" \
  -type f -name '*.swift' -print | LC_ALL=C sort)
{
  for source_file in "${source_manifest_files[@]}"; do
    source_hash="$(shasum -a 256 "$source_file" | awk '{print $1}')"
    source_relative="${source_file#$REPO_ROOT/}"
    print -r -- "$source_hash  $source_relative"
  done
} | LC_ALL=C sort > "$EXPECTED_SOURCE_MANIFEST"
cmp -s -- \
  "$EXPECTED_SOURCE_MANIFEST" \
  "$APP_PATH/Contents/Resources/readdaily-source-manifest.sha256" \
  || fail "包内 Swift/helper 二进制不是由当前源码树构建"

if grep -R -a -q "/Users/guopeijun" "$APP_PATH"; then
  fail "应用包仍含开发机绝对路径"
fi
if find "$RUNTIME" -type f \( -name '*.pyc' -o -name '.DS_Store' \) | grep -q .; then
  fail "包内运行时含缓存或 .DS_Store"
fi
if find "$RUNTIME" -type f \( \
    -name 'issue.json' -o -name '*.pdf' -o -name '*.jpg' -o \
    -name '*.jpeg' -o -name '*.webp' -o -name '*.tif' -o \
    -name '*.tiff' \) | grep -q .; then
  fail "包内运行时意外包含报纸证据或版面媒体"
fi
if find "$RUNTIME" -type d \( \
    -name '_state' -o -name '_drafts' -o -name '_summaries' -o \
    -name '_transactions' -o -name '_activity' -o -name 'pages' -o \
    -name 'text' \) | grep -q .; then
  fail "包内运行时意外包含用户归档目录"
fi

MAIN_BINARY="$APP_PATH/Contents/MacOS/ConstructionReadingDesk"
file "$MAIN_BINARY" | grep -q "Mach-O 64-bit executable arm64" \
  || fail "主程序不是 arm64 Mach-O"
otool -l "$MAIN_BINARY" \
  | awk '$1 == "minos" && $2 == "13.0" { found = 1 } END { exit(found ? 0 : 1) }' \
  || fail "主程序最低系统版本不是 13.0"
[[ "$(shasum -a 256 "$RUNTIME/third_party/wechat-article-pdf/scripts/download_article.py" | awk '{print $1}')" == "6d64672c6295374919a83fb00599a1b8bc9b08e3c12156358903ba9fa9bd0995" ]] \
  || fail "包内 wechat-article-pdf 脚本不是固定版本"
for helper in \
  "$RUNTIME/skills/newspaper-fetch/bin/vocr" \
  "$RUNTIME/skills/newspaper-fetch/bin/pdfocr"; do
  [[ -x "$helper" ]] || fail "OCR helper 不可执行：$helper"
  file "$helper" | grep -q "Mach-O 64-bit executable arm64" \
    || fail "OCR helper 不是 arm64 Mach-O：$helper"
  otool -l "$helper" \
    | awk '$1 == "minos" && $2 == "13.0" { found = 1 } END { exit(found ? 0 : 1) }' \
    || fail "OCR helper 最低系统版本不是 13.0：$helper"
done

codesign --verify --deep --strict --verbose=2 "$APP_PATH"

expected_hash="$(awk 'NR == 1 { print $1 }' "$CHECKSUM_PATH")"
actual_hash="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{ print $1 }')"
[[ -n "$expected_hash" && "$expected_hash" == "$actual_hash" ]] \
  || fail "发布压缩包 SHA-256 不一致"

/usr/bin/ditto -x -k "$ARCHIVE_PATH" "$VERIFY_DIR"
UNPACKED_APP="$VERIFY_DIR/Read Daily.app"
if ! /usr/bin/diff -qr -- "$APP_PATH" "$UNPACKED_APP" >/dev/null; then
  fail "发布压缩包中的应用与已验证应用不一致"
fi
codesign --verify --deep --strict --verbose=2 "$UNPACKED_APP"

PYTHON_BIN="${READDAILY_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [[ -x "$candidate" ]]; then PYTHON_BIN="$candidate"; break; fi
  done
fi
[[ -n "$PYTHON_BIN" ]] || fail "未找到 Python 3，无法执行包内 API 验证"

UNPACKED_RUNTIME="$UNPACKED_APP/Contents/Resources/readdaily"
API_RESULT="$VERIFY_DIR/registry.json"
PYTHONDONTWRITEBYTECODE=1 \
  "$PYTHON_BIN" "$UNPACKED_RUNTIME/scripts/readdaily.py" api newspaper-registry \
  --archive "$VERIFY_DIR/archive" --vault "$VERIFY_DIR/vault" > "$API_RESULT"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" - "$API_RESULT" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["ok"] is True
rows = payload["data"]["newspapers"]
assert [row["source"] for row in rows] == [
    "rmrb", "gmrb", "jjrb", "zgjsb", "kjrb", "nmrb", "nfrb", "bjrb"
]
PY

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$UNPACKED_RUNTIME/skills/newspaper-fetch/scripts" \
  "$PYTHON_BIN" - "$UNPACKED_RUNTIME" <<'PY'
import pathlib
import sys

import local_pdf

runtime = pathlib.Path(sys.argv[1])
expected = runtime / "skills" / "newspaper-fetch" / "bin" / "pdfocr"
actual = local_pdf._helper_binary(runtime / "_verify_archive")
assert actual.resolve() == expected.resolve(), (actual, expected)
PY

if find "$UNPACKED_APP" -type f \( -name '*.pyc' -o -name '.DS_Store' \) | grep -q .; then
  fail "API 冒烟后包内出现 Python 缓存或 .DS_Store"
fi
codesign --verify --deep --strict --verbose=2 "$UNPACKED_APP"

print "验证通过：Read Daily $VERSION（macOS arm64）"
print "应用：$APP_PATH"
print "压缩包：$ARCHIVE_PATH"
print "SHA-256：$actual_hash"
