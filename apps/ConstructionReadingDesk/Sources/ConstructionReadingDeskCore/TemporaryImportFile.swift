import Foundation

public struct TemporaryImportFile: Sendable {
    public let url: URL
    public let removesAfterSuccessfulImport: Bool

    public init(url: URL, removesAfterSuccessfulImport: Bool) {
        self.url = url
        self.removesAfterSuccessfulImport = removesAfterSuccessfulImport
    }

    public func finish(importSucceeded: Bool) {
        guard importSucceeded, removesAfterSuccessfulImport else { return }
        try? FileManager.default.removeItem(at: url)
    }
}
