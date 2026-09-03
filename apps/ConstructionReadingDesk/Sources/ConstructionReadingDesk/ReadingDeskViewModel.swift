import ConstructionReadingDeskCore
import Foundation
import SwiftUI

struct PresentedError: Identifiable {
    let id = UUID()
    let title: String
    let detail: String
    let recovery: String
}

struct IssuePayloadMappingExecutor: Sendable {
    typealias Transform = @Sendable (JSONValue?) throws -> IssueDetail

    private let transform: Transform

    init(
        transform: @escaping Transform = { value in
            try WorkbenchPayloadMapper().issue(from: value)
        }
    ) {
        self.transform = transform
    }

    func map(_ value: JSONValue?) async throws -> IssueDetail {
        let transform = self.transform
        let mappingTask = Task.detached(priority: .userInitiated) {
            try Task.checkCancellation()
            let issue = try transform(value)
            try Task.checkCancellation()
            return issue
        }
        return try await withTaskCancellationHandler {
            try await mappingTask.value
        } onCancel: {
            mappingTask.cancel()
        }
    }
}

@MainActor
final class ReadingDeskViewModel: ObservableObject {
    @Published private(set) var issues: [IssueSummary] = []
    @Published private(set) var dashboardDay: DailyReadingDay?
    @Published private(set) var availableDates: [String] = []
    @Published var selectedDate: String?
    @Published var selectedIssueID: String?
    @Published private(set) var issueDetail: IssueDetail?
    @Published var selectedEditionID: String?
    @Published var editorState: DraftEditorState?
    @Published private(set) var dirtyUnitIDs: Set<String> = []
    @Published private(set) var isBusy = false
    @Published private(set) var isFetchingDaily = false
    @Published private(set) var operationTitle = ""
    @Published private(set) var fetchOperationTitle = ""
    @Published private(set) var isIssueLoading = false
    @Published private(set) var issueLoadTitle = ""
    @Published var presentedError: PresentedError?
    @Published var publishPlan: PublishPlan? {
        didSet {
            if publishPlan == nil { publishPlanRevision = nil }
        }
    }
    @Published private(set) var history: [HistoryTransaction] = []
    @Published var notice: String?
    @Published private(set) var lastLog = ""
    @Published var showingDiscardChangesConfirmation = false

    private unowned let settings: AppSettings
    private var retryAction: RetryAction?
    private var navigationGate = UnsavedChangesGate<NavigationAction>()
    private let constructionSourceID = "zgjsb"
    private var editRevision = 0
    private var publishPlanRevision: Int?
    private let clientFactory: (ReadDailyConfiguration) -> ReadDailyClient
    private let issuePayloadMappingExecutor: IssuePayloadMappingExecutor
    private var issueLoadTask: Task<Void, Never>?
    private var fetchTask: Task<Void, Never>?
    private var issueLoadGeneration = 0
    private var readingMarkTasks: [IssueCacheKey: Task<Void, Never>] = [:]
    private var readingMarkTaskIDs: [IssueCacheKey: UUID] = [:]
    private var readingStatusOverrides: [IssueCacheKey: ReadingCompletionStatus] = [:]
    private var readingStatusRevisions: [IssueCacheKey: UInt64] = [:]
    private var readingBackendRevisions: [IssueCacheKey: Int] = [:]
    private var nextReadingStatusRevision: UInt64 = 0
    private var issueCache: [IssueCacheKey: IssueDetail] = [:]
    private var issueCacheOrder: [IssueCacheKey] = []
    private let issueCacheCapacity = 8

    private struct IssueCacheKey: Hashable, Sendable {
        let source: String
        let date: String
    }

    private enum NavigationAction: Equatable, Sendable {
        case refresh
        case fetchDaily(String)
        case selectDate(String)
        case selectIssue(String?)
        case importPDF(URL, Bool)
        case applySettings(ReadDailySettingsValues)
    }

    private enum RetryAction {
        case refresh
        case loadIssue(String, String)
        case fetchDaily(String)
        case importPDF(URL, Bool)
        case save
        case preview
        case history
        case publish(String)
        case rollback(String)
        case readingMark(ReadingCompletionStatus)
    }

    init(
        settings: AppSettings,
        issuePayloadMapper: @escaping IssuePayloadMappingExecutor.Transform = { value in
            try WorkbenchPayloadMapper().issue(from: value)
        },
        clientFactory: @escaping (ReadDailyConfiguration) -> ReadDailyClient = {
            ReadDailyClient(configuration: $0)
        }
    ) {
        self.settings = settings
        issuePayloadMappingExecutor = IssuePayloadMappingExecutor(transform: issuePayloadMapper)
        self.clientFactory = clientFactory
    }

    var selectedIssue: IssueSummary? {
        issues.first { $0.stableID == selectedIssueID }
    }

    var selectedEdition: EditionRecord? {
        issueDetail?.editions.first { $0.id == selectedEditionID }
    }

    var selectedReadingStatus: ReadingCompletionStatus {
        guard let issue = selectedIssue else { return .unread }
        return readingStatus(for: IssueCacheKey(source: issue.sourceID, date: issue.date))
    }

    func displayedReadingStatus(for entry: DailyNewspaperEntry) -> ReadingCompletionStatus {
        let date = entry.issue?.date ?? dashboardDay?.date ?? selectedDate ?? ""
        return readingStatus(for: IssueCacheKey(source: entry.source.id, date: date))
    }

    var displayedReadCount: Int {
        dashboardDay?.entries.filter { displayedReadingStatus(for: $0) == .completed }.count ?? 0
    }

    var canPublishSelectedIssue: Bool { issueDetail?.sourceID == constructionSourceID }

    var canNavigateEditions: Bool { !isBusy }

    var isEditorialBusy: Bool { isBusy || isFetchingDaily }

    var activeOperationTitle: String {
        if isBusy { return operationTitle }
        if isIssueLoading { return issueLoadTitle }
        return fetchOperationTitle
    }

