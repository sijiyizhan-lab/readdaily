import SwiftUI

@main
struct ConstructionReadingDeskApp: App {
    @StateObject private var settings: AppSettings
    @StateObject private var viewModel: ReadingDeskViewModel

    init() {
        let settings = AppSettings()
        _settings = StateObject(wrappedValue: settings)
        _viewModel = StateObject(wrappedValue: ReadingDeskViewModel(settings: settings))
    }

    var body: some Scene {
        WindowGroup("Read Daily") {
            ReadingDeskRootView(settings: settings, viewModel: viewModel)
                .frame(minWidth: 980, minHeight: 760)
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unifiedCompact(showsTitle: true))
        .commands {
            CommandGroup(after: .newItem) {
                Button("添加 PDF…") { NotificationCenter.default.post(name: .readingDeskImportPDF, object: nil) }
                    .keyboardShortcut("o", modifiers: .command)
                    .disabled(viewModel.isEditorialBusy)
                Button("刷新读报台") { viewModel.refresh() }
                    .keyboardShortcut("r", modifiers: .command)
                    .disabled(viewModel.isEditorialBusy)
                Divider()
                Button("保存整期草稿") { viewModel.saveDraft() }
                    .keyboardShortcut("s", modifiers: .command)
                    .disabled(
                        viewModel.isEditorialBusy || viewModel.isIssueLoading
                            || viewModel.issueDetail == nil || !viewModel.hasUnsavedChanges
                    )
                Button("预览发布") { viewModel.previewPublish() }
                    .keyboardShortcut("p", modifiers: [.command, .shift])
                    .disabled(
                        viewModel.isEditorialBusy || viewModel.isIssueLoading
                            || !viewModel.canPublishSelectedIssue
                    )
            }
        }

        Settings {
            SettingsPane(settings: settings, viewModel: viewModel)
        }
    }
}

extension Notification.Name {
    static let readingDeskImportPDF = Notification.Name("readingDeskImportPDF")
}
