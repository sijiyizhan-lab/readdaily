#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h}"
PACKAGE_DIR="$REPO_ROOT/apps/ConstructionReadingDesk"
OUTPUT_APP="${1:-$REPO_ROOT/dist/Read Daily.app}"

if [[ "$OUTPUT_APP" != *.app || "$OUTPUT_APP" == "/" || "$OUTPUT_APP" == "$REPO_ROOT" ]]; then
  print -u2 "错误：输出路径必须是明确的 .app 路径。"
  exit 2
fi

INFO_PLIST="$PACKAGE_DIR/Resources/Info.plist"
SOURCE_ICON="$REPO_ROOT/assets/logo/readdaily-icon.svg"
WECHAT_COMPONENT="$REPO_ROOT/third_party/wechat-article-pdf"
WECHAT_SCRIPT_SHA256="6d64672c6295374919a83fb00599a1b8bc9b08e3c12156358903ba9fa9bd0995"
WECHAT_LICENSE_SHA256="0fa72df2b1cd7b11097ccf64bf19e22595766c73f3aa19cecd96af51c07660a2"
VERSION="$(plutil -extract CFBundleShortVersionString raw "$INFO_PLIST")"
plutil -lint "$INFO_PLIST" >/dev/null

if [[ ! -r "$SOURCE_ICON" ]]; then
  print -u2 "错误：未找到 Read Daily SVG 图标：$SOURCE_ICON"
  exit 3
fi
if [[ ! -r "$WECHAT_COMPONENT/scripts/download_article.py" \
      || ! -r "$WECHAT_COMPONENT/LICENSE" \
      || ! -r "$WECHAT_COMPONENT/UPSTREAM.md" ]]; then
  print -u2 "错误：仓库缺少固定版本的 wechat-article-pdf 组件。"
  exit 3
fi
if [[ "$(shasum -a 256 "$WECHAT_COMPONENT/scripts/download_article.py" | awk '{print $1}')" != "$WECHAT_SCRIPT_SHA256" \
      || "$(shasum -a 256 "$WECHAT_COMPONENT/LICENSE" | awk '{print $1}')" != "$WECHAT_LICENSE_SHA256" ]]; then
  print -u2 "错误：wechat-article-pdf 组件哈希与固定版本不一致。"
  exit 3
fi

OUTPUT_PARENT="${OUTPUT_APP:h}"
OUTPUT_NAME="${OUTPUT_APP:t}"
mkdir -p "$OUTPUT_PARENT"
STAGING_APP="$OUTPUT_PARENT/.${OUTPUT_NAME}.$$.staging"
BACKUP_APP="$OUTPUT_PARENT/.${OUTPUT_NAME}.$$.backup"
ARCHIVE_BASENAME="Read-Daily-v${VERSION}-macOS-arm64.zip"
ARCHIVE_PATH="$OUTPUT_PARENT/$ARCHIVE_BASENAME"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
STAGING_ARCHIVE="$OUTPUT_PARENT/.${ARCHIVE_BASENAME}.$$.staging"
STAGING_CHECKSUM="$OUTPUT_PARENT/.${ARCHIVE_BASENAME}.$$.sha256.staging"
ICON_WORK="$(mktemp -d "${TMPDIR:-/tmp}/readdaily-icon.XXXXXX")"

cleanup() {
  if [[ -d "$STAGING_APP" ]]; then /bin/rm -rf -- "$STAGING_APP"; fi
  if [[ -e "$STAGING_ARCHIVE" ]]; then /bin/rm -f -- "$STAGING_ARCHIVE"; fi
  if [[ -e "$STAGING_CHECKSUM" ]]; then /bin/rm -f -- "$STAGING_CHECKSUM"; fi
  if [[ -d "$BACKUP_APP" && ! -e "$OUTPUT_APP" ]]; then /bin/mv -- "$BACKUP_APP" "$OUTPUT_APP"; fi
  if [[ -d "$BACKUP_APP" ]]; then /bin/rm -rf -- "$BACKUP_APP"; fi
  if [[ -d "$ICON_WORK" ]]; then /bin/rm -rf -- "$ICON_WORK"; fi
}
trap cleanup EXIT INT TERM

write_compiled_source_manifest() {
  local destination="$1"
  local source_file source_hash source_relative swift_source
  local -a manifest_files
  manifest_files=(
    "$PACKAGE_DIR/Package.swift"
    "$PACKAGE_DIR/Resources/Info.plist"
    "$REPO_ROOT/scripts/build_macos_app.sh"
    "$REPO_ROOT/scripts/vocr.swift"
    "$REPO_ROOT/scripts/pdfocr.swift"
  )
  while IFS= read -r swift_source; do
    manifest_files+=("$swift_source")
  done < <(find "$PACKAGE_DIR/Sources" -type f -name '*.swift' -print | LC_ALL=C sort)
  {
    for source_file in "${manifest_files[@]}"; do
      source_hash="$(shasum -a 256 "$source_file" | awk '{print $1}')"
      source_relative="${source_file#$REPO_ROOT/}"
      print -r -- "$source_hash  $source_relative"
    done
  } | LC_ALL=C sort > "$destination"
}

