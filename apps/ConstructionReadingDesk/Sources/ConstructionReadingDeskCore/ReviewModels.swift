import Foundation

public enum ReadingTopic: String, Codable, CaseIterable, Identifiable, Sendable {
    case investment = "建设投资与房地产"
    case urbanRenewal = "城市更新与城市治理"
    case intelligentConstruction = "智能建造与智能制造"
    case industryInnovation = "产业创新与建筑业转型"
    case consultingAndSupplyChain = "工程咨询、招投标与供应链"
    case housingAndCommunity = "住房民生与社区服务"
    case safetyAndResilience = "建设安全与城市韧性"

    public var id: String { rawValue }
}

public struct FactFields: Codable, Equatable, Sendable {
    public var subject: String
    public var action: String
    public var object: String
    public var value: String
    public var unit: String
    public var time: String
    public var source: String

    public init(
        subject: String = "",
        action: String = "",
        object: String = "",
        value: String = "",
        unit: String = "",
        time: String = "",
        source: String = ""
    ) {
        self.subject = subject
        self.action = action
        self.object = object
        self.value = value
        self.unit = unit
        self.time = time
        self.source = source
    }

    fileprivate func trimmingWhitespace() -> FactFields {
        FactFields(
            subject: subject.trimmed,
            action: action.trimmed,
            object: object.trimmed,
            value: value.trimmed,
            unit: unit.trimmed,
            time: time.trimmed,
            source: source.trimmed
        )
    }

    public var isEmpty: Bool {
        [subject, action, object, value, unit, time, source]
            .allSatisfy { $0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
}

public struct OCRContentBlock: Codable, Equatable, Sendable {
    public let kind: String
    public let title: String?
    public let text: String

    public init(kind: String, title: String? = nil, text: String) {
        self.kind = kind
        self.title = title
        self.text = text
    }
}

public struct ArticleDraft: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var ocrText: String
    public var ocrBlocks: [OCRContentBlock]
    public var proofreadText: String
    public var ocrReviewStatus: OCRReviewStatus
    public var ocrSuspicions: [String]
    public var summary: String
    public var topics: Set<ReadingTopic>
    public var facts: [FactFields]
    public var importance: Int

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case ocrText = "ocr_text"
        case ocrBlocks = "ocr_blocks"
        case text
        case correctedOCRText = "corrected_ocr_text"
        case legacyProofreadText = "proofread_text"
        case proofreadStatus = "proofread_status"
        case legacyOCRReviewStatus = "ocr_review_status"
        case ocrSuspicions = "ocr_suspicions"
        case summary
        case topics
        case category
        case facts
        case importance
    }

