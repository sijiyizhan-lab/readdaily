import Foundation

public struct ProcessResult: Equatable, Sendable {
    public let terminationStatus: Int32
    public let standardOutput: Data
    public let standardError: Data

    public init(terminationStatus: Int32, standardOutput: Data, standardError: Data) {
        self.terminationStatus = terminationStatus
        self.standardOutput = standardOutput
        self.standardError = standardError
    }

    public var standardOutputText: String {
        String(data: standardOutput, encoding: .utf8) ?? ""
    }

    public var standardErrorText: String {
        String(data: standardError, encoding: .utf8) ?? ""
    }
}

public protocol ProcessRunning: Sendable {
    func run(_ request: ProcessRequest) async throws -> ProcessResult
}

public final class FoundationProcessRunner: ProcessRunning, @unchecked Sendable {
    public init() {}

    public func run(_ request: ProcessRequest) async throws -> ProcessResult {
        let process = Process()
        let standardOutput = Pipe()
        let standardError = Pipe()
        process.executableURL = request.executableURL
        process.arguments = request.arguments
        process.environment = ProcessInfo.processInfo.environment.merging(request.environment) { _, new in new }
        process.standardOutput = standardOutput
        process.standardError = standardError

        do {
            try process.run()
        } catch {
            throw ReadDailyClientError.launchFailed(error.localizedDescription)
        }

        let outputTask = Task.detached(priority: .userInitiated) {
            standardOutput.fileHandleForReading.readDataToEndOfFile()
        }
        let errorTask = Task.detached(priority: .userInitiated) {
            standardError.fileHandleForReading.readDataToEndOfFile()
        }

        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    process.waitUntilExit()
                    continuation.resume()
                }
            }
        } onCancel: {
            if process.isRunning { process.terminate() }
        }

        let output = await outputTask.value
        let error = await errorTask.value
        return ProcessResult(
            terminationStatus: process.terminationStatus,
            standardOutput: output,
            standardError: error
        )
    }
}

public enum ReadDailyClientError: LocalizedError, Equatable {
    case scriptMissing(String)
    case launchFailed(String)
    case nonzeroExit(status: Int32, stderr: String)
    case emptyOutput
    case invalidJSON(String)
    case incompatibleSchema(Int)
    case backendRejected(message: String, recovery: String?)
    case draftEncoding(String)

    public var errorDescription: String? {
        switch self {
        case .scriptMissing:
            return "找不到读报后端。"
        case .launchFailed:
            return "无法启动本地读报后端。"
        case .nonzeroExit(let status, _):
            return "读报后端执行失败（退出码 \(status)）。"
        case .emptyOutput:
            return "读报后端没有返回数据。"
        case .invalidJSON:
            return "读报后端返回了无法识别的数据。"
        case .incompatibleSchema(let version):
            return "后端数据版本 \(version) 与当前应用不兼容。"
        case .backendRejected(let message, _):
            return message
        case .draftEncoding:
            return "无法准备草稿保存文件。"
        }
    }

    public var failureReason: String? {
        switch self {
        case .scriptMissing(let path): return "未找到 \(path)"
        case .launchFailed(let detail): return detail
        case .nonzeroExit(_, let stderr): return stderr.isEmpty ? "进程异常退出，但没有提供错误日志。" : stderr
        case .emptyOutput: return "标准输出为空。"
        case .invalidJSON(let detail): return detail
        case .incompatibleSchema: return "应用当前仅支持 schema_version = 1。"
        case .backendRejected(let message, _): return message
        case .draftEncoding(let detail): return detail
        }
    }

    public var recoverySuggestion: String? {
        switch self {
        case .scriptMissing:
            return "请在设置中重新选择 readdaily 仓库目录。"
        case .launchFailed:
            return "请安装 Python 3，或通过 READDAILY_PYTHON 指定可执行文件后重试。"
        case .nonzeroExit:
            return "查看界面中的错误详情，修正路径或输入文件后重试。"
        case .emptyOutput, .invalidJSON:
            return "请确认客户端与仓库版本一致；详细日志只应写入 stderr。"
        case .incompatibleSchema:
            return "请升级 Read Daily 或切换到兼容版本的读报引擎。"
        case .backendRejected(_, let recovery):
            return recovery ?? "请按后端提示修正后重试。"
        case .draftEncoding:
            return "请确认系统临时目录可写，然后重试。"
        }
    }
}

