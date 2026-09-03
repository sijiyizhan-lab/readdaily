public struct UnsavedChangesGate<Action: Equatable & Sendable>: Equatable, Sendable {
    public private(set) var pendingAction: Action?

    public init() {}

    public var requiresConfirmation: Bool { pendingAction != nil }

    @discardableResult
    public mutating func request(_ action: Action, hasUnsavedChanges: Bool) -> Bool {
        guard hasUnsavedChanges else {
            pendingAction = nil
            return true
        }
        pendingAction = action
        return false
    }

    public mutating func confirmDiscard() -> Action? {
        defer { pendingAction = nil }
        return pendingAction
    }

    public mutating func cancel() {
        pendingAction = nil
    }
}