    public init(
        id: String,
        title: String,
        ocrText: String = "",
        ocrBlocks: [OCRContentBlock] = [],
        proofreadText: String? = nil,
        ocrReviewStatus: OCRReviewStatus = .unreviewed,
        ocrSuspicions: [String] = [],
        summary: String = "",
        topics: Set<ReadingTopic> = [],
        facts: [FactFields] = [FactFields()],
        importance: Int = 3
    ) {
        self.id = id
        self.title = title
        self.ocrText = ocrText
        self.ocrBlocks = ocrBlocks.isEmpty ? OCRDocumentLayout.fallbackBlocks(from: ocrText) : ocrBlocks
        self.proofreadText = proofreadText ?? ocrText
        self.ocrReviewStatus = ocrReviewStatus
        self.ocrSuspicions = ocrSuspicions
        self.summary = summary
        self.topics = topics
        self.facts = facts
        self.importance = min(max(importance, 1), 5)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        title = try container.decodeIfPresent(String.self, forKey: .title) ?? "未命名内容"
        ocrText = try container.decodeIfPresent(String.self, forKey: .ocrText)
            ?? container.decodeIfPresent(String.self, forKey: .text)
            ?? ""
        let decodedBlocks = try container.decodeIfPresent([OCRContentBlock].self, forKey: .ocrBlocks) ?? []
        ocrBlocks = decodedBlocks.isEmpty ? OCRDocumentLayout.fallbackBlocks(from: ocrText) : decodedBlocks
        proofreadText = try container.decodeIfPresent(String.self, forKey: .correctedOCRText)
            ?? container.decodeIfPresent(String.self, forKey: .legacyProofreadText)
            ?? ocrText
        let rawReviewStatus = try container.decodeIfPresent(String.self, forKey: .proofreadStatus)
            ?? container.decodeIfPresent(String.self, forKey: .legacyOCRReviewStatus)
        ocrReviewStatus = OCRReviewStatus.fromBackend(rawReviewStatus)
        ocrSuspicions = try container.decodeIfPresent([String].self, forKey: .ocrSuspicions) ?? []
        summary = try container.decodeIfPresent(String.self, forKey: .summary) ?? ""
        if let topicSet = try? container.decode(Set<ReadingTopic>.self, forKey: .topics) {
            topics = topicSet
        } else if let topicSet = try? container.decode(Set<ReadingTopic>.self, forKey: .category) {
            topics = topicSet
        } else {
            topics = []
        }
        if let list = try? container.decode([FactFields].self, forKey: .facts) {
            facts = list
        } else if let single = try? container.decode(FactFields.self, forKey: .facts) {
            facts = [single]
        } else {
            facts = [FactFields()]
        }
        importance = min(max(try container.decodeIfPresent(Int.self, forKey: .importance) ?? 3, 1), 5)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(title, forKey: .title)
        try container.encode(ocrText, forKey: .ocrText)
        try container.encode(ocrBlocks, forKey: .ocrBlocks)
        try container.encode(proofreadText, forKey: .correctedOCRText)
        try container.encode(ocrReviewStatus.rawValue, forKey: .proofreadStatus)
        try container.encode(ocrSuspicions, forKey: .ocrSuspicions)
        try container.encode(summary, forKey: .summary)
        try container.encode(topics, forKey: .topics)
        try container.encode(facts, forKey: .facts)
        try container.encode(importance, forKey: .importance)
    }
}

public struct DraftEditorState: Equatable, Sendable {
    public var draft: ArticleDraft
    public private(set) var savedDraft: ArticleDraft

    public init(draft: ArticleDraft) {
        self.draft = draft
        self.savedDraft = draft
    }

    public var hasUnsavedChanges: Bool { draft != savedDraft }

    public mutating func setImportance(_ value: Int) {
        draft.importance = min(max(value, 1), 5)
    }

    public mutating func markSaved() {
        savedDraft = draft
    }

    public mutating func discardChanges() {
        draft = savedDraft
    }

    public func normalizedDraft() -> ArticleDraft {
        ArticleDraft(
            id: draft.id,
            title: draft.title.trimmed,
            ocrText: draft.ocrText,
            ocrBlocks: draft.ocrBlocks,
            proofreadText: draft.proofreadText,
            ocrReviewStatus: draft.ocrReviewStatus,
            ocrSuspicions: draft.ocrSuspicions.map(\.trimmed).filter { !$0.isEmpty },
            summary: draft.summary.trimmed,
            topics: draft.topics,
            facts: draft.facts.map { $0.trimmingWhitespace() },
            importance: draft.importance
        )
    }
}

public enum OCRReviewStatus: String, Codable, CaseIterable, Identifiable, Sendable {
    case unreviewed
    case edited
    case confirmed

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .unreviewed: return "未校对"
        case .edited: return "已编辑，待确认"
        case .confirmed: return "已确认"
        }
    }

    public var symbolName: String {
        switch self {
        case .unreviewed: return "circle.dashed"
        case .edited: return "pencil.circle"
        case .confirmed: return "checkmark.seal.fill"
        }
    }

    public static func fromBackend(_ value: String?) -> OCRReviewStatus {
        switch value {
        case "confirmed", "verified": return .confirmed
        case "edited", "in_progress", "needs_attention": return .edited
        default: return .unreviewed
        }
    }
}

public struct OCRParagraph: Equatable, Identifiable, Sendable {
    public let index: Int
    public let lines: [String]

    public var id: Int { index }
}

public struct OCRDocumentLayout: Equatable, Sendable {
    public let verbatimText: String
    public let paragraphs: [OCRParagraph]

