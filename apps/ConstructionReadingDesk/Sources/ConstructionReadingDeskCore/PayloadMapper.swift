import Foundation

public enum PayloadMappingError: LocalizedError, Equatable {
    case expectedObject(String)
    case missingField(String)

    public var errorDescription: String? {
        switch self {
        case .expectedObject(let context): return "\(context)的数据格式不正确。"
        case .missingField(let field): return "返回数据缺少 \(field)。"
        }
    }

    public var recoverySuggestion: String? {
        "请确认 Read Daily 与读报引擎版本一致。"
    }
}

public struct WorkbenchPayloadMapper: Sendable {
    public init() {}

    public func inbox(from value: JSONValue?, sourceID: String? = nil) throws -> [IssueSummary] {
        let root = try object(value, context: "收件箱")
        let values = root["issues"]?.arrayValue ?? root["items"]?.arrayValue ?? []
        let issues: [IssueSummary] = values.compactMap { item -> IssueSummary? in
            guard let row = item.objectValue else { return nil }
            let source = row.string("source", "source_id") ?? "unknown"
            let date = row.string("date") ?? "未知日期"
            let warnings = row.strings("warnings")
            return IssueSummary(
                id: row.string("id"),
                sourceID: source,
                sourceName: row.string("source_name", "sourceName"),
                date: date,
                issueNumber: row.string("issue_no", "issue_number"),
                stage: row.string("status", "stage"),
                readingStatus: row.string("reading_status"),
                warningCount: row.integer("warning_count") ?? warnings.count,
                pageCount: row.object("coverage")?.integer("editions") ?? row.integer("page_count"),
                warnings: warnings
            )
        }
        guard let sourceID else { return issues }
        return issues.filter { $0.sourceID == sourceID }
    }

    public func dailyDashboard(from value: JSONValue?) throws -> DailyReadingDay {
        let root = try object(value, context: "每日仪表盘")
        guard let date = root.string("date") else {
            throw PayloadMappingError.missingField("date")
        }
        var values = root["newspapers"]?.arrayValue ?? []
        if values.isEmpty {
            values = (root["categories"]?.arrayValue ?? []).flatMap { category in
                category.objectValue?["newspapers"]?.arrayValue ?? []
            }
        }
        let rows = Dictionary(
            values.compactMap { value -> (String, [String: JSONValue])? in
                guard let row = value.objectValue,
                      let source = row.string("source", "source_id") else { return nil }
                return (source, row)
            },
            uniquingKeysWith: { current, _ in current }
        )
        let sections = NewspaperCategory.allCases.map { category in
            DailyNewspaperSection(
                category: category,
                entries: NewspaperRegistry.dailySources
                    .filter { $0.category == category }
                    .map { source in
                        guard let row = rows[source.id] else {
                            return DailyNewspaperEntry(source: source, issue: nil)
                        }
                        let rowStatus = row.string("status", "stage")
                        let acquisitionStatus = row.string("acquisition_status") ?? rowStatus
                        let status = dailyStatus(
                            rowStatus ?? acquisitionStatus,
                            available: row.boolean("available")
                        )
                        let readingStatus = ReadingCompletionStatus(rawValue: row.string("reading_status") ?? "unread") ?? .unread
                        let warnings = row.strings("warnings")
                        let issue: IssueSummary? = row.boolean("available") == false ? nil : IssueSummary(
                            id: row.string("id"),
                            sourceID: source.id,
                            sourceName: row.string("source_name") ?? source.name,
                            date: row.string("date") ?? date,
                            issueNumber: row.string("issue_no", "issue_number"),
                            stage: rowStatus ?? acquisitionStatus,
                            readingStatus: readingStatus.rawValue,
                            warningCount: row.integer("warning_count") ?? warnings.count,
                            pageCount: row.object("coverage")?.integer("editions") ?? row.integer("page_count"),
                            warnings: warnings
                        )
                        return DailyNewspaperEntry(
                            source: source,
                            issue: issue,
                            status: status,
                            readingStatus: readingStatus
                        )
                    }
            )
        }
        return DailyReadingDay(
            date: date,
            sections: sections,
            availableDates: root.strings("available_dates")
        )
    }

