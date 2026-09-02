# CHANGELOG

## 0.2.0 (2026-09-02)
- 新增 `readdaily all` 无人值守模式：OpenAI 兼容 LLM 逐篇归纳（严格 JSON schema + 校验重试）
- 每日摘要 LLM 生成（跨报主题块）+ 主体自动跟踪（top 实体）
- 新增 launchd 任务 com.guopeijun.readdaily-llm（10:50/20:15）
- 配置：llm.json（chmod 600，不入仓库）；README 已更新

## 0.1.0 (2026-09-02)
- 首次开源：4 类渠道适配器（wechat_read/founder/paper_api/cms_index），注册表驱动
- 7 家报纸实测全通（人民日报/光明日报/经济日报/科技日报/农民日报/南方日报/中国建设报）
- 归纳流水线（LLM schema + 校验门）→ Obsidian 归档（报纸原文/每日摘要/主体档案/看板）
- 主题检索 `readdaily query` + 主体跟踪 `readdaily tracking`
- launchd 定时（10:25/20:00 自动抓取）；install.sh 幂等安装（多端技能软链 + OCR 构建）