# Capture the source identity before compiling.  A second snapshot after both
# the app and OCR helpers are built must match, otherwise a build could bind an
# old binary to a manifest generated from newer source files.
INITIAL_SOURCE_MANIFEST="$ICON_WORK/readdaily-source-before.sha256"
FINAL_SOURCE_MANIFEST="$ICON_WORK/readdaily-source-after.sha256"
write_compiled_source_manifest "$INITIAL_SOURCE_MANIFEST"

SWIFT_SOURCE_MAP="$REPO_ROOT=/readdaily"
swift build --package-path "$PACKAGE_DIR" -c release \
  -Xswiftc -file-prefix-map -Xswiftc "$SWIFT_SOURCE_MAP" \
  -Xswiftc -debug-prefix-map -Xswiftc "$SWIFT_SOURCE_MAP"
BIN_DIR="$(swift build --package-path "$PACKAGE_DIR" -c release --show-bin-path)"
BINARY="$BIN_DIR/ConstructionReadingDesk"

if [[ ! -x "$BINARY" ]]; then
  print -u2 "错误：未找到 release 可执行文件：$BINARY"
  exit 3
fi

CONTENTS="$STAGING_APP/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES_DIR="$CONTENTS/Resources"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
install -m 755 "$BINARY" "$MACOS_DIR/ConstructionReadingDesk"
/usr/bin/strip -S -x "$MACOS_DIR/ConstructionReadingDesk"
install -m 644 "$INFO_PLIST" "$CONTENTS/Info.plist"

# 从仓库中的唯一 SVG 源生成标准多尺寸 macOS 图标。
ICONSET="$ICON_WORK/ReadDaily.iconset"
MASTER_ICON="$ICON_WORK/ReadDaily-1024.png"
mkdir -p "$ICONSET"
sips -s format png "$SOURCE_ICON" --out "$MASTER_ICON" >/dev/null
for pair in \
  "16 icon_16x16.png" "32 icon_16x16@2x.png" \
  "32 icon_32x32.png" "64 icon_32x32@2x.png" \
  "128 icon_128x128.png" "256 icon_128x128@2x.png" \
  "256 icon_256x256.png" "512 icon_256x256@2x.png" \
  "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  pixels="${pair%% *}"
  filename="${pair#* }"
  sips -z "$pixels" "$pixels" "$MASTER_ICON" --out "$ICONSET/$filename" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RESOURCES_DIR/ReadDaily.icns"
install -m 644 "$SOURCE_ICON" "$RESOURCES_DIR/readdaily-icon.svg"
for banner in banner-morning-city.svg banner-reading-desk.svg banner-weather.svg; do
  install -m 644 "$PACKAGE_DIR/Sources/ConstructionReadingDesk/Resources/$banner" "$RESOURCES_DIR/$banner"
done

# Bind compiled Swift/helper binaries to the exact pre-build source tree.  The
# release verifier recomputes this manifest to reject stale apps.
SOURCE_MANIFEST="$RESOURCES_DIR/readdaily-source-manifest.sha256"
install -m 644 "$INITIAL_SOURCE_MANIFEST" "$SOURCE_MANIFEST"

# GitHub Release 产物只携带工作台必需运行时，避免把 LaunchAgent、本机路径和缓存带入公开包。
BUNDLED_REPO="$RESOURCES_DIR/readdaily"
FETCH_RUNTIME="$BUNDLED_REPO/skills/newspaper-fetch"
READER_RUNTIME="$BUNDLED_REPO/skills/newspaper-reader/scripts"
mkdir -p \
  "$BUNDLED_REPO/scripts" \
  "$FETCH_RUNTIME/scripts/adapters" \
  "$FETCH_RUNTIME/bin" \
  "$READER_RUNTIME" \
  "$RESOURCES_DIR/licenses"
install -m 755 "$REPO_ROOT/scripts/readdaily.py" "$BUNDLED_REPO/scripts/readdaily.py"
install -m 644 "$REPO_ROOT/skills/newspaper-fetch/sources.json" "$FETCH_RUNTIME/sources.json"
for script in fetch.py lib.py local_pdf.py wechat_engine.py; do
  install -m 644 "$REPO_ROOT/skills/newspaper-fetch/scripts/$script" "$FETCH_RUNTIME/scripts/$script"
