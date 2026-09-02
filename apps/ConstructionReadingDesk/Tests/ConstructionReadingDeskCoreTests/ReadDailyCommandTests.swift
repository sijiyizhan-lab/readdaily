import Foundation
import Testing
@testable import ConstructionReadingDeskCore

@Suite("后端进程命令")
struct ReadDailyCommandTests {
    private let configuration = ReadDailyConfiguration(
        repositoryURL: URL(fileURLWithPath: "/Users/test/readdaily"),
        archiveURL: URL(fileURLWithPath: "/Users/test/Library/Application Support/readdaily/news-archive"),
        vaultURL: URL(fileURLWithPath: "/Users/test/Maitty的知识库"),
        pythonExecutableURL: URL(fileURLWithPath: "/opt/custom/bin/python3")
    )

    @Test("capabilities 严格使用 API 契约并分隔含空格路径")
    func capabilitiesUsesExactAPIContractAndKeepsPathsAsSeparateArguments() throws {
        let request = try ReadDailyCommandFactory(configuration: configuration).make(.capabilities)

        #expect(request.executableURL.path == "/opt/custom/bin/python3")
        #expect(request.arguments == [
            "/Users/test/readdaily/scripts/readdaily.py", "api", "capabilities",
            "--archive", "/Users/test/Library/Application Support/readdaily/news-archive",
            "--vault", "/Users/test/Maitty的知识库",
        ])
        #expect(request.environment["PYTHONDONTWRITEBYTECODE"] == "1")
    }

    @Test("issue 附加来源与日期")
    func issueAddsSourceAndDateAfterSharedPaths() throws {
        let request = try ReadDailyCommandFactory(configuration: configuration).make(
            .issue(source: "zgjsb", date: "2026-09-01")
        )

        #expect(Array(request.arguments.suffix(4)) == ["--source", "zgjsb", "--date", "2026-09-01"])
    }

    @Test("Read Daily 收件箱始终限定中国建设报")
    func constructionInboxAddsSourceScope() throws {
        let request = try ReadDailyCommandFactory(configuration: configuration).make(
            .inbox(source: "zgjsb")
        )

        #expect(Array(request.arguments.suffix(2)) == ["--source", "zgjsb"])
    }

    @Test("draft-save 通过临时输入文件传参")
    func draftSaveUsesInputFileInsteadOfInlineJSONOrVaultWrite() throws {
        let input = URL(fileURLWithPath: "/private/tmp/readdaily-draft.json")

        let request = try ReadDailyCommandFactory(configuration: configuration).make(.draftSave(inputFile: input))

        #expect(Array(request.arguments.suffix(2)) == ["--input", input.path])
        #expect(!request.arguments.contains(where: { $0.contains("\"summary\"") }))
    }

    @Test("发布与回滚使用不透明标识")
    func publishAndRollbackUseOpaqueIdentifiers() throws {
        let factory = ReadDailyCommandFactory(configuration: configuration)

        let publish = try factory.make(.publishApply(planID: "plan-123"))
        let rollback = try factory.make(.rollback(transactionID: "tx-456"))

        #expect(Array(publish.arguments.suffix(2)) == ["--plan-id", "plan-123"])
        #expect(Array(rollback.arguments.suffix(2)) == ["--transaction-id", "tx-456"])
    }

    @Test("阅读状态记录使用来源日期与明确状态")
    func readingMarkUsesSourceDateAndStatus() throws {
        let request = try ReadDailyCommandFactory(configuration: configuration).make(
            .readingMark(source: "rmrb", date: "2026-09-03", status: .completed)
        )

        #expect(Array(request.arguments.suffix(6)) == [
            "--source", "rmrb", "--date", "2026-09-03", "--status", "completed",
        ])
    }

    @Test("导入命令保留 PDF 路径、日期与来源")
    func importKeepsPDFPathDateAndSourceAsTypedArguments() throws {
        let factory = ReadDailyCommandFactory(configuration: configuration)
        let pdf = URL(fileURLWithPath: "/Users/test/Inbox/中国建设报 9月1日.pdf")

        let request = try factory.make(.importFile(path: pdf, date: "2026-09-01", source: "zgjsb"))

        #expect(Array(request.arguments.suffix(6)) == [
            "--path", pdf.path, "--date", "2026-09-01", "--source", "zgjsb",
        ])
    }

    @Test("抓取当日八报只传日期不限定单一来源")
    func dailyFetchUsesDateWithoutSourceFilter() throws {
        let request = try ReadDailyCommandFactory(configuration: configuration).make(
            .fetchDaily(date: "2026-09-03")
        )

        #expect(request.arguments == [
            "/Users/test/readdaily/scripts/readdaily.py", "fetch", "--date", "2026-09-03",
        ])
        #expect(!request.arguments.contains("--source"))
    }
}
