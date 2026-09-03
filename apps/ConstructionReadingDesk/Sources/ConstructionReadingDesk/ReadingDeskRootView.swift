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
        GeometryReader { geometry in
            NavigationSplitView {
                DailyInboxSidebar(viewModel: viewModel)
                    .navigationSplitViewColumnWidth(min: 230, ideal: 285, max: 330)
            } content: {
                EditionColumn(viewModel: viewModel)
                    .navigationSplitViewColumnWidth(min: 230, ideal: 300, max: 350)
            } detail: {
                ReviewEditor(
                    settings: settings,
                    viewModel: viewModel,
                    layoutMode: ReadingWorkspaceLayout.mode(for: Double(geometry.size.width))
                )
            }
            .background(ReadingDeskBackground())
            .overlay {
                if isDropTarget {
                    RoundedRectangle(cornerRadius: 16)
                        .fill(ReadingDeskTheme.accentSoft.opacity(0.3))
                        .overlay {
                            RoundedRectangle(cornerRadius: 16)
                                .stroke(ReadingDeskTheme.accent, style: StrokeStyle(lineWidth: 3, dash: [10]))
                        }
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
                guard !viewModel.isEditorialBusy else { return }
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
                Button("放弃编辑并继续", role: .destructive) { viewModel.confirmDiscardAndContinue() }
                Button("留在当前内容", role: .cancel) { viewModel.cancelPendingNavigation() }
            } message: {
                Text("切换日期、报纸或刷新会丢失尚未保存的校对、摘要、主题与事实字段。")
            }
            .sheet(item: $viewModel.publishPlan) { plan in
                PublishPreviewSheet(
                    plan: plan,
                    isBusy: viewModel.isEditorialBusy,
                    onCancel: { viewModel.publishPlan = nil },
                    onConfirm: { viewModel.confirmPublish(planID: plan.id) }
                )
            }
            .sheet(isPresented: $showSettings) {
                SettingsPane(settings: settings, viewModel: viewModel) {
                    showSettings = false
                }
            }
            .sheet(isPresented: $showHistory) {
                HistorySheet(
                    transactions: viewModel.history,
                    isBusy: viewModel.isEditorialBusy,
                    onRefresh: { viewModel.loadHistory() },
                    onRollback: { viewModel.rollback(transactionID: $0) },
                    onClose: { showHistory = false }
                )
                .onAppear { viewModel.loadHistory() }
            }
        }
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            if viewModel.isEditorialBusy || viewModel.isIssueLoading {
                let title = viewModel.activeOperationTitle
                ProgressView().controlSize(.small).help(title)
                    .accessibilityLabel(title)
            }
            Button { viewModel.refresh() } label: { Label("刷新", systemImage: "arrow.clockwise") }
                .labelStyle(.iconOnly)
                .disabled(viewModel.isEditorialBusy)
                .keyboardShortcut("r", modifiers: .command)
                .help("刷新读报台")

            Button { viewModel.fetchDailyPapers() } label: {
                Label("抓取当日8报", systemImage: "arrow.down.doc.fill")
            }
            .disabled(viewModel.isEditorialBusy || viewModel.selectedDate == nil)
            .help("按所选日期抓取八家报纸；首次打开默认今天")

            Menu {
                Button { isImporting = true } label: {
                    Label("导入中国建设报 PDF…", systemImage: "plus.rectangle.on.folder")
                }
                Divider()
                Button { showHistory = true } label: { Label("发布历史", systemImage: "clock.arrow.circlepath") }
                Button { showSettings = true } label: { Label("设置", systemImage: "gearshape") }
            } label: {
                Label("更多", systemImage: "ellipsis.circle")
            }
            .disabled(viewModel.isEditorialBusy)

            Divider()

            Button { viewModel.saveDraft() } label: {
                Label("保存草稿", systemImage: viewModel.hasUnsavedChanges ? "square.and.arrow.down.fill" : "square.and.arrow.down")
            }
            .labelStyle(.iconOnly)
            .disabled(viewModel.isEditorialBusy || viewModel.issueDetail == nil || !viewModel.hasUnsavedChanges)
            .keyboardShortcut("s", modifiers: .command)
            .help("保存草稿，不写入 Obsidian")

            Button { viewModel.previewPublish() } label: {
                Label("预览发布", systemImage: "doc.text.magnifyingglass")
            }
            .buttonStyle(.borderedProminent)
            .tint(ReadingDeskTheme.accent)
            .disabled(viewModel.isEditorialBusy || !viewModel.canPublishSelectedIssue)
            .help(viewModel.canPublishSelectedIssue ? "预览后确认发布" : "目前仅中国建设报支持发布")
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        guard !viewModel.isEditorialBusy else { return false }
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

private struct DailyInboxSidebar: View {
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        ZStack {
            ReadingDeskBackground()
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 11) {
                    ReadDailyLogo(size: 44)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Read Daily").font(.headline.weight(.bold))
                        Text("本地读报 · 证据可追溯").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if viewModel.dashboardDay != nil {
                        Text("\(viewModel.displayedReadCount)/8 已读")
                            .font(.caption.monospacedDigit().weight(.semibold))
                            .foregroundStyle(ReadingDeskTheme.accentText)
                    }
                }
                .readingDeskCard(padding: 10)

                dateSelector

                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if let day = viewModel.dashboardDay {
                            ForEach(day.sections) { section in
                                VStack(alignment: .leading, spacing: 7) {
                                    ReadingDeskSectionTitle(
                                        title: section.category.rawValue,
                                        systemImage: categorySymbol(section.category),
                                        count: section.entries.count
                                    )
                                    ForEach(section.entries) { entry in
                                        DailyPaperRow(
                                            entry: entry,
                                            isSelected: entry.issue?.stableID == viewModel.selectedIssueID,
                                            isLoading: viewModel.isIssueLoading
                                                && entry.issue?.stableID == viewModel.selectedIssueID,
                                            readingStatus: viewModel.displayedReadingStatus(for: entry)
                                        ) {
                                            if let issue = entry.issue { viewModel.selectIssue(issue.stableID) }
                                        }
                                        .disabled(viewModel.isBusy)
                                    }
                                }
                            }
                        } else if !viewModel.isEditorialBusy {
                            EmptyRow(title: "暂无报纸", detail: "刷新或抓取当日8报", symbol: "tray")
                                .readingDeskCard(padding: 12, cool: true)
                        }
                    }
                    .padding(.bottom, 8)
                }

                if viewModel.isEditorialBusy || viewModel.isIssueLoading {
                    HStack(spacing: 9) {
                        ProgressView().controlSize(.small)
                        Text(viewModel.activeOperationTitle)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .readingDeskCard(padding: 10, cool: true)
                }
            }
            .padding(12)
        }
    }

    private var dateSelector: some View {
        HStack {
            Label("读报日期", systemImage: "calendar")
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            Menu(viewModel.selectedDate ?? "暂无日期") {
                ForEach(viewModel.availableDates, id: \.self) { date in
                    Button(date) { viewModel.selectDate(date) }
                }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(viewModel.isEditorialBusy || viewModel.availableDates.isEmpty)
            .accessibilityLabel("选择读报日期")
            .accessibilityValue(viewModel.selectedDate ?? "暂无日期")
        }
        .frame(minHeight: 44)
        .readingDeskCard(padding: 10, cool: true)
    }

    private func categorySymbol(_ category: NewspaperCategory) -> String {
        switch category {
        case .centralParty: return "building.columns"
        case .ministryIndustry: return "building.2"
        case .localParty: return "map"
        }
    }
}