    var hasUnsavedChanges: Bool { !dirtyUnitIDs.isEmpty || editorState?.hasUnsavedChanges == true }

    var allWarnings: [String] {
        Array(Set(issues.flatMap(\.warnings) + (issueDetail?.warnings ?? []))).sorted()
    }

    func refresh() {
        guard !isEditorialBusy else { return }
        guard navigationGate.request(.refresh, hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        refreshNow()
    }

    @discardableResult
    func applySettings(_ values: ReadDailySettingsValues) -> Bool {
        let current = settings.values
        guard values != current else { return true }
        guard !isEditorialBusy else {
            show(ReadDailyClientError.backendRejected(
                message: "当前操作尚未完成，不能切换数据目录。",
                recovery: "等待当前操作完成后，再应用设置。"
            ))
            return false
        }
        guard values.changesDataContext(comparedTo: current) else {
            settings.apply(values)
            notice = "今日信息设置已应用。"
            return true
        }
        guard navigationGate.request(.applySettings(values), hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return false
        }
        applySettingsNow(values)
        return true
    }

    private func applySettingsNow(_ values: ReadDailySettingsValues) {
        invalidateIssueCache(cancelReadingMarks: true)
        settings.apply(values)
        issues = []
        dashboardDay = nil
        availableDates = []
        selectedDate = nil
        history = []
        publishPlan = nil
        clearIssueSelection()
        editRevision = 0
        notice = "路径设置已应用，正在从新目录重新载入。"
        refreshNow()
    }

    private func refreshNow() {
        guard !isEditorialBusy else { return }
        invalidateIssueCache()
        invalidateLoadedIssue()
        run(title: "正在刷新收件箱", retry: .refresh) { [weak self] in
            guard let self else { return }
            let client = self.makeClient()
            try await self.refreshInboxNow(client: client, loadSelection: true)
        }
    }

    func selectDate(_ date: String) {
        guard !isEditorialBusy else { return }
        guard date != selectedDate else { return }
        guard navigationGate.request(.selectDate(date), hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        selectDateNow(date)
    }

    private func selectDateNow(_ date: String) {
        guard !isEditorialBusy else { return }
        selectedDate = date
        invalidateLoadedIssue(clearSelection: true)
        run(title: "正在读取 \(date) 的读报台", retry: .refresh) { [weak self] in
            guard let self else { return }
            let client = self.makeClient()
            await self.loadDashboardNow(date: date, client: client)
            let first = self.issues.first { $0.date == date }
            self.selectedIssueID = first?.stableID
            if let first {
                self.startIssueLoad(
                    source: first.sourceID,
                    date: first.date,
                    client: client,
                    recordOpened: true
                )
            } else {
                self.clearIssueSelection()
            }
        }
    }

    func selectIssue(_ id: String?) {
        guard !isBusy else { return }
        guard id != selectedIssueID else { return }
        guard navigationGate.request(.selectIssue(id), hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        selectIssueNow(id)
    }

    private func selectIssueNow(_ id: String?) {
        guard !isBusy else { return }
        selectedIssueID = id
        guard let issue = selectedIssue else {
            invalidateLoadedIssue()
            return
        }
        selectedDate = issue.date
        startIssueLoad(
            source: issue.sourceID,
            date: issue.date,
            client: makeClient(),
            recordOpened: true
        )
    }

    func selectEdition(_ id: String?) {
        guard canNavigateEditions else { return }
        guard id != selectedEditionID else { return }
        commitCurrentEditor()
        selectedEditionID = id
        loadEditorForSelection()
    }

    func updateTitle(_ value: String, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { $0.title = value }
    }

    func updateSummary(_ value: String, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { $0.summary = value }
    }

    func updateProofreadText(_ value: String, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            draft.proofreadText = value
            draft.ocrReviewStatus = value == draft.ocrText ? .unreviewed : .edited
        }
    }

    func setOCRReviewStatus(_ status: OCRReviewStatus, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { $0.ocrReviewStatus = status }
    }

    func restoreOriginalOCR(expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            draft.proofreadText = draft.ocrText
            draft.ocrReviewStatus = .unreviewed
        }
    }

    func addOCRSuspicion(expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { $0.ocrSuspicions.append("") }
    }

    func updateOCRSuspicion(at index: Int, value: String, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            guard draft.ocrSuspicions.indices.contains(index) else { return }
            draft.ocrSuspicions[index] = value
        }
    }

