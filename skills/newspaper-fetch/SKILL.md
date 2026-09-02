---
name: newspaper-fetch
description: 多报每日抓取框架（注册表+适配器）。从网站/公众号免费公开渠道按日抓取报纸（方正常见版式/微信读报/PDF），归一化为统一 issue.json（期号/版次/版面图/文章或版级 OCR 文本），带状态机（fetched→parsed→summarized→archived→tracked）、期号连续性校验、幂等与离线回退。触发：跑报/抓报/下载报纸/今天报纸抓了没/适配新报纸。
方案：sources.json 注册表 → 每源渠道适配器（wechat_read 复用 jianshebao-daily 引擎+Vision OCR；founder 通用方正版式+--probe 模式探测）→ ~/Library/Application Support/readdaily/news-archive/<source>/<date>/issue.json + pages/ + text/ → _state/ 状态机 → _dailylog.jsonl。
边界：只抓已公开内容，不绕过验证码/登录；反爬仅做 UA/限速/Chrome 兜底。
Trigger: 多报抓取, newspaper-fetch, 适配新报纸, 抓昨天的报, 所有源跑一遍.
---

# newspaper-fetch：多报每日抓取框架

## 数据流

```
sources.json（注册表：源/渠道/入口/启用/优先级）
  └─ fetch.py 编排器（--date/--source/--stage/--offline/--probe）
       ├─ wechat_read  适配器：搜狗定位→src=11→本地ize→导读表→版级 Vision OCR
       └─ founder      适配器：方正版式探测(--probe)→版面图+文章清单（正文细化在 P2）
            落盘：~/Library/Application Support/readdaily/news-archive/<source>/<date>/{issue.json,pages/,text/}
            状态：~/Library/Application Support/readdaily/news-archive/_state/<source>/<date>.json
            日志：~/Library/Application Support/readdaily/news-archive/_dailylog.jsonl
```

## 常用命令

```bash
F=~/.agents/skills/newspaper-fetch/scripts/fetch.py
python3 "$F" --date 2026-09-02                    # 全部启用源（默认今天）
python3 "$F" --date 2026-09-02 --source zgjsb     # 指定源
python3 "$F" --date 2026-09-02 --stage parsed     # 只做解析(OCR)
python3 "$F" --date 2026-09-02 --offline          # 微信渠道不触发网络搜索
python3 "$F" --probe gmrb --date 2026-09-02       # 方正源版式探测（新源适配第一步）
```

## 执行要点（Agent 每次按此走）

1. **先看状态**：`python3 "$F" --date <今天>`（幂等，已抓自动跳过）；查 `_dailylog.jsonl` 了解历史。
2. **新源适配流程**：①web 找入口并更新 sources.json（channel/patterns/enabled=false）→ ②`--probe` 找可用版式 → ③改适配器或补 pattern → ④连续 3 个工作日 `--date` 逐日验收（期号连续性 + 版面数 + 文本量）。
3. **验证**：issue.json 的 editions/issue_no 非空；`链式校验`（期号差 1）OK；text/ 目录 OCR 文本 >200 字节/单元。
4. **排障**：裸 curl 受限（403/JS 壳）→ 先试 `--probe`，再考虑给适配器加 Chrome 兜底；搜狗限流 → 自动 UA 为新版 Chrome，等 10 分钟重试，勿刷。

## 依赖
Python3 + requests；wechat_read 依赖 jianshebao-daily 技能及其 bin/vocr（Vision OCR）；founder 为纯 requests。
