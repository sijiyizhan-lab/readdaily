# Read Daily（macOS）

Read Daily 是 `readdaily` 的原生 macOS 每日读报与人工复核客户端。它按日期和固定分类呈现人民日报、光明日报、经济日报、中国建设报、科技日报、农民日报、南方日报、北京日报，记录每份报纸的获取与阅读状态，并提供原版、OCR 校对和中文摘要工作台。

最低系统版本为 macOS 13。Swift 客户端使用 SwiftUI、AppKit 与 PDFKit，不包含第三方框架；发布包另内置本仓库的 Python 业务代码、预编译 OCR helper 和保留独立 MIT 许可证的微信文章下载组件。

## 开发运行

```bash
cd apps/ConstructionReadingDesk
swift run ConstructionReadingDesk
```

开发态首次打开后检查“设置”中的路径：

- 内置读报引擎：开发态回退到 `~/readdaily`
- 报纸归档目录：默认 `~/Library/Application Support/readdaily/news-archive`
- Obsidian Vault：默认 `~/Maitty的知识库`

应用通过以下稳定接口调用后端，不通过 shell 拼接参数。Python 位置依次读取 `READDAILY_PYTHON`，再探测 Homebrew 与系统常见位置：

```text
python3 <bundle-or-repo>/scripts/readdaily.py api <command> --archive <path> --vault <path>
```

## 构建 `.app`

从仓库根目录运行：

```bash
./scripts/build_macos_app.sh
open "dist/Read Daily.app"
```

默认产物是 `dist/Read Daily.app`，并同时生成可上传 GitHub Release 的 `dist/Read-Daily-v0.3.0-macOS-arm64.zip` 与 `.sha256`。也可传入一个以 `.app` 结尾的输出路径：

```bash
./scripts/build_macos_app.sh "$HOME/Desktop/Read Daily.app"
```

构建脚本从 `assets/logo/readdaily-icon.svg` 生成多尺寸 `ReadDaily.icns`，把最小 Python CLI、八报抓取器、工作台 API、macOS 13 OCR helper 与许可证打入 `Contents/Resources`，再执行 ad-hoc 签名。微信文章下载组件固定在仓库 `third_party/wechat-article-pdf`，构建前校验脚本与许可证 SHA-256，不读取开发机 HOME 下的可变副本。脚本不会安装 LaunchAgent、修改技能软链，也不会读取或写入真实 Vault。下载版默认使用包内引擎，不依赖 `~/readdaily`。

正式上传前执行：

```bash
./scripts/verify_macos_release.sh
```

验证覆盖 Bundle 名称/图标、最小运行时、许可证、无开发机绝对路径、helper 架构与最低系统版本、源码—二进制清单、代码签名、ZIP/SHA-256，以及从解压副本调用包内八报 API。若编译期间 Swift 或 OCR helper 源码发生变化，构建会在替换正式 App 前中止。

## 使用流程

1. 刷新后默认进入本地今天；即使今天尚无归档，也可直接“抓取当日8报”。日期菜单同时保留历史归档日期。本地 PDF 导入当前明确限定中国建设报。
2. 在左栏按“日期 → 中央党报/部委行业报/地方党报”查看获取与今日阅读状态；缺报会显示“当日未获取”。
3. 在中栏通过真实版面缩略图选版；证据区按原版在上、OCR 在下呈现。点击原版可在 25%–400% 范围查看。
4. OCR 原文只读；后端提供文章结构时按原始标题、块顺序和空白呈现，缺少结构时才按空行分段。校订文本、校对状态和疑点均需显式编辑并保存，不会静默覆盖原文。
5. 右侧摘要工作栏填写中文标题、摘要、7 个规范主题、多条事实和重要性，并可标记“今日已读”。
6. “保存草稿”只提交有改动的版次并写入归档目录，不触碰 Obsidian；摘要、主题、事实可逐步补齐。发布预览仍重新校验整期完整性。
7. 目前只有中国建设报可以“预览发布”；预览展示文件清单和 unified diff，再次确认才调用 Python 发布器。
8. 在“发布历史”查看事务，并在冲突检测保护下回滚。

主题固定使用知识库已有的 7 个完整中文名称，避免产生编号节点或英文标题：

- 建设投资与房地产
- 城市更新与城市治理
- 智能建造与智能制造
- 产业创新与建筑业转型
- 工程咨询、招投标与供应链
- 住房民生与社区服务
- 建设安全与城市韧性

## 测试

```bash
swift test --package-path apps/ConstructionReadingDesk --enable-swift-testing --disable-xctest \
  -Xswiftc -F -Xswiftc /Library/Developer/CommandLineTools/Library/Developer/Frameworks
swift build --package-path apps/ConstructionReadingDesk -c release
```

测试覆盖版本化 Codable、真实 API 载荷映射、进程命令参数、严格 stdout JSON、中文错误、复核编辑状态、异步版图换页与响应式/可访问性策略。v0.3.0 发布基线为 61 项 Swift 测试（12 个套件）与仓库级 301 项 Python 测试。

## 边界

- Swift 客户端不直接写 Vault；唯一写入入口是 Python 的 `publish-apply`。
- 草稿临时 JSON 写入系统临时目录，调用结束后删除。
- OCR、原版图、缓存、日志、发布计划和事务快照均留在归档目录。
- 所有抓取、导入、草稿、阅读记录和发布入口都会拒绝归档与 Vault 相等、软链别名或父子重叠；它们先从原始配置路径逐级以 `O_NOFOLLOW` 固定目录句柄，再完成隔离校验并在整个操作中复用；切换路径设置需明确应用，旧工作区会先清空再重载。
- 抓取按“归档目录 + 日期”加锁，同日手动抓取与定时任务不会竞争固定临时文件；各来源必须验证请求日期及重定向后的日期，拒绝把旧期写成当天。
- 抓取与解析以整期事务提交，失败会恢复原期；版图必须通过系统 ImageIO 的真实像素解码，通用版面至少为短边 1000、长边 1400 像素，高分辨率渠道使用更高门槛，不能仅凭文件头或文件体积进入归档。
- 已发现的版次必须真实、连续且唯一，已发现的文章必须全部归属对应版次并完整解析；系统不会把缺版、孤立文章或正文截断伪装成成功。
- 本地 PDF 必须逐页生成完整清单；报头日期唯一匹配后才可进入可发布状态，无法确认或冲突时只允许人工复核。导入先锁定稳定快照，保证 OCR、归档 PDF 与来源哈希来自同一字节版本。
- 发布计划同时绑定报纸证据与当前持久草稿；应用计划前会再次校验二者。发布与回滚使用内容级 CAS、固定目录句柄、目录 `fsync` 和 Vault 专属持久哨兵，既阻止越界换链，也不会和当天抓取丢失状态更新。
- 日报知识卡使用“日期 + 中文报纸名”的稳定文件名；期号修正会更新同一卡片，不生成编号或随机英文节点。
- 抓取只使用仓库已配置的公开渠道，不绕过登录、验证码或付费墙。
- 天气只读取设置中的本地文字，不发起网络请求；没有配置时显示“天气未配置”。
- 分发包需要 Python 3（支持 `READDAILY_PYTHON`，并自动探测 Homebrew 与系统常见路径）；抓取代码已无 `requests`/Pillow 硬依赖。中国建设报在线生成 PDF 仍需要 Chrome/Chromium。
- 包使用 ad-hoc 签名且未公证；首次启动应在 Finder 中右键选择“打开”。完整 Gatekeeper 体验仍需 Developer ID、Hardened Runtime、公证与 Staple。
- 当前应用为本地单用户工作台，不提供云同步、多人协作或移动端。
