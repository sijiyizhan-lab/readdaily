# 建设读报台（macOS）

“建设读报台”是 `readdaily` 的原生 macOS 人工复核客户端。它负责查看原版图与 OCR、编辑中文摘要和事实字段、选择既有中文主题、预览发布差异，以及从发布历史中安全回滚。

最低系统版本为 macOS 13。客户端使用 SwiftUI、AppKit 与 PDFKit，不包含第三方依赖。

## 开发运行

```bash
cd apps/ConstructionReadingDesk
swift run ConstructionReadingDesk
```

首次打开后检查“设置”中的三个路径：

- `readdaily` 仓库：默认 `~/readdaily`
- 报纸归档目录：默认 `~/Library/Application Support/readdaily/news-archive`
- Obsidian Vault：默认 `~/Maitty的知识库`

应用通过以下稳定接口调用后端，不通过 shell 拼接参数：

```text
/usr/bin/python3 <repo>/scripts/readdaily.py api <command> --archive <path> --vault <path>
```

## 构建 `.app`

从仓库根目录运行：

```bash
./scripts/build_macos_app.sh
open "dist/建设读报台.app"
```

默认产物是 `dist/建设读报台.app`。也可传入一个以 `.app` 结尾的输出路径：

```bash
./scripts/build_macos_app.sh "$HOME/Desktop/建设读报台.app"
```

构建脚本只编译 Swift Package、组装应用包并执行 ad-hoc 签名；不会安装 LaunchAgent、不会修改技能软链，也不会读取或写入真实 Vault。

## 使用流程

1. 从工具栏添加/拖入 PDF，或抓取中国建设报。
2. 在左栏选择日期与报纸；查看阶段和质量告警。
3. 在中栏逐版检查；右栏对照原图和 OCR，填写摘要、主题、事实字段和重要性。
4. “保存草稿”只写归档目录，不触碰 Obsidian。
5. “预览发布”展示完整文件清单和 unified diff；再次点击“确认发布”才调用 Python 发布器。
6. 在“发布历史”查看事务，并在冲突检测保护下回滚。

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
cd apps/ConstructionReadingDesk
swift test
swift build -c release
```

测试覆盖版本化 Codable、真实 API 载荷映射、进程命令参数、严格 stdout JSON、中文错误与复核编辑状态。

## 边界

- Swift 客户端不直接写 Vault；唯一写入入口是 Python 的 `publish-apply`。
- 草稿临时 JSON 写入系统临时目录，调用结束后删除。
- OCR、原版图、缓存、日志、发布计划和事务快照均留在归档目录。
- 抓取只使用仓库已配置的公开渠道，不绕过登录、验证码或付费墙。
- 当前应用为本地单用户工作台，不提供云同步、多人协作或移动端。
