import AppKit
import ConstructionReadingDeskCore
import SwiftUI
import UniformTypeIdentifiers

struct ReadingDeskRootView: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var viewModel: ReadingDeskViewModel
    @State private var isImporting = false
    @State private var isDropTarget = false
    @State private var showSettings = false
    @State private var showHistory = false

    var body: some View {
        NavigationSplitView {
            InboxSidebar(viewModel: viewModel)
                .navigationSplitViewColumnWidth(min: 240, ideal: 286, max: 360)
        } content: {
            EditionColumn(viewModel: viewModel)
                .navigationSplitViewColumnWidth(min: 245, ideal: 300, max: 390)
        } detail: {
            ReviewEditor(viewModel: viewModel)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .overlay {
            if isDropTarget {
                RoundedRectangle(cornerRadius: 16)
                    .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 3, dash: [10]))
                    .padding(14)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }
        }
        .overlay(alignment: .bottom) {
            if let notice = viewModel.notice {
                NoticeBanner(text: notice) { viewModel.notice = nil }
                    .padding(18)
            }
        }
        .toolbar { toolbar }
        .fileImporter(isPresented: $isImporting, allowedContentTypes: [.pdf], allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls):
                if let url = urls.first { viewModel.importPDF(url) }
            case .failure(let error):
                viewModel.presentExternalError(title: "无法选择 PDF", detail: error.localizedDescription)
            }
        }
        .onDrop(of: [UTType.pdf.identifier], isTargeted: $isDropTarget, perform: handleDrop)
        .onReceive(NotificationCenter.default.publisher(for: .readingDeskImportPDF)) { _ in
            isImporting = true
        }
        .task { viewModel.refresh() }
        .alert(item: $viewModel.presentedError) { error in
            Alert(
                title: Text(error.title),
                message: Text("\(error.detail)\n\n\(error.recovery)"),
                primaryButton: .default(Text("重试")) { viewModel.retryLastAction() },
                secondaryButton: .cancel(Text("关闭"))
            )
        }
        .confirmationDialog(
            "有尚未保存的复核编辑",
            isPresented: $viewModel.showingDiscardChangesConfirmation,
            titleVisibility: .visible
        ) {
            Button("放弃编辑并继续", role: .destructive) {
                viewModel.confirmDiscardAndContinue()
            }
            Button("留在当前期次", role: .cancel) {
                viewModel.cancelPendingNavigation()
            }
        } message: {
            Text("切换期次或刷新会丢失尚未保存的标题、摘要、主题与事实字段。")
        }
        .sheet(item: $viewModel.publishPlan) { plan in
            PublishPreviewSheet(
                plan: plan,
                isBusy: viewModel.isBusy,
                onCancel: { viewModel.publishPlan = nil },
                onConfirm: { viewModel.confirmPublish(planID: plan.id) }
            )
        }
        .sheet(isPresented: $showSettings) {
            SettingsPane(settings: settings) {
                showSettings = false
                viewModel.refresh()
            }
        }
        .sheet(isPresented: $showHistory) {
            HistorySheet(
                transactions: viewModel.history,
                isBusy: viewModel.isBusy,
                onRefresh: { viewModel.loadHistory() },
                onRollback: { viewModel.rollback(transactionID: $0) },
                onClose: { showHistory = false }
            )
            .onAppear { viewModel.loadHistory() }
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            if viewModel.isBusy {
                ProgressView()
                    .controlSize(.small)
                    .help(viewModel.operationTitle)
                    .accessibilityLabel(viewModel.operationTitle)
            }
            Button { viewModel.refresh() } label: {
                Label("刷新", systemImage: "arrow.clockwise")
            }
            .disabled(viewModel.isBusy)
            .help("刷新收件箱（⌘R）")
            .accessibilityLabel("刷新收件箱")

            Button { isImporting = true } label: {
                Label("添加 PDF", systemImage: "plus.rectangle.on.folder")
            }
            .disabled(viewModel.isBusy)
            .help("添加 PDF（⌘O），也可拖入窗口")
            .accessibilityLabel("添加报纸 PDF")

            Button { viewModel.fetchConstructionPaper() } label: {
                Label("抓取中国建设报", systemImage: "arrow.down.doc")
            }
            .disabled(viewModel.isBusy)
            .help("从已配置的公开渠道抓取中国建设报")
            .accessibilityLabel("抓取中国建设报")

            Divider()

            Button { viewModel.saveDraft() } label: {
                Label("保存草稿", systemImage: viewModel.hasUnsavedChanges ? "square.and.arrow.down.fill" : "square.and.arrow.down")
            }
            .disabled(viewModel.isBusy || viewModel.issueDetail == nil)
            .help("保存整期草稿（⌘S），不会写入 Obsidian")
            .accessibilityLabel("保存整期草稿")

            Button { viewModel.previewPublish() } label: {
                Label("预览发布", systemImage: "doc.text.magnifyingglass")
            }
            .disabled(viewModel.isBusy || viewModel.issueDetail == nil)
            .help("先查看文件清单与差异，再确认发布")
            .accessibilityLabel("预览发布")

            Button {
                showHistory = true
            } label: {
                Label("发布历史", systemImage: "clock.arrow.circlepath")
            }
            .disabled(viewModel.isBusy)
            .help("查看发布历史与回滚")
            .accessibilityLabel("查看发布历史")

            Button { showSettings = true } label: {
                Label("设置", systemImage: "gearshape")
            }
            .disabled(viewModel.isBusy)
            .help("设置仓库、归档与知识库路径")
            .accessibilityLabel("打开设置")
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first(where: { $0.hasItemConformingToTypeIdentifier(UTType.pdf.identifier) }) else {
            return false
        }
        provider.loadFileRepresentation(forTypeIdentifier: UTType.pdf.identifier) { url, error in
            if let error {
                DispatchQueue.main.async {
                    viewModel.presentExternalError(title: "无法读取拖入的 PDF", detail: error.localizedDescription)
                }
                return
            }
            guard let url else { return }
            let copy = FileManager.default.temporaryDirectory
                .appendingPathComponent("readdaily-drop-\(UUID().uuidString)")
                .appendingPathExtension("pdf")
            do {
                try FileManager.default.copyItem(at: url, to: copy)
                DispatchQueue.main.async { viewModel.importPDF(copy, removeAfterImport: true) }
            } catch {
                DispatchQueue.main.async {
                    viewModel.presentExternalError(title: "无法准备拖入的 PDF", detail: error.localizedDescription)
                }
            }
        }
        return true
    }
}

