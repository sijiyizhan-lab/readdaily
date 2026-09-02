---
name: newspaper-reader
description: 读报归纳与归档。把 newspaper-fetch 抓到的报纸（期/版/文章或版级 OCR）做：LLM 摘要+栏目归类（政治/军事/经济/民生/生产/科技/文化/生态/其他）+实体抽取+重要性打分 → 写入 Obsidian（报纸原文/每日摘要/主体跟踪/看板）+ 主体档案时间线增量更新。触发：读报/今天有什么新闻/生成摘要/归档 Obsidian/跟踪 XX 主体。
方案：reader.py prepare（工作清单）→ Agent 阅读文本产出 _summaries JSON（标准 schema）→ ingest（校验+置 summarized）→ archive（Obsidian 笔记）→ digest（跨源汇总+撰写每日摘要）→ tracking（主体档案追加+宏观/微观双视角）。
边界：摘要基于公开报纸文本；OCR 文本有识别噪声需交叉验证；同一主体去重（实体归一）。
Trigger: 读报, 每日摘要, 归档, 归纳, 跟踪主体, newspaper-reader.
---

# newspaper-reader：读报归纳 → Obsidian 归档 → 主体跟踪

## 归档契约（LLM 输出 schema：_summaries/<source>/<date>.json）

```jsonc
{"units": [{
  "id": "zgjsb_20260902_01",        // = issue.json units[].id
  "summary": "100-150字中文摘要",
  "category": ["经济", "科技"],       // 枚举：政治/军事/经济/民生/生产/科技/文化/生态/其他
  "entities": ["城市地下管网"],       // 人物/机构/政策/行业/事件
  "importance": 5,                   // 1-5
  "keywords": ["地下管网", "城市更新"]
}]}
```

## 流水线（每源每日四步）

```bash
R=~/.agents/skills/newspaper-reader/scripts/reader.py
python3 "$R" prepare --date 2026-09-02        # ① 未归纳单位清单（读文本）
# ② 我（Agent）阅读文本 → 按 schema 写 _summaries/<source>/<date>.json
python3 "$R" ingest  --date 2026-09-02        # ③ 校验(summary≥30字/category合法) → summarized
python3 "$R" archive --date 2026-09-02        # ④ Obsidian 报纸原文/<date>/<报>/(版)笔记
python3 "$R" digest  --date 2026-09-02        #   跨源汇总 → 撰写 每日摘要/<date>_全国新闻摘要.md
python3 "$R" tracking --entity 城市地下管网 --date 2026-09-02  # 主体档案追加时间线
```

## 执行要点

1. **先 prepare 再读**：prepare 列出 unit id + 字数；只处理 sourced 已 parsed 未 summarized 的单位（避免重复）。
2. **摘要标准**：事实导向（谁/何时/何地/数据），含变化信号（↑↓→）与待观察点；实体归一（同一主体统一命名）；OCR 噪声（繁体/错字）按上下文纠偏后再归纳。
3. **每日摘要撰写**：digest 给出分类分布+实体频次+条目包 → 按《每日摘要模板》写主题块（跨报聚类），落 vault/每日摘要/。
4. **跟踪**：tracking 命令自动追加时间线；背景/宏观/微观三节由我撰写/更新（变化检测：与档案最后一条对比，标注趋势方向）。
5. **幂等**：ingest 后 unit 带 summary；已 summarized 的源在 prepare 中自动排除；archive 覆盖写（内容确定性）。

## Obsidian 结构（vault = ~/Maitty的知识库/读报）

报纸原文/YYYY-MM-DD/<报>/NN版_版名.md ｜ 每日摘要/YYYY-MM-DD_全国新闻摘要.md ｜
主体跟踪/<主体>_档案.md ｜ 看板/（周报/月报）｜ _templates/（模板）
