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
    }

    @Published var repositoryPath: String { didSet { defaults.set(repositoryPath, forKey: Key.repository) } }
    @Published var archivePath: String { didSet { defaults.set(archivePath, forKey: Key.archive) } }
    @Published var vaultPath: String { didSet { defaults.set(vaultPath, forKey: Key.vault) } }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let detected = ReadDailyConfiguration.detectedDefaults
        repositoryPath = defaults.string(forKey: Key.repository) ?? detected.repositoryURL.path
        archivePath = defaults.string(forKey: Key.archive) ?? detected.archiveURL.path
        vaultPath = defaults.string(forKey: Key.vault) ?? detected.vaultURL.path
    }

    var configuration: ReadDailyConfiguration {
        ReadDailyConfiguration(
            repositoryURL: URL(fileURLWithPath: expanded(repositoryPath), isDirectory: true),
            archiveURL: URL(fileURLWithPath: expanded(archivePath), isDirectory: true),
            vaultURL: URL(fileURLWithPath: expanded(vaultPath), isDirectory: true)
        )
    }

    func resetToDetectedDefaults() {
        let detected = ReadDailyConfiguration.detectedDefaults
        repositoryPath = detected.repositoryURL.path
        archivePath = detected.archiveURL.path
        vaultPath = detected.vaultURL.path
    }

    private func expanded(_ path: String) -> String {
        (path as NSString).expandingTildeInPath
    }
}

struct SettingsPane: View {
    @ObservedObject var settings: AppSettings
    var onDone: (() -> Void)?

    var body: some View {
        Form {
            Section("本地路径") {
                pathRow(title: "readdaily 仓库", text: $settings.repositoryPath)
                pathRow(title: "报纸归档目录", text: $settings.archivePath)
                pathRow(title: "Obsidian Vault", text: $settings.vaultPath)
            }
            Section("安全边界") {
                Label("应用只调用 Python 后端发布；Swift 客户端不会直接写入 Vault。", systemImage: "lock.shield")
                    .foregroundStyle(.secondary)
                Label("OCR、缓存、日志与事务快照保留在归档目录，不进入知识库。", systemImage: "externaldrive")
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button("恢复默认路径") { settings.resetToDetectedDefaults() }
                    .controlSize(.large)
                Spacer()
                if let onDone {
                    Button("完成") { onDone() }
                        .keyboardShortcut(.defaultAction)
                        .controlSize(.large)
                }
            }
        }
        .formStyle(.grouped)
        .padding()
        .frame(minWidth: 660, minHeight: 390)
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
