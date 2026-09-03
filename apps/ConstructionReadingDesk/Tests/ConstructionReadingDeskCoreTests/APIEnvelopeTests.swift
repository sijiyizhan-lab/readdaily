import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("API 信封解码")
struct APIEnvelopeTests {
    @Test("解码带版本号的成功响应并保留告警")
    func decodesVersionedSuccessEnvelopeAndKeepsWarnings() throws {
        let json = #"{"schema_version":1,"ok":true,"data":{"issues":[{"id":"zgjsb-2026-09-01","source_id":"zgjsb","source_name":"中国建设报","date":"2026-09-01","stage":"待复核","warning_count":2}]},"warnings":["第3版 OCR 置信度偏低"]}"#

        let envelope = try JSONDecoder().decode(APIEnvelope<InboxPayload>.self, from: Data(json.utf8))

        #expect(envelope.schemaVersion == 1)
        #expect(envelope.ok)
        #expect(envelope.warnings == ["第3版 OCR 置信度偏低"])
        #expect(envelope.data?.issues.first?.sourceName == "中国建设报")
        #expect(envelope.data?.issues.first?.warningCount == 2)
    }

    @Test("兼容可选字段与未来新增字段")
    func toleratesOptionalAndUnknownBackendFields() throws {
        let json = #"{"schema_version":1,"ok":true,"data":{"issues":[{"source":"zgjsb","date":"2026-09-01","future_field":{"enabled":true}}]},"warnings":[]}"#

        let envelope = try JSONDecoder().decode(APIEnvelope<InboxPayload>.self, from: Data(json.utf8))

        #expect(envelope.data?.issues.first?.sourceID == "zgjsb")
        #expect(envelope.data?.issues.first?.stableID == "zgjsb-2026-09-01")
        #expect(envelope.data?.issues.first?.issueNumber == nil)
    }

    @Test("失败响应无需 data 也可解码")
    func decodesBackendFailureWithoutData() throws {
        let json = #"{"schema_version":1,"ok":false,"error":{"code":"not_found","message":"未找到报纸","recovery":"请先导入 PDF"},"warnings":[]}"#

        let envelope = try JSONDecoder().decode(APIEnvelope<InboxPayload>.self, from: Data(json.utf8))

        #expect(!envelope.ok)
        #expect(envelope.data == nil)
        #expect(envelope.error?.message == "未找到报纸")
        #expect(envelope.error?.recovery == "请先导入 PDF")
    }

    @Test("后端未提供 id 时不同来源和日期仍有唯一列表身份")
    func derivesUniqueListIdentityWithoutBackendID() {
        let first = IssueSummary(sourceID: "zgjsb", date: "2026-09-02")
        let second = IssueSummary(sourceID: "rmrb", date: "2026-09-02")

        #expect(first.id != second.id)
        #expect(first.id == first.stableID)
    }
}
