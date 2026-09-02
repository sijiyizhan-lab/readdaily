import Foundation

public struct ReadDailyConfiguration: Equatable, Sendable {
    public var repositoryURL: URL
    public var archiveURL: URL
    public var vaultURL: URL

    public init(repositoryURL: URL, archiveURL: URL, vaultURL: URL) {
        self.repositoryURL = repositoryURL
        self.archiveURL = archiveURL
        self.vaultURL = vaultURL
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
    case inbox(source: String? = nil, date: String? = nil)
    case issue(source: String, date: String)
    case draftSave(inputFile: URL)
    case importFile(path: URL, date: String? = nil, source: String? = nil)
    case publishPlan(source: String? = nil, date: String? = nil)
    case publishApply(planID: String)
    case history
    case rollback(transactionID: String)
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
        let script = configuration.repositoryURL.appendingPathComponent("scripts/readdaily.py").path
        let environment = [
            "READDAILY_ARCHIVE": configuration.archiveURL.path,
            "READDAILY_VAULT": configuration.vaultURL.path,
        ]

        if case .fetchConstruction(let date) = command {
            var arguments = [script, "fetch", "--source", "zgjsb"]
            append("--date", date, to: &arguments)
            return ProcessRequest(
                executableURL: URL(fileURLWithPath: "/usr/bin/python3"),
                arguments: arguments,
                environment: environment
            )
        }

        var arguments = [script, "api", command.name]
        arguments += ["--archive", configuration.archiveURL.path, "--vault", configuration.vaultURL.path]

        switch command {
        case .capabilities, .history:
            break
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
        case .fetchConstruction:
            break
        }

        return ProcessRequest(
            executableURL: URL(fileURLWithPath: "/usr/bin/python3"),
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
        case .inbox: return "inbox"
        case .issue: return "issue"
        case .draftSave: return "draft-save"
        case .importFile: return "import-file"
        case .publishPlan: return "publish-plan"
        case .publishApply: return "publish-apply"
        case .history: return "history"
        case .rollback: return "rollback"
        case .fetchConstruction: return "fetch"
        }
    }
}
