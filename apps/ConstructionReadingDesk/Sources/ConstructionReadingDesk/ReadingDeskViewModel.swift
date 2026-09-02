import ConstructionReadingDeskCore
import Foundation
import SwiftUI

struct PresentedError: Identifiable {
    let id = UUID()
    let title: String
    let detail: String
    let recovery: String
}

@MainActor
final class ReadingDeskViewModel: ObservableObject {
    @Published private(set) var issues: [IssueSummary] = []
    @Published var selectedIssueID: String?
    @Published private(set) var issueDetail: IssueDetail?
    @Published var selectedEditionID: String?
    @Published var editorState: DraftEditorState?
    @Published private(set) var dirtyUnitIDs: Set<String> = []
    @Published private(set) var isBusy = false
    @Published private(set) var operationTitle = ""
    @Published var presentedError: PresentedError?
    @Published var publishPlan: PublishPlan?
    @Published private(set) var history: [HistoryTransaction] = []
    @Published var notice: String?
    @Published private(set) var lastLog = ""
    @Published var showingDiscardChangesConfirmation = false

    private unowned let settings: AppSettings
    private var retryAction: RetryAction?
    private var navigationGate = UnsavedChangesGate<NavigationAction>()
    private let constructionSourceID = "zgjsb"

    private enum NavigationAction: Equatable, Sendable {
        case refresh
        case selectIssue(String?)
        case importPDF(URL, Bool)
    }

    private enum RetryAction {
        case refresh
        case loadIssue(String, String)
        case fetch
        case importPDF(URL, Bool)
        case save
        case preview
        case history
        case publish(String)
        case rollback(String)
    }

    init(settings: AppSettings) {
        self.settings = settings
    }

    var selectedIssue: IssueSummary? {
        issues.first { $0.stableID == selectedIssueID }
    }

    var selectedEdition: EditionRecord? {
        issueDetail?.editions.first { $0.id == selectedEditionID }
    }

    var hasUnsavedChanges: Bool { !dirtyUnitIDs.isEmpty || editorState?.hasUnsavedChanges == true }

    var allWarnings: [String] {
        Array(Set(issues.flatMap(\.warnings) + (issueDetail?.warnings ?? []))).sorted()
    }

