import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("拖入 PDF 临时文件")
struct TemporaryImportFileTests {
    @Test("导入失败保留文件供重试，成功后才删除")
    func retainsOnFailureAndDeletesOnSuccess() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("readdaily-import-test-\(UUID().uuidString).pdf")
        try Data("%PDF-test".utf8).write(to: url)
        let lease = TemporaryImportFile(url: url, removesAfterSuccessfulImport: true)

        lease.finish(importSucceeded: false)
        #expect(FileManager.default.fileExists(atPath: url.path))

        lease.finish(importSucceeded: true)
        #expect(!FileManager.default.fileExists(atPath: url.path))
    }
}