private struct InboxSidebar: View {
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text("建设读报台")
                    .font(.title2.weight(.bold))
                Text("本地复核 · 证据可追溯")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(16)

            List(selection: Binding(
                get: { viewModel.selectedIssueID },
                set: { viewModel.selectIssue($0) }
            )) {
                Section("报纸收件箱") {
                    if viewModel.issues.isEmpty && !viewModel.isBusy {
                        EmptyRow(
                            title: "暂无报纸",
                            detail: "添加 PDF 或抓取中国建设报",
                            symbol: "tray"
                        )
                    }
                    ForEach(viewModel.issues) { issue in
                        InboxRow(issue: issue)
                            .tag(Optional(issue.stableID))
                    }
                }

                if !viewModel.allWarnings.isEmpty {
                    Section("质量告警") {
                        ForEach(Array(viewModel.allWarnings.prefix(8).enumerated()), id: \.offset) { _, warning in
                            Label(warning, systemImage: "exclamationmark.triangle.fill")
                                .font(.caption)
                                .foregroundStyle(.orange)
                                .accessibilityLabel("质量告警：\(warning)")
                        }
                    }
                }
            }
            .listStyle(.sidebar)

            if viewModel.isBusy {
                HStack(spacing: 10) {
                    ProgressView()
                    Text(viewModel.operationTitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.bar)
            }
        }
    }
}

