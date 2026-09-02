---
name: read-daily
description: |
  多报每日读报 Agent 主入口（readdaily）。路由协调三大能力：①newspaper-fetch 每天自动抓取（微信读报/方正/API/index.json 四类渠道，注册表驱动）②newspaper-reader 归纳→Obsidian 归档（LLM 摘要/分类/实体/重要性 → 报纸原文+每日摘要+主体档案）③主题检索与跟踪（「XX 主题近期报纸怎么说」全文检索 + 时间线延续）。
  触发方式：用户说「读报/今天报纸/XX主题近期报道/跟踪XX主体/更新 Obsidian/跑一遍流程」。
  方案：readdaily CLI（fetch/status/query/tracking/ingest/archive/digest）＋ 环境变量 READDAILY_ARCHIVE/VAULT 控制数据根与 Obsidian vault（默认 ~/Library/Application Support/readdaily/）。定时：launchd 10:25/20:00 自动抓取；归纳依赖 Agent 会话或 API Key。
  边界：只抓已公开内容，不绕过验证码/登录/付费（环球时报为订阅源时需用户确认）；报纸版权归原报社，仅供个人阅读研究。
  Trigger: readdaily, 读报, 今天报纸, 主题检索, 城市更新报道, tracking 主体.
---

# read-daily：多报每日读报 Agent（主入口）

## 执行流程

```
readdaily fetch            # ① 抓取（launchd 10:25/20:00 自动；手动补跑）
readdaily status           # ② 查看各源状态
readdaily prepare --date   # ③ 归纳流水线（read-daily 指令）：
#    prepare → 我(LLM)阅读 → 写 _summaries → ingest（校验）→ archive（Obsidian）→ digest → tracking
readdaily query "关键词"    # ④ 主题检索：跨源跨版全文命中清单
readdaily tracking --entity "城市更新" --date …  # ⑤ 主体档案追加时间线
```

## 执行要点（Agent 每次按此走）

1. **先 status 再动手**：`readdaily status` 看当日各源 fetched/parsed/summarized；已 summarized 无需重做。
2. **归纳三步**：prepare（未归纳单位清单）→ 按 schema 写 `_summaries/<source>/<date>.json`（summary 100-150字 + category 政治/经济/军事/民生/生产/科技/文化/生态/其他 + entities[] + importance 1-5 + keywords[]）→ ingest 校验（summary≥30字、category 合法）→ archive。
3. **每日摘要**：digest 给分类分布+实体频次+条目包 → 按《每日摘要模板》写主题块（跨报聚类+宏观/微观双视角）→ vault/每日摘要/。
4. **主体跟踪**：tracking 自动追加时间线；背景/宏观趋势/微观场景由我撰写；变化检测 = 与档案最后一条对比（↑↓→）。
5. **主题问答**：`readdaily query "<主题>" --days N` 直接给命中清单（证据式读报），再按主题速报模板归档（可选）。
6. **排障**：单源失败看 `_dailylog.jsonl`；微信渠道需 macOS（Vision OCR）；新源适配见 docs/适配新报纸指南.md。

## 环境
- Python3；macOS: swiftc 构建 Vision OCR（install.sh 自动）；Chrome 非必需（方正渠道纯文本）。
- 数据根 ~/Library/Application Support/readdaily/（TCC 安全）；Obsidian vault 同处，可 --vault 覆盖。