    public init(text: String) {
        verbatimText = text
        let lines = text.components(separatedBy: "\n")
        var result: [OCRParagraph] = []
        var current: [String] = []
        for line in lines {
            if line.isEmpty {
                if !current.isEmpty {
                    result.append(OCRParagraph(index: result.count, lines: current))
                    current.removeAll(keepingCapacity: true)
                }
            } else {
                current.append(line)
            }
        }
        if !current.isEmpty {
            result.append(OCRParagraph(index: result.count, lines: current))
        }
        paragraphs = result
    }

    public static func fallbackBlocks(from text: String) -> [OCRContentBlock] {
        OCRDocumentLayout(text: text).paragraphs.map { paragraph in
            OCRContentBlock(kind: "paragraph", text: paragraph.lines.joined(separator: "\n"))
        }
    }
}

public struct EditionRecord: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var title: String
    public var pageNumber: Int?
    public var imagePath: String?
    public var pdfPath: String?
    public var ocrText: String
    public var articles: [ArticleDraft]

    public init(
        id: String,
        title: String,
        pageNumber: Int? = nil,
        imagePath: String? = nil,
        pdfPath: String? = nil,
        ocrText: String = "",
        articles: [ArticleDraft] = []
    ) {
        self.id = id
        self.title = title
        self.pageNumber = pageNumber
        self.imagePath = imagePath
        self.pdfPath = pdfPath
        self.ocrText = ocrText
        self.articles = articles
    }
}

public struct IssueDetail: Codable, Equatable, Sendable {
    public var sourceID: String
    public var sourceName: String
    public var date: String
    public var issueNumber: String?
    public var evidenceSHA256: String
    public var editions: [EditionRecord]
    public var warnings: [String]

    public init(
        sourceID: String,
        sourceName: String,
        date: String,
        issueNumber: String? = nil,
        evidenceSHA256: String,
        editions: [EditionRecord],
        warnings: [String] = []
    ) {
        self.sourceID = sourceID
        self.sourceName = sourceName
        self.date = date
        self.issueNumber = issueNumber
        self.evidenceSHA256 = evidenceSHA256
        self.editions = editions
        self.warnings = warnings
    }
}

public struct DraftUnit: Codable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let ocrText: String
    public let proofreadText: String?
    public let ocrReviewStatus: OCRReviewStatus
    public let ocrSuspicions: [String]
    public let summary: String
    public let topics: [ReadingTopic]
    public let facts: [FactFields]
    public let importance: Int

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case ocrText = "ocr_text"
        case proofreadText = "corrected_ocr_text"
        case ocrReviewStatus = "proofread_status"
        case ocrSuspicions = "ocr_suspicions"
        case summary
        case topics
        case facts
        case importance
    }

    public init(
        id: String,
        title: String,
        ocrText: String = "",
        proofreadText: String? = nil,
        ocrReviewStatus: OCRReviewStatus = .unreviewed,
        ocrSuspicions: [String] = [],
        summary: String,
        topics: Set<ReadingTopic>,
        facts: [FactFields],
        importance: Int
    ) {
        self.id = id
        self.title = title
        self.ocrText = ocrText
        if ocrReviewStatus == .unreviewed, proofreadText == nil || proofreadText == ocrText {
            self.proofreadText = nil
        } else {
            self.proofreadText = proofreadText
        }
        self.ocrReviewStatus = ocrReviewStatus
        self.ocrSuspicions = ocrSuspicions
        self.summary = summary
        self.topics = ReadingTopic.allCases.filter(topics.contains)
        self.facts = facts
        self.importance = min(max(importance, 1), 5)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(title, forKey: .title)
        try container.encode(ocrText, forKey: .ocrText)
        if let proofreadText {
            try container.encode(proofreadText, forKey: .proofreadText)
        } else {
            try container.encodeNil(forKey: .proofreadText)
        }
        try container.encode(ocrReviewStatus, forKey: .ocrReviewStatus)
        try container.encode(ocrSuspicions, forKey: .ocrSuspicions)
        try container.encode(summary, forKey: .summary)
        try container.encode(topics, forKey: .topics)
        try container.encode(facts, forKey: .facts)
        try container.encode(importance, forKey: .importance)
    }
}

public struct DraftSaveRequest: Codable, Equatable, Sendable {
    public let source: String
    public let date: String
    public let evidenceSHA256: String
    public let units: [DraftUnit]

    enum CodingKeys: String, CodingKey {
        case source
        case date
        case evidenceSHA256 = "evidence_sha256"
        case units
    }

