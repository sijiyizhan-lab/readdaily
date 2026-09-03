import Foundation
import Testing
@testable import ConstructionReadingDesk
@testable import ConstructionReadingDeskCore

@Suite("Read Daily 导航状态一致性", .serialized)
@MainActor
struct ReadingDeskNavigationStateTests {
    @Test("整批抓取期间保留当前报纸并允许即时换版")
    func editionNavigationRemainsResponsiveDuringDailyFetch() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.holdFetch(date: TestEnvironment.today)
        viewModel.fetchDailyPapers()
        await waitUntil {
            await environment.runner.fetchRequestCount(date: TestEnvironment.today) == 1
        }
        #expect(viewModel.isFetchingDaily)
        #expect(!viewModel.isBusy)
        #expect(viewModel.canNavigateEditions)
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")

        viewModel.selectEdition("zgjsb-page-2")
        #expect(viewModel.selectedEditionID == "zgjsb-page-2")
        #expect(viewModel.editorState?.draft.id == "zgjsb-page-2")

        await environment.runner.releaseFetch(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.selectedEditionID == "zgjsb-page-2")
        #expect(!viewModel.isFetchingDaily)
        #expect(viewModel.presentedError == nil)
    }

    @Test("整批抓取期间可切换到其他报纸且结果不会被回载覆盖")
    func newspaperNavigationRemainsResponsiveDuringDailyFetch() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.holdFetch(date: TestEnvironment.today)
        viewModel.fetchDailyPapers()
        await waitUntil {
            await environment.runner.fetchRequestCount(date: TestEnvironment.today) == 1
        }

        viewModel.selectIssue("kjrb-\(TestEnvironment.today)")
        await waitUntil {
            viewModel.issueDetail?.sourceID == "kjrb" && viewModel.isFetchingDaily
        }
        #expect(viewModel.selectedIssueID == "kjrb-\(TestEnvironment.today)")
        #expect(viewModel.issueDetail?.sourceID == "kjrb")
        #expect(!viewModel.isBusy)

        await environment.runner.releaseFetch(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)
        #expect(viewModel.selectedIssueID == "kjrb-\(TestEnvironment.today)")
        #expect(viewModel.issueDetail?.sourceID == "kjrb")
        #expect(await environment.runner.issueRequestCount(
            source: "kjrb",
            date: TestEnvironment.today
        ) == 2)
        #expect(viewModel.presentedError == nil)
    }

    @Test("抓取开始后迟到的输入法编辑会被保留且阻止自动回载")
    func delayedEditorSetterSurvivesDailyFetchCompletion() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        let issueRequestsBeforeFetch = await environment.runner.issueRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdFetch(date: TestEnvironment.today)
        viewModel.fetchDailyPapers()
        await waitUntil {
            await environment.runner.fetchRequestCount(date: TestEnvironment.today) == 1
        }

        // Simulate a TextField/IME setter that was queued immediately before
        // the editor became disabled for the background fetch.
        viewModel.updateTitle("输入法延迟提交的标题")
        #expect(viewModel.editorState?.draft.title == "输入法延迟提交的标题")
        #expect(viewModel.hasUnsavedChanges)

        await environment.runner.releaseFetch(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)

        #expect(viewModel.editorState?.draft.title == "输入法延迟提交的标题")
        #expect(viewModel.hasUnsavedChanges)
        #expect(await environment.runner.issueRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == issueRequestsBeforeFetch)
        #expect(viewModel.notice == "抓取完成；当前未保存编辑已保留，请保存后再刷新本报。")
        #expect(viewModel.presentedError == nil)
    }

    @Test("旧版次迟到的输入回调不会污染已经切换的新版本")
    func staleEditorSetterCannotMutateNewlySelectedEdition() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.holdFetch(date: TestEnvironment.today)
        viewModel.fetchDailyPapers()
        await waitUntil {
            await environment.runner.fetchRequestCount(date: TestEnvironment.today) == 1
        }

        viewModel.selectEdition("zgjsb-page-2")
        let secondEditionTitle = viewModel.editorState?.draft.title
        viewModel.updateTitle(
            "第一版迟到的输入",
            expectedEditionID: "zgjsb-page-1"
        )

        #expect(viewModel.selectedEditionID == "zgjsb-page-2")
        #expect(viewModel.editorState?.draft.title == secondEditionTitle)
        #expect(!viewModel.hasUnsavedChanges)

        await environment.runner.releaseFetch(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)
        #expect(viewModel.selectedEditionID == "zgjsb-page-2")
        #expect(viewModel.editorState?.draft.title == secondEditionTitle)
        #expect(viewModel.presentedError == nil)
    }

    @Test("抓取失败会复位独立进度状态并继续允许换版")
    func failedDailyFetchRestoresResponsiveNavigation() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)
        await environment.runner.failFetch(date: TestEnvironment.today)

        viewModel.fetchDailyPapers()
        await waitUntil {
            await environment.runner.fetchRequestCount(date: TestEnvironment.today) == 1
                && !viewModel.isFetchingDaily
        }

        #expect(!viewModel.isBusy)
        #expect(viewModel.canNavigateEditions)
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.presentedError != nil)

        viewModel.selectEdition("zgjsb-page-2")
        #expect(viewModel.selectedEditionID == "zgjsb-page-2")
    }

    @Test("抓取期间拒绝拖放导入时会清理临时 PDF")
    func busyPDFImportRemovesTemporaryDropCopy() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.holdFetch(date: TestEnvironment.today)
        viewModel.fetchDailyPapers()
        await waitUntil {
            await environment.runner.fetchRequestCount(date: TestEnvironment.today) == 1
        }
        let temporaryPDF = environment.root.appendingPathComponent("queued-drop.pdf")
        try Data("%PDF-1.4".utf8).write(to: temporaryPDF)

        viewModel.importPDF(temporaryPDF, removeAfterImport: true)

        #expect(!FileManager.default.fileExists(atPath: temporaryPDF.path))
        #expect(viewModel.presentedError?.title == "当前操作尚未完成，不能导入 PDF。")
        #expect(viewModel.isFetchingDaily)

        await environment.runner.releaseFetch(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)
    }

    @Test("大型详情映射不占用主线程，阻塞期间仍可快速切回")
    func largeIssueMappingKeepsMainActorResponsiveAndLatestWins() async throws {
        let environment = try TestEnvironment()
        let mappingProbe = BlockingIssuePayloadMapper()
        defer {
            mappingProbe.releaseBlockedCall()
            environment.cleanup()
        }
        let viewModel = environment.makeViewModel { value in
            try mappingProbe.map(value)
        }
        await loadInitialIssue(in: viewModel)

        mappingProbe.blockNextCall()
        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        let mappingStarted = await Task.detached {
            mappingProbe.waitUntilBlockedCallStarts()
        }.value
        #expect(mappingStarted)
        #expect(viewModel.isIssueLoading)

        var mainActorHeartbeat = false
        await Task { @MainActor in
            mainActorHeartbeat = true
        }.value
        #expect(mainActorHeartbeat)
        #expect(!mappingProbe.blockedCallRanOnMainThread)

        // The first issue is cached. Switching back must cancel the stale load
        // and restore it even while the detached mapper is still blocked.
        viewModel.selectIssue("zgjsb-\(TestEnvironment.today)")
        #expect(viewModel.selectedIssueID == "zgjsb-\(TestEnvironment.today)")
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(!viewModel.isIssueLoading)

        mappingProbe.releaseBlockedCall()
        for _ in 0..<20 { await Task.yield() }
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.presentedError == nil)
    }

    @Test("快速 A→B 切报时只有最后一次选择可以更新正文")
    func rapidIssueSwitchIsLatestWins() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        await environment.runner.holdIssue(source: "rmrb", date: TestEnvironment.otherDate)
        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntil {
            await environment.runner.issueRequestCount(source: "rmrb", date: TestEnvironment.otherDate) == 1
        }
        #expect(viewModel.isIssueLoading)
        #expect(!viewModel.isBusy)

        viewModel.selectIssue("zgjsb-\(TestEnvironment.today)")
        await waitUntilSettled(viewModel)
        #expect(viewModel.selectedIssueID == "zgjsb-\(TestEnvironment.today)")
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.presentedError == nil)

        await environment.runner.releaseIssue(source: "rmrb", date: TestEnvironment.otherDate)
        for _ in 0..<20 { await Task.yield() }
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.presentedError == nil)
    }

    @Test("打开状态写回延迟不会阻塞正文显示或继续切报")
    func delayedReadingMarkDoesNotBlockNavigation() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()

        viewModel.refresh()
        await waitUntil {
            let markCount = await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            )
            return viewModel.issueDetail?.sourceID == "zgjsb" && markCount == 1
        }
        #expect(!viewModel.isBusy)
        #expect(!viewModel.isIssueLoading)
        #expect(viewModel.selectedReadingStatus == .opened)

        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntilSettled(viewModel)
        #expect(viewModel.issueDetail?.sourceID == "rmrb")
        #expect(viewModel.presentedError == nil)

        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
    }

    @Test("手动完成会取消尚未落盘的自动 opened 写回")
    func manualCompletionCancelsPendingAutomaticOpenedMark() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }

        viewModel.markSelectedIssue(.completed)
        await waitUntilIdle(viewModel)
        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        await waitUntil {
            await environment.runner.storedReadingStatus(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == .completed
        }

        #expect(viewModel.selectedReadingStatus == .completed)
        #expect(viewModel.presentedError == nil)
    }

    @Test("过期 revision 的自动 opened 不会覆盖外部完成状态")
    func staleAutomaticOpenedUsesBackendCAS() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }

        // Simulate another app instance completing the paper after this
        // automatic request captured its expected backend revision.
        await environment.runner.setReadingStatus(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        await waitUntil { viewModel.selectedReadingStatus == .completed }

        #expect(await environment.runner.storedReadingStatus(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == .completed)
        #expect(viewModel.selectedReadingStatus == .completed)
    }

    @Test("手动阅读状态写入失败后以仪表盘权威状态为准")
    func failedManualReadingMarkReconcilesAuthoritativeStatus() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }
        #expect(viewModel.selectedReadingStatus == .opened)

        await environment.runner.failReadingMark(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        viewModel.markSelectedIssue(.completed)
        await waitUntilIdle(viewModel)
        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        for _ in 0..<20 { await Task.yield() }

        #expect(viewModel.selectedReadingStatus == .unread)
        #expect(viewModel.presentedError != nil)
    }

    @Test("手动写入与仪表盘同时失败时清除未确认的乐观状态")
    func failedManualMarkAndDashboardRestoreLastConfirmedStatus() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }
        #expect(viewModel.selectedReadingStatus == .opened)

        await environment.runner.failReadingMark(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.setDashboardFailure(true)
        viewModel.markSelectedIssue(.completed)
        await waitUntilIdle(viewModel)
        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        for _ in 0..<20 { await Task.yield() }

        #expect(viewModel.selectedReadingStatus == .unread)
        #expect(viewModel.presentedError != nil)
    }

    @Test("切报不会为了写阅读状态而重新拉取每日仪表盘")
    func issueSwitchDoesNotReloadDashboard() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)
        let initialCount = await environment.runner.dashboardRequestCount()

        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntilSettled(viewModel)

        #expect(await environment.runner.dashboardRequestCount() == initialCount)
        #expect(viewModel.issueDetail?.sourceID == "rmrb")
    }

    @Test("旧仪表盘响应不能覆盖稍后完成的 opened 写入")
    func staleDashboardDoesNotOverrideNewerOpenedStatus() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }

        await environment.runner.holdDashboard(date: TestEnvironment.today)
        viewModel.refresh()
        await waitUntil { await environment.runner.dashboardRequestCount() == 2 }

        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        await waitUntil {
            await environment.runner.storedReadingStatus(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == .opened
        }
        await environment.runner.releaseDashboard(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)

        #expect(viewModel.selectedReadingStatus == .opened)

        // Even if a subsequent manual write and its reconciliation both fail,
        // dropping the overlay must reveal the confirmed opened baseline, not
        // the stale unread dashboard response.
        await environment.runner.failReadingMark(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.setDashboardFailure(true)
        viewModel.markSelectedIssue(.completed)
        await waitUntilIdle(viewModel)
        #expect(viewModel.selectedReadingStatus == .opened)
    }

    @Test("旧 opened 成功响应不能覆盖已由仪表盘确认的较新完成状态")
    func staleAutomaticOpenedSuccessCannotOverrideNewerDashboardRevision() async throws {
        let environment = try TestEnvironment()
        defer {
            Task {
                await environment.runner.releaseReadingMarkResponseAfterCommit(
                    .opened,
                    source: "zgjsb",
                    date: TestEnvironment.today
                )
            }
            environment.cleanup()
        }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMarkResponseAfterCommit(
            .opened,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        let viewModel = environment.makeViewModel()

        viewModel.refresh()
        await waitUntil {
            await environment.runner.storedReadingStatus(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == .opened
        }

        // Simulate another app instance completing the newspaper while this
        // app still waits for the older opened response.
        await environment.runner.setReadingStatus(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        viewModel.refresh()
        await waitUntilSettled(viewModel)
        #expect(await environment.runner.storedReadingStatus(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == .completed)

        await environment.runner.releaseReadingMarkResponseAfterCommit(
            .opened,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await waitUntil { viewModel.selectedReadingStatus == .completed }

        #expect(viewModel.selectedReadingStatus == .completed)
        #expect(viewModel.displayedReadCount == 1)
        #expect(viewModel.presentedError == nil)
    }

    @Test("opened 后台写入失败只移除乐观层并保留已确认完成状态")
    func failedAutomaticOpenedPreservesNewerConfirmedStatus() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }

        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.setDashboardStatus(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.failReadingMark(
            .opened,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdDashboard(date: TestEnvironment.today)
        viewModel.refresh()
        await waitUntil { await environment.runner.dashboardRequestCount() == 2 }
        #expect(viewModel.selectedReadingStatus == .opened)

        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        await environment.runner.releaseDashboard(date: TestEnvironment.today)
        await waitUntilSettled(viewModel)
        await waitUntil { viewModel.selectedReadingStatus == .completed }

        #expect(viewModel.selectedReadingStatus == .completed)
        #expect(viewModel.displayedReadCount == 1)
    }

    @Test("收件箱请求期间完成的阅读写回不会被旧响应擦除")
    func staleCompactInboxDoesNotOverrideReadingWriteCompletedInFlight() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }

        await environment.runner.setInboxOmitsReadingStatus(true)
        await environment.runner.holdInbox()
        await environment.runner.setDashboardFailure(true)
        viewModel.refresh()
        await waitUntil { await environment.runner.inboxRequestCount() == 2 }

        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        await waitUntil {
            await environment.runner.storedReadingStatus(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == .opened
        }
        await environment.runner.releaseInbox()
        await waitUntilSettled(viewModel)

        #expect(viewModel.selectedReadingStatus == .opened)
    }

    @Test("自动 opened 已提交但响应失败时以权威回查为准")
    func ambiguousAutomaticOpenedFailureReconcilesCommittedStatus() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.failReadingMarkAfterCommit(
            .opened,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        let viewModel = environment.makeViewModel()

        viewModel.refresh()
        await waitUntilSettled(viewModel)
        await waitUntil {
            guard await environment.runner.dashboardRequestCount() >= 2 else { return false }
            return await environment.runner.storedReadingStatus(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == .opened
        }
        await waitUntil { viewModel.selectedReadingStatus == .opened }

        #expect(viewModel.selectedReadingStatus == .opened)
        #expect(await environment.runner.readingMarkRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == 1)
    }

    @Test("旧的自动 opened 回查不能覆盖后续手动完成")
    func staleAutomaticReconciliationCannotOverrideManualCompletion() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdReadingMark(source: "zgjsb", date: TestEnvironment.today)
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.readingMarkRequestCount(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == 1
        }

        await environment.runner.failReadingMark(
            .opened,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        await environment.runner.holdDashboard(date: TestEnvironment.today)
        await environment.runner.releaseReadingMark(source: "zgjsb", date: TestEnvironment.today)
        await waitUntil { await environment.runner.dashboardRequestCount() == 2 }

        // This reconciliation already captured unread and is now suspended.
        // The manual write's own dashboard refresh fails, so only the write
        // establishes the newer confirmed state.
        await environment.runner.setDashboardFailure(true)
        viewModel.markSelectedIssue(.completed)
        await waitUntilIdle(viewModel)
        #expect(viewModel.selectedReadingStatus == .completed)

        await environment.runner.releaseDashboard(date: TestEnvironment.today)
        for _ in 0..<20 { await Task.yield() }

        #expect(viewModel.selectedReadingStatus == .completed)
        #expect(await environment.runner.storedReadingStatus(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == .completed)
    }

    @Test("无阅读字段的收件箱与仪表盘失败不会擦除已确认状态")
    func compactInboxAndDashboardFailurePreserveConfirmedStatus() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .unread,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        let viewModel = environment.makeViewModel()
        viewModel.refresh()
        await waitUntil {
            await environment.runner.storedReadingStatus(
                source: "zgjsb",
                date: TestEnvironment.today
            ) == .opened
        }
        #expect(viewModel.selectedReadingStatus == .opened)

        await environment.runner.setInboxOmitsReadingStatus(true)
        await environment.runner.setDashboardFailure(true)
        viewModel.refresh()
        await waitUntilSettled(viewModel)
        #expect(viewModel.selectedReadingStatus == .opened)

        await environment.runner.failReadingMark(
            .completed,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        viewModel.markSelectedIssue(.completed)
        await waitUntilIdle(viewModel)

        #expect(viewModel.selectedReadingStatus == .opened)
        #expect(viewModel.presentedError != nil)
    }

    @Test("已打开的报纸不会重复写入 opened 状态")
    func openedIssueDoesNotRepeatReadingMark() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        await environment.runner.setReadingStatus(
            .opened,
            source: "zgjsb",
            date: TestEnvironment.today
        )
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntilSettled(viewModel)
        viewModel.selectIssue("zgjsb-\(TestEnvironment.today)")
        await waitUntilSettled(viewModel)

        #expect(await environment.runner.readingMarkRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == 0)
    }

    @Test("返回已经读取的报纸命中内存缓存")
    func returningToIssueUsesMemoryCache() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)
        #expect(await environment.runner.issueRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == 1)

        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntilSettled(viewModel)
        viewModel.selectIssue("zgjsb-\(TestEnvironment.today)")
        await waitUntilSettled(viewModel)

        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(await environment.runner.issueRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == 1)
    }

    @Test("保存后返回同一报纸会重拉后端派生状态而不复用旧告警")
    func savedIssueIsReloadedInsteadOfUsingStaleCache() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        viewModel.updateSummary("已补全摘要")
        viewModel.saveDraft()
        await waitUntilIdle(viewModel)

        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        await waitUntilSettled(viewModel)
        viewModel.selectIssue("zgjsb-\(TestEnvironment.today)")
        await waitUntilSettled(viewModel)

        #expect(await environment.runner.issueRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == 2)
    }

    @Test("保存响应失败后返回同一报纸也不会复用可能过期的缓存")
    func failedSaveInvalidatesIssueCacheBeforeRequest() async throws {
        let environment = try TestEnvironment()
        defer { environment.cleanup() }
        let viewModel = environment.makeViewModel()
        await loadInitialIssue(in: viewModel)

        viewModel.updateSummary("服务端可能已保存但客户端收到失败")
        await environment.runner.setDraftSaveFailure(true)
        viewModel.saveDraft()
        await waitUntilIdle(viewModel)
        #expect(viewModel.presentedError != nil)

        viewModel.selectIssue("rmrb-\(TestEnvironment.otherDate)")
        #expect(viewModel.showingDiscardChangesConfirmation)
        viewModel.confirmDiscardAndContinue()
        await waitUntilSettled(viewModel)
        viewModel.selectIssue("zgjsb-\(TestEnvironment.today)")
        await waitUntilSettled(viewModel)

        #expect(await environment.runner.issueRequestCount(
            source: "zgjsb",
            date: TestEnvironment.today
        ) == 2)
    }

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

        await waitUntilSettled(viewModel)
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
        await waitUntilSettled(viewModel)

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
        await waitUntilSettled(viewModel)

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
        await waitUntilSettled(viewModel)
        #expect(viewModel.selectedIssueID == "zgjsb-\(TestEnvironment.today)")
        #expect(viewModel.issueDetail?.sourceID == "zgjsb")
        #expect(viewModel.editorState?.draft.id == "zgjsb-page-1")
    }

    private func waitUntilIdle(_ viewModel: ReadingDeskViewModel) async {
        for _ in 0..<300 {
            if !viewModel.isBusy && !viewModel.isFetchingDaily { return }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        Issue.record("等待 ViewModel 操作结束超时")
    }

    private func waitUntilSettled(_ viewModel: ReadingDeskViewModel) async {
        await waitUntil {
            !viewModel.isBusy && !viewModel.isFetchingDaily && !viewModel.isIssueLoading
        }
    }

    private func waitUntil(_ condition: @escaping @MainActor () async -> Bool) async {
        for _ in 0..<1_000 {
            if await condition() { return }
            await Task.yield()
        }
        Issue.record("等待异步状态变化超时")
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
        let workbenchScripts = repository
            .appendingPathComponent("skills/newspaper-reader/scripts", isDirectory: true)
        let fetchScripts = repository
            .appendingPathComponent("skills/newspaper-fetch/scripts", isDirectory: true)
        try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: workbenchScripts, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: fetchScripts, withIntermediateDirectories: true)
        try Data("#!/usr/bin/env python3\n".utf8)
            .write(to: scripts.appendingPathComponent("readdaily.py"))
        try Data("#!/usr/bin/env python3\n".utf8)
            .write(to: workbenchScripts.appendingPathComponent("workbench_api.py"))
        try Data("#!/usr/bin/env python3\n".utf8)
            .write(to: fetchScripts.appendingPathComponent("fetch.py"))

        suiteName = "ReadDailyNavigationTests.\(UUID().uuidString)"
        defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.set(repository.path, forKey: "readingDesk.repositoryPath")
        defaults.set(root.appendingPathComponent("archive").path, forKey: "readingDesk.archivePath")
        defaults.set(root.appendingPathComponent("vault").path, forKey: "readingDesk.vaultPath")
        settings = AppSettings(defaults: defaults)
        runner = NavigationStateRunner(today: Self.today, otherDate: Self.otherDate)
    }

    @MainActor
    func makeViewModel(
        issuePayloadMapper: @escaping IssuePayloadMappingExecutor.Transform = { value in
            try WorkbenchPayloadMapper().issue(from: value)
        }
    ) -> ReadingDeskViewModel {
        ReadingDeskViewModel(
            settings: settings,
            issuePayloadMapper: issuePayloadMapper
        ) { [runner] configuration in
            ReadDailyClient(configuration: configuration, runner: runner)
        }
    }

    func cleanup() {
        defaults.removePersistentDomain(forName: suiteName)
        try? FileManager.default.removeItem(at: root)
    }
}

