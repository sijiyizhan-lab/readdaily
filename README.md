# Read Daily — 八报每日读报与 Obsidian 知识工作台

<p align="center"><img src="assets/logo/readdaily-lockup.png" alt="ReadDaily 读报" width="560"></p>

把【人民日报/光明日报/经济日报/中国建设报/科技日报/农民日报/南方日报/北京日报】自动抓成结构化全文，
由 Agent/LLM 归纳出**栏目分类+摘要+实体+重要性**，归档到 Obsidian（报纸原文/每日摘要/主体档案/看板），
并支持**主题检索与主体时间线跟踪**。已在 2026-09-02 以 8 家报纸实测：99 个版次 / 约 700K 字当日入库。

> 版权声明：下载内容版权归各报社。本项目仅提供**公开内容**的个人阅读/研究自动化，禁止商用转发。
> 边界承诺：不绕过验证码/登录/付费墙（按订阅源处理时需用户授权）。

## 快速开始（macOS）

```bash
git clone https://github.com/sijiyizhan-lab/readdaily.git && cd readdaily
./install.sh                      # 技能软链（Codex/Claude/通用 Agent）→ OCR 构建 → launchd → 数据/Vault 初始化
python3 scripts/readdaily.py status       # 看状态
python3 scripts/readdaily.py query "城市更新"   # 主题检索（当日演示）
```

安装后：
- **每日 10:25 / 20:00 自动抓取** 8 家报纸（launchd；非 macOS 用 cron，见 install.sh 输出）
- 在你常用的 Agent/Codex 里说「读报」→ 按 read-daily 技能执行归纳→Obsidian→跟踪

## Read Daily（原生 macOS 应用）

仓库内含 SwiftUI 每日读报工作台：按日期与三类呈现固定八报，显示版面缩略图和阅读进度；原版在上、OCR 与显式校对在下，右侧完成中文摘要、主题、事实字段和发布复核。

```bash
./scripts/build_macos_app.sh
./scripts/verify_macos_release.sh
open "dist/Read Daily.app"
```

构建产物为 `dist/Read Daily.app`、版本化 ZIP 和 SHA-256 文件。应用内置后端、OCR helper 和 MIT 许可的微信文章下载组件，不依赖另行克隆本仓库；运行仍需 macOS 13+、Apple Silicon 与 Python 3，中国建设报在线生成 PDF 另需 Chrome/Chromium。当前公开包为 ad-hoc 签名、未公证版本，首次启动请在 Finder 中右键选择“打开”。详见 [应用说明](apps/ConstructionReadingDesk/README.md)与[0.3.1 发布说明](docs/releases/v0.3.1.md)。

应用默认读取 `~/Library/Application Support/readdaily/news-archive`，Vault 默认是 `~/Maitty的知识库`，均可在设置中修改。「保存草稿」和阅读记录只写归档目录；只有中国建设报的“预览发布 → 确认发布”会写入 Vault 下的 `09-建设新闻与报纸摘要`，并保留人工内容、检测冲突、创建回滚快照。

## 架构一览（单仓库、本地优先）

```
sources.json（注册表：渠道类型/入口/启用状态）
  └─ fetch.py 编排器（fetched→parsed 状态机，幂等、断点续跑）
       ├─ wechat_read  微信读报（搜狗定位→src11→版面图→Vision OCR 版级文本）
       ├─ founder      方正数字报（index_url 版次发现 + layoutData 版图/文章 + 全文抽取）
       ├─ paper_api    JSON-API 数字报（科技日报型，匿名 uv/* 接口）
       └─ cms_index    index.json 型（农民日报，显式文件路径绕过 WAF 目录封禁）
  └─ reader.py（归纳流水线）→ _summaries schema → Obsidian（报纸原文/每日摘要/主体档案/看板）
  └─ workbench_api.py（版本化 JSON 接口）→ 草稿/发布预览/事务回滚
       └─ ConstructionReadingDesk（SwiftUI 本地复核客户端）
  └─ readdaily.py CLI（fetch/status/query/tracking/ingest/archive/digest）
```

