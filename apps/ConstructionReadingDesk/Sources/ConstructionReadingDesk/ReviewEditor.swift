import AppKit
import ConstructionReadingDeskCore
import PDFKit
import SwiftUI

struct ReviewEditor: View {
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        if let issue = viewModel.issueDetail,
           let edition = viewModel.selectedEdition,
           let draft = viewModel.editorState?.draft {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    reviewHeader(issue: issue, edition: edition, draft: draft)
                    sourceAndOCR(edition: edition, draft: draft)
                    summaryEditor(draft: draft)
                    topicEditor(draft: draft)
                    factEditor(draft: draft)
                    importanceEditor(draft: draft)
                    publishingBoundary
                }
                .padding(24)
                .frame(maxWidth: 1120, alignment: .leading)
            }
            .background(Color(nsColor: .textBackgroundColor).opacity(0.35))
        } else {
            EmptyState(
                title: "选择一个版次开始复核",
                detail: "对照原版图检查 OCR，再完成摘要、主题、事实字段和重要性。",
                symbol: "doc.text.magnifyingglass"
            )
        }
    }

    @ViewBuilder
    private func reviewHeader(issue: IssueDetail, edition: EditionRecord, draft: ArticleDraft) -> some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 6) {
                Text("第\(edition.pageNumber ?? 0)版 · \(edition.title)")
                    .font(.title2.weight(.bold))
                Text("\(issue.sourceName) · \(issue.date) · 原文 \(draft.ocrText.count) 字")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if viewModel.dirtyUnitIDs.contains(edition.id) {
                Label("尚未保存", systemImage: "circle.fill")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.blue)
            }
        }
    }

    @ViewBuilder
    private func sourceAndOCR(edition: EditionRecord, draft: ArticleDraft) -> some View {
        HSplitView {
            GroupBox("原版图") {
                PagePreview(
                    imagePath: edition.imagePath,
                    pdfPath: edition.pdfPath,
                    pageIndex: max((edition.pageNumber ?? 1) - 1, 0)
                )
                .frame(minWidth: 320, idealWidth: 480, maxWidth: .infinity, minHeight: 390)
            }
            .accessibilityLabel("第\(edition.pageNumber ?? 0)版原版图")

            GroupBox("OCR 原文（只读）") {
                TextEditor(text: .constant(draft.ocrText.isEmpty ? "当前版次没有可用 OCR 原文。" : draft.ocrText))
                    .font(.body)
                    .lineSpacing(5)
                    .disabled(true)
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .accessibilityLabel("OCR 原文")
            }
            .frame(minWidth: 320, idealWidth: 500, maxWidth: .infinity, minHeight: 390)
        }
        .frame(minHeight: 420)
    }

    @ViewBuilder
    private func summaryEditor(draft: ArticleDraft) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                TextField("中文标题", text: Binding(
                    get: { viewModel.editorState?.draft.title ?? "" },
                    set: viewModel.updateTitle
                ))
                .textFieldStyle(.roundedBorder)
                .font(.headline)
                .accessibilityLabel("知识卡片中文标题")

                TextEditor(text: Binding(
                    get: { viewModel.editorState?.draft.summary ?? "" },
                    set: viewModel.updateSummary
                ))
                .font(.body)
                .lineSpacing(5)
                .frame(minHeight: 132)
                .padding(8)
                .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                .overlay {
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color(nsColor: .separatorColor))
                }
                .accessibilityLabel("中文摘要")

                HStack {
                    Text("建议 100–180 字，保留数字、主体、动作和来源。")
                    Spacer()
                    Text("\(draft.summary.count) 字")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        } label: {
            Label("中文摘要", systemImage: "text.alignleft")
        }
    }

    @ViewBuilder
    private func topicEditor(draft: ArticleDraft) -> some View {
        GroupBox {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 10)], alignment: .leading, spacing: 10) {
                ForEach(ReadingTopic.allCases) { topic in
                    Button {
                        viewModel.toggleTopic(topic)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: draft.topics.contains(topic) ? "checkmark.circle.fill" : "circle")
                            Text(topic.rawValue)
                                .lineLimit(2)
                            Spacer(minLength: 0)
                        }
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .padding(.horizontal, 10)
                    }
                    .buttonStyle(.bordered)
                    .tint(draft.topics.contains(topic) ? .accentColor : .secondary)
                    .accessibilityLabel("主题：\(topic.rawValue)")
                    .accessibilityValue(draft.topics.contains(topic) ? "已选择" : "未选择")
                }
            }
            .padding(.vertical, 4)
        } label: {
            Label("知识主题", systemImage: "point.3.connected.trianglepath.dotted")
        }
    }

    @ViewBuilder
    private func factEditor(draft: ArticleDraft) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 16) {
                ForEach(Array(draft.facts.indices), id: \.self) { index in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Text("事实 \(index + 1)").font(.subheadline.weight(.semibold))
                            Spacer()
                            Button(role: .destructive) {
                                viewModel.removeFact(at: index)
                            } label: {
                                Label("删除事实", systemImage: "trash")
                            }
                            .controlSize(.large)
                            .accessibilityLabel("删除第 \(index + 1) 条事实")
                        }
                        Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 12) {
                            factRow("主体", index: index, keyPath: \FactFields.subject, prompt: "例如：住房城乡建设部")
                            factRow("动作", index: index, keyPath: \FactFields.action, prompt: "例如：发布、开工、投入")
                            factRow("对象", index: index, keyPath: \FactFields.object, prompt: "例如：城市更新项目")
                            factRow("数值", index: index, keyPath: \FactFields.value, prompt: "例如：2400")
                            factRow("单位", index: index, keyPath: \FactFields.unit, prompt: "例如：亿元、个、辆")
                            factRow("时间", index: index, keyPath: \FactFields.time, prompt: "例如：2026年9月")
                            factRow("来源", index: index, keyPath: \FactFields.source, prompt: "例如：中国建设报第1版")
                        }
                    }
                    if index != draft.facts.indices.last { Divider() }
                }
                if draft.facts.isEmpty {
                    Text("当前没有事实字段；发布前至少需要补充一条。")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                Button { viewModel.addFact() } label: {
                    Label("添加事实", systemImage: "plus.circle")
                }
                .controlSize(.large)
                .accessibilityLabel("添加一条事实")
            }
            .padding(.vertical, 4)
        } label: {
            Label("事实字段", systemImage: "checklist")
        }
    }

    @ViewBuilder
    private func factRow(_ label: String, index: Int, keyPath: WritableKeyPath<FactFields, String>, prompt: String) -> some View {
        GridRow {
            Text(label)
                .font(.subheadline.weight(.semibold))
                .frame(width: 52, alignment: .trailing)
            TextField(prompt, text: Binding(
                get: {
                    guard let facts = viewModel.editorState?.draft.facts,
                          facts.indices.contains(index) else { return "" }
                    return facts[index][keyPath: keyPath]
                },
                set: { viewModel.updateFact(at: index, keyPath, value: $0) }
            ))
            .textFieldStyle(.roundedBorder)
            .accessibilityLabel("事实字段：\(label)")
        }
    }

    @ViewBuilder
    private func importanceEditor(draft: ArticleDraft) -> some View {
        GroupBox {
            HStack(spacing: 16) {
                Text("低")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Picker("重要性", selection: Binding(
                    get: { viewModel.editorState?.draft.importance ?? 3 },
                    set: viewModel.setImportance
                )) {
                    ForEach(1...5, id: \.self) { value in
                        Text("\(value)").tag(value)
                    }
                }
                .pickerStyle(.segmented)
                .accessibilityLabel("重要性等级")
                .accessibilityValue("\(draft.importance)级")
                Text("高")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
        } label: {
            Label("重要性", systemImage: "chart.bar.fill")
        }
    }

    private var publishingBoundary: some View {
        Label(
            "保存草稿只写本地归档。只有在“预览发布”中查看全部文件差异并再次确认后，Python 后端才会写入 Obsidian。",
            systemImage: "lock.shield"
        )
        .font(.footnote)
        .foregroundStyle(.secondary)
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct PagePreview: View {
    let imagePath: String?
    let pdfPath: String?
    let pageIndex: Int

    var body: some View {
        Group {
            if let imagePath, let image = NSImage(contentsOfFile: imagePath) {
                ScrollView([.horizontal, .vertical]) {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else if let pdfPath, FileManager.default.fileExists(atPath: pdfPath) {
                PDFPreview(url: URL(fileURLWithPath: pdfPath), pageIndex: pageIndex)
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "doc.questionmark")
                        .font(.system(size: 38, weight: .light))
                        .foregroundStyle(.secondary)
                    Text("原版图不可用")
                        .font(.headline)
                    Text("可继续核对 OCR，或检查归档目录中的版面文件。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct PDFPreview: NSViewRepresentable {
    let url: URL
    let pageIndex: Int

    func makeNSView(context: Context) -> PDFView {
        let view = PDFView()
        view.autoScales = true
        view.displayMode = .singlePageContinuous
        view.displaysPageBreaks = true
        return view
    }

    func updateNSView(_ view: PDFView, context: Context) {
        if view.document?.documentURL != url {
            view.document = PDFDocument(url: url)
        }
        if let page = view.document?.page(at: pageIndex) {
            view.go(to: page)
        }
    }
}
