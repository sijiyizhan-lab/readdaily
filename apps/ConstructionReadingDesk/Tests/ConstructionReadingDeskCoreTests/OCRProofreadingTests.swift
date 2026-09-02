import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("OCR 显式校对")
struct OCRProofreadingTests {
    @Test("校对文本和疑点 Codable 往返不覆盖 OCR 原文")
    func draftRoundTripKeepsOriginalAndReviewedTextSeparate() throws {
        let draft = ArticleDraft(
            id: "page-1",
            title: "头版",
            ocrText: "原始 第一行\n原始 第二行",
            ocrBlocks: [
                OCRContentBlock(kind: "article", title: "原始标题", text: "原始 第一行\n原始 第二行"),
            ],
            proofreadText: "校对 第一行\n校对 第二行",
            ocrReviewStatus: .edited,
            ocrSuspicions: ["第二段数字待核对"]
        )

        let data = try JSONEncoder().encode(draft)
        let decoded = try JSONDecoder().decode(ArticleDraft.self, from: data)

        #expect(decoded.ocrText == "原始 第一行\n原始 第二行")
        #expect(decoded.ocrBlocks == [
            OCRContentBlock(kind: "article", title: "原始标题", text: "原始 第一行\n原始 第二行"),
        ])
        #expect(decoded.proofreadText == "校对 第一行\n校对 第二行")
        #expect(decoded.ocrReviewStatus == .edited)
        #expect(decoded.ocrSuspicions == ["第二段数字待核对"])
    }

    @Test("草稿规范化不会静默裁剪原文或校对文本")
    func normalizationPreservesOCRVerbatim() {
        let draft = ArticleDraft(
            id: "page-1",
            title: " 标题 ",
            ocrText: "  原文开头\n\n原文结尾  ",
            proofreadText: "  校对开头\n\n校对结尾  ",
            ocrReviewStatus: .confirmed
        )

        let normalized = DraftEditorState(draft: draft).normalizedDraft()

        #expect(normalized.title == "标题")
        #expect(normalized.ocrText == "  原文开头\n\n原文结尾  ")
        #expect(normalized.proofreadText == "  校对开头\n\n校对结尾  ")
    }

    @Test("编码使用后端最终校对字段且兼容旧字段解码")
    func usesCanonicalProofreadKeysAndDecodesLegacyAliases() throws {
        let draft = ArticleDraft(
            id: "page-1",
            title: "头版",
            ocrText: "原始",
            proofreadText: "校对",
            ocrReviewStatus: .confirmed,
            ocrSuspicions: ["疑点"]
        )
        let object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(draft)) as? [String: Any])
        #expect(object["corrected_ocr_text"] as? String == "校对")
        #expect(object["proofread_status"] as? String == "confirmed")
        #expect((object["ocr_suspicions"] as? [String]) == ["疑点"])
        #expect(object["proofread_text"] == nil)

        let legacy = Data(#"{"id":"p","title":"旧数据","ocr_text":"原始","proofread_text":"旧校对","ocr_review_status":"verified"}"#.utf8)
        let decoded = try JSONDecoder().decode(ArticleDraft.self, from: legacy)
        #expect(decoded.proofreadText == "旧校对")
        #expect(decoded.ocrReviewStatus == .confirmed)
    }

    @Test("恢复原文时以 null 明确清除旧人工校订字段")
    func restoringOriginalExplicitlyClearsCorrectedOCRText() throws {
        let unit = DraftUnit(
            id: "page-1",
            title: "头版",
            ocrText: "原始 OCR",
            proofreadText: "原始 OCR",
            ocrReviewStatus: .unreviewed,
            summary: "摘要",
            topics: [.urbanRenewal],
            facts: [FactFields(subject: "主体", action: "行动", object: "对象", source: "来源")],
            importance: 3
        )

        let object = try #require(JSONSerialization.jsonObject(with: JSONEncoder().encode(unit)) as? [String: Any])
        #expect(object.keys.contains("corrected_ocr_text"))
        #expect(object["corrected_ocr_text"] is NSNull)
        #expect(object["ocr_text"] as? String == "原始 OCR")
    }

    @Test("OCR 版面解析保留空行与逐行文字")
    func layoutPreservesParagraphAndLineStructure() {
        let text = "第一段第一行\n第一段第二行\n\n第二段\n"

        let layout = OCRDocumentLayout(text: text)

        #expect(layout.paragraphs.count == 2)
        #expect(layout.paragraphs[0].lines == ["第一段第一行", "第一段第二行"])
        #expect(layout.paragraphs[1].lines == ["第二段"])
        #expect(layout.verbatimText == text)
    }
}