private final class BlockingIssuePayloadMapper: @unchecked Sendable {
    private let condition = NSCondition()
    private var shouldBlockNextCall = false
    private var blockedCallStarted = false
    private var blockedCallReleased = false
    private var _blockedCallRanOnMainThread = false

    var blockedCallRanOnMainThread: Bool {
        condition.lock()
        defer { condition.unlock() }
        return _blockedCallRanOnMainThread
    }

    func blockNextCall() {
        condition.lock()
        shouldBlockNextCall = true
        blockedCallStarted = false
        blockedCallReleased = false
        _blockedCallRanOnMainThread = false
        condition.unlock()
    }

    func map(_ value: JSONValue?) throws -> IssueDetail {
        condition.lock()
        let shouldBlock = shouldBlockNextCall
        if shouldBlock {
            shouldBlockNextCall = false
            blockedCallStarted = true
            _blockedCallRanOnMainThread = Thread.isMainThread
            condition.broadcast()
            while !blockedCallReleased {
                condition.wait()
            }
        }
        condition.unlock()
        return try WorkbenchPayloadMapper().issue(from: value)
    }

    func waitUntilBlockedCallStarts() -> Bool {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(2)
        while !blockedCallStarted {
            guard condition.wait(until: deadline) else { return blockedCallStarted }
        }
        return true
    }