    public init(
        source: String,
        date: String,
        evidenceSHA256: String,
        units: [DraftUnit]
    ) {
        self.source = source
        self.date = date
        self.evidenceSHA256 = evidenceSHA256
        self.units = units
    }
}

public enum DraftSaveScope {
    public static func unitIDs(
        all: [String],
        dirty: Set<String>,
        requireCompleteIssue: Bool
    ) -> [String] {
        requireCompleteIssue ? all : all.filter(dirty.contains)
    }
}

public enum PublishRevisionPolicy {
    public static func canUsePlan(
        previewRevision: Int?,
        currentRevision: Int,
        hasUnsavedChanges: Bool
    ) -> Bool {
        previewRevision == currentRevision && !hasUnsavedChanges
    }
}

public struct PublishChange: Codable, Equatable, Identifiable, Sendable {
    public var id: String { path }
    public let path: String
    public let changeType: String
    public let diff: String

    enum CodingKeys: String, CodingKey {
        case path
        case changeType = "change_type"
        case type
        case diff
    }

    public init(path: String, changeType: String, diff: String = "") {
        self.path = path
        self.changeType = changeType
        self.diff = diff
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        path = try container.decodeIfPresent(String.self, forKey: .path) ?? "未知文件"
        changeType = try container.decodeIfPresent(String.self, forKey: .changeType)
            ?? container.decodeIfPresent(String.self, forKey: .type)
            ?? "修改"
        diff = try container.decodeIfPresent(String.self, forKey: .diff) ?? ""
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(path, forKey: .path)
        try container.encode(changeType, forKey: .changeType)
        try container.encode(diff, forKey: .diff)
    }
}

public struct PublishPlan: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let changes: [PublishChange]
    public let warnings: [String]

    enum CodingKeys: String, CodingKey {
        case id
        case planID = "plan_id"
        case changes
        case files
        case warnings
    }

    public init(id: String, changes: [PublishChange], warnings: [String] = []) {
        self.id = id
        self.changes = changes
        self.warnings = warnings
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .planID)
            ?? container.decodeIfPresent(String.self, forKey: .id)
            ?? ""
        changes = try container.decodeIfPresent([PublishChange].self, forKey: .changes)
            ?? container.decodeIfPresent([PublishChange].self, forKey: .files)
            ?? []
        warnings = try container.decodeIfPresent([String].self, forKey: .warnings) ?? []
    }

    public func mergingWarnings(_ additionalWarnings: [String]) -> PublishPlan {
        var seen = Set<String>()
        let merged = (warnings + additionalWarnings).filter { warning in
            guard !warning.isEmpty, !seen.contains(warning) else { return false }
            seen.insert(warning)
            return true
        }
        return PublishPlan(id: id, changes: changes, warnings: merged)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .planID)
        try container.encode(changes, forKey: .changes)
        try container.encode(warnings, forKey: .warnings)
    }
}

public struct HistoryTransaction: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let date: String
    public let summary: String
    public let files: [String]
    public let canRollback: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case transactionID = "transaction_id"
        case date
        case createdAt = "created_at"
        case summary
        case files
        case canRollback = "can_rollback"
    }

    public init(id: String, date: String, summary: String, files: [String] = [], canRollback: Bool = true) {
        self.id = id
        self.date = date
        self.summary = summary
        self.files = files
        self.canRollback = canRollback
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decodeIfPresent(String.self, forKey: .transactionID)
            ?? container.decodeIfPresent(String.self, forKey: .id)
            ?? ""
        date = try container.decodeIfPresent(String.self, forKey: .createdAt)
            ?? container.decodeIfPresent(String.self, forKey: .date)
            ?? ""
        summary = try container.decodeIfPresent(String.self, forKey: .summary) ?? "已发布到知识库"
        files = try container.decodeIfPresent([String].self, forKey: .files) ?? []
        canRollback = try container.decodeIfPresent(Bool.self, forKey: .canRollback) ?? true
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .transactionID)
        try container.encode(date, forKey: .createdAt)
        try container.encode(summary, forKey: .summary)
        try container.encode(files, forKey: .files)
        try container.encode(canRollback, forKey: .canRollback)
    }
}

private extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}
