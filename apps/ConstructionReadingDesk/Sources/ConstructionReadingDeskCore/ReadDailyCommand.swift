import Foundation

public struct ReadDailyConfiguration: Equatable, Sendable {
    public var repositoryURL: URL
    public var archiveURL: URL
    public var vaultURL: URL
    public var pythonExecutableURL: URL

    public init(
        repositoryURL: URL,
        archiveURL: URL,
        vaultURL: URL,
        pythonExecutableURL: URL? = nil
    ) {
        self.repositoryURL = repositoryURL
        self.archiveURL = archiveURL
        self.vaultURL = vaultURL
        self.pythonExecutableURL = pythonExecutableURL ?? Self.detectedPythonExecutableURL
    }

    public static var detectedPythonExecutableURL: URL {
        if let configured = ProcessInfo.processInfo.environment["READDAILY_PYTHON"],
           !configured.isEmpty {
            return URL(fileURLWithPath: (configured as NSString).expandingTildeInPath)
        }
        let candidates = [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        if let path = candidates.first(where: FileManager.default.isExecutableFile(atPath:)) {
            return URL(fileURLWithPath: path)
        }
        return URL(fileURLWithPath: "/usr/bin/python3")
    }

    public static var detectedDefaults: ReadDailyConfiguration {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return ReadDailyConfiguration(
            repositoryURL: home.appendingPathComponent("readdaily", isDirectory: true),
            archiveURL: home.appendingPathComponent("Library/Application Support/readdaily/news-archive", isDirectory: true),
            vaultURL: home.appendingPathComponent("Maitty的知识库", isDirectory: true)
        )
    }
}

public enum ReadDailyAPICommand: Equatable, Sendable {
    case capabilities
    case newspaperRegistry
    case dailyDashboard(date: String? = nil)
    case inbox(source: String? = nil, date: String? = nil)
    case issue(source: String, date: String)
    case draftSave(inputFile: URL)
    case importFile(path: URL, date: String? = nil, source: String? = nil)
    case publishPlan(source: String? = nil, date: String? = nil)
    case publishApply(planID: String)
    case history
    case rollback(transactionID: String)
    case readingMark(
        source: String,
        date: String,
        status: ReadingCompletionStatus,
        expectedRevision: Int? = nil
    )
    case fetchDaily(date: String)
    case fetchConstruction(date: String? = nil)
}

public struct ProcessRequest: Equatable, Sendable {
    public let executableURL: URL
    public let arguments: [String]
    public let environment: [String: String]

    public init(executableURL: URL, arguments: [String], environment: [String: String] = [:]) {
        self.executableURL = executableURL
        self.arguments = arguments
        self.environment = environment
    }
}

public enum CommandFactoryError: LocalizedError, Equatable {
    case emptyIdentifier(String)

    public var errorDescription: String? {
        switch self {
        case .emptyIdentifier(let label): return "\(label)不能为空。"
        }
    }
}

public struct ReadDailyCommandFactory: Sendable {
    public let configuration: ReadDailyConfiguration

    public init(configuration: ReadDailyConfiguration) {
        self.configuration = configuration
    }

    public func make(_ command: ReadDailyAPICommand) throws -> ProcessRequest {
        let commandLineScript = configuration.repositoryURL
            .appendingPathComponent("scripts/readdaily.py").path
        let environment = [
            "READDAILY_ARCHIVE": configuration.archiveURL.path,
            "READDAILY_VAULT": configuration.vaultURL.path,
            // The bundled runtime is inside the signed app and must remain
            // byte-for-byte immutable after Python imports its modules.
            "PYTHONDONTWRITEBYTECODE": "1",
        ]

        if case .fetchConstruction(let date) = command {
            var arguments = [commandLineScript, "fetch", "--source", "zgjsb"]
            append("--date", date, to: &arguments)
            return ProcessRequest(
                executableURL: configuration.pythonExecutableURL,
                arguments: arguments,
                environment: environment
            )
        }
        if case .fetchDaily(let date) = command {
            var arguments = [commandLineScript, "fetch"]
            append("--date", date, to: &arguments)
            return ProcessRequest(
                executableURL: configuration.pythonExecutableURL,
                arguments: arguments,
                environment: environment
            )
        }

        let workbenchScript = configuration.repositoryURL
            .appendingPathComponent("skills/newspaper-reader/scripts/workbench_api.py").path
        var arguments = [workbenchScript, command.name]
        arguments += ["--archive", configuration.archiveURL.path, "--vault", configuration.vaultURL.path]

        switch command {
        case .capabilities, .newspaperRegistry, .history:
            break
        case .dailyDashboard(let date):
            append("--date", date, to: &arguments)
        case .inbox(let source, let date), .publishPlan(let source, let date):
            append("--source", source, to: &arguments)
            append("--date", date, to: &arguments)
        case .issue(let source, let date):
            append("--source", source, to: &arguments)
            append("--date", date, to: &arguments)
        case .draftSave(let inputFile):
            arguments += ["--input", inputFile.path]
        case .importFile(let path, let date, let source):
            arguments += ["--path", path.path]
            append("--date", date, to: &arguments)
            append("--source", source, to: &arguments)
        case .publishApply(let planID):
            guard !planID.isEmpty else { throw CommandFactoryError.emptyIdentifier("发布计划编号") }
            arguments += ["--plan-id", planID]
        case .rollback(let transactionID):
            guard !transactionID.isEmpty else { throw CommandFactoryError.emptyIdentifier("发布记录编号") }
            arguments += ["--transaction-id", transactionID]
        case .readingMark(let source, let date, let status, let expectedRevision):
            append("--source", source, to: &arguments)
            append("--date", date, to: &arguments)
            append("--status", status.rawValue, to: &arguments)
            append("--expected-reading-revision", expectedRevision.map(String.init), to: &arguments)
        case .fetchDaily, .fetchConstruction:
            break
        }

        return ProcessRequest(
            executableURL: configuration.pythonExecutableURL,
            arguments: arguments,
            environment: environment
        )
    }

    private func append(_ flag: String, _ value: String?, to arguments: inout [String]) {
        guard let value, !value.isEmpty else { return }
        arguments += [flag, value]
    }
}

private extension ReadDailyAPICommand {
    var name: String {
        switch self {
        case .capabilities: return "capabilities"
        case .newspaperRegistry: return "newspaper-registry"
        case .dailyDashboard: return "daily-dashboard"
        case .inbox: return "inbox"
        case .issue: return "issue"
        case .draftSave: return "draft-save"
        case .importFile: return "import-file"
        case .publishPlan: return "publish-plan"
        case .publishApply: return "publish-apply"
        case .history: return "history"
        case .rollback: return "rollback"
        case .readingMark: return "reading-mark"
        case .fetchDaily: return "fetch"
        case .fetchConstruction: return "fetch"
        }
    }
}
