import Testing
@testable import ConstructionReadingDeskCore

@Suite("未保存编辑离开门禁")
struct UnsavedChangesGateTests {
    private enum Action: Equatable, Sendable {
        case refresh
        case selectIssue(String)
    }

    @Test("存在未保存编辑时暂存动作并等待明确确认")
    func blocksNavigationUntilConfirmed() {
        var gate = UnsavedChangesGate<Action>()

        let mayProceed = gate.request(.selectIssue("zgjsb-2026-09-01"), hasUnsavedChanges: true)

        #expect(!mayProceed)
        #expect(gate.requiresConfirmation)
        #expect(gate.confirmDiscard() == .selectIssue("zgjsb-2026-09-01"))
        #expect(!gate.requiresConfirmation)
    }

    @Test("取消离开会清除待执行动作")
    func cancellationKeepsCurrentWorkspace() {
        var gate = UnsavedChangesGate<Action>()
        _ = gate.request(.refresh, hasUnsavedChanges: true)

        gate.cancel()

        #expect(!gate.requiresConfirmation)
        #expect(gate.confirmDiscard() == nil)
    }

    @Test("没有未保存编辑时允许立即执行")
    func cleanWorkspaceDoesNotPrompt() {
        var gate = UnsavedChangesGate<Action>()

        let mayProceed = gate.request(.refresh, hasUnsavedChanges: false)
        #expect(mayProceed)
        #expect(!gate.requiresConfirmation)
    }
}
