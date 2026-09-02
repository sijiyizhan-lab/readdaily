import Foundation
import Testing
@testable import ConstructionReadingDesk
@testable import ConstructionReadingDeskCore

@Suite("Read Daily 导航状态一致性", .serialized)
@MainActor
struct ReadingDeskNavigationStateTests {
    @Test("确认丢弃会清除跨版未保存草稿，目标加载失败也不会复活旧编辑")
    func confirmedDiscardRemovesEveryUnsavedEditionWhenTargetLoadFails() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        viewModel.updateTitle("第一版未保存标题")
        viewModel.selectEdition("zgjsb-page-2")
        viewModel.updateSummary("第二版未保存摘要")
        #expect(viewModel.dirtyUnitIDs == ["zgjsb-page-1", "zgjsb-page-2"])

        await environment.runner.failIssue(source: "rmrb", date: TestEnvironment.otherDate)
        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        #expect(viewModel.showingDiscardChangesConfirmation)

        viewModel.confirmDiscardAndContinue()
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
        #expect(viewModel.dirtyUnitIDs.isEmpty)
        #expect(!viewModel.hasUnsavedChanges)

        await waitUntilIdle(viewModel)
        #expect(viewModel.selectedIssueID == "rmrb-\(TestEnvironment.otherDate)")
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
        #expect(!viewModel.hasUnsavedChanges)
    }

    @Test("跨报纸加载失败不会把旧报纸内容留给新选择")
    func failedIssueSelectionLeavesNoStaleEditableDetail() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.failIssue(source: "rmrb", date: TestEnvironment.otherDate)
        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntilIdle(viewModel)

        #expect(viewModel.selectedIssueID == "rmrb-\(TestEnvironment.otherDate)")
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.selectedEditionID == nil)
        #expect(viewModel.editorState == nil)
    }

    @Test("跨日期加载失败不会继续显示旧日期正文")
    func failedDateSelectionLeavesNoStaleEditableDetail() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.failIssue(source: "rmrb", date: TestEnvironment.otherDate)
        viewModel.selectDate(TestEnvironment.otherDate)
        await waitUntilIdle(viewModel)

        #expect(viewModel.selectedDate == TestEnvironment.otherDate)
        #expect(viewModel.selectedIssueID == "rmrb-\(TestEnvironment.otherDate)")
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
    }

    @Test("刷新收件箱失败时旧正文立即失效")
    func failedRefreshLeavesNoStaleEditableDetail() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.setInboxFailure(true)
        viewModel.refresh()
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
        await waitUntilIdle(viewModel)

        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
    }

    @Test("导入失败时旧正文不会留在导入后的工作区")
    func failedImportLeavesNoStaleEditableDetail() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)
        let pdf = environment.root.appendingPathComponent("incoming.pdf")
        try Data("%PDF-1.4".utf8).write(to: pdf)

        await environment.runner.setImportFailure(true)
        viewModel.importPDF(pdf)
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
        await waitUntilIdle(viewModel)

        #expect(viewModel.selectedIssueID == nil)
        #expect(viewModel.issueDetail == nil)
        #expect(viewModel.editorState == nil)
    }

    private func loadInitialIssue(in viewModel: ReadingDeskViewModel) async {
        viewModel.refresh()
        await waitUntilIdle(viewModel)
        #expect(viewModel.selectedIssueID == "zgjsb-\(TestEnvironment.today)")
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.editorState?.draft.id == "zgjsb-page-1")
    }

    private func waitUntilIdle(_ viewModel: ReadingDeskViewModel) async {
        for _ in 0..<300 {
            if !viewModel.isBusy { return }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        Issue.record("等待 ViewModel 操作结束超时")
    }
}

private final class TestEnvironment {
    static let today = ReadingDatePolicy.localDay()
    static let otherDate = "2026-08-31"

    let root: URL
    let settings: AppSettings
    let runner: NavigationStateRunner
    private let defaults: UserDefaults
    private let suiteName: String