    func refresh() {
        guard navigationGate.request(.refresh, hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        refreshNow()
    }

    private func refreshNow() {
        run(title: "正在刷新收件箱", retry: .refresh) { [weak self] in
            guard let self else { return }
            let client = self.makeClient()
            _ = try await client.perform(.capabilities)
            let envelope = try await client.perform(.inbox(source: self.constructionSourceID))
            let mapped = try WorkbenchPayloadMapper().inbox(
                from: envelope.data,
                sourceID: self.constructionSourceID
            )
            self.issues = mapped.sorted { lhs, rhs in
                lhs.date == rhs.date ? lhs.sourceName ?? lhs.sourceID < rhs.sourceName ?? rhs.sourceID : lhs.date > rhs.date
            }
            self.lastLog = envelope.warnings.joined(separator: "\n")
            if self.selectedIssueID == nil || !mapped.contains(where: { $0.stableID == self.selectedIssueID }) {
                self.selectedIssueID = mapped.first?.stableID
            }
            if let selected = self.selectedIssue {
                try await self.loadIssueNow(source: selected.sourceID, date: selected.date, client: client)
            } else {
                self.issueDetail = nil
                self.selectedEditionID = nil
                self.editorState = nil
            }
        }
    }

    func selectIssue(_ id: String?) {
        guard id != selectedIssueID else { return }
        guard navigationGate.request(.selectIssue(id), hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        selectIssueNow(id)
    }

    private func selectIssueNow(_ id: String?) {
        selectedIssueID = id
        guard let issue = selectedIssue else {
            issueDetail = nil
            selectedEditionID = nil
            editorState = nil
            return
        }
        run(title: "正在读取整期报纸", retry: .loadIssue(issue.sourceID, issue.date)) { [weak self] in
            guard let self else { return }
            try await self.loadIssueNow(source: issue.sourceID, date: issue.date, client: self.makeClient())
        }
    }

    func selectEdition(_ id: String?) {
        guard id != selectedEditionID else { return }
        commitCurrentEditor()
        selectedEditionID = id
        loadEditorForSelection()
    }

    func updateTitle(_ value: String) { mutateDraft { $0.title = value } }
    func updateSummary(_ value: String) { mutateDraft { $0.summary = value } }
    func setImportance(_ value: Int) {
        guard var editor = editorState else { return }
        editor.setImportance(value)
        editorState = editor
        markCurrentDirty()
    }

    func toggleTopic(_ topic: ReadingTopic) {
        mutateDraft { draft in
            if draft.topics.contains(topic) { draft.topics.remove(topic) }
            else { draft.topics.insert(topic) }
        }
    }

    func updateFact(at index: Int, _ keyPath: WritableKeyPath<FactFields, String>, value: String) {
        mutateDraft { draft in
            guard draft.facts.indices.contains(index) else { return }
            draft.facts[index][keyPath: keyPath] = value
        }
    }

    func addFact() {
        mutateDraft { $0.facts.append(FactFields()) }
    }

    func removeFact(at index: Int) {
        mutateDraft { draft in
            guard draft.facts.indices.contains(index) else { return }
            draft.facts.remove(at: index)
        }
    }

    func fetchConstructionPaper() {
        run(title: "正在抓取中国建设报", retry: .fetch) { [weak self] in
            guard let self else { return }
            let client = self.makeClient()
            self.lastLog = try await client.fetchConstructionPaper()
            let envelope = try await client.perform(.inbox(source: self.constructionSourceID))
            self.issues = try WorkbenchPayloadMapper().inbox(
                from: envelope.data,
                sourceID: self.constructionSourceID
            )
            self.notice = "抓取完成，已刷新收件箱。"
        }
    }

    func importPDF(_ url: URL, removeAfterImport: Bool = false) {
        let action = NavigationAction.importPDF(url, removeAfterImport)
        guard navigationGate.request(action, hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        importPDFNow(url, removeAfterImport: removeAfterImport)
    }

    private func importPDFNow(_ url: URL, removeAfterImport: Bool) {
        run(title: "正在导入并解析 PDF", retry: .importPDF(url, removeAfterImport)) { [weak self] in
            guard let self else { return }
            let temporaryFile = TemporaryImportFile(
                url: url,
                removesAfterSuccessfulImport: removeAfterImport
            )
            let accessed = url.startAccessingSecurityScopedResource()
            defer {
                if accessed { url.stopAccessingSecurityScopedResource() }
            }
            let client = self.makeClient()
            let imported = try await client.perform(.importFile(path: url, source: "zgjsb"))
            temporaryFile.finish(importSucceeded: true)
            self.retryAction = .refresh
            self.lastLog = imported.warnings.joined(separator: "\n")
            let envelope = try await client.perform(.inbox(source: self.constructionSourceID))
            let mapped = try WorkbenchPayloadMapper().inbox(
                from: envelope.data,
                sourceID: self.constructionSourceID
            )
            self.issues = mapped
            if let importedObject = imported.data?.objectValue,
               let source = importedObject["source"]?.stringValue,
               let date = importedObject["date"]?.stringValue,
               let issue = mapped.first(where: { $0.sourceID == source && $0.date == date }) {
                self.selectedIssueID = issue.stableID
                try await self.loadIssueNow(source: source, date: date, client: client)
            }
            self.notice = "PDF 已导入归档目录，等待人工复核。"
        }
    }

    func saveDraft() {
        run(title: "正在保存整期草稿", retry: .save) { [weak self] in
            guard let self else { return }
            let request = try self.makeDraftRequest()
            let envelope = try await self.makeClient().saveDraft(request)
            self.lastLog = envelope.warnings.joined(separator: "\n")
            self.dirtyUnitIDs.removeAll()
            if var editor = self.editorState {
                editor.markSaved()
                self.editorState = editor
            }
            self.notice = "草稿已保存到本地归档，尚未写入 Obsidian。"
        }
    }

    func previewPublish() {
        run(title: "正在生成发布预览", retry: .preview) { [weak self] in
            guard let self else { return }
            let request = try self.makeDraftRequest()
            let client = self.makeClient()
            _ = try await client.saveDraft(request)
            self.dirtyUnitIDs.removeAll()
            if var editor = self.editorState {
                editor.markSaved()
                self.editorState = editor
            }
            let envelope = try await client.perform(.publishPlan(source: request.source, date: request.date))
            self.publishPlan = try WorkbenchPayloadMapper()
                .publishPlan(from: envelope.data)
                .mergingWarnings(envelope.warnings)
            self.lastLog = (envelope.warnings + (self.publishPlan?.warnings ?? [])).joined(separator: "\n")
        }
    }

    func confirmPublish(planID: String) {
        run(title: "正在发布到 Obsidian", retry: .publish(planID)) { [weak self] in
            guard let self else { return }
            let result = try await self.makeClient().perform(.publishApply(planID: planID))
            let transactionID = result.data?.objectValue?["transaction_id"]?.stringValue
            self.publishPlan = nil
            self.notice = transactionID == nil ? "发布完成。" : "发布完成，已创建可回滚事务。"
            try await self.loadHistoryNow(client: self.makeClient())
            let inbox = try await self.makeClient().perform(.inbox(source: self.constructionSourceID))
            self.issues = try WorkbenchPayloadMapper().inbox(
                from: inbox.data,
                sourceID: self.constructionSourceID
            )
        }
    }

    func loadHistory() {
        run(title: "正在读取发布历史", retry: .history) { [weak self] in
            guard let self else { return }
            try await self.loadHistoryNow(client: self.makeClient())
        }
    }

    func rollback(transactionID: String) {
        run(title: "正在回滚发布", retry: .rollback(transactionID)) { [weak self] in
            guard let self else { return }
            let client = self.makeClient()
            _ = try await client.perform(.rollback(transactionID: transactionID))
            try await self.loadHistoryNow(client: client)
            let inbox = try await client.perform(.inbox(source: self.constructionSourceID))
            self.issues = try WorkbenchPayloadMapper().inbox(
                from: inbox.data,
                sourceID: self.constructionSourceID
            )
            self.notice = "发布已回滚；若知识库文件在发布后被手工修改，后端会拒绝覆盖。"
        }
    }

    func retryLastAction() {
        guard let retryAction else { return }
        presentedError = nil
        switch retryAction {
        case .refresh: refresh()
        case .loadIssue(let source, let date):
            run(title: "正在重新读取报纸", retry: retryAction) { [weak self] in
                guard let self else { return }
                try await self.loadIssueNow(source: source, date: date, client: self.makeClient())
            }
        case .fetch: fetchConstructionPaper()
        case .importPDF(let url, let remove): importPDF(url, removeAfterImport: remove)
        case .save: saveDraft()
        case .preview: previewPublish()
        case .history: loadHistory()
        case .publish(let id): confirmPublish(planID: id)
        case .rollback(let id): rollback(transactionID: id)
        }
    }

    func confirmDiscardAndContinue() {
        showingDiscardChangesConfirmation = false
        guard let action = navigationGate.confirmDiscard() else { return }
        dirtyUnitIDs.removeAll()
        switch action {
        case .refresh: refreshNow()
        case .selectIssue(let id): selectIssueNow(id)
        case .importPDF(let url, let removeAfterImport):
            importPDFNow(url, removeAfterImport: removeAfterImport)
        }
    }

    func cancelPendingNavigation() {
        showingDiscardChangesConfirmation = false
        if case .importPDF(let url, true) = navigationGate.pendingAction {
            try? FileManager.default.removeItem(at: url)
        }
        navigationGate.cancel()
    }

    func presentExternalError(title: String, detail: String) {
        retryAction = nil
        presentedError = PresentedError(
            title: title,
            detail: detail,
            recovery: "请重新选择 PDF 文件后重试。"
        )
    }

    private func makeClient() -> ReadDailyClient {
        ReadDailyClient(configuration: settings.configuration)
    }

    private func loadIssueNow(source: String, date: String, client: ReadDailyClient) async throws {
        let envelope = try await client.perform(.issue(source: source, date: date))
        issueDetail = try WorkbenchPayloadMapper().issue(from: envelope.data)
        selectedEditionID = issueDetail?.editions.first?.id
        dirtyUnitIDs.removeAll()
        loadEditorForSelection()
        lastLog = envelope.warnings.joined(separator: "\n")
    }

    private func loadHistoryNow(client: ReadDailyClient) async throws {
        let envelope = try await client.perform(.history)
        history = try WorkbenchPayloadMapper().history(from: envelope.data)
        lastLog = envelope.warnings.joined(separator: "\n")
    }

    private func mutateDraft(_ mutation: (inout ArticleDraft) -> Void) {
        guard var editor = editorState else { return }
        mutation(&editor.draft)
        editorState = editor
        markCurrentDirty()
    }

    private func markCurrentDirty() {
        if let id = selectedEditionID { dirtyUnitIDs.insert(id) }
    }

    private func commitCurrentEditor() {
        guard let state = editorState,
              let editionID = selectedEditionID,
              var detail = issueDetail,
              let index = detail.editions.firstIndex(where: { $0.id == editionID }) else { return }
        detail.editions[index].articles = [state.normalizedDraft()]
        issueDetail = detail
    }

    private func loadEditorForSelection() {
        guard let article = selectedEdition?.articles.first else {
            editorState = nil
            return
        }
        editorState = DraftEditorState(draft: article)
    }

    private func makeDraftRequest() throws -> DraftSaveRequest {
        commitCurrentEditor()
        guard let detail = issueDetail else {
            throw ReadDailyClientError.backendRejected(message: "请先选择一期报纸。", recovery: "从左侧收件箱选择报纸后重试。")
        }
        var missing: [String] = []
        let units: [DraftUnit] = detail.editions.compactMap { edition in
            guard let article = edition.articles.first else { return nil }
            if article.summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                missing.append("第\(edition.pageNumber ?? 0)版摘要")
            }
            if article.topics.isEmpty { missing.append("第\(edition.pageNumber ?? 0)版主题") }
            if article.facts.isEmpty {
                missing.append("第\(edition.pageNumber ?? 0)版事实字段")
            }
            for (factIndex, fact) in article.facts.enumerated() {
                if fact.subject.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || fact.action.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || fact.object.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || fact.source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    missing.append("第\(edition.pageNumber ?? 0)版第\(factIndex + 1)条事实")
                }
            }
            return DraftUnit(
                id: article.id,
                title: article.title.trimmingCharacters(in: .whitespacesAndNewlines),
                summary: article.summary.trimmingCharacters(in: .whitespacesAndNewlines),
                topics: article.topics,
                facts: article.facts,
                importance: article.importance
            )
        }
        guard units.count == detail.editions.count, missing.isEmpty else {
            let preview = missing.prefix(6).joined(separator: "、")
            throw ReadDailyClientError.backendRejected(
                message: "整期复核尚未完成。",
                recovery: "请补齐\(preview)\(missing.count > 6 ? "等项目" : "")。"
            )
        }
        return DraftSaveRequest(source: detail.sourceID, date: detail.date, units: units)
    }

    private func run(
        title: String,
        retry: RetryAction,
        operation: @escaping @MainActor () async throws -> Void
    ) {
        guard !isBusy else { return }
        isBusy = true
        operationTitle = title
        presentedError = nil
        retryAction = retry
        Task {
            defer {
                isBusy = false
                operationTitle = ""
            }
            do {
                try await operation()
            } catch {
                show(error)
            }
        }
    }

    private func show(_ error: Error) {
        let localized = error as? LocalizedError
        presentedError = PresentedError(
            title: localized?.errorDescription ?? "操作失败",
            detail: localized?.failureReason ?? error.localizedDescription,
            recovery: localized?.recoverySuggestion ?? "请修正后重试。"
        )
    }
}