done
for adapter in "$REPO_ROOT"/skills/newspaper-fetch/scripts/adapters/*.py; do
  install -m 644 "$adapter" "$FETCH_RUNTIME/scripts/adapters/${adapter:t}"
done
for script in workbench_api.py vault_publisher.py; do
  install -m 644 "$REPO_ROOT/skills/newspaper-reader/scripts/$script" "$READER_RUNTIME/$script"
done
install -m 644 "$REPO_ROOT/LICENSE" "$RESOURCES_DIR/licenses/readdaily-LICENSE.txt"

# 中国建设报下载器固定在仓库内，构建不依赖开发机 HOME 下的可变组件。
mkdir -p "$BUNDLED_REPO/third_party/wechat-article-pdf/scripts"
install -m 755 \
  "$WECHAT_COMPONENT/scripts/download_article.py" \
  "$BUNDLED_REPO/third_party/wechat-article-pdf/scripts/download_article.py"
install -m 644 \
  "$WECHAT_COMPONENT/UPSTREAM.md" \
  "$BUNDLED_REPO/third_party/wechat-article-pdf/UPSTREAM.md"
install -m 644 \
  "$WECHAT_COMPONENT/LICENSE" \
  "$RESOURCES_DIR/licenses/wechat-article-pdf-LICENSE.txt"

swiftc -O \
  -target arm64-apple-macosx13.0 \
  -framework Vision -framework ImageIO -framework CoreGraphics \
  "$REPO_ROOT/scripts/vocr.swift" \
  -o "$FETCH_RUNTIME/bin/vocr"
swiftc -O \
  -target arm64-apple-macosx13.0 \
  -framework PDFKit -framework Vision -framework AppKit \
  "$REPO_ROOT/scripts/pdfocr.swift" \
  -o "$FETCH_RUNTIME/bin/pdfocr"
chmod 755 \
  "$FETCH_RUNTIME/bin/vocr" \
  "$FETCH_RUNTIME/bin/pdfocr"
for helper in \
  "$FETCH_RUNTIME/bin/vocr" \
  "$FETCH_RUNTIME/bin/pdfocr"; do
  if ! file "$helper" | grep -q "Mach-O 64-bit executable arm64"; then
    print -u2 "错误：OCR helper 不是预期的 arm64 Mach-O：$helper"
    exit 4
  fi
  if ! otool -l "$helper" | awk '$1 == "minos" && $2 == "13.0" { found = 1 } END { exit(found ? 0 : 1) }'; then
    print -u2 "错误：OCR helper 最低系统版本不是 macOS 13.0：$helper"
    exit 4
  fi
done

write_compiled_source_manifest "$FINAL_SOURCE_MANIFEST"
if ! cmp -s -- "$INITIAL_SOURCE_MANIFEST" "$FINAL_SOURCE_MANIFEST"; then
  print -u2 "错误：编译期间 Swift/helper 源码发生变化，已拒绝生成混合版本应用。"
  exit 4
fi

if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - --timestamp=none "$FETCH_RUNTIME/bin/vocr"
  codesign --force --sign - --timestamp=none "$FETCH_RUNTIME/bin/pdfocr"
  codesign --force --sign - --timestamp=none "$STAGING_APP"
  codesign --verify --deep --strict "$STAGING_APP"
fi

# 先完成、签名并验证 staging，再替换同名当前版本；旧“建设读报台.app”不在目标范围内。
if [[ -e "$OUTPUT_APP" ]]; then /bin/mv -- "$OUTPUT_APP" "$BACKUP_APP"; fi
/bin/mv -- "$STAGING_APP" "$OUTPUT_APP"
if [[ -d "$BACKUP_APP" ]]; then /bin/rm -rf -- "$BACKUP_APP"; fi

/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$OUTPUT_APP" "$STAGING_ARCHIVE"
(
  cd "$OUTPUT_PARENT"
  shasum -a 256 "${STAGING_ARCHIVE:t}" \
    | sed "s#${STAGING_ARCHIVE:t}#${ARCHIVE_PATH:t}#" \
    > "$STAGING_CHECKSUM"
)
/bin/mv -f -- "$STAGING_ARCHIVE" "$ARCHIVE_PATH"
/bin/mv -f -- "$STAGING_CHECKSUM" "$CHECKSUM_PATH"

print "构建完成：$OUTPUT_APP"
print "GitHub Release 压缩包：$ARCHIVE_PATH"
print "SHA-256：$CHECKSUM_PATH"
print "图标：$(plutil -extract CFBundleIconFile raw "$OUTPUT_APP/Contents/Info.plist")"
print "版本：$VERSION"
