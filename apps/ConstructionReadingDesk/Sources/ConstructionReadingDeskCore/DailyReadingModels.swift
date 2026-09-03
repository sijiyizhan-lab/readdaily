import Foundation

public enum NewspaperCategory: String, Codable, CaseIterable, Identifiable, Sendable {
    case centralParty = "中央党报"
    case ministryIndustry = "部委行业报"
    case localParty = "地方党报"

    public var id: String {
        switch self {
        case .centralParty: return "central_party"
        case .ministryIndustry: return "ministry_industry"
        case .localParty: return "local_party"
        }
    }
}

public struct NewspaperSource: Codable, Equatable, Hashable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let category: NewspaperCategory

    public init(id: String, name: String, category: NewspaperCategory) {
        self.id = id
        self.name = name
        self.category = category
    }
}

public enum NewspaperRegistry {
    public static let dailySources: [NewspaperSource] = [
        NewspaperSource(id: "rmrb", name: "人民日报", category: .centralParty),
        NewspaperSource(id: "gmrb", name: "光明日报", category: .centralParty),
        NewspaperSource(id: "jjrb", name: "经济日报", category: .centralParty),
        NewspaperSource(id: "zgjsb", name: "中国建设报", category: .ministryIndustry),
        NewspaperSource(id: "kjrb", name: "科技日报", category: .ministryIndustry),
        NewspaperSource(id: "nmrb", name: "农民日报", category: .ministryIndustry),
        NewspaperSource(id: "nfrb", name: "南方日报", category: .localParty),
        NewspaperSource(id: "bjrb", name: "北京日报", category: .localParty),
    ]

    public static func source(id: String) -> NewspaperSource? {
        dailySources.first { $0.id == id }
    }
}

public enum DailyRunStatus: String, Codable, Equatable, Sendable {
    case notStarted = "missing"
    case running
    case readyForReview = "needs_review"
    case reviewComplete = "review_complete"
    case readyToPublish = "ready_to_publish"
    case published
    case failed

    public var accessibleLabel: String {
        switch self {
        case .notStarted: return "当日未获取"
        case .running: return "执行中"
        case .readyForReview: return "已读取，待校对"
        case .reviewComplete: return "校对完成"
        case .readyToPublish: return "校对完成，待发布"
        case .published: return "已发布"
        case .failed: return "执行失败"
        }
    }

    public var symbolName: String {
        switch self {
        case .notStarted: return "xmark.circle"
        case .running: return "clock.arrow.circlepath"
        case .readyForReview: return "exclamationmark.circle.fill"
        case .reviewComplete: return "checkmark.seal"
        case .readyToPublish: return "checkmark.circle"
        case .published: return "checkmark.circle.fill"
        case .failed: return "xmark.octagon.fill"
        }
    }

    public var isAvailable: Bool {
        switch self {
        case .readyForReview, .reviewComplete, .readyToPublish, .published: return true
        case .notStarted, .running, .failed: return false
        }
    }

    public static func from(issue: IssueSummary?) -> DailyRunStatus {
        guard let issue else { return .notStarted }
        switch issue.stage {
        case "published": return .published
        case "ready_to_publish": return .readyToPublish
        case "review_complete": return .reviewComplete
        case "needs_review", "success", "ready": return .readyForReview
        case "running", "processing", "fetching", "ocr": return .running
        case "failed", "error": return .failed
        default: return issue.pageCount == nil ? .notStarted : .readyForReview
        }
    }
}

public enum ReadingCompletionStatus: String, Codable, CaseIterable, Sendable {
    case unread
    case opened
    case completed

    public var accessibleLabel: String {
        switch self {
        case .unread: return "今日未读"
        case .opened: return "今日已打开，尚未完成"
        case .completed: return "今日已读完"
        }
    }

    public var symbolName: String {
        switch self {
        case .unread: return "circle"
        case .opened: return "book.pages"
        case .completed: return "checkmark.circle.fill"
        }
    }
}

public struct DailyNewspaperEntry: Equatable, Identifiable, Sendable {
    public let source: NewspaperSource
    public let issue: IssueSummary?
    public let status: DailyRunStatus
    public let readingStatus: ReadingCompletionStatus
    public let readingRevision: Int

    public var id: String { source.id }