private struct DailyPaperRow: View {
    let entry: DailyNewspaperEntry
    let isSelected: Bool
    let isLoading: Bool
    let readingStatus: ReadingCompletionStatus
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 9) {
                Image(systemName: entry.status.symbolName).foregroundStyle(statusColor).frame(width: 18)
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(entry.source.name).font(.body.weight(.semibold))
                        Spacer()
                        if isLoading {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: readingStatus.symbolName).foregroundStyle(readingColor)
                        }
                    }
                    Text(entry.status.accessibleLabel).font(.caption).foregroundStyle(.secondary)
                    Label {
                        Text(readingStatus.accessibleLabel)
                            .foregroundStyle(.primary)
                    } icon: {
                        Image(systemName: readingStatus.symbolName)
                            .foregroundStyle(readingColor)
                    }
                    .font(.caption2.weight(.medium))
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, minHeight: 70, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 11).fill(isSelected ? ReadingDeskTheme.accentSoft : ReadingDeskTheme.card))
            .overlay { RoundedRectangle(cornerRadius: 11).stroke(isSelected ? ReadingDeskTheme.accent : ReadingDeskTheme.border, lineWidth: isSelected ? 1.5 : 1) }
            .overlay(alignment: .leading) { if isSelected { Capsule().fill(ReadingDeskTheme.accent).frame(width: 3).padding(.vertical, 9) } }
        }
        .buttonStyle(.plain)
        .disabled(entry.issue == nil)
        .opacity(entry.issue == nil ? 0.72 : 1)
        .accessibilityLabel("\(entry.source.name)，\(entry.status.accessibleLabel)，\(readingStatus.accessibleLabel)")
        .accessibilityValue(isSelected ? "已选择" : "未选择")
    }

    private var statusColor: Color {
        switch entry.status {
        case .published, .reviewComplete, .readyToPublish: return ReadingDeskTheme.statusPositive
        case .readyForReview: return ReadingDeskTheme.statusAttention
        case .running: return .blue
        case .failed: return ReadingDeskTheme.statusFailure
        case .notStarted: return .secondary
        }
    }

    private var readingColor: Color {
        switch readingStatus {
        case .completed: return ReadingDeskTheme.statusPositive
        case .opened: return ReadingDeskTheme.statusAttention
        case .unread: return .secondary
        }
    }
}