private struct InboxRow: View {
    let issue: IssueSummary

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(stageColor)
                .frame(width: 9, height: 9)
                .padding(.top, 6)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(issue.sourceName ?? issue.sourceID)
                        .font(.body.weight(.semibold))
                    Spacer()
                    if issue.warningCount > 0 {
                        Label("\(issue.warningCount)", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
                HStack(spacing: 6) {
                    Text(issue.date)
                    if let number = issue.issueNumber { Text("第\(number)期") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                Text(stageText)
                    .font(.caption2.weight(.medium))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(stageColor.opacity(0.14), in: Capsule())
                    .foregroundStyle(stageColor)
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }

    private var stageText: String {
        switch issue.stage {
        case "needs_review": return "待复核"
        case "ready_to_publish": return "待发布"
        case "published": return "已发布"
        case "failed": return "处理失败"
        default: return issue.stage ?? "等待处理"
        }
    }

    private var stageColor: Color {
        switch issue.stage {
        case "published": return .green
        case "ready_to_publish": return .blue
        case "failed": return .red
        default: return .orange
        }
    }
}

private struct EditionColumn: View {
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let issue = viewModel.issueDetail {
                VStack(alignment: .leading, spacing: 5) {
                    Text(issue.sourceName)
                        .font(.headline)
                    Text([issue.date, issue.issueNumber.map { "第\($0)期" }].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(16)

                List(selection: Binding(
                    get: { viewModel.selectedEditionID },
                    set: { viewModel.selectEdition($0) }
                )) {
                    Section("版次 · \(issue.editions.count)") {
                        ForEach(issue.editions) { edition in
                            EditionRow(
                                edition: edition,
                                isDirty: viewModel.dirtyUnitIDs.contains(edition.id)
                            )
                            .tag(Optional(edition.id))
                        }
                    }
                }
                .listStyle(.inset)
            } else {
                EmptyState(
                    title: "选择一期报纸",
                    detail: "左侧显示已导入或已抓取的报纸。",
                    symbol: "newspaper"
                )
            }
        }
    }
}

private struct EditionRow: View {
    let edition: EditionRecord
    let isDirty: Bool

    var body: some View {
        HStack(spacing: 10) {
            ZStack(alignment: .topTrailing) {
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color(nsColor: .controlBackgroundColor))
                    .frame(width: 42, height: 54)
                    .overlay {
                        Image(systemName: edition.imagePath == nil ? "doc.text" : "photo")
                            .foregroundStyle(.secondary)
                    }
                if isDirty {
                    Circle().fill(.blue).frame(width: 8, height: 8).offset(x: 3, y: -3)
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(edition.pageNumber.map { "第\($0)版" } ?? "版次")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(edition.title)
                    .font(.body.weight(.medium))
                    .lineLimit(2)
                Text("OCR \(edition.ocrText.count) 字")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }
}

private struct EmptyRow: View {
    let title: String
    let detail: String
    let symbol: String

    var body: some View {
        Label {
            VStack(alignment: .leading) {
                Text(title)
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        } icon: {
            Image(systemName: symbol)
        }
        .padding(.vertical, 8)
    }
}

struct EmptyState: View {
    let title: String
    let detail: String
    let symbol: String

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol)
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(.secondary)
            Text(title).font(.title3.weight(.semibold))
            Text(detail).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(32)
        .accessibilityElement(children: .combine)
    }
}

private struct NoticeBanner: View {
    let text: String
    let dismiss: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            Text(text)
            Button(action: dismiss) { Image(systemName: "xmark") }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭通知")
        }
        .padding(.horizontal, 16)
        .frame(minHeight: 44)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
        .shadow(radius: 8, y: 3)
    }
}