    func releaseBlockedCall() {
        condition.lock()
        blockedCallReleased = true
        condition.broadcast()
        condition.unlock()
    }
}

private actor NavigationStateRunner: ProcessRunning {
    private let today: String
    private let otherDate: String
    private var failingIssues: Set<String> = []
    private var inboxFails = false
    private var inboxOmitsReadingStatus = false
    private var heldInbox = false
    private var inboxWaiters: [CheckedContinuation<Void, Never>] = []
    private var inboxRequests = 0
    private var dashboardFails = false
    private var importFails = false
    private var draftSaveFails = false
    private var readingStatuses: [String: ReadingCompletionStatus] = [:]
    private var readingRevisions: [String: Int] = [:]
    private var heldIssues: Set<String> = []
    private var issueWaiters: [String: [CheckedContinuation<Void, Never>]] = [:]
    private var heldReadingMarks: Set<String> = []
    private var failingReadingMarks: Set<String> = []
    private var ambiguousReadingMarks: Set<String> = []
    private var readingMarkWaiters: [String: [CheckedContinuation<Void, Never>]] = [:]
    private var heldReadingMarkResponses: Set<String> = []
    private var readingMarkResponseWaiters: [String: [CheckedContinuation<Void, Never>]] = [:]
    private var heldDashboards: Set<String> = []
    private var dashboardWaiters: [String: [CheckedContinuation<Void, Never>]] = [:]
    private var issueRequests: [String: Int] = [:]
    private var readingMarkRequests: [String: Int] = [:]
    private var dashboardRequests = 0
    private var dashboardStatusOverrides: [String: ReadingCompletionStatus] = [:]
    private var heldFetches: Set<String> = []
    private var failingFetches: Set<String> = []
    private var fetchWaiters: [String: [CheckedContinuation<Void, Never>]] = [:]
    private var fetchRequests: [String: Int] = [:]

    init(today: String, otherDate: String) {
        self.today = today
        self.otherDate = otherDate
        readingStatuses["kjrb|\(today)"] = .unread
    }

    func failIssue(source: String, date: String) {
        failingIssues.insert("\(source)|\(date)")
    }

    func setInboxFailure(_ value: Bool) {
        inboxFails = value
    }

    func setInboxOmitsReadingStatus(_ value: Bool) {
        inboxOmitsReadingStatus = value
    }

    func setDashboardFailure(_ value: Bool) {
        dashboardFails = value
    }

    func setImportFailure(_ value: Bool) {
        importFails = value
    }

    func setDraftSaveFailure(_ value: Bool) {
        draftSaveFails = value
    }

    func setReadingStatus(_ status: ReadingCompletionStatus, source: String, date: String) {
        let requestKey = key(source: source, date: date)
        readingStatuses[requestKey] = status
        readingRevisions[requestKey, default: 0] += 1
    }

    func setDashboardStatus(_ status: ReadingCompletionStatus, source: String, date: String) {
        dashboardStatusOverrides[key(source: source, date: date)] = status
    }

    func holdIssue(source: String, date: String) {
        heldIssues.insert(key(source: source, date: date))
    }

    func holdInbox() {
        heldInbox = true
    }

    func releaseInbox() {
        heldInbox = false
        let waiters = inboxWaiters
        inboxWaiters.removeAll()
        waiters.forEach { $0.resume() }
    }

    func releaseIssue(source: String, date: String) {
        let requestKey = key(source: source, date: date)
        heldIssues.remove(requestKey)
        issueWaiters.removeValue(forKey: requestKey)?.forEach { $0.resume() }
    }

    func holdReadingMark(source: String, date: String) {
        heldReadingMarks.insert(key(source: source, date: date))
    }

    func releaseReadingMark(source: String, date: String) {
        let requestKey = key(source: source, date: date)
        heldReadingMarks.remove(requestKey)
        readingMarkWaiters.removeValue(forKey: requestKey)?.forEach { $0.resume() }
    }

    func holdReadingMarkResponseAfterCommit(
        _ status: ReadingCompletionStatus,
        source: String,
        date: String
    ) {
        heldReadingMarkResponses.insert(readingMarkKey(status, source: source, date: date))
    }

    func releaseReadingMarkResponseAfterCommit(
        _ status: ReadingCompletionStatus,
        source: String,
        date: String
    ) {
        let responseKey = readingMarkKey(status, source: source, date: date)
        heldReadingMarkResponses.remove(responseKey)
        readingMarkResponseWaiters.removeValue(forKey: responseKey)?.forEach { $0.resume() }
    }

    func failReadingMark(
        _ status: ReadingCompletionStatus,
        source: String,
        date: String
    ) {
        failingReadingMarks.insert(readingMarkKey(status, source: source, date: date))
    }

    func failReadingMarkAfterCommit(
        _ status: ReadingCompletionStatus,
        source: String,
        date: String
    ) {
        ambiguousReadingMarks.insert(readingMarkKey(status, source: source, date: date))
    }

    func holdDashboard(date: String) {
        heldDashboards.insert(date)
    }

    func releaseDashboard(date: String) {
        heldDashboards.remove(date)
        dashboardWaiters.removeValue(forKey: date)?.forEach { $0.resume() }
    }

    func issueRequestCount(source: String, date: String) -> Int {
        issueRequests[key(source: source, date: date), default: 0]
    }

    func readingMarkRequestCount(source: String, date: String) -> Int {
        readingMarkRequests[key(source: source, date: date), default: 0]
    }

    func dashboardRequestCount() -> Int { dashboardRequests }

    func inboxRequestCount() -> Int { inboxRequests }

    func holdFetch(date: String) {
        heldFetches.insert(date)
    }

    func failFetch(date: String) {
        failingFetches.insert(date)
    }

    func releaseFetch(date: String) {
        heldFetches.remove(date)
        fetchWaiters.removeValue(forKey: date)?.forEach { $0.resume() }
    }

    func fetchRequestCount(date: String) -> Int {
        fetchRequests[date, default: 0]
    }

    func storedReadingStatus(source: String, date: String) -> ReadingCompletionStatus {
        readingStatus(source: source, date: date)
    }

    func run(_ request: ProcessRequest) async throws -> ProcessResult {
        let arguments = request.arguments
        if arguments.first?.hasSuffix("/skills/newspaper-fetch/scripts/fetch.py") == true {
            let date = value(after: "--date", in: arguments) ?? today
            fetchRequests[date, default: 0] += 1
            if failingFetches.contains(date) {
                return failure("抓取失败")
            }
            if heldFetches.contains(date) {
                await withCheckedContinuation { continuation in
                    fetchWaiters[date, default: []].append(continuation)
                }
            }
            try Task.checkCancellation()
            return ProcessResult(
                terminationStatus: 0,
                standardOutput: Data("fetch complete".utf8),
                standardError: Data()
            )
        }
        if arguments.contains("inbox") {
            inboxRequests += 1
            let payload = inboxPayload
            if heldInbox {
                await withCheckedContinuation { continuation in
                    inboxWaiters.append(continuation)
                }
            }
            if inboxFails { return failure("收件箱加载失败") }
            return success(payload)
        }
        if arguments.contains("daily-dashboard") {
            dashboardRequests += 1
            if dashboardFails { return failure("仪表盘加载失败") }
            let date = value(after: "--date", in: arguments) ?? today
            let payload = dashboardPayload(date: date)
            if heldDashboards.contains(date) {
                await withCheckedContinuation { continuation in
                    dashboardWaiters[date, default: []].append(continuation)
                }
            }
            return success(payload)
        }
        if arguments.contains("issue") {
            let source = value(after: "--source", in: arguments) ?? ""
            let date = value(after: "--date", in: arguments) ?? ""
            let requestKey = key(source: source, date: date)
            issueRequests[requestKey, default: 0] += 1
            if heldIssues.contains(requestKey) {
                await withCheckedContinuation { continuation in
                    issueWaiters[requestKey, default: []].append(continuation)
                }
            }
            if failingIssues.contains(requestKey) {
                return failure("期次加载失败")
            }
            return success(issuePayload(source: source, date: date))
        }
        if arguments.contains("reading-mark") {
            let source = value(after: "--source", in: arguments) ?? ""
            let date = value(after: "--date", in: arguments) ?? ""
            let status = ReadingCompletionStatus(
                rawValue: value(after: "--status", in: arguments) ?? "unread"
            ) ?? .unread
            let expectedRevision = value(after: "--expected-reading-revision", in: arguments)
                .flatMap(Int.init)
            let requestKey = key(source: source, date: date)
            readingMarkRequests[requestKey, default: 0] += 1
            if status == .opened, heldReadingMarks.contains(requestKey) {
                await withCheckedContinuation { continuation in
                    readingMarkWaiters[requestKey, default: []].append(continuation)
                }
            }
            if failingReadingMarks.contains(readingMarkKey(status, source: source, date: date)) {
                return failure("阅读状态写入失败")
            }
            try Task.checkCancellation()
            let currentRevision = readingRevisions[requestKey, default: 0]
            if status == .opened,
               let expectedRevision,
               expectedRevision != currentRevision {
                let currentStatus = readingStatus(source: source, date: date)
                return success(
                    "{\"reading_status\":\"\(currentStatus.rawValue)\",\"reading_revision\":\(currentRevision),\"activity_conflict\":true}"
                )
            }
            readingStatuses[requestKey] = status
            let nextRevision = currentRevision + 1
            readingRevisions[requestKey] = nextRevision
            let responseKey = readingMarkKey(status, source: source, date: date)
            if heldReadingMarkResponses.contains(responseKey) {
                await withCheckedContinuation { continuation in
                    readingMarkResponseWaiters[responseKey, default: []].append(continuation)
                }
            }
            if ambiguousReadingMarks.contains(readingMarkKey(status, source: source, date: date)) {
                return failure("阅读状态已经提交，但响应传输失败")
            }
            return success(
                "{\"reading_status\":\"\(status.rawValue)\",\"reading_revision\":\(nextRevision)}"
            )
        }
        if arguments.contains("draft-save") {
            if draftSaveFails { return failure("草稿可能已保存，但响应失败") }
            return success("{}")
        }
        if arguments.contains("import-file") {
            if importFails { return failure("导入失败") }
            return success(#"{"source":"zgjsb","date":"\#(today)"}"#)
        }
        return success("{}")
    }

    private var inboxPayload: String {
        let constructionStatus = readingStatus(source: "zgjsb", date: today)
        let scienceStatus = readingStatus(source: "kjrb", date: today)
        let peopleStatus = readingStatus(source: "rmrb", date: otherDate)
        let constructionReading = inboxOmitsReadingStatus
            ? ""
            : ",\"reading_status\":\"\(constructionStatus.rawValue)\""
        let peopleReading = inboxOmitsReadingStatus
            ? ""
            : ",\"reading_status\":\"\(peopleStatus.rawValue)\""
        return """
        {"issues":[
          {"id":"zgjsb-\(today)","source":"zgjsb","source_name":"中国建设报","date":"\(today)","stage":"needs_review"\(constructionReading),"page_count":2},
          {"id":"kjrb-\(today)","source":"kjrb","source_name":"科技日报","date":"\(today)","stage":"needs_review","reading_status":"\(scienceStatus.rawValue)","page_count":1},
          {"id":"rmrb-\(otherDate)","source":"rmrb","source_name":"人民日报","date":"\(otherDate)","stage":"needs_review"\(peopleReading),"page_count":1}
        ]}
        """
    }

    private func dashboardPayload(date: String) -> String {
        let sources = date == otherDate
            ? [("rmrb", "人民日报")]
            : [("zgjsb", "中国建设报"), ("kjrb", "科技日报")]
        let newspapers = sources.map { source, sourceName in
            let requestKey = key(source: source, date: date)
            let status = dashboardStatusOverrides[requestKey]
                ?? readingStatus(source: source, date: date)
            let revision = readingRevisions[requestKey, default: 0]
            return """
            {"id":"\(source)-\(date)","source":"\(source)","source_name":"\(sourceName)","date":"\(date)","available":true,"stage":"needs_review","reading_status":"\(status.rawValue)","reading_revision":\(revision)}
            """
        }.joined(separator: ",")
        return """
        {"date":"\(date)","available_dates":["\(today)","\(otherDate)"],"newspapers":[\(newspapers)]}
        """
    }

    private func readingStatus(source: String, date: String) -> ReadingCompletionStatus {
        readingStatuses[key(source: source, date: date)] ?? .completed
    }

    private func key(source: String, date: String) -> String { "\(source)|\(date)" }

    private func readingMarkKey(
        _ status: ReadingCompletionStatus,
        source: String,
        date: String
    ) -> String {
        "\(key(source: source, date: date))|\(status.rawValue)"
    }

    private func issuePayload(source: String, date: String) -> String {
        let sourceName: String
        switch source {
        case "rmrb": sourceName = "人民日报"
        case "kjrb": sourceName = "科技日报"
        default: sourceName = "中国建设报"
        }
        let prefix = source
        let unitCount = source == "zgjsb" ? 2 : 1
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