## 无人值守模式（可选，推荐）

配置一次即可全自动：每天 10:25/20:00 抓取，10:50/20:15 由 LLM 完成
归纳→Obsidian 归档→每日摘要→主体跟踪，全程无需人类/Agent 会话。

```bash
# 1) 配置 LLM（OpenAI 兼容；密钥只存本机，不入仓库）
mkdir -p ~/Library/Application\ Support/readdaily/news-archive
cat > ~/Library/Application\ Support/readdaily/news-archive/llm.json <<'EOF'
{"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": "sk-..."}
EOF
chmod 600 ~/Library/Application\ Support/readdaily/news-archive/llm.json

# 2) 安装即含 com.guopeijun.readdaily-llm 任务；或手动补跑
python3 scripts/readdaily.py all            # 抓取+归纳+归档+摘要+跟踪（幂等）
```

## 子命令

| 命令 | 说明 |
|---|---|
| `readdaily fetch` | 抓取当日全部启用源（--date/--source/--offline） |
| `readdaily status` | 各源状态 + 当日期数/字数 |
| `readdaily prepare/ingest/archive/digest` | 归纳流水线（配合 Agent/LLM） |
| `readdaily tracking --entity 城市更新` | 主体档案追加时间线 |
| `readdaily query "城市更新" --days 2` | 跨源全文检索（证据式读报答案） |
| `readdaily api capabilities` | 工作台 JSON 接口能力与路径检查 |

## 给 Codex / Claude Code / 任意 Agent

```bash
./install.sh --all-hosts   # 软链到 ~/.claude/skills、~/.codex/skills、~/.agents/skills、~/.workbuddy/skills
```
- **Codex**：`~/.codex/skills/read-daily → <repo>/skills/read-daily`（同一源目录，改仓库即同步）
- 仓库根 `AGENTS.md` 提供架构与命令速查（在仓库内使用 Agent 时自动生效）
- 无 Agent 时也可以 `--llm-api` 自行接 API 归纳（见 docs/架构.md §LLM 通道）

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `READDAILY_ARCHIVE` | `~/Library/Application Support/readdaily/news-archive` | 数据根（规避 macOS TCC 对 Downloads 的保护） |
| `READDAILY_VAULT` | `…/readdaily/vault` | Obsidian vault（推荐指向你自己的 vault） |
| `READDAILY_VOCR` | 仓库内 `bin/vocr` | Vision OCR 二进制（install.sh 或 App 构建脚本生成） |
| `READDAILY_PDFOCR` | 仓库内 `bin/pdfocr` | 可选覆盖 PDFKit + Vision OCR helper |
| `READDAILY_PYTHON` | 自动探测 | macOS App 使用的 Python 3 可执行文件 |
| `READDAILY_WECHAT_DOWNLOADER` | 包内或已安装 Skill | 可选覆盖微信文章下载器路径 |
| `READDAILY_PUBLISHER_STATE_ROOT` | `~/Library/Application Support/readdaily/publisher-state` | 跨归档 Vault 发布事务哨兵；必须与 archive 和 Vault 完全分离 |

## 已知边界

- macOS App Release 当前仅提供 Apple Silicon 架构，采用 ad-hoc 签名且尚未 Apple 公证
- 微信渠道（中国建设报）仅 macOS；在线生成 PDF 需要 Chrome/Chromium，内置 OCR 不要求运行时安装 Swift 编译器
- 方正系 4 家期号存于头版图/API 字段，正文抓取已就绪，期号提取在 roadmap
- 环球时报为订阅制（不绕过）；北京日报数字报已并入门户（官网新闻流待确认接入）

## 文档

- docs/架构.md · docs/适配新报纸指南.md · docs/故障排查.md · CHANGELOG.md