public struct ReadDailyResponseDecoder: Sendable {
    public init() {}

    public func decode(_ result: ProcessResult) throws -> APIEnvelope<JSONValue> {
        let decodedEnvelope = try? JSONDecoder().decode(
            APIEnvelope<JSONValue>.self,
            from: result.standardOutput
        )
        if let envelope = decodedEnvelope {
            guard envelope.schemaVersion == 1 else {
                throw ReadDailyClientError.incompatibleSchema(envelope.schemaVersion)
            }
            guard envelope.ok else {
                throw ReadDailyClientError.backendRejected(
                    message: envelope.error?.message ?? "读报后端拒绝了本次操作。",
                    recovery: envelope.error?.recovery
                )
            }
            guard result.terminationStatus == 0 else {
                throw ReadDailyClientError.nonzeroExit(
                    status: result.terminationStatus,
                    stderr: result.standardErrorText.trimmingCharacters(in: .whitespacesAndNewlines)
                )
            }
            return envelope
        }
        if result.terminationStatus != 0 {
            throw ReadDailyClientError.nonzeroExit(
                status: result.terminationStatus,
                stderr: result.standardErrorText.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
        guard !result.standardOutput.isEmpty else { throw ReadDailyClientError.emptyOutput }
        let preview = result.standardOutputText.prefix(240)
        throw ReadDailyClientError.invalidJSON("输出不是完整的 API JSON；输出开头：\(preview)")
    }
}

public actor ReadDailyClient {
    private let configuration: ReadDailyConfiguration
    private let runner: any ProcessRunning
    private let responseDecoder = ReadDailyResponseDecoder()

    public init(
        configuration: ReadDailyConfiguration,
        runner: any ProcessRunning = FoundationProcessRunner()
    ) {
        self.configuration = configuration
        self.runner = runner
    }

    public func perform(_ command: ReadDailyAPICommand) async throws -> APIEnvelope<JSONValue> {
        let request = try validatedRequest(for: command)
        let result = try await runner.run(request)
        return try responseDecoder.decode(result)
    }

    public func saveDraft(_ draft: DraftSaveRequest) async throws -> APIEnvelope<JSONValue> {
        let inputURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("readdaily-draft-\(UUID().uuidString)")
            .appendingPathExtension("json")
        defer { try? FileManager.default.removeItem(at: inputURL) }

        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            try encoder.encode(draft).write(to: inputURL, options: [.atomic])
        } catch {
            throw ReadDailyClientError.draftEncoding(error.localizedDescription)
        }
        return try await perform(.draftSave(inputFile: inputURL))
    }

    public func fetchConstructionPaper(date: String? = nil) async throws -> String {
        let request = try validatedRequest(for: .fetchConstruction(date: date))
        return try await performRawFetch(request)
    }

    public func fetchDaily(date: String) async throws -> String {
        let request = try validatedRequest(for: .fetchDaily(date: date))
        return try await performRawFetch(request)
    }

    private func performRawFetch(_ request: ProcessRequest) async throws -> String {
        let result = try await runner.run(request)
        guard result.terminationStatus == 0 else {
            throw ReadDailyClientError.nonzeroExit(
                status: result.terminationStatus,
                stderr: result.standardErrorText.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
        return result.standardOutputText
    }

    private func validatedRequest(for command: ReadDailyAPICommand) throws -> ProcessRequest {
        let request = try ReadDailyCommandFactory(configuration: configuration).make(command)
        guard let script = request.arguments.first, !script.isEmpty else {
            throw ReadDailyClientError.scriptMissing("未指定后端脚本")
        }
        guard FileManager.default.isReadableFile(atPath: script) else {
            throw ReadDailyClientError.scriptMissing(script)
        }
        return request
    }
}
