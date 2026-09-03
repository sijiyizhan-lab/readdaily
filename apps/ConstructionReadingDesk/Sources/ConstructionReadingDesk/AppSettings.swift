import AppKit
import ConstructionReadingDeskCore
import Foundation
import SwiftUI

@MainActor
final class AppSettings: ObservableObject {
    private enum Key {
        static let repository = "readingDesk.repositoryPath"
        static let archive = "readingDesk.archivePath"
        static let vault = "readingDesk.vaultPath"
        static let weather = "readingDesk.weatherText"
    }

    @Published var repositoryPath: String { didSet { defaults.set(repositoryPath, forKey: Key.repository) } }
    @Published var archivePath: String { didSet { defaults.set(archivePath, forKey: Key.archive) } }
    @Published var vaultPath: String { didSet { defaults.set(vaultPath, forKey: Key.vault) } }
    @Published var weatherText: String { didSet { defaults.set(weatherText, forKey: Key.weather) } }

    private let defaults: UserDefaults

    private static func preferredRepositoryPath(fallback: ReadDailyConfiguration) -> String {
        guard let bundled = Bundle.main.resourceURL?.appendingPathComponent("readdaily", isDirectory: true),
              FileManager.default.isReadableFile(atPath: bundled.appendingPathComponent("scripts/readdaily.py").path)
        else { return fallback.repositoryURL.path }
        return bundled.path
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let detected = ReadDailyConfiguration.detectedDefaults
        let preferredRepository = Self.preferredRepositoryPath(fallback: detected)
        if let savedRepository = defaults.string(forKey: Key.repository),
           FileManager.default.isReadableFile(
               atPath: URL(fileURLWithPath: (savedRepository as NSString).expandingTildeInPath)
                   .appendingPathComponent("scripts/readdaily.py").path
           ) {
            repositoryPath = savedRepository
        } else {
            repositoryPath = preferredRepository
        }
        archivePath = defaults.string(forKey: Key.archive) ?? detected.archiveURL.path
        vaultPath = defaults.string(forKey: Key.vault) ?? detected.vaultURL.path
        weatherText = defaults.string(forKey: Key.weather) ?? ""
    }

    var configuration: ReadDailyConfiguration {
        ReadDailyConfiguration(
            repositoryURL: URL(fileURLWithPath: expanded(repositoryPath), isDirectory: true),
            archiveURL: URL(fileURLWithPath: expanded(archivePath), isDirectory: true),
            vaultURL: URL(fileURLWithPath: expanded(vaultPath), isDirectory: true)
        )
    }

    var values: ReadDailySettingsValues {
        ReadDailySettingsValues(
            repositoryPath: repositoryPath,
            archivePath: archivePath,
            vaultPath: vaultPath,
            weatherText: weatherText
        )
    }

    func apply(_ values: ReadDailySettingsValues) {
        repositoryPath = values.repositoryPath
        archivePath = values.archivePath
        vaultPath = values.vaultPath
        weatherText = values.weatherText
    }

    func detectedDefaultValues() -> ReadDailySettingsValues {
        let detected = ReadDailyConfiguration.detectedDefaults
        return ReadDailySettingsValues(
            repositoryPath: Self.preferredRepositoryPath(fallback: detected),
            archivePath: detected.archiveURL.path,
            vaultPath: detected.vaultURL.path,
            weatherText: ""
        )
    }

    func resetToDetectedDefaults() {
        apply(detectedDefaultValues())
    }

    private func expanded(_ path: String) -> String {
        (path as NSString).expandingTildeInPath
    }
}

struct SettingsPane: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var viewModel: ReadingDeskViewModel
    var onDone: (() -> Void)?
    @State private var draft: ReadDailySettingsValues

    init(
        settings: AppSettings,
        viewModel: ReadingDeskViewModel,
        onDone: (() -> Void)? = nil
    ) {
        self.settings = settings
        self.viewModel = viewModel
        self.onDone = onDone
        _draft = State(initialValue: settings.values)
    }

    var body: some View {
        Form {
            Section("本地路径") {
                pathRow(title: "内置读报引擎（高级）", text: $draft.repositoryPath)
                pathRow(title: "报纸归档目录", text: $draft.archivePath)
                pathRow(title: "Obsidian Vault", text: $draft.vaultPath)
            }
            Section("今日信息") {
                TextField("本地天气摘要（可选）", text: $draft.weatherText)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("本地天气摘要")
                Text("应用不会联网查询天气；留空时明确显示“天气未配置”。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("安全边界") {
                Label("应用只调用 Python 后端发布；Swift 客户端不会直接写入 Vault。", systemImage: "lock.shield")
                    .foregroundStyle(.secondary)
                Label("OCR、缓存、日志与事务快照保留在归档目录，不进入知识库。", systemImage: "externaldrive")
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("恢复默认路径") { draft = settings.detectedDefaultValues() }
                    .controlSize(.large)
                Spacer()
                Text(draft == settings.values ? "设置已应用" : "有未应用更改")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel(draft == settings.values ? "设置已应用" : "有未应用的设置更改")
                Button(onDone == nil ? "放弃更改" : "取消") {
                    draft = settings.values
                    onDone?()
                }
                .controlSize(.large)
                Button("应用") {
                    if viewModel.applySettings(draft) { onDone?() }
                }
                .keyboardShortcut(.defaultAction)
                .controlSize(.large)
                .disabled(draft == settings.values || viewModel.isEditorialBusy)
            }
        }
        .formStyle(.grouped)
        .padding()
        .frame(minWidth: 660, minHeight: 430)
    }

    @ViewBuilder
    private func pathRow(title: String, text: Binding<String>) -> some View {
        HStack(spacing: 12) {
            TextField(title, text: text)
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel(title)
            Button("选择…") {
                let panel = NSOpenPanel()
                panel.title = "选择\(title)"
                panel.canChooseDirectories = true
                panel.canChooseFiles = false
                panel.allowsMultipleSelection = false
                if panel.runModal() == .OK, let url = panel.url {
                    text.wrappedValue = url.path
                }
            }
            .controlSize(.large)
            .accessibilityLabel("选择\(title)")
        }
    }
}