    public init(
        source: NewspaperSource,
        issue: IssueSummary?,
        status: DailyRunStatus? = nil,
        readingStatus: ReadingCompletionStatus? = nil,
        readingRevision: Int = 0
    ) {
        self.source = source
        self.issue = issue
        self.status = status ?? DailyRunStatus.from(issue: issue)
        self.readingStatus = readingStatus
            ?? issue?.readingStatus.flatMap(ReadingCompletionStatus.init(rawValue:))
            ?? .unread
        self.readingRevision = max(0, readingRevision)
    }
}

public struct DailyNewspaperSection: Equatable, Identifiable, Sendable {
    public let category: NewspaperCategory
    public let entries: [DailyNewspaperEntry]

    public var id: String { category.id }
}

public struct DailyReadingDay: Equatable, Identifiable, Sendable {
    public let date: String
    public let sections: [DailyNewspaperSection]
    public let availableDates: [String]

    public var id: String { date }
    public var entries: [DailyNewspaperEntry] { sections.flatMap(\.entries) }
    public var completedCount: Int { entries.filter { $0.status.isAvailable }.count }
    public var readCount: Int { entries.filter { $0.readingStatus == .completed }.count }

    public init(date: String, sections: [DailyNewspaperSection], availableDates: [String] = []) {
        self.date = date
        self.sections = sections
        self.availableDates = availableDates
    }
}

public struct DailyReadingDashboard: Equatable, Sendable {
    public let dates: [String]
    private let issues: [IssueSummary]

    public init(issues: [IssueSummary]) {
        self.issues = issues.filter { NewspaperRegistry.source(id: $0.sourceID) != nil }
        dates = Array(Set(self.issues.map(\.date))).sorted(by: >)
    }

    public func day(for date: String) -> DailyReadingDay {
        Self.day(date: date, issues: issues)
    }

    public static func day(date: String, issues: [IssueSummary]) -> DailyReadingDay {
        let rowsBySource = Dictionary(
            issues.filter { $0.date == date }.map { ($0.sourceID, $0) },
            uniquingKeysWith: { current, _ in current }
        )
        let sections = NewspaperCategory.allCases.map { category in
            DailyNewspaperSection(
                category: category,
                entries: NewspaperRegistry.dailySources
                    .filter { $0.category == category }
                    .map { source in
                        let issue = rowsBySource[source.id]
                        return DailyNewspaperEntry(source: source, issue: issue)
                    }
            )
        }
        return DailyReadingDay(date: date, sections: sections, availableDates: [])
    }
}

public enum ReadingDatePolicy {
    public static func localDay(now: Date = Date(), calendar: Calendar = .current) -> String {
        let components = calendar.dateComponents([.year, .month, .day], from: now)
        return String(
            format: "%04d-%02d-%02d",
            components.year ?? 0,
            components.month ?? 0,
            components.day ?? 0
        )
    }

    public static func menuDates(
        availableDates: [String],
        selectedDate: String?,
        today: String
    ) -> [String] {
        let values = availableDates + [selectedDate, today].compactMap { $0 }
        return Array(Set(values.filter { !$0.isEmpty })).sorted(by: >)
    }

    public static func initialSelection(
        selectedDate: String?,
        availableDates: [String],
        today: String
    ) -> String? {
        selectedDate ?? (!today.isEmpty ? today : availableDates.sorted(by: >).first)
    }
}

public struct ReadDailySettingsValues: Equatable, Sendable {
    public var repositoryPath: String
    public var archivePath: String
    public var vaultPath: String
    public var weatherText: String

    public init(
        repositoryPath: String,
        archivePath: String,
        vaultPath: String,
        weatherText: String
    ) {
        self.repositoryPath = repositoryPath
        self.archivePath = archivePath
        self.vaultPath = vaultPath
        self.weatherText = weatherText
    }

    public func changesDataContext(comparedTo other: ReadDailySettingsValues) -> Bool {
        repositoryPath != other.repositoryPath
            || archivePath != other.archivePath
            || vaultPath != other.vaultPath
    }
}

public struct LocalWeatherSummary: Equatable, Sendable {
    public let configuredText: String

    public init(configuredText: String?) {
        self.configuredText = configuredText ?? ""
    }

    public var isConfigured: Bool {
        !configuredText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public var displayText: String {
        isConfigured ? configuredText.trimmingCharacters(in: .whitespacesAndNewlines) : "天气未配置"
    }
}
