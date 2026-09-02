# AGENTS.md — readdaily 项目工作指南（Codex/Claude Code/任意 Agent 读取）

## 这是什么
多报每日读报系统：抓取（4 类渠道适配器）→ 结构化 issue.json → Agent/LLM 归纳 → Obsidian 归档 → 主题检索/跟踪。
仓库 = 单一源；skills/ 目录被 `install.sh` 软链到 ~/.codex/skills 与 ~/.agents/skills，改仓库即全端同步。

## 常用命令（仓库内）
```bash
python3 scripts/readdaily.py status                  # 各源与当日数据状态
python3 scripts/readdaily.py fetch                   # 抓取（缺省今日全部启用源）
python3 scripts/readdaily.py query "城市更新" --days 2  # 全文检索（证据式读报）
python3 scripts/readdaily.py prepare --date 2026-09-02   # 归纳流水线每步
python3 skills/newspaper-fetch/scripts/fetch.py --probe gmrb   # 渠道探测（新源适配）
./install.sh --dry-run                               # 安装预演
```

## 数据流约定
- 数据根：`READDAILY_ARCHIVE`（默认 ~/Library/Application Support/readdaily/news-archive）
- scheme：`<source>/<date>/issue.json`（editions + units[articles{title,text}|text_path]），状态机 `_state/<source>/<date>.json`（fetched→parsed→summarized→archived→tracked）
- 归纳 schema：`_summaries/<source>/<date>.json` = `{units:[{id,summary(100-150字),category[…],entities[…],importance(1-5),keywords[…]}]}`
- 分类枚举：政治/经济/军事/民生/生产/科技/文化/生态/其他
- Vault：报纸原文/YYYY-MM-DD/报/、每日摘要/、主体跟踪/<主体>_档案.md、看板/
- Obsidian 模板在 config/templates/（install 时复制到 vault/_templates）

## 代码结构
```
skills/newspaper-fetch/scripts/
  fetch.py                 # 编排器（--date/--source/--stage/--offline/--probe）
  lib.py                   # 共享库（http/状态机/校验/日志）
  wechat_engine.py         # 微信读报引擎（搜狗定位→src11→电子报→版级 OCR）
  adapters/{wechat_read,founder,paper_api,cms_index}.py
skills/newspaper-reader/scripts/reader.py    # prepare/ingest/archive/digest/tracking
scripts/readdaily.py       # CLI 公共入口
scripts/vocr.swift         # Vision OCR 源码（install 构建）
```

## 工程约定
- 适配器接口：`fetch(src, d, archive_root) -> (issue, err)`、`parse(src, d, archive_root) -> (issue, err)`
- 新源流程：sources.json 登记（enabled=false）→ `fetch.py --probe` → 适配/补 pattern → 连续 3 工作日验收 → enabled=true
- 只抓公开内容；不绕过验证码/登录/付费；不把报社内容转商用
- 环境：Python3（requests 可选回退 urllib）；macOS 才有 Vision OCR（微信渠道）
