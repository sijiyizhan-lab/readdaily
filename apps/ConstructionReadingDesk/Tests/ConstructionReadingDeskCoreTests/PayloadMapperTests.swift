import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("工作台载荷映射")
struct PayloadMapperTests {
    @Test("收件箱映射真实 API 状态、期号和告警")
    func mapsInboxSchema() throws {
        let data = try payload(#"{"date":null,"issues":[{"source":"zgjsb","source_name":"中国建设报","date":"2026-09-01","issue_no":9168,"status":"needs_review","coverage":{"editions":8},"warnings":["第3版正文为空"]}],"stats":{"issue_count":1}}"#)

        let issues = try WorkbenchPayloadMapper().inbox(from: data)

        #expect(issues.count == 1)
        #expect(issues[0].stableID == "zgjsb-2026-09-01")
        #expect(issues[0].issueNumber == "9168")
        #expect(issues[0].pageCount == 8)
        #expect(issues[0].warningCount == 1)
    }

    @Test("客户端对旧后端响应再次过滤非建设报期次")
    func filtersInboxToConstructionSource() throws {
        let data = try payload(#"{"issues":[{"source":"zgjsb","source_name":"中国建设报","date":"2026-09-02"},{"source":"rmrb","source_name":"人民日报","date":"2026-09-02"}]}"#)

        let issues = try WorkbenchPayloadMapper().inbox(from: data, sourceID: "zgjsb")

        #expect(issues.map(\.sourceID) == ["zgjsb"])
    }

    @Test("期次按版面单元映射原图、OCR、摘要、主题和事实")
    func mapsIssueUnitsIntoReviewEditions() throws {
        let data = try payload(#"{"source":"zgjsb","source_name":"中国建设报","date":"2026-09-01","issue_no":"9168","pdf_path":"/archive/source.pdf","units":[{"id":"zgjsb_20260901_01","edition_no":1,"edition_name":"要闻","title":"1版 要闻","page_image":"/archive/page-01.jpg","text":"OCR 正文","summary":"摘要","topics":["城市更新与城市治理"],"facts":[{"subject":"住建部","action":"发布","object":"标准","value":"12","unit":"项","time":"9月","source":"第1版"},{"subject":"企业","action":"投资","object":"项目","value":"20","unit":"亿元","time":"9月","source":"第1版"}],"importance":5,"warnings":[]}],"warnings":[]}"#)

        let issue = try WorkbenchPayloadMapper().issue(from: data)

        #expect(issue.editions.count == 1)
        #expect(issue.editions[0].pageNumber == 1)
        #expect(issue.editions[0].imagePath == "/archive/page-01.jpg")
        #expect(issue.editions[0].pdfPath == "/archive/source.pdf")
        #expect(issue.editions[0].articles[0].ocrText == "OCR 正文")
        #expect(issue.editions[0].articles[0].topics == [.urbanRenewal])
        #expect(issue.editions[0].articles[0].facts.count == 2)
        #expect(issue.editions[0].articles[0].facts[0].subject == "住建部")
        #expect(issue.editions[0].articles[0].facts[1].subject == "企业")
        #expect(issue.editions[0].articles[0].importance == 5)
    }

    @Test("发布预览识别新增和修改文件")
    func mapsPublishPlanChanges() throws {
        let data = try payload(#"{"plan_id":"abc123","warnings":[],"changes":[{"relative_path":"09-建设新闻与报纸摘要/日报/a.md","before_exists":false,"diff":"+新增"},{"relative_path":"09-建设新闻与报纸摘要/主题/b.md","before_exists":true,"diff":"-旧\n+新"}]}"#)

        let plan = try WorkbenchPayloadMapper().publishPlan(from: data)

        #expect(plan.id == "abc123")
        #expect(plan.changes.map(\.changeType) == ["新增", "修改"])
    }

    @Test("历史记录保留可回滚事务")
    func mapsHistoryTransactions() throws {
        let data = try payload(#"{"transactions":[{"transaction_id":"tx1","source":"zgjsb","date":"2026-09-01","status":"applied","created_at":"2026-09-02T12:00:00+08:00"}]}"#)

        let history = try WorkbenchPayloadMapper().history(from: data)

        #expect(history.first?.id == "tx1")
        #expect(history.first?.canRollback == true)
        #expect(history.first?.summary.contains("中国建设报") == true)
    }

    @Test("发布计划合并信封告警并去重")
    func mergesEnvelopeWarningsIntoPublishPreview() {
        let plan = PublishPlan(id: "p1", changes: [], warnings: ["第1版需复核"])

        let merged = plan.mergingWarnings(["第2版 OCR 偏低", "第1版需复核"])

        #expect(merged.warnings == ["第1版需复核", "第2版 OCR 偏低"])
    }

    private func payload(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }
}
