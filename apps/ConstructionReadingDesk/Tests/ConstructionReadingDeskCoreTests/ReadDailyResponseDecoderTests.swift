import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("进程响应边界")
struct ReadDailyResponseDecoderTests {
    @Test("只接受完整且纯净的版本化 JSON")
    func acceptsStrictVersionedJSON() throws {
        let result = ProcessResult(
            terminationStatus: 0,
            standardOutput: Data(#"{"schema_version":1,"ok":true,"data":{"count":2},"warnings":[]}"#.utf8),
            standardError: Data()
        )

        let envelope = try ReadDailyResponseDecoder().decode(result)

        #expect(envelope.ok)
        #expect(envelope.schemaVersion == 1)
        #expect(envelope.data?.objectValue?["count"]?.intValue == 2)
    }

    @Test("拒绝 JSON 前混入的普通日志")
    func rejectsLogsMixedIntoStandardOutput() {
        let output = "开始执行\n" + #"{"schema_version":1,"ok":true,"data":null,"warnings":[]}"#
        let result = ProcessResult(
            terminationStatus: 0,
            standardOutput: Data(output.utf8),
            standardError: Data()
        )

        #expect(throws: ReadDailyClientError.self) {
            try ReadDailyResponseDecoder().decode(result)
        }
    }

    @Test("非零退出保留 stderr 并转成中文错误")
    func turnsNonzeroExitIntoRecoverableChineseError() {
        let result = ProcessResult(
            terminationStatus: 2,
            standardOutput: Data(),
            standardError: Data("找不到输入文件".utf8)
        )

        do {
            _ = try ReadDailyResponseDecoder().decode(result)
            Issue.record("预期抛出错误")
        } catch let error as ReadDailyClientError {
            #expect(error.errorDescription?.contains("执行失败") == true)
            #expect(error.failureReason?.contains("找不到输入文件") == true)
            #expect(error.recoverySuggestion != nil)
        } catch {
            Issue.record("收到非预期错误：\(error)")
        }
    }

    @Test("非零退出但 stdout 是错误信封时优先显示后端说明")
    func backendEnvelopeTakesPriorityOverNonzeroExit() {
        let json = #"{"schema_version":1,"ok":false,"data":null,"warnings":[],"error":{"code":"validation_error","message":"整期复核尚未完成","recovery":"请补齐第2版事实字段"}}"#
        let result = ProcessResult(
            terminationStatus: 2,
            standardOutput: Data(json.utf8),
            standardError: Data("process exited 2".utf8)
        )

        do {
            _ = try ReadDailyResponseDecoder().decode(result)
            Issue.record("预期抛出后端错误")
        } catch let error as ReadDailyClientError {
            #expect(error == .backendRejected(message: "整期复核尚未完成", recovery: "请补齐第2版事实字段"))
            #expect(error.errorDescription == "整期复核尚未完成")
            #expect(error.recoverySuggestion == "请补齐第2版事实字段")
        } catch {
            Issue.record("收到非预期错误：\(error)")
        }
    }
}
