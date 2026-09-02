# readdaily — 多报每日读报系统（全自动抓取 → LLM 归纳 → Obsidian）

把【每日报纸（人民日报/光明日报/经济日报/科技日报/中国建设报/农民日报/南方日报…）】自动抓成结构化全文，
由 Agent/LLM 归纳出**栏目分类+摘要+实体+重要性**，归档到 Obsidian（报纸原文/每日摘要/主体档案/看板），
并支持**主题检索与主体时间线跟踪**。已在 2026-09-02 以 7 家报纸实测：343 文章单位 / 619K 字当日入库。

> 版权声明：下载内容版权归各报社。本项目仅提供**公开内容**的个人阅读/研究自动化，禁止商用转发。
> 边界承诺：不绕过验证码/登录/付费墙（按订阅源处理时需用户授权）。

## 快速开始（macOS）

```bash
git clone https://github.com/<you>/readdaily.git && cd readdaily
./install.sh                      # 技能软链（Codex/Claude/通用 Agent）→ OCR 构建 → launchd → 数据/Vault 初始化
python3 scripts/readdaily.py status       # 看状态
python3 scripts/readdaily.py query "城市更新"   # 主题检索（当日演示）
```

安装后：
- **每日 10:25 / 20:00 自动抓取** 7 家报纸（launchd；非 macOS 用 cron，见 install.sh 输出）
- 在你常用的 Agent/Codex 里说「读报」→ 按 read-daily 技能执行归纳→Obsidian→跟踪

## 架构一览（单仓库、零服务依赖）

```
sources.json（注册表：渠道类型/入口/启用状态）
  └─ fetch.py 编排器（fetched→parsed 状态机，幂等、断点续跑）
       ├─ wechat_read  微信读报（搜狗定位→src11→版面图→Vision OCR 版级文本）
       ├─ founder      方正数字报（index_url 版次发现 + layoutData 版图/文章 + 全文抽取）
       ├─ paper_api    JSON-API 数字报（科技日报型，匿名 uv/* 接口）
       └─ cms_index    index.json 型（农民日报，显式文件路径绕过 WAF 目录封禁）
  └─ reader.py（归纳流水线）→ _summaries schema → Obsidian（报纸原文/每日摘要/主体档案/看板）
  └─ readdaily.py CLI（fetch/status/query/tracking/ingest/archive/digest）
```

## 子命令

| 命令 | 说明 |
|---|---|
| `readdaily fetch` | 抓取当日全部启用源（--date/--source/--offline） |
| `readdaily status` | 各源状态 + 当日期数/字数 |
| `readdaily prepare/ingest/archive/digest` | 归纳流水线（配合 Agent/LLM） |
| `readdaily tracking --entity 城市更新` | 主体档案追加时间线 |
| `readdaily query "城市更新" --days 2` | 跨源全文检索（证据式读报答案） |

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
| `READDAILY_VOCR` | 仓库内 `bin/vocr` | Vision OCR 二进制（install.sh 构建） |

## 已知边界

- 微信渠道（中国建设报）仅 macOS（Vision OCR）；其余渠道跨平台（Python3 即可）
- 方正系 4 家期号存于头版图/API 字段，正文抓取已就绪，期号提取在 roadmap
- 环球时报为订阅制（不绕过）；北京日报数字报已并入门户（官网新闻流待确认接入）

## 文档

- docs/架构.md · docs/适配新报纸指南.md · docs/故障排查.md · CHANGELOG.md