    @MainActor
    init() throws {
        root = FileManager.default.temporaryDirectory
            .appendingPathComponent("readdaily-navigation-\(UUID().uuidString)", isDirectory: true)
        let repository = root.appendingPathComponent("repository", isDirectory: true)
        let scripts = repository.appendingPathComponent("scripts", isDirectory: true)
        try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
        try Data("#!/usr/bin/env python3\n".utf8)
            .write(to: scripts.appendingPathComponent("readdaily.py"))

        suiteName = "ReadDailyNavigationTests.\(UUID().uuidString)"
        defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.set(repository.path, forKey: "readingDesk.repositoryPath")
        defaults.set(root.appendingPathComponent("archive").path, forKey: "readingDesk.archivePath")
        defaults.set(root.appendingPathComponent("vault").path, forKey: "readingDesk.vaultPath")
        settings = AppSettings(defaults: defaults)
        runner = NavigationStateRunner(today: Self.today, otherDate: Self.otherDate)
    }

    @MainActor
    func makeViewModel() -> ReadingDeskViewModel {
        ReadingDeskViewModel(settings: settings) { [runner] configuration in
            ReadDailyClient(configuration: configuration, runner: runner)
        }
    }

    func cleanup() {
        defaults.removePersistentDomain(forName: suiteName)
        try? FileManager.default.removeItem(at: root)
    }
}

private actor NavigationStateRunner: ProcessRunning {
    private let today: String
    private let otherDate: String
    private var failingIssues: Set<String> = []
    private var inboxFails = false
    private var importFails = false

    init(today: String, otherDate: String) {
        self.today = today
        self.otherDate = otherDate
    }

    func failIssue(source: String, date: String) {
        failingIssues.insert("\(source)|\(date)")
    }

    func setInboxFailure(_ value: Bool) {
        inboxFails = value
    }

    func setImportFailure(_ value: Bool) {
        importFails = value
    }

    func run(_ request: ProcessRequest) async throws -> ProcessResult {
        let arguments = request.arguments
        if arguments.contains("inbox") {
            if inboxFails { return failure("收件箱加载失败") }
            return success(inboxPayload)
        }
        if arguments.contains("daily-dashboard") {
            let date = value(after: "--date", in: arguments) ?? today
            return success(dashboardPayload(date: date))
        }
        if arguments.contains("issue") {
            let source = value(after: "--source", in: arguments) ?? ""
            let date = value(after: "--date", in: arguments) ?? ""
            if failingIssues.contains("\(source)|\(date)") {
                return failure("期次加载失败")
            }
            return success(issuePayload(source: source, date: date))
        }
        if arguments.contains("import-file") {
            if importFails { return failure("导入失败") }
            return success(#"{"source":"zgjsb","date":"\#(today)"}"#)
        }
        return success("{}")
    }

    private var inboxPayload: String {
        """
        {"issues":[
          {"id":"zgjsb-\(today)","source":"zgjsb","source_name":"中国建设报","date":"\(today)","stage":"needs_review","reading_status":"completed","page_count":2},
          {"id":"rmrb-\(otherDate)","source":"rmrb","source_name":"人民日报","date":"\(otherDate)","stage":"needs_review","reading_status":"completed","page_count":1}
        ]}
        """
    }

    private func dashboardPayload(date: String) -> String {
        """
        {"date":"\(date)","available_dates":["\(today)","\(otherDate)"],"newspapers":[]}
        """
    }

    private func issuePayload(source: String, date: String) -> String {
        let sourceName = source == "rmrb" ? "人民日报" : "中国建设报"
        let prefix = source == "rmrb" ? "rmrb" : "zgjsb"
        let unitCount = source == "rmrb" ? 1 : 2
        let units = (1...unitCount).map { number in
            """
            {"id":"\(prefix)-page-\(number)","edition_no":\(number),"edition_name":"第\(number)版","title":"第\(number)版原始标题","ocr_text":"第\(number)版 OCR 原文","summary":"第\(number)版原始摘要","topics":["城市更新与城市治理"],"facts":[],"importance":3}
            """
        }.joined(separator: ",")
        return """
        {"issue":{"source":"\(source)","source_name":"\(sourceName)","date":"\(date)","evidence_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","units":[\(units)]}}
        """
    }

    private func success(_ payload: String) -> ProcessResult {
        let body = #"{"schema_version":1,"ok":true,"data":\#(payload),"warnings":[]}"#
        return ProcessResult(terminationStatus: 0, standardOutput: Data(body.utf8), standardError: Data())
    }

    private func failure(_ message: String) -> ProcessResult {
        ProcessResult(terminationStatus: 1, standardOutput: Data(), standardError: Data(message.utf8))
    }

    private func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}