private struct EditionColumn: View {
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        ZStack {
            ReadingDeskBackground()
            VStack(alignment: .leading, spacing: 12) {
                if let issue = viewModel.issueDetail {
                    HStack(spacing: 11) {
                        Image(systemName: "newspaper.fill")
                            .font(.title2).foregroundStyle(ReadingDeskTheme.accent)
                            .frame(width: 42, height: 42)
                            .background(ReadingDeskTheme.cardCool, in: RoundedRectangle(cornerRadius: 9))
                        VStack(alignment: .leading, spacing: 3) {
                            Text(issue.sourceName).font(.headline)
                            Text("\(issue.date) · \(issue.editions.count) 个版次")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .readingDeskCard(padding: 12)

                    ReadingDeskSectionTitle(title: "版次", systemImage: "rectangle.stack", count: issue.editions.count)
                        .padding(.horizontal, 4)

                    ScrollView {
                        LazyVStack(spacing: 8) {
                            ForEach(issue.editions) { edition in
                                Button { viewModel.selectEdition(edition.id) } label: {
                                    EditionRow(
                                        edition: edition,
                                        isDirty: viewModel.dirtyUnitIDs.contains(edition.id),
                                        isSelected: viewModel.selectedEditionID == edition.id
                                    )
                                }
                                .buttonStyle(.plain)
                                .disabled(!viewModel.canNavigateEditions)
                            }
                        }
                    }
                } else if viewModel.isIssueLoading {
                    VStack(spacing: 12) {
                        ProgressView().controlSize(.regular)
                        Text("正在读取报纸").font(.headline)
                        Text(viewModel.selectedIssue?.sourceName ?? "请稍候…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(24)
                    .readingDeskCard(cool: true)
                } else {
                    EmptyState(
                        title: "选择一份报纸",
                        detail: "从左侧日期和分类中选择已有期次。",
                        symbol: "newspaper"
                    )
                    .readingDeskCard(cool: true)
                }
            }
            .padding(12)
        }
    }
}

private struct EditionRow: View {
    let edition: EditionRecord
    let isDirty: Bool
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 10) {
            AsyncPageImage(
                imagePath: edition.imagePath,
                pdfPath: edition.pdfPath,
                pageIndex: max((edition.pageNumber ?? 1) - 1, 0),
                targetPixels: 180,
                accessibilityText: "第\(edition.pageNumber ?? 0)版缩略图"
            )
            .frame(width: 44, height: 56)
            .clipShape(RoundedRectangle(cornerRadius: 5))
            .overlay { RoundedRectangle(cornerRadius: 5).stroke(ReadingDeskTheme.border) }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(edition.pageNumber.map { "第\($0)版" } ?? "版次")
                        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    if isDirty {
                        Label("未保存", systemImage: "circle.fill")
                            .font(.caption2)
                            .foregroundStyle(ReadingDeskTheme.statusAttention)
                    }
                }
                Text(edition.title).font(.body.weight(.medium)).lineLimit(2)
                Text("OCR \(edition.ocrText.count) 字").font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, minHeight: 78, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 11).fill(isSelected ? ReadingDeskTheme.accentSoft : ReadingDeskTheme.card))
        .overlay { RoundedRectangle(cornerRadius: 11).stroke(isSelected ? ReadingDeskTheme.accent : ReadingDeskTheme.border, lineWidth: isSelected ? 1.5 : 1) }
        .overlay(alignment: .leading) { if isSelected { Capsule().fill(ReadingDeskTheme.accent).frame(width: 3).padding(.vertical, 9) } }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("第\(edition.pageNumber ?? 0)版，\(edition.title)，OCR \(edition.ocrText.count) 字\(isDirty ? "，未保存" : "")")
    }
}

struct EmptyState: View {
    let title: String
    let detail: String
    let symbol: String

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol).font(.system(size: 34, weight: .light)).foregroundStyle(.secondary)
            Text(title).font(.headline)
            Text(detail).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }
}

private struct EmptyRow: View {
    let title: String
    let detail: String
    let symbol: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
    }
}

private struct NoticeBanner: View {
    let text: String
    let dismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(ReadingDeskTheme.statusPositive)
            Text(text)
            Button(action: dismiss) { Image(systemName: "xmark") }
                .buttonStyle(.plain).accessibilityLabel("关闭提示")
        }
        .padding(.horizontal, 14)
        .frame(minHeight: 44)
        .background(.regularMaterial, in: Capsule())
        .shadow(radius: 7, y: 2)
        .accessibilityElement(children: .contain)
        .onAppear { announce(text) }
        .onChange(of: text) { announce($0) }
    }

    private func announce(_ message: String) {
        NSAccessibility.post(
            element: NSApp as Any,
            notification: .announcementRequested,
            userInfo: [
                .announcement: message,
                .priority: NSAccessibilityPriorityLevel.medium.rawValue,
            ]
        )
    }
}
