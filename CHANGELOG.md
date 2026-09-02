# CHANGELOG

## 0.1.0 (2026-09-02)
- 首次开源：4 类渠道适配器（wechat_read/founder/paper_api/cms_index），注册表驱动
- 7 家报纸实测全通（人民日报/光明日报/经济日报/科技日报/农民日报/南方日报/中国建设报）
- 归纳流水线（LLM schema + 校验门）→ Obsidian 归档（报纸原文/每日摘要/主体档案/看板）
- 主题检索 `readdaily query` + 主体跟踪 `readdaily tracking`
- launchd 定时（10:25/20:00 自动抓取）；install.sh 幂等安装（多端技能软链 + OCR 构建）
