import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("复核编辑状态")
struct DraftEditorStateTests {
    @Test("主题只使用知识库已有的七个完整中文名称")
    func canonicalTopicsUseOnlyTheSevenExistingChineseKnowledgeCardNames() {
        #expect(ReadingTopic.allCases.map(\.rawValue) == [
            "建设投资与房地产",
            "城市更新与城市治理",
            "智能建造与智能制造",
            "产业创新与建筑业转型",
            "工程咨询、招投标与供应链",
            "住房民生与社区服务",
            "建设安全与城市韧性",
        ])
    }

    @Test("保存成功前持续标记未保存修改")
    func editorTracksChangesAndClearsDirtyStateOnlyAfterSaveSucceeds() {
        let draft = ArticleDraft(id: "a1", title: "城市更新观察", summary: "旧摘要")
        var editor = DraftEditorState(draft: draft)
        #expect(!editor.hasUnsavedChanges)

        editor.draft.summary = "新摘要"
        #expect(editor.hasUnsavedChanges)

        editor.markSaved()
        #expect(!editor.hasUnsavedChanges)
        #expect(editor.savedDraft.summary == "新摘要")
    }

    @Test("重要性始终限制在一至五")
    func importanceIsAlwaysClampedToOneThroughFive() {
        var editor = DraftEditorState(draft: ArticleDraft(id: "a1", title: "测试"))

        editor.setImportance(9)
        #expect(editor.draft.importance == 5)

        editor.setImportance(-3)
        #expect(editor.draft.importance == 1)
    }

    @Test("规范化草稿清理人工编辑字段空白")
    func normalizedDraftTrimsHumanEditableFactFields() {
        var draft = ArticleDraft(id: "a1", title: "  标题  ", summary: "  摘要  ")
        draft.facts = [
            FactFields(subject: "  住建部 ", action: " 发布 ", object: " 标准 ", value: " 12 ", unit: " 项 ", time: " 9月 ", source: " 第1版 "),
            FactFields(subject: "  企业 ", action: " 投资 ", object: " 项目 ", source: " 第1版 "),
        ]

        let normalized = DraftEditorState(draft: draft).normalizedDraft()

        #expect(normalized.title == "标题")
        #expect(normalized.summary == "摘要")
        #expect(normalized.facts.count == 2)
        #expect(normalized.facts[0].subject == "住建部")
        #expect(normalized.facts[1].subject == "企业")
    }

    @Test("整期草稿编码符合 Python API 的 source/date/units schema")
    func issueDraftEncodingMatchesBackendSchema() throws {
        let unit = DraftUnit(
            id: "zgjsb_20260901_01",
            title: "城市更新标题",
            summary: "摘要",
            topics: [.urbanRenewal],
            facts: [
                FactFields(subject: "住建部", action: "发布", object: "标准", source: "第1版"),
                FactFields(subject: "企业", action: "投资", object: "项目", source: "第1版"),
            ],
            importance: 4
        )
        let request = DraftSaveRequest(source: "zgjsb", date: "2026-09-01", units: [unit])

        let object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        let units = try #require(object["units"] as? [[String: Any]])

        #expect(object["source"] as? String == "zgjsb")
        #expect(object["source_id"] == nil)
        #expect(units.first?["id"] as? String == "zgjsb_20260901_01")
        #expect(units.first?["title"] as? String == "城市更新标题")
        #expect((units.first?["facts"] as? [[String: Any]])?.count == 2)
        #expect(units.first?["importance"] as? Int == 4)
    }

    @Test("草稿 Codable 往返保留可编辑标题与每一条事实")
    func draftRoundTripPreservesTitleAndEveryFact() throws {
        let original = ArticleDraft(
            id: "a1",
            title: "保留这个中文标题",
            summary: "摘要",
            facts: [
                FactFields(subject: "甲", action: "建设", object: "项目一", source: "第1版"),
                FactFields(subject: "乙", action: "投资", object: "项目二", value: "20", unit: "亿元", source: "第1版"),
            ]
        )

        let decoded = try JSONDecoder().decode(ArticleDraft.self, from: JSONEncoder().encode(original))

        #expect(decoded.title == original.title)
        #expect(decoded.facts == original.facts)
    }
}
