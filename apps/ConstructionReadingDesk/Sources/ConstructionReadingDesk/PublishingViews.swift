import ConstructionReadingDeskCore
import SwiftUI

struct PublishPreviewSheet: View {
    let plan: PublishPlan
    let isBusy: Bool
    let onCancel: () -> Void
    let onConfirm: () -> Void
    @State private var selectedChangeID: String?
    @State private var confirming = false

    private var selectedChange: PublishChange? {
        plan.changes.first { $0.id == selectedChangeID } ?? plan.changes.first
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("发布预览")
                        .font(.title2.weight(.bold))
                    Text("确认前请检查每个文件及统一差异。当前尚未写入 Obsidian。")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Label("\(plan.changes.count) 个文件", systemImage: "doc.on.doc")
                    .foregroundStyle(.secondary)
            }
            .padding(20)

            if !plan.warnings.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(Array(plan.warnings.enumerated()), id: \.offset) { _, warning in
                        Label(warning, systemImage: "exclamationmark.triangle.fill")
                    }
                }
                .font(.caption)
                .foregroundStyle(.orange)
                .padding(.horizontal, 20)
                .padding(.bottom, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            Divider()
            HSplitView {
                List(selection: $selectedChangeID) {
                    ForEach(plan.changes) { change in
                        HStack(spacing: 10) {
                            Image(systemName: change.changeType == "新增" ? "doc.badge.plus" : "doc.badge.ellipsis")
                                .foregroundStyle(change.changeType == "新增" ? .green : .blue)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(change.path).lineLimit(2)
                                Text(change.changeType)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 4)
                        .tag(Optional(change.id))
                    }
                }
                .frame(minWidth: 320, idealWidth: 390)

                VStack(alignment: .leading, spacing: 8) {
                    Text(selectedChange?.path ?? "选择文件")
                        .font(.headline)
                        .lineLimit(2)
                    Divider()
                    ScrollView([.horizontal, .vertical]) {
                        Text(selectedChange?.diff.isEmpty == false ? selectedChange?.diff ?? "" : "该文件没有可显示的文本差异。")
                            .font(.system(size: 12, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .topLeading)
                            .padding(10)
                    }
                    .background(Color(nsColor: .textBackgroundColor))
                }
                .padding(14)
                .frame(minWidth: 500, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }

            Divider()
            HStack {
                Label("发布后会生成可回滚事务", systemImage: "arrow.uturn.backward.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("取消", action: onCancel)
                    .keyboardShortcut(.cancelAction)
                    .controlSize(.large)
                    .disabled(isBusy)
                Button("确认发布") { confirming = true }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(isBusy || plan.changes.isEmpty)
                    .accessibilityLabel("确认发布到 Obsidian")
            }
            .padding(16)
        }
        .frame(minWidth: 980, minHeight: 680)
        .onAppear { selectedChangeID = plan.changes.first?.id }
        .confirmationDialog(
            "确认发布这 \(plan.changes.count) 个文件？",
            isPresented: $confirming,
            titleVisibility: .visible
        ) {
            Button("确认发布到 Obsidian", action: onConfirm)
            Button("继续检查", role: .cancel) {}
        } message: {
            Text("只会写入预览列出的文件；发布后可在历史记录中回滚。")
        }
    }
}

struct HistorySheet: View {
    let transactions: [HistoryTransaction]
    let isBusy: Bool
    let onRefresh: () -> Void
    let onRollback: (String) -> Void
    let onClose: () -> Void
    @State private var pendingRollback: HistoryTransaction?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("发布历史")
                        .font(.title2.weight(.bold))
                    Text("回滚会恢复发布前快照；文件若已被手工修改，后端会拒绝覆盖。")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(action: onRefresh) { Label("刷新", systemImage: "arrow.clockwise") }
                    .controlSize(.large)
                    .disabled(isBusy)
            }
            .padding(20)
            Divider()

            if transactions.isEmpty && !isBusy {
                EmptyState(
                    title: "暂无发布记录",
                    detail: "完成一次确认发布后，这里会出现可审计事务。",
                    symbol: "clock"
                )
            } else {
                List(transactions) { transaction in
                    HStack(spacing: 14) {
                        Image(systemName: transaction.canRollback ? "checkmark.seal.fill" : "arrow.uturn.backward.circle.fill")
                            .font(.title3)
                            .foregroundStyle(transaction.canRollback ? .green : .secondary)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(transaction.summary).font(.headline)
                            Text(transaction.date).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if transaction.canRollback {
                            Button("回滚…", role: .destructive) { pendingRollback = transaction }
                                .controlSize(.large)
                                .disabled(isBusy)
                                .accessibilityLabel("回滚 \(transaction.summary)")
                        } else {
                            Text("已回滚")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 7)
                }
            }

            Divider()
            HStack {
                if isBusy { ProgressView().controlSize(.small) }
                Spacer()
                Button("关闭", action: onClose)
                    .keyboardShortcut(.cancelAction)
                    .controlSize(.large)
                    .disabled(isBusy)
            }
            .padding(16)
        }
        .frame(minWidth: 760, minHeight: 520)
        .confirmationDialog(
            "确认回滚这次发布？",
            isPresented: Binding(
                get: { pendingRollback != nil },
                set: { if !$0 { pendingRollback = nil } }
            ),
            titleVisibility: .visible
        ) {
            if let transaction = pendingRollback {
                Button("回滚发布", role: .destructive) {
                    pendingRollback = nil
                    onRollback(transaction.id)
                }
            }
            Button("取消", role: .cancel) { pendingRollback = nil }
        } message: {
            Text("该操作会恢复发布前的知识库文件；发生内容冲突时不会强制覆盖。")
        }
    }
}
