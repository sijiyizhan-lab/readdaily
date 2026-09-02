import AppKit
import ConstructionReadingDeskCore
import PDFKit
import SwiftUI

struct ReviewEditor: View {
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        ZStack {
            ReadingDeskBackground()
            if let issue = viewModel.issueDetail,
               let edition = viewModel.selectedEdition,
               let draft = viewModel.editorState?.draft {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        ReadingDeskWelcomeBanner(
                            issue: issue,
                            edition: edition,
                            warningCount: viewModel.allWarnings.count
                        )
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
                .groupBoxStyle(ReadingDeskGroupBoxStyle())
            } else {
                VStack(spacing: 18) {
                    ReadingDeskWelcomeBanner(issue: nil, edition: nil, warningCount: 0)
                    EmptyState(
                        title: "选择一个版次开始复核",
                        detail: "对照原版图检查 OCR，再完成摘要、主题、事实字段和重要性。",
                        symbol: "doc.text.magnifyingglass"
                    )
                    .readingDeskCard(cool: true)
                }
                .padding(24)
            }
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
                    .foregroundStyle(ReadingDeskTheme.accent)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(ReadingDeskTheme.accentSoft, in: Capsule())
            }
        }
        .readingDeskCard(padding: 16, cool: true)
    }

    @ViewBuilder
    private func sourceAndOCR(edition: EditionRecord, draft: ArticleDraft) -> some View {
        HSplitView {
            GroupBox {
                PagePreview(
                    imagePath: edition.imagePath,
                    pdfPath: edition.pdfPath,
                    pageIndex: max((edition.pageNumber ?? 1) - 1, 0)
                )
                .frame(minWidth: 280, idealWidth: 450, maxWidth: .infinity, minHeight: 370)
            } label: {
                Label("原版图", systemImage: "doc.richtext")
            }
            .accessibilityLabel("第\(edition.pageNumber ?? 0)版原版图")

            GroupBox {
                ScrollView {
                    Text(draft.ocrText.isEmpty ? "当前版次没有可用 OCR 原文。" : draft.ocrText)
                        .font(.body)
                        .lineSpacing(5)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                        .padding(10)
                }
                .background(ReadingDeskTheme.field, in: RoundedRectangle(cornerRadius: 8))
                .overlay {
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(ReadingDeskTheme.border)
                }
                .accessibilityLabel("OCR 原文，只读")
            } label: {
                HStack {
                    Label("OCR 原文", systemImage: "text.viewfinder")
                    Spacer()
                    Text("\(draft.ocrText.count) 字")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            .frame(minWidth: 280, idealWidth: 480, maxWidth: .infinity, minHeight: 370)
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
                .scrollContentBackground(.hidden)
                .background(ReadingDeskTheme.field, in: RoundedRectangle(cornerRadius: 8))
                .overlay {
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(ReadingDeskTheme.border)
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
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: 10)], alignment: .leading, spacing: 10) {
                ForEach(ReadingTopic.allCases) { topic in
                    Button {
                        viewModel.toggleTopic(topic)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: draft.topics.contains(topic) ? "checkmark.circle.fill" : "circle")
                            Text(topic.rawValue)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                        }
                        .foregroundStyle(draft.topics.contains(topic) ? ReadingDeskTheme.accent : Color.primary)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .padding(.horizontal, 12)
                        .background(
                            Capsule()
                                .fill(draft.topics.contains(topic) ? ReadingDeskTheme.accentSoft : ReadingDeskTheme.field)
                        )
                        .overlay {
                            Capsule()
                                .stroke(
                                    draft.topics.contains(topic) ? ReadingDeskTheme.accent : ReadingDeskTheme.border,
                                    lineWidth: draft.topics.contains(topic) ? 1.5 : 1
                                )
                        }
                    }
                    .buttonStyle(.plain)
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
                    .readingDeskCard(padding: 12, cool: true)
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
                .buttonStyle(.bordered)
                .tint(ReadingDeskTheme.accent)
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
        .background(ReadingDeskTheme.cardCool, in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .stroke(ReadingDeskTheme.border)
        }
    }
}

private struct ReadingDeskWelcomeBanner: View {
    let issue: IssueDetail?
    let edition: EditionRecord?
    let warningCount: Int
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.colorSchemeContrast) private var contrast

    private static let dateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "yyyy年M月d日"
        return formatter
    }()

    var body: some View {
        HStack(spacing: 16) {
            ZStack {
                Circle()
                    .fill(ReadingDeskTheme.card.opacity(0.86))
                Image(systemName: "building.2.crop.circle.fill")
                    .font(.system(size: 28, weight: .semibold))
                    .foregroundStyle(ReadingDeskTheme.accent)
            }
            .frame(width: 50, height: 50)
            .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 5) {
                Text("今日建设读报")
                    .font(.title3.weight(.bold))
                Text(summaryText)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            Spacer(minLength: 12)

            if let issue {
                HStack(spacing: 8) {
                    metric("\(issue.editions.count) 版", symbol: "rectangle.stack")
                    if warningCount > 0 {
                        metric("\(warningCount) 告警", symbol: "exclamationmark.triangle")
                    }
                }
            }
        }
        .padding(.horizontal, 18)
        .frame(maxWidth: .infinity, minHeight: 72, alignment: .leading)
        .background(
            LinearGradient(
                colors: reduceTransparency
                    ? [ReadingDeskTheme.card, ReadingDeskTheme.card]
                    : [ReadingDeskTheme.bannerStart, ReadingDeskTheme.bannerEnd],
                startPoint: .leading,
                endPoint: .trailing
            ),
            in: RoundedRectangle(cornerRadius: 14, style: .continuous)
        )
        .overlay {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(contrast == .increased ? ReadingDeskTheme.strongBorder : ReadingDeskTheme.border)
        }
        .shadow(color: reduceTransparency ? .clear : .black.opacity(0.055), radius: 7, y: 2)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private var summaryText: String {
        if let issue, let edition {
            let number = issue.issueNumber.map { "第\($0)期 · " } ?? ""
            return "\(issue.date) · \(number)正在复核第\(edition.pageNumber ?? 0)版"
        }
        return "\(Self.dateFormatter.string(from: Date())) · 准备一份中国建设报开始本地复核"
    }

    private func metric(_ text: String, symbol: String) -> some View {
        Label(text, systemImage: symbol)
            .font(.caption.weight(.semibold))
            .foregroundStyle(ReadingDeskTheme.accent)
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(ReadingDeskTheme.card.opacity(0.8), in: Capsule())
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
        .background(ReadingDeskTheme.field, in: RoundedRectangle(cornerRadius: 8))
        .overlay {
            RoundedRectangle(cornerRadius: 8)
                .stroke(ReadingDeskTheme.border)
        }
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
