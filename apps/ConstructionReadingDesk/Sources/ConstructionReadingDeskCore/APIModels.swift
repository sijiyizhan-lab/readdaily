import Foundation

public struct APIEnvelope<Payload: Codable & Equatable & Sendable>: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let ok: Bool
    public let data: Payload?
    public let warnings: [String]
    public let error: BackendErrorPayload?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case ok
        case data
        case warnings
        case error
    }

    public init(
        schemaVersion: Int = 1,
        ok: Bool,
        data: Payload? = nil,
        warnings: [String] = [],
        error: BackendErrorPayload? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.ok = ok
        self.data = data
        self.warnings = warnings
        self.error = error
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decodeIfPresent(Int.self, forKey: .schemaVersion) ?? 1
        ok = try container.decode(Bool.self, forKey: .ok)
        data = try container.decodeIfPresent(Payload.self, forKey: .data)
        warnings = try container.decodeIfPresent([String].self, forKey: .warnings) ?? []
        error = try container.decodeIfPresent(BackendErrorPayload.self, forKey: .error)
    }
}

public struct BackendErrorPayload: Codable, Equatable, Sendable {
    public let code: String?
    public let message: String
    public let recovery: String?

    public init(code: String? = nil, message: String, recovery: String? = nil) {
        self.code = code
        self.message = message
        self.recovery = recovery
    }
}

public enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "不支持的 JSON 值")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    public var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }

    public var arrayValue: [JSONValue]? {
        guard case .array(let value) = self else { return nil }
        return value
    }

    public var stringValue: String? {
        switch self {
        case .string(let value): return value
        case .number(let value):
            return value.rounded() == value ? String(Int(value)) : String(value)
        case .bool(let value): return value ? "true" : "false"
        default: return nil
        }
    }

    public var intValue: Int? {
        switch self {
        case .number(let value): return Int(value)
        case .string(let value): return Int(value)
        default: return nil
        }
    }
}

public struct InboxPayload: Codable, Equatable, Sendable {
    public let issues: [IssueSummary]

    public init(issues: [IssueSummary]) {
        self.issues = issues
    }
}

public struct IssueSummary: Codable, Equatable, Hashable, Identifiable, Sendable {
    public let backendID: String?
    public let sourceID: String
    public let sourceName: String?
    public let date: String
    public let issueNumber: String?
    public let stage: String?
    public let warningCount: Int
    public let pageCount: Int?
    public let warnings: [String]

    public var id: String { stableID }
    public var stableID: String { backendID ?? "\(sourceID)-\(date)" }

    enum CodingKeys: String, CodingKey {
        case backendID = "id"
        case sourceID = "source_id"
        case source
        case sourceName = "source_name"
        case date
        case issueNumber = "issue_number"
        case stage
        case warningCount = "warning_count"
        case pageCount = "page_count"
        case warnings
    }

    public init(
        id: String? = nil,
        sourceID: String,
        sourceName: String? = nil,
        date: String,
        issueNumber: String? = nil,
        stage: String? = nil,
        warningCount: Int = 0,
        pageCount: Int? = nil,
        warnings: [String] = []
    ) {
        self.backendID = id
        self.sourceID = sourceID
        self.sourceName = sourceName
        self.date = date
        self.issueNumber = issueNumber
        self.stage = stage
        self.warningCount = warningCount
        self.pageCount = pageCount
        self.warnings = warnings
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        backendID = try container.decodeIfPresent(String.self, forKey: .backendID)
        sourceID = try container.decodeIfPresent(String.self, forKey: .sourceID)
            ?? container.decodeIfPresent(String.self, forKey: .source)
            ?? "unknown"
        sourceName = try container.decodeIfPresent(String.self, forKey: .sourceName)
        date = try container.decodeIfPresent(String.self, forKey: .date) ?? "未知日期"
        issueNumber = try container.decodeIfPresent(String.self, forKey: .issueNumber)
        stage = try container.decodeIfPresent(String.self, forKey: .stage)
        warningCount = try container.decodeIfPresent(Int.self, forKey: .warningCount) ?? 0
        pageCount = try container.decodeIfPresent(Int.self, forKey: .pageCount)
        warnings = try container.decodeIfPresent([String].self, forKey: .warnings) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(backendID, forKey: .backendID)
        try container.encode(sourceID, forKey: .sourceID)
        try container.encodeIfPresent(sourceName, forKey: .sourceName)
        try container.encode(date, forKey: .date)
        try container.encodeIfPresent(issueNumber, forKey: .issueNumber)
        try container.encodeIfPresent(stage, forKey: .stage)
        try container.encode(warningCount, forKey: .warningCount)
        try container.encodeIfPresent(pageCount, forKey: .pageCount)
        try container.encode(warnings, forKey: .warnings)
    }
}