    public func issue(from value: JSONValue?) throws -> IssueDetail {
        let initial = try object(value, context: "期次")
        let root = initial["issue"]?.objectValue ?? initial
        guard let source = root.string("source", "source_id") else {
            throw PayloadMappingError.missingField("source")
        }
        guard let date = root.string("date") else {
            throw PayloadMappingError.missingField("date")
        }
        guard let evidenceSHA256 = root.string("evidence_sha256"),
              !evidenceSHA256.isEmpty else {
            throw PayloadMappingError.missingField("evidence_sha256")
        }
        let sourceName = root.string("source_name", "sourceName") ?? source
        let pdfPath = root.string("pdf_path")
        let unitValues = root["units"]?.arrayValue ?? root["editions"]?.arrayValue ?? []
        var issueWarnings = root.strings("warnings")
        let editions = unitValues.enumerated().compactMap { offset, value -> EditionRecord? in
            guard let unit = value.objectValue else { return nil }
            let id = unit.string("id") ?? "\(source)_\(date.replacingOccurrences(of: "-", with: ""))_\(offset + 1)"
            let title = unit.string("title", "edition_name") ?? "第\(offset + 1)版"
            let topics = Set(unit.strings("topics", "category").compactMap(ReadingTopic.init(rawValue:)))
            let facts = unit["facts"]?.arrayValue?.compactMap { factValue -> FactFields? in
                guard let fact = factValue.objectValue else { return nil }
                return FactFields(
                    subject: fact.string("subject") ?? "",
                    action: fact.string("action") ?? "",
                    object: fact.string("object") ?? "",
                    value: fact.string("value") ?? "",
                    unit: fact.string("unit") ?? "",
                    time: fact.string("time") ?? "",
                    source: fact.string("source") ?? ""
                )
            } ?? []
            let warnings = unit.strings("warnings")
            issueWarnings.append(contentsOf: warnings)
            let ocrText = unit.string("ocr_text", "text") ?? ""
            let ocrBlocks = (unit["ocr_blocks"]?.arrayValue ?? []).compactMap { blockValue -> OCRContentBlock? in
                guard let block = blockValue.objectValue,
                      let text = block.string("text") else { return nil }
                return OCRContentBlock(
                    kind: block.string("kind") ?? "paragraph",
                    title: block.string("title"),
                    text: text
                )
            }
            let article = ArticleDraft(
                id: id,
                title: title,
                ocrText: ocrText,
                ocrBlocks: ocrBlocks,
                proofreadText: unit.string("corrected_ocr_text", "proofread_text") ?? ocrText,
                ocrReviewStatus: OCRReviewStatus.fromBackend(unit.string("proofread_status", "ocr_review_status")),
                ocrSuspicions: unit.strings("ocr_suspicions"),
                summary: unit.string("summary") ?? "",
                topics: topics,
                facts: facts,
                importance: unit.integer("importance") ?? 3
            )
            return EditionRecord(
                id: id,
                title: unit.string("edition_name", "title") ?? title,
                pageNumber: unit.integer("edition_no", "page_number") ?? offset + 1,
                imagePath: unit.string("page_image", "image_path"),
                pdfPath: pdfPath,
                ocrText: article.ocrText,
                articles: [article]
            )
        }
        return IssueDetail(
            sourceID: source,
            sourceName: sourceName,
            date: date,
            issueNumber: root.string("issue_no", "issue_number"),
            evidenceSHA256: evidenceSHA256,
            editions: editions,
            warnings: Array(Set(issueWarnings)).sorted()
        )
    }

    public func publishPlan(from value: JSONValue?) throws -> PublishPlan {
        let root = try object(value, context: "发布预览")
        guard let planID = root.string("plan_id", "id"), !planID.isEmpty else {
            throw PayloadMappingError.missingField("plan_id")
        }
        let changes = (root["changes"]?.arrayValue ?? []).compactMap { value -> PublishChange? in
            guard let item = value.objectValue else { return nil }
            let path = item.string("relative_path", "path") ?? "未知文件"
            let type = item.boolean("before_exists") == true ? "修改" : "新增"
            return PublishChange(path: path, changeType: type, diff: item.string("diff") ?? "")
        }
        return PublishPlan(id: planID, changes: changes, warnings: root.strings("warnings"))
    }

    public func history(from value: JSONValue?) throws -> [HistoryTransaction] {
        let root = try object(value, context: "发布历史")
        return (root["transactions"]?.arrayValue ?? root["history"]?.arrayValue ?? []).compactMap { value in
            guard let item = value.objectValue,
                  let id = item.string("transaction_id", "id") else { return nil }
            let source = item.string("source") == "zgjsb" ? "中国建设报" : (item.string("source") ?? "报纸")
            let date = item.string("date") ?? "未知日期"
            let status = item.string("status") ?? "unknown"
            let statusText: String
            switch status {
            case "applied": statusText = "已发布"
            case "rolled_back": statusText = "已回滚"
            default: statusText = status
            }
            return HistoryTransaction(
                id: id,
                date: item.string("created_at", "date") ?? date,
                summary: "\(source) \(date) · \(statusText)",
                files: [],
                canRollback: status == "applied"
            )
        }
    }

    private func object(_ value: JSONValue?, context: String) throws -> [String: JSONValue] {
        guard let object = value?.objectValue else {
            throw PayloadMappingError.expectedObject(context)
        }
        return object
    }

    private func dailyStatus(_ rawValue: String?, available: Bool?) -> DailyRunStatus {
        switch rawValue {
        case "published": return .published
        case "ready_to_publish": return .readyToPublish
        case "review_complete": return .reviewComplete
        case "success", "complete", "ready", "needs_review": return .readyForReview
        case "pending", "running", "processing", "fetching", "ocr": return available == false ? .notStarted : .running
        case "failed", "error": return .failed
        case "missing": return .notStarted
        default: return available == true ? .readyForReview : .notStarted
        }
    }
}

private extension Dictionary where Key == String, Value == JSONValue {
    func string(_ keys: String...) -> String? {
        for key in keys {
            if let result = self[key]?.stringValue { return result }
        }
        return nil
    }

    func integer(_ keys: String...) -> Int? {
        for key in keys {
            if let result = self[key]?.intValue { return result }
        }
        return nil
    }

    func boolean(_ key: String) -> Bool? {
        guard case .bool(let value) = self[key] else { return nil }
        return value
    }

    func object(_ key: String) -> [String: JSONValue]? {
        self[key]?.objectValue
    }

    func strings(_ keys: String...) -> [String] {
        for key in keys {
            if let values = self[key]?.arrayValue {
                return values.compactMap(\.stringValue)
            }
        }
        return []
    }
}