    func removeOCRSuspicion(at index: Int, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            guard draft.ocrSuspicions.indices.contains(index) else { return }
            draft.ocrSuspicions.remove(at: index)
        }
    }

    func setImportance(_ value: Int, expectedEditionID: String? = nil) {
        // Reading and editing stay available during a background fetch. Its
        // edit revision prevents the completion refresh from reloading over it.
        guard !isBusy else { return }
        guard var editor = editorState else { return }
        guard expectedEditionID == nil
                || (selectedEditionID == expectedEditionID && editor.draft.id == expectedEditionID) else { return }
        editor.setImportance(value)
        editorState = editor
        markCurrentDirty()
    }

    func toggleTopic(_ topic: ReadingTopic, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            if draft.topics.contains(topic) { draft.topics.remove(topic) }
            else { draft.topics.insert(topic) }
        }
    }

    func updateFact(
        at index: Int,
        _ keyPath: WritableKeyPath<FactFields, String>,
        value: String,
        expectedEditionID: String? = nil
    ) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            guard draft.facts.indices.contains(index) else { return }
            draft.facts[index][keyPath: keyPath] = value
        }
    }

    func addFact(expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { $0.facts.append(FactFields()) }
    }

    func removeFact(at index: Int, expectedEditionID: String? = nil) {
        mutateDraft(expectedEditionID: expectedEditionID) { draft in
            guard draft.facts.indices.contains(index) else { return }
            draft.facts.remove(at: index)
        }
    }

    func fetchDailyPapers() {
        guard !isEditorialBusy else { return }
        guard let date = selectedDate ?? dashboardDay?.date ?? availableDates.first else {
            show(ReadDailyClientError.backendRejected(
                message: "尚未选择读报日期。",
                recovery: "先刷新读报台，或从左栏选择一个有数据的日期。"
            ))
            return
        }
        guard navigationGate.request(.fetchDaily(date), hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        fetchDailyPapersNow(date: date)
    }

    private func fetchDailyPapersNow(date: String) {
        guard !isEditorialBusy else { return }
        selectedDate = date
        let revisionAtStart = editRevision
        isFetchingDaily = true
        fetchOperationTitle = "正在抓取 \(date) 当日8报"
        presentedError = nil
        retryAction = nil
        fetchTask = Task { [weak self] in
            guard let self else { return }
            defer {
                self.isFetchingDaily = false
                self.fetchOperationTitle = ""
                self.fetchTask = nil
            }
            do {
                let client = self.makeClient()
                self.lastLog = try await client.fetchDaily(date: date)
                self.invalidateIssueCache()
                try await self.refreshInboxNow(client: client, loadSelection: false)

                // Navigation is intentionally available while acquisition runs.
                // Reload only the still-selected paper and never replace edits
                // that arrived after the fetch began (for example a delayed IME
                // binding update).
                let keptLateEdits = self.hasUnsavedChanges
                    && self.editRevision != revisionAtStart
                if !keptLateEdits,
                   let selected = self.selectedIssue,
                   selected.date == date {
                    self.startIssueLoad(
                        source: selected.sourceID,
                        date: selected.date,
                        client: client,
                        recordOpened: false
                    )
                }

                let entries = self.dashboardDay?.entries ?? []
                let available = entries.filter { $0.issue != nil }.count
                let failed = entries.filter { $0.status == .failed }.count
                if keptLateEdits {
                    self.notice = "抓取完成；当前未保存编辑已保留，请保存后再刷新本报。"
                } else if available == NewspaperRegistry.dailySources.count, failed == 0 {
                    self.notice = "当日8报均已获取，读报台已刷新。"
                } else {
                    self.notice = "抓取结束：已获取 \(available)/8 份"
                        + (failed > 0 ? "，\(failed) 份失败" : "")
                        + "；缺报可稍后重试。"
                }
            } catch {
                self.show(error, retry: .fetchDaily(date))
            }
        }
    }

    func importPDF(_ url: URL, removeAfterImport: Bool = false) {
        guard !isEditorialBusy else {
            if removeAfterImport { try? FileManager.default.removeItem(at: url) }
            show(ReadDailyClientError.backendRejected(
                message: "当前操作尚未完成，不能导入 PDF。",
                recovery: "等待当前抓取、保存或发布完成后，再重新选择 PDF。"
            ))
            return
        }
        let action = NavigationAction.importPDF(url, removeAfterImport)
        guard navigationGate.request(action, hasUnsavedChanges: hasUnsavedChanges) else {
            showingDiscardChangesConfirmation = true
            return
        }
        importPDFNow(url, removeAfterImport: removeAfterImport)
    }

    private func importPDFNow(_ url: URL, removeAfterImport: Bool) {
        guard !isEditorialBusy else {
            if removeAfterImport { try? FileManager.default.removeItem(at: url) }
            return
        }
        invalidateIssueCache()
        invalidateLoadedIssue(clearSelection: true)
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
            try await self.refreshInboxNow(client: client, loadSelection: false)
            let mapped = self.issues
            if let importedObject = imported.data?.objectValue,
               let source = importedObject["source"]?.stringValue,
               let date = importedObject["date"]?.stringValue,
               let issue = mapped.first(where: { $0.sourceID == source && $0.date == date }) {
                self.selectedDate = date
                self.selectedIssueID = issue.stableID
                await self.loadDashboardNow(date: date, client: client)
                self.startIssueLoad(source: source, date: date, client: client, recordOpened: true)
            }
            self.notice = "PDF 已导入归档目录，等待人工复核。"
        }
    }

    func saveDraft() {
        run(title: "正在保存整期草稿", retry: .save) { [weak self] in
            guard let self else { return }
            let request = try self.makeDraftRequest(requirePublishReady: false)
            self.removeIssueFromCache(source: request.source, date: request.date)
            let revision = self.editRevision
            let envelope = try await self.makeClient().saveDraft(request)
            self.lastLog = envelope.warnings.joined(separator: "\n")
            if self.editRevision == revision {
                self.dirtyUnitIDs.removeAll()
                if var editor = self.editorState {
                    editor.markSaved()
                    self.editorState = editor
                }
            }
            self.notice = self.editRevision == revision
                ? "草稿已保存到本地归档，尚未写入 Obsidian。"
                : "保存完成；保存期间又有新编辑，请再次保存。"
        }
    }

    func previewPublish() {
        run(title: "正在生成发布预览", retry: .preview) { [weak self] in
            guard let self else { return }
            guard self.canPublishSelectedIssue else {
                throw ReadDailyClientError.backendRejected(
                    message: "当前报纸暂不支持发布到建设主题库。",
                    recovery: "目前仅中国建设报可进入 Obsidian 发布流程；其他报纸可阅读、校对并保存草稿。"
                )
            }
            let request = try self.makeDraftRequest(requirePublishReady: true)
            self.removeIssueFromCache(source: request.source, date: request.date)
            let revision = self.editRevision
            self.publishPlan = nil
            let client = self.makeClient()
            _ = try await client.saveDraft(request)
            guard self.editRevision == revision else {
                self.notice = "生成预览期间内容已变化；当前修改仍未保存，请重新保存并预览。"
                return
            }
            self.dirtyUnitIDs.removeAll()
            if var editor = self.editorState {
                editor.markSaved()
                self.editorState = editor
            }
            let envelope = try await client.perform(.publishPlan(source: request.source, date: request.date))
            guard PublishRevisionPolicy.canUsePlan(
                previewRevision: revision,
                currentRevision: self.editRevision,
                hasUnsavedChanges: self.hasUnsavedChanges
            ) else {
                self.notice = "生成预览期间内容已变化；旧预览已作废，请重新预览。"
                return
            }
            let plan = try WorkbenchPayloadMapper()
                .publishPlan(from: envelope.data)
                .mergingWarnings(envelope.warnings)
            self.publishPlanRevision = revision
            self.publishPlan = plan
            self.lastLog = (envelope.warnings + plan.warnings).joined(separator: "\n")
        }
    }

    func confirmPublish(planID: String) {
        guard PublishRevisionPolicy.canUsePlan(
            previewRevision: publishPlanRevision,
            currentRevision: editRevision,
            hasUnsavedChanges: hasUnsavedChanges
        ) else {
            publishPlan = nil
            show(ReadDailyClientError.backendRejected(
                message: "发布预览已过期。",
                recovery: "保存当前修改并重新生成发布预览后再确认。"
            ))
            return
        }
        run(title: "正在发布到 Obsidian", retry: .publish(planID)) { [weak self] in
            guard let self else { return }
            let result = try await self.makeClient().perform(.publishApply(planID: planID))
            let transactionID = result.data?.objectValue?["transaction_id"]?.stringValue
            self.publishPlan = nil
            self.notice = transactionID == nil ? "发布完成。" : "发布完成，已创建可回滚事务。"
            try await self.loadHistoryNow(client: self.makeClient())
            try await self.refreshInboxNow(client: self.makeClient(), loadSelection: false)
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
            try await self.refreshInboxNow(client: client, loadSelection: false)
            self.notice = "发布已回滚；若知识库文件在发布后被手工修改，后端会拒绝覆盖。"
        }
    }

    func markSelectedIssue(_ status: ReadingCompletionStatus) {
        guard !isEditorialBusy else { return }
        guard let issue = selectedIssue else { return }
        let key = IssueCacheKey(source: issue.sourceID, date: issue.date)
        cancelAutomaticReadingMark(for: key)
        run(title: status == .completed ? "正在标记今日已读" : "正在更新阅读状态", retry: .readingMark(status)) { [weak self] in
            guard let self else { return }
            let client = self.makeClient()
            do {
                let envelope = try await client.perform(
                    .readingMark(source: issue.sourceID, date: issue.date, status: status)
                )
                self.lastLog = envelope.warnings.joined(separator: "\n")
                let persistedStatus = envelope.data?.objectValue?["reading_status"]?.stringValue
                    .flatMap(ReadingCompletionStatus.init(rawValue:)) ?? status
                let backendRevision = envelope.data?.objectValue?["reading_revision"]?.intValue
                self.setLocalReadingStatus(
                    persistedStatus,
                    for: key,
                    backendRevision: backendRevision
                )
                await self.loadDashboardNow(date: issue.date, client: client)
                self.notice = status == .completed
                    ? "已记录今日读完 \(issue.sourceName ?? issue.sourceID)。"
                    : "已撤销今日完成标记。"
            } catch {
                // The cancelled automatic ``opened`` process may already have
                // committed.  Re-read the authoritative dashboard instead of
                // guessing which status won the race.
                let reconciled = await self.loadDashboardNow(date: issue.date, client: client)
                if !reconciled {
                    // Drop the speculative overlay when neither write nor
                    // reconciliation can be confirmed.  The inbox snapshot is
                    // the last known durable state and a later refresh can
                    // resolve an already-committed automatic write.
                    self.clearReadingStatusOverride(for: key)
                }
                throw error
            }
        }
    }

    func retryLastAction() {
        guard let retryAction else { return }
        presentedError = nil
        switch retryAction {
        case .refresh: refresh()
        case .loadIssue(let source, let date):
            startIssueLoad(source: source, date: date, client: makeClient(), recordOpened: false)
        case .fetchDaily(let date):
            selectedDate = date
            fetchDailyPapers()
        case .importPDF(let url, let remove): importPDF(url, removeAfterImport: remove)
        case .save: saveDraft()
        case .preview: previewPublish()
        case .history: loadHistory()
        case .publish(let id): confirmPublish(planID: id)
        case .rollback(let id): rollback(transactionID: id)
        case .readingMark(let status): markSelectedIssue(status)
        }
    }

    func confirmDiscardAndContinue() {
        showingDiscardChangesConfirmation = false
        guard let action = navigationGate.confirmDiscard() else { return }
        switch action {
        case .refresh: refreshNow()
        case .fetchDaily(let date): fetchDailyPapersNow(date: date)
        case .selectDate(let date): selectDateNow(date)
        case .selectIssue(let id): selectIssueNow(id)
        case .importPDF(let url, let removeAfterImport):
            importPDFNow(url, removeAfterImport: removeAfterImport)
        case .applySettings(let values):
            applySettingsNow(values)
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
        clientFactory(settings.configuration)
    }

    private func refreshInboxNow(client: ReadDailyClient, loadSelection: Bool) async throws {
        let issuesBeforeRequest = Dictionary(uniqueKeysWithValues: issues.map { ($0.stableID, $0) })
        let readingRevisionSnapshot = readingStatusRevisions
        let envelope = try await client.perform(.inbox())
        let issuesAtResponse = Dictionary(uniqueKeysWithValues: issues.map { ($0.stableID, $0) })
        let responseIssues = try WorkbenchPayloadMapper().inbox(from: envelope.data)
            .filter { NewspaperRegistry.source(id: $0.sourceID) != nil }
            .sorted { left, right in
                if left.date != right.date { return left.date > right.date }
                let leftOrder = NewspaperRegistry.dailySources.firstIndex { $0.id == left.sourceID } ?? .max
                let rightOrder = NewspaperRegistry.dailySources.firstIndex { $0.id == right.sourceID } ?? .max
                return leftOrder < rightOrder
            }
        let mapped = responseIssues.map { responseIssue in
            let key = IssueCacheKey(source: responseIssue.sourceID, date: responseIssue.date)
            let revisionChanged = readingStatusRevisions[key] != readingRevisionSnapshot[key]
            let confirmed = revisionChanged
                ? issuesAtResponse[responseIssue.stableID] ?? issuesBeforeRequest[responseIssue.stableID]
                : issuesBeforeRequest[responseIssue.stableID]
            guard let confirmed,
                  responseIssue.readingStatus == nil || revisionChanged else {
                return responseIssue
            }
            // The compact inbox endpoint may omit reading_status entirely.
            // Preserve the last confirmed local snapshot until the dashboard
            // supplies a newer authoritative value.
            return replacingReadingStatus(in: responseIssue, with: confirmed.readingStatus)
        }
        issues = mapped

        let legacyDates = Array(Set(mapped.map(\.date))).sorted(by: >)
        let today = ReadingDatePolicy.localDay()
        selectedDate = ReadingDatePolicy.initialSelection(
            selectedDate: selectedDate,
            availableDates: legacyDates,
            today: today
        )

        if let date = selectedDate {
            await loadDashboardNow(date: date, client: client)
        } else {
            do {
                let readingRevisionSnapshot = readingStatusRevisions
                let dashboardEnvelope = try await client.perform(.dailyDashboard())
                let day = try WorkbenchPayloadMapper().dailyDashboard(from: dashboardEnvelope.data)
                applyDashboard(
                    day,
                    legacyDates: legacyDates,
                    readingRevisionSnapshot: readingRevisionSnapshot
                )
                selectedDate = day.date
                lastLog = (envelope.warnings + dashboardEnvelope.warnings).joined(separator: "\n")
            } catch {
                dashboardDay = nil
                availableDates = ReadingDatePolicy.menuDates(
                    availableDates: legacyDates,
                    selectedDate: selectedDate,
                    today: today
                )
                lastLog = envelope.warnings.joined(separator: "\n")
            }
        }

        guard loadSelection else { return }
        let date = selectedDate
        let current = mapped.first { $0.stableID == selectedIssueID && $0.date == date }
        let issue = current ?? mapped.first { $0.date == date }
        selectedIssueID = issue?.stableID
        if let issue {
            startIssueLoad(
                source: issue.sourceID,
                date: issue.date,
                client: client,
                recordOpened: true
            )
        } else {
            clearIssueSelection()
        }
    }

    @discardableResult
    private func loadDashboardNow(date: String, client: ReadDailyClient) async -> Bool {
        let legacyDates = Array(Set(issues.map(\.date))).sorted(by: >)
        let readingRevisionSnapshot = readingStatusRevisions
        do {
            let envelope = try await client.perform(.dailyDashboard(date: date))
            let day = try WorkbenchPayloadMapper().dailyDashboard(from: envelope.data)
            applyDashboard(
                day,
                legacyDates: legacyDates,
                readingRevisionSnapshot: readingRevisionSnapshot
            )
            lastLog = envelope.warnings.joined(separator: "\n")
            return true
        } catch {
            dashboardDay = DailyReadingDashboard(issues: issues).day(for: date)
            availableDates = ReadingDatePolicy.menuDates(
                availableDates: legacyDates,
                selectedDate: selectedDate,
                today: ReadingDatePolicy.localDay()
            )
            let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            lastLog = "仪表盘使用兼容模式：\(message)"
            return false
        }
    }

    private func applyDashboard(
        _ day: DailyReadingDay,
        legacyDates: [String],
        readingRevisionSnapshot: [IssueCacheKey: UInt64]
    ) {
        let responseRevisions = Dictionary(uniqueKeysWithValues: day.entries.map { entry in
            (IssueCacheKey(source: entry.source.id, date: day.date), entry.readingRevision)
        })
        func responseIsStale(for key: IssueCacheKey) -> Bool {
            let localWriteFinishedAfterRequest =
                readingStatusRevisions[key] != readingRevisionSnapshot[key]
            let backendRevisionWentBackwards =
                (responseRevisions[key] ?? 0) < (readingBackendRevisions[key] ?? 0)
            return localWriteFinishedAfterRequest || backendRevisionWentBackwards
        }
        readingStatusOverrides = readingStatusOverrides.filter {
            guard $0.key.date == day.date else { return true }
            if readingMarkTasks[$0.key] != nil { return true }
            // A status write completed after this dashboard request began.
            // Keep the newer local result until a later authoritative refresh.
            return responseIsStale(for: $0.key)
        }
        dashboardDay = day
        availableDates = ReadingDatePolicy.menuDates(
            availableDates: day.availableDates + legacyDates + [day.date],
            selectedDate: selectedDate,
            today: ReadingDatePolicy.localDay()
        )
        let dashboardIssues = day.entries.compactMap(\.issue)
        var byID = Dictionary(uniqueKeysWithValues: issues.map { ($0.stableID, $0) })
        dashboardIssues.forEach { dashboardIssue in
            let key = IssueCacheKey(source: dashboardIssue.sourceID, date: dashboardIssue.date)
            if responseIsStale(for: key),
               let current = byID[dashboardIssue.stableID] {
                // The response was captured before a newer local write
                // completed.  Accept its metadata, but do not roll back the
                // last confirmed reading state stored in the inbox snapshot.
                byID[dashboardIssue.stableID] = replacingReadingStatus(
                    in: dashboardIssue,
                    with: current.readingStatus
                )
            } else {
                byID[dashboardIssue.stableID] = dashboardIssue
            }
        }
        issues = byID.values.sorted { left, right in
            if left.date != right.date { return left.date > right.date }
            let leftOrder = NewspaperRegistry.dailySources.firstIndex { $0.id == left.sourceID } ?? .max
            let rightOrder = NewspaperRegistry.dailySources.firstIndex { $0.id == right.sourceID } ?? .max
            return leftOrder < rightOrder
        }
        for entry in day.entries {
            let key = IssueCacheKey(source: entry.source.id, date: day.date)
            if !responseIsStale(for: key) {
                readingBackendRevisions[key] = max(
                    readingBackendRevisions[key] ?? 0,
                    entry.readingRevision
                )
            }
        }
    }

    private func clearIssueSelection() {
        selectedIssueID = nil
        invalidateLoadedIssue()
    }

    private func invalidateLoadedIssue(clearSelection: Bool = false) {
        cancelIssueLoad()
        if clearSelection { selectedIssueID = nil }
        issueDetail = nil
        selectedEditionID = nil
        editorState = nil
        dirtyUnitIDs.removeAll()
        publishPlan = nil
        editRevision += 1
    }

    private func startIssueLoad(
        source: String,
        date: String,
        client: ReadDailyClient,
        recordOpened: Bool = false
    ) {
        let key = IssueCacheKey(source: source, date: date)
        let preferredEditionID = (
            issueDetail?.sourceID == key.source && issueDetail?.date == key.date
        ) ? selectedEditionID : nil
        invalidateLoadedIssue()
        let readingStatusAtOpen = readingStatus(for: key)
        let readingRevisionAtOpen = readingRevision(for: key)
        retryAction = .loadIssue(source, date)
        presentedError = nil

        if let cached = cachedIssue(for: key) {
            applyLoadedIssue(
                cached,
                warnings: [],
                key: key,
                preferredEditionID: preferredEditionID
            )
            if recordOpened {
                recordOpenedInBackgroundIfNeeded(
                    key: key,
                    previousStatus: readingStatusAtOpen,
                    expectedRevision: readingRevisionAtOpen,
                    client: client,
                    issueWarnings: []
                )
            }
            return
        }

        isIssueLoading = true
        issueLoadTitle = "正在读取整期报纸"
        let generation = issueLoadGeneration
        issueLoadTask = Task { [weak self] in
            guard let self else { return }
            do {
                let envelope = try await client.perform(.issue(source: source, date: date))
                try Task.checkCancellation()
                let loadedIssue = try await self.issuePayloadMappingExecutor.map(envelope.data)
                try Task.checkCancellation()
                guard self.isCurrentIssueLoad(generation: generation, key: key) else { return }
                guard loadedIssue.sourceID == source, loadedIssue.date == date else {
                    throw ReadDailyClientError.backendRejected(
                        message: "读报后端返回了其他报纸或日期的内容。",
                        recovery: "请刷新收件箱后重新选择目标报纸。"
                    )
                }

                self.storeIssueInCache(loadedIssue, for: key)
                self.applyLoadedIssue(
                    loadedIssue,
                    warnings: envelope.warnings,
                    key: key,
                    preferredEditionID: preferredEditionID
                )
                self.finishIssueLoad(generation: generation)
                if recordOpened {
                    self.recordOpenedInBackgroundIfNeeded(
                        key: key,
                        previousStatus: readingStatusAtOpen,
                        expectedRevision: readingRevisionAtOpen,
                        client: client,
                        issueWarnings: envelope.warnings
                    )
                }
            } catch {
                guard !Task.isCancelled,
                      self.isCurrentIssueLoad(generation: generation, key: key) else { return }
                self.finishIssueLoad(generation: generation)
                self.show(error, retry: .loadIssue(source, date))
            }
        }
    }

    private func applyLoadedIssue(
        _ loadedIssue: IssueDetail,
        warnings: [String],
        key: IssueCacheKey,
        preferredEditionID: String? = nil
    ) {
        guard selectedIssue?.sourceID == key.source,
              selectedIssue?.date == key.date else { return }
        issueDetail = loadedIssue
        selectedEditionID = preferredEditionID.flatMap { previous in
            loadedIssue.editions.contains { $0.id == previous } ? previous : nil
        } ?? loadedIssue.editions.first?.id
        dirtyUnitIDs.removeAll()
        loadEditorForSelection()
        lastLog = warnings.joined(separator: "\n")
    }

    private func isCurrentIssueLoad(generation: Int, key: IssueCacheKey) -> Bool {
        generation == issueLoadGeneration
            && selectedIssue?.sourceID == key.source
            && selectedIssue?.date == key.date
    }

    private func finishIssueLoad(generation: Int) {
        guard generation == issueLoadGeneration else { return }
        issueLoadTask = nil
        isIssueLoading = false
        issueLoadTitle = ""
    }

    private func cancelIssueLoad() {
        issueLoadGeneration &+= 1
        issueLoadTask?.cancel()
        issueLoadTask = nil
        isIssueLoading = false
        issueLoadTitle = ""
    }

    private func recordOpenedInBackgroundIfNeeded(
        key: IssueCacheKey,
        previousStatus: ReadingCompletionStatus,
        expectedRevision: Int,
        client: ReadDailyClient,
        issueWarnings: [String]
    ) {
        guard previousStatus == .unread, readingMarkTasks[key] == nil else { return }
        // Show immediate feedback without overwriting the last confirmed
        // inbox/dashboard snapshot used for failure reconciliation.
        setLocalReadingStatus(.opened, for: key, updateIssues: false)
        let taskID = UUID()
        readingMarkTaskIDs[key] = taskID
        readingMarkTasks[key] = Task { [weak self] in
            guard let self else { return }
            do {
                let activity = try await client.perform(
                    .readingMark(
                        source: key.source,
                        date: key.date,
                        status: .opened,
                        expectedRevision: expectedRevision
                    )
                )
                try Task.checkCancellation()
                guard self.readingMarkTaskIDs[key] == taskID else { return }
                let persistedStatus = activity.data?.objectValue?["reading_status"]?.stringValue
                    .flatMap(ReadingCompletionStatus.init(rawValue:)) ?? .opened
                let backendRevision = activity.data?.objectValue?["reading_revision"]?.intValue
                let knownBackendRevision = self.readingBackendRevisions[key] ?? 0
                let effectiveResponseRevision = backendRevision ?? expectedRevision
                guard effectiveResponseRevision >= knownBackendRevision else {
                    // Another client or a dashboard refresh has already
                    // observed a newer durable state. Remove only this task's
                    // speculative `opened` overlay; never roll the confirmed
                    // status or its known backend revision backwards.
                    self.clearReadingStatusOverride(for: key)
                    self.finishAutomaticReadingMark(for: key, taskID: taskID)
                    self.lastLog = (issueWarnings + activity.warnings).joined(separator: "\n")
                    return
                }
                self.setLocalReadingStatus(
                    persistedStatus,
                    for: key,
                    backendRevision: backendRevision
                )
                self.finishAutomaticReadingMark(for: key, taskID: taskID)
                self.lastLog = (issueWarnings + activity.warnings).joined(separator: "\n")
            } catch {
                let wasCancelled = Task.isCancelled
                guard self.readingMarkTaskIDs[key] == taskID else { return }
                guard !wasCancelled else {
                    self.finishAutomaticReadingMark(for: key, taskID: taskID)
                    return
                }
                let localRevisionBeforeReconcile = self.readingStatusRevisions[key]
                let snapshot = await self.readingStatusSnapshot(for: key, client: client)
                guard self.readingMarkTaskIDs[key] == taskID else { return }
                guard self.readingStatusRevisions[key] == localRevisionBeforeReconcile else {
                    self.finishAutomaticReadingMark(for: key, taskID: taskID)
                    return
                }
                if let snapshot,
                   snapshot.entry.readingRevision >= (self.readingBackendRevisions[key] ?? 0) {
                    self.setLocalReadingStatus(
                        snapshot.entry.readingStatus,
                        for: key,
                        backendRevision: snapshot.entry.readingRevision
                    )
                } else {
                    self.clearReadingStatusOverride(for: key)
                }
                self.finishAutomaticReadingMark(for: key, taskID: taskID)
                let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                self.lastLog = (
                    issueWarnings
                        + (snapshot?.warnings ?? [])
                        + ["已打开报纸，但未能确认首次阅读写入：\(message)"]
                )
                    .joined(separator: "\n")
            }
        }
    }

    private func finishAutomaticReadingMark(for key: IssueCacheKey, taskID: UUID) {
        guard readingMarkTaskIDs[key] == taskID else { return }
        readingMarkTasks[key] = nil
        readingMarkTaskIDs[key] = nil
    }

    private func cancelAutomaticReadingMark(for key: IssueCacheKey) {
        readingMarkTaskIDs[key] = nil
        readingMarkTasks.removeValue(forKey: key)?.cancel()
    }

    private func readingStatus(for key: IssueCacheKey) -> ReadingCompletionStatus {
        let inboxStatus = issues.first {
            $0.sourceID == key.source && $0.date == key.date
        }?.readingStatus.flatMap(ReadingCompletionStatus.init(rawValue:))
        let dashboardStatus = dashboardDay?.entries.first {
            $0.source.id == key.source && ($0.issue?.date ?? dashboardDay?.date) == key.date
        }?.readingStatus
        return readingStatusOverrides[key] ?? inboxStatus ?? dashboardStatus ?? .unread
    }

    private func readingRevision(for key: IssueCacheKey) -> Int {
        readingBackendRevisions[key] ?? dashboardDay?.entries.first {
            $0.source.id == key.source && ($0.issue?.date ?? dashboardDay?.date) == key.date
        }?.readingRevision ?? 0
    }

    private func setLocalReadingStatus(
        _ status: ReadingCompletionStatus,
        for key: IssueCacheKey,
        updateIssues: Bool = true,
        backendRevision: Int? = nil
    ) {
        if updateIssues { noteReadingStatusChange(for: key) }
        if let backendRevision {
            readingBackendRevisions[key] = max(
                readingBackendRevisions[key] ?? 0,
                backendRevision
            )
        }
        readingStatusOverrides[key] = status
        guard updateIssues else { return }
        issues = issues.map { issue in
            guard issue.sourceID == key.source, issue.date == key.date else { return issue }
            return replacingReadingStatus(in: issue, with: status.rawValue)
        }
    }

    private func replacingReadingStatus(
        in issue: IssueSummary,
        with readingStatus: String?
    ) -> IssueSummary {
        IssueSummary(
            id: issue.backendID,
            sourceID: issue.sourceID,
            sourceName: issue.sourceName,
            date: issue.date,
            issueNumber: issue.issueNumber,
            stage: issue.stage,
            readingStatus: readingStatus,
            warningCount: issue.warningCount,
            pageCount: issue.pageCount,
            warnings: issue.warnings
        )
    }

    private func clearReadingStatusOverride(for key: IssueCacheKey) {
        readingStatusOverrides[key] = nil
    }

    private func noteReadingStatusChange(for key: IssueCacheKey) {
        nextReadingStatusRevision &+= 1
        readingStatusRevisions[key] = nextReadingStatusRevision
    }

    private func cachedIssue(for key: IssueCacheKey) -> IssueDetail? {
        guard let cached = issueCache[key] else { return nil }
        issueCacheOrder.removeAll { $0 == key }
        issueCacheOrder.append(key)
        return cached
    }

    private func storeIssueInCache(_ issue: IssueDetail, for key: IssueCacheKey) {
        issueCache[key] = issue
        issueCacheOrder.removeAll { $0 == key }
        issueCacheOrder.append(key)
        while issueCacheOrder.count > issueCacheCapacity {
            issueCache.removeValue(forKey: issueCacheOrder.removeFirst())
        }
    }

    private func removeIssueFromCache(source: String, date: String) {
        let key = IssueCacheKey(source: source, date: date)
        issueCache[key] = nil
        issueCacheOrder.removeAll { $0 == key }
    }

    private func readingStatusSnapshot(
        for key: IssueCacheKey,
        client: ReadDailyClient
    ) async -> (entry: DailyNewspaperEntry, warnings: [String])? {
        do {
            let envelope = try await client.perform(.dailyDashboard(date: key.date))
            let day = try WorkbenchPayloadMapper().dailyDashboard(from: envelope.data)
            guard let entry = day.entries.first(where: { $0.source.id == key.source }) else {
                return nil
            }
            return (entry, envelope.warnings)
        } catch {
            return nil
        }
    }

    private func invalidateIssueCache(cancelReadingMarks: Bool = false) {
        issueCache.removeAll(keepingCapacity: true)
        issueCacheOrder.removeAll(keepingCapacity: true)
        guard cancelReadingMarks else { return }
        readingMarkTasks.values.forEach { $0.cancel() }
        readingMarkTasks.removeAll(keepingCapacity: true)
        readingMarkTaskIDs.removeAll(keepingCapacity: true)
        readingStatusOverrides.removeAll(keepingCapacity: true)
        readingStatusRevisions.removeAll(keepingCapacity: true)
        readingBackendRevisions.removeAll(keepingCapacity: true)
        nextReadingStatusRevision = 0
    }

    private func loadHistoryNow(client: ReadDailyClient) async throws {
        let envelope = try await client.perform(.history)
        history = try WorkbenchPayloadMapper().history(from: envelope.data)
        lastLog = envelope.warnings.joined(separator: "\n")
    }

    private func mutateDraft(
        expectedEditionID: String? = nil,
        _ mutation: (inout ArticleDraft) -> Void
    ) {
        // Keep editing available during a background fetch, including setters
        // already queued by TextField/IME. The completion refresh compares
        // editRevision and preserves these user edits.
        guard !isBusy else { return }
        guard var editor = editorState else { return }
        guard expectedEditionID == nil
                || (selectedEditionID == expectedEditionID && editor.draft.id == expectedEditionID) else { return }
        mutation(&editor.draft)
        editorState = editor
        markCurrentDirty()
    }

    private func markCurrentDirty() {
        publishPlan = nil
        if let id = selectedEditionID { dirtyUnitIDs.insert(id) }
        editRevision += 1
    }

    private func commitCurrentEditor() {
        guard let state = editorState,
              state.hasUnsavedChanges,
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

    private func makeDraftRequest(requirePublishReady: Bool) throws -> DraftSaveRequest {
        commitCurrentEditor()
        guard let detail = issueDetail else {
            throw ReadDailyClientError.backendRejected(message: "请先选择一期报纸。", recovery: "从左侧收件箱选择报纸后重试。")
        }
        let scopedIDs = Set(DraftSaveScope.unitIDs(
            all: detail.editions.map(\.id),
            dirty: dirtyUnitIDs,
            requireCompleteIssue: requirePublishReady
        ))
        guard requirePublishReady || !scopedIDs.isEmpty else {
            throw ReadDailyClientError.backendRejected(
                message: "当前没有需要保存的修改。",
                recovery: "先完成一处校对、摘要或事实编辑后再保存。"
            )
        }
        var missing: [String] = []
        let units: [DraftUnit] = detail.editions.compactMap { edition in
            guard scopedIDs.contains(edition.id) else { return nil }
            guard let article = edition.articles.first else { return nil }
            let facts = article.facts.filter { !$0.isEmpty }
            if requirePublishReady {
                if article.summary.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    missing.append("第\(edition.pageNumber ?? 0)版摘要")
                }
                if article.topics.isEmpty { missing.append("第\(edition.pageNumber ?? 0)版主题") }
                if facts.isEmpty {
                    missing.append("第\(edition.pageNumber ?? 0)版事实字段")
                }
                for (factIndex, fact) in facts.enumerated() {
                    if fact.subject.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        || fact.action.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        || fact.object.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        || fact.source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        missing.append("第\(edition.pageNumber ?? 0)版第\(factIndex + 1)条事实")
                    }
                }
            }
            return DraftUnit(
                id: article.id,
                title: article.title.trimmingCharacters(in: .whitespacesAndNewlines),
                ocrText: article.ocrText,
                proofreadText: article.proofreadText,
                ocrReviewStatus: article.ocrReviewStatus,
                ocrSuspicions: article.ocrSuspicions.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty },
                summary: article.summary.trimmingCharacters(in: .whitespacesAndNewlines),
                topics: article.topics,
                facts: facts,
                importance: article.importance
            )
        }
        guard (!requirePublishReady || units.count == detail.editions.count),
              !requirePublishReady || missing.isEmpty else {
            let preview = missing.prefix(6).joined(separator: "、")
            throw ReadDailyClientError.backendRejected(
                message: "整期复核尚未完成。",
                recovery: "请补齐\(preview)\(missing.count > 6 ? "等项目" : "")。"
            )
        }
        return DraftSaveRequest(
            source: detail.sourceID,
            date: detail.date,
            evidenceSHA256: detail.evidenceSHA256,
            units: units
        )
    }

    private func run(
        title: String,
        retry: RetryAction,
        operation: @escaping @MainActor () async throws -> Void
    ) {
        guard !isEditorialBusy else { return }
        isBusy = true
        operationTitle = title
        presentedError = nil
        retryAction = nil
        Task {
            defer {
                isBusy = false
                operationTitle = ""
            }
            do {
                try await operation()
            } catch {
                show(error, retry: retry)
            }
        }
    }

    private func show(_ error: Error, retry: RetryAction? = nil) {
        retryAction = retry
        let localized = error as? LocalizedError
        presentedError = PresentedError(
            title: localized?.errorDescription ?? "操作失败",
            detail: localized?.failureReason ?? error.localizedDescription,
            recovery: localized?.recoverySuggestion ?? "请修正后重试。"
        )
    }
}
