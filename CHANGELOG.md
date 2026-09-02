# CHANGELOG

## 0.2.1 (2026-09-02)
- 北京日报贯通：数字报专用域（bjrbdzb.bjd.com.cn）移动版 innerHTML 化 → 新适配器 `mobile_epaper`（版次/全文/版面图，同系统晚报/副中心报可复用）
- 注册表：环球时报按用户决策移除（dropped）；8 家全部启用
- 当日全集：8 源 87→99 条目，跨报摘要含北京日报 12 版

## 0.3.0 (2026-09-02)
- 新增原生 macOS「建设读报台」：收件箱、三栏复核、PDF 拖入/导入、发布差异和历史回滚
- 新增版本化 JSON 工作台 API，归一化多适配器的版面、OCR、摘要和状态
- 新增本地 PDFKit + Vision OCR 导入，按 SHA-256 去重，报头期号与文件名冲突会标记复核
- 新增 Obsidian 事务发布器：仅管理 `09-建设新闻与报纸摘要`，包含路径边界、差异预览、哈希冲突、幂等与回滚快照
- 修复公共 CLI 的 Python 启动方式、reader 子命令转发以及应用指定 archive 优先级
- 新增 Python 和 Swift 回归测试，覆盖真实 API 载荷、草稿校验、路径逃逸、发布冲突、幂等与回滚

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
