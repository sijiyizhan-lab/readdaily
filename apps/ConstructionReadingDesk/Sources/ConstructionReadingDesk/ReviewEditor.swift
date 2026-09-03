import AppKit
import ConstructionReadingDeskCore
import SwiftUI

struct ReviewEditor: View {
    @ObservedObject var settings: AppSettings
    @ObservedObject var viewModel: ReadingDeskViewModel
    let layoutMode: ReadingWorkspaceMode

    var body: some View {
        ZStack {
            ReadingDeskBackground()
            if let issue = viewModel.issueDetail,
               let edition = viewModel.selectedEdition,
               let draft = viewModel.editorState?.draft {
                VStack(spacing: 0) {
                    DailyBannerCarousel(
                        date: viewModel.selectedDate ?? issue.date,
                        day: viewModel.dashboardDay,
                        weather: LocalWeatherSummary(configuredText: settings.weatherText),
                        sourceName: issue.sourceName,
                        readCount: viewModel.displayedReadCount,
                        compact: layoutMode == .stacked
                    )
                    .padding(.horizontal, 20)
                    .padding(.top, 16)
                    .padding(.bottom, 12)

                    workspace(issue: issue, edition: edition, draft: draft)
                        .disabled(viewModel.isBusy)
                }
                .groupBoxStyle(ReadingDeskGroupBoxStyle())
            } else if viewModel.isIssueLoading {
                VStack(spacing: 18) {
                    DailyBannerCarousel(
                        date: viewModel.selectedDate,
                        day: viewModel.dashboardDay,
                        weather: LocalWeatherSummary(configuredText: settings.weatherText),
                        sourceName: viewModel.selectedIssue?.sourceName,
                        readCount: viewModel.displayedReadCount,
                        compact: layoutMode == .stacked
                    )
                    VStack(spacing: 12) {
                        ProgressView().controlSize(.regular)
                        Text("正在读取整期报纸").font(.headline)
                        Text(viewModel.selectedIssue?.sourceName ?? "正在准备原版与 OCR 证据…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .readingDeskCard(cool: true)
                }
                .padding(20)
            } else {
                VStack(spacing: 18) {
                    DailyBannerCarousel(
                        date: viewModel.selectedDate,
                        day: viewModel.dashboardDay,
                        weather: LocalWeatherSummary(configuredText: settings.weatherText),
                        sourceName: nil,
                        readCount: viewModel.displayedReadCount,
                        compact: layoutMode == .stacked
                    )
                    EmptyState(
                        title: "选择一个版次开始阅读",
                        detail: "左侧选择日期和报纸，中栏选择版次；原版与 OCR 证据会在这里上下呈现。",
                        symbol: "doc.text.magnifyingglass"
                    )
                    .readingDeskCard(cool: true)
                }
                .padding(20)
            }
        }
    }

    @ViewBuilder
    private func workspace(issue: IssueDetail, edition: EditionRecord, draft: ArticleDraft) -> some View {
        switch layoutMode {
        case .sideBySide:
            HSplitView {
                EvidenceWorkspace(issue: issue, edition: edition, draft: draft, viewModel: viewModel)
                    .frame(minWidth: 500, idealWidth: 680, maxWidth: .infinity)
                SummaryWorkspace(issue: issue, edition: edition, draft: draft, viewModel: viewModel)
                    .frame(minWidth: 340, idealWidth: 410, maxWidth: 480)
            }
            .accessibilityLabel("原版、OCR 与摘要并排工作区")
        case .stacked:
            VSplitView {
                EvidenceWorkspace(issue: issue, edition: edition, draft: draft, viewModel: viewModel)
                    .frame(minWidth: 0, maxWidth: .infinity, minHeight: 320)
                SummaryWorkspace(issue: issue, edition: edition, draft: draft, viewModel: viewModel)
                    .frame(minWidth: 0, maxWidth: .infinity, minHeight: 240)
            }
            .accessibilityLabel("原版、OCR 与摘要上下工作区")
        }
    }
}

private struct EvidenceWorkspace: View {
    let issue: IssueDetail
    let edition: EditionRecord
    let draft: ArticleDraft
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("第\(edition.pageNumber ?? 0)版 · \(edition.title)")
                            .font(.title2.weight(.bold))
                        Text("\(issue.sourceName) · \(issue.date) · OCR \(draft.ocrText.count) 字")
                            .font(.subheadline).foregroundStyle(.secondary)
                    }
                    Spacer()
                    OCRStatusBadge(status: draft.ocrReviewStatus)
                }
                .readingDeskCard(padding: 16, cool: true)

                GroupBox {
                    PageEvidencePreview(edition: edition)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .overlay { RoundedRectangle(cornerRadius: 8).stroke(ReadingDeskTheme.border) }
                } label: {
                    HStack {
                        Label("原版图", systemImage: "doc.richtext")
                        Spacer()
                        Label("点击可缩放", systemImage: "arrow.up.left.and.arrow.down.right")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                OCRProofreadingCard(draft: draft, warnings: issue.warnings, viewModel: viewModel)
            }
            .padding(.leading, 20)
            .padding(.trailing, 12)
            .padding(.bottom, 20)
        }
        .accessibilityLabel("原版与 OCR 证据区")
    }
}

private struct OCRProofreadingCard: View {
    let draft: ArticleDraft
    let warnings: [String]
    @ObservedObject var viewModel: ReadingDeskViewModel

    private var layout: OCRDocumentLayout { OCRDocumentLayout(text: draft.ocrText) }

    private var displayBlocks: [OCRContentBlock] {
        if !draft.ocrBlocks.isEmpty { return draft.ocrBlocks }
        return layout.paragraphs.map {
            OCRContentBlock(kind: "paragraph", text: $0.lines.joined(separator: "\n"))
        }
    }

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 12) {
                    Label("原始 OCR（只读）", systemImage: "lock.fill")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                    if displayBlocks.isEmpty {
                        Text("当前版次没有可用 OCR 原文。")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(Array(displayBlocks.enumerated()), id: \.offset) { index, block in
                            VStack(alignment: .leading, spacing: 6) {
                                if let title = block.title, !title.isEmpty {
                                    Text(title)
                                        .font(.headline)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                }
                                Text(block.text.isEmpty ? " " : block.text)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .padding(.bottom, 8)
                            .accessibilityElement(children: .combine)
                            .accessibilityLabel(
                                "OCR 结构块 \(index + 1)，\(block.kind == "article" ? "文章" : "段落")"
                                + (block.title.map { "，标题 \($0)" } ?? "")
                                + "，\(block.text)"
                            )
                        }
                    }
                }
                .font(.body)
                .lineSpacing(4)
                .textSelection(.enabled)
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(ReadingDeskTheme.field, in: RoundedRectangle(cornerRadius: 8))
                .overlay { RoundedRectangle(cornerRadius: 8).stroke(ReadingDeskTheme.border) }
                .accessibilityLabel("原始 OCR，只读，共 \(draft.ocrText.count) 字")

                Divider()

                HStack {
                    Label("校对编辑", systemImage: "pencil.and.outline")
                        .font(.headline)
                    Spacer()
                    Picker("校对状态", selection: Binding(
                        get: { viewModel.editorState?.draft.ocrReviewStatus ?? .unreviewed },
                        set: viewModel.setOCRReviewStatus
                    )) {
                        ForEach(OCRReviewStatus.allCases) { status in
                            Label(status.label, systemImage: status.symbolName).tag(status)
                        }
                    }
                    .frame(width: 180)
                    .accessibilityLabel("OCR 校对状态")
                    Button("恢复原文") { viewModel.restoreOriginalOCR() }
                        .disabled(draft.proofreadText == draft.ocrText)
                }

                TextEditor(text: Binding(
                    get: { viewModel.editorState?.draft.proofreadText ?? "" },
                    set: viewModel.updateProofreadText
                ))
                .font(.body)
                .lineSpacing(4)
                .frame(minHeight: 230)
                .padding(8)
                .scrollContentBackground(.hidden)
                .background(ReadingDeskTheme.field, in: RoundedRectangle(cornerRadius: 8))
                .overlay { RoundedRectangle(cornerRadius: 8).stroke(ReadingDeskTheme.border) }
                .accessibilityLabel("OCR 校对文本")

                suspicionEditor

                if !warnings.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Label("上下文提示", systemImage: "exclamationmark.triangle.fill")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(ReadingDeskTheme.statusAttention)
                        ForEach(Array(warnings.prefix(6).enumerated()), id: \.offset) { _, warning in
                            Text("• \(warning)").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        } label: {
            HStack {
                Label("OCR 原文与校对", systemImage: "text.viewfinder")
                Spacer()
                Text("\(draft.ocrText.count) 字").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
    }

    private var suspicionEditor: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("疑点记录", systemImage: "questionmark.bubble")
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Button { viewModel.addOCRSuspicion() } label: { Label("添加疑点", systemImage: "plus") }
            }
            ForEach(Array(draft.ocrSuspicions.indices), id: \.self) { index in
                HStack {
                    TextField("记录需要回看原版的位置或内容", text: Binding(
                        get: {
                            guard let values = viewModel.editorState?.draft.ocrSuspicions,
                                  values.indices.contains(index) else { return "" }
                            return values[index]
                        },
                        set: { viewModel.updateOCRSuspicion(at: index, value: $0) }
                    ))
                    Button(role: .destructive) { viewModel.removeOCRSuspicion(at: index) } label: {
                        Image(systemName: "trash")
                    }
                    .accessibilityLabel("删除第 \(index + 1) 条 OCR 疑点")
                }
            }
        }
    }
}

private struct SummaryWorkspace: View {
    let issue: IssueDetail
    let edition: EditionRecord
    let draft: ArticleDraft
    @ObservedObject var viewModel: ReadingDeskViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Label("摘要工作栏", systemImage: "sidebar.right")
                        .font(.title3.weight(.bold))
                    Spacer()
                    if viewModel.dirtyUnitIDs.contains(edition.id) {
                        Label("未保存", systemImage: "circle.fill")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(ReadingDeskTheme.statusAttention)
                    }
                }

                summaryEditor
                topicEditor
                factEditor
                importanceEditor
                readingActions
                publishingActions
            }
            .padding(.leading, 12)
            .padding(.trailing, 20)
            .padding(.bottom, 20)
        }
        .accessibilityLabel("摘要与发布工作栏")
    }

    private var summaryEditor: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                TextField("中文标题", text: Binding(
                    get: { viewModel.editorState?.draft.title ?? "" },
                    set: viewModel.updateTitle
                ))
                .textFieldStyle(.roundedBorder)
                .accessibilityLabel("知识卡片中文标题")
                TextEditor(text: Binding(
                    get: { viewModel.editorState?.draft.summary ?? "" },
                    set: viewModel.updateSummary
                ))
                .frame(minHeight: 150)
                .padding(7)
                .scrollContentBackground(.hidden)
                .background(ReadingDeskTheme.field, in: RoundedRectangle(cornerRadius: 8))
                .overlay { RoundedRectangle(cornerRadius: 8).stroke(ReadingDeskTheme.border) }
                .accessibilityLabel("中文摘要")
                HStack {
                    Text("保留主体、动作、数字和来源")
                    Spacer()
                    Text("\(draft.summary.count) 字").monospacedDigit()
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        } label: { Label("中文摘要", systemImage: "text.alignleft") }
    }

    private var topicEditor: some View {
        GroupBox {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 8)], alignment: .leading, spacing: 8) {
                ForEach(ReadingTopic.allCases) { topic in
                    Button { viewModel.toggleTopic(topic) } label: {
                        Label(topic.rawValue, systemImage: draft.topics.contains(topic) ? "checkmark.circle.fill" : "circle")
                            .font(.caption.weight(.medium))
                            .foregroundStyle(draft.topics.contains(topic) ? ReadingDeskTheme.accentText : .primary)
                            .padding(.horizontal, 10).frame(minHeight: 38)
                            .background(draft.topics.contains(topic) ? ReadingDeskTheme.accentSoft : ReadingDeskTheme.field, in: Capsule())
                            .overlay { Capsule().stroke(draft.topics.contains(topic) ? ReadingDeskTheme.accent : ReadingDeskTheme.border) }
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(draft.topics.contains(topic) ? "已选择" : "未选择")
                }
            }
        } label: { Label("知识主题", systemImage: "tag") }
    }

    private var factEditor: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(Array(draft.facts.indices), id: \.self) { index in
                    VStack(alignment: .leading, spacing: 7) {
                        HStack {
                            Text("事实 \(index + 1)").font(.caption.weight(.semibold))
                            Spacer()
                            Button(role: .destructive) { viewModel.removeFact(at: index) } label: {
                                Image(systemName: "trash")
                            }
                            .accessibilityLabel("删除第 \(index + 1) 条事实")
                        }
                        factField("主体", index: index, keyPath: \FactFields.subject)
                        factField("动作", index: index, keyPath: \FactFields.action)
                        factField("对象", index: index, keyPath: \FactFields.object)
                        HStack {
                            factField("数值", index: index, keyPath: \FactFields.value)
                            factField("单位", index: index, keyPath: \FactFields.unit)
                        }
                        factField("时间", index: index, keyPath: \FactFields.time)
                        factField("来源", index: index, keyPath: \FactFields.source)
                    }
                    .readingDeskCard(padding: 10, cool: true)
                }
                Button { viewModel.addFact() } label: { Label("添加事实", systemImage: "plus.circle") }
                    .frame(minHeight: 44)
            }
        } label: { Label("事实字段", systemImage: "checklist") }
    }

    private func factField(
        _ label: String,
        index: Int,
        keyPath: WritableKeyPath<FactFields, String>
    ) -> some View {
        TextField(label, text: Binding(
            get: {
                guard let facts = viewModel.editorState?.draft.facts,
                      facts.indices.contains(index) else { return "" }
                return facts[index][keyPath: keyPath]
            },
            set: { viewModel.updateFact(at: index, keyPath, value: $0) }
        ))
        .textFieldStyle(.roundedBorder)
        .accessibilityLabel("事实 \(index + 1) \(label)")
    }

    private var importanceEditor: some View {
        GroupBox {
            Picker("重要性", selection: Binding(
                get: { viewModel.editorState?.draft.importance ?? 3 },
                set: viewModel.setImportance
            )) {
                ForEach(1...5, id: \.self) { value in Text("\(value)").tag(value) }
            }
            .pickerStyle(.segmented)
            .accessibilityValue("\(draft.importance)级")
        } label: { Label("重要性", systemImage: "chart.bar.fill") }
    }

    private var readingActions: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Label(
                    viewModel.selectedReadingStatus.accessibleLabel,
                    systemImage: viewModel.selectedReadingStatus.symbolName
                )
                .foregroundStyle(
                    viewModel.selectedReadingStatus == .completed
                        ? ReadingDeskTheme.statusPositive
                        : ReadingDeskTheme.statusAttention
                )
                if viewModel.selectedReadingStatus == .completed {
                    Button("撤销今日完成") { viewModel.markSelectedIssue(.unread) }
                        .frame(maxWidth: .infinity, minHeight: 44)
                } else {
                    Button { viewModel.markSelectedIssue(.completed) } label: {
                        Label("标记今日已读", systemImage: "checkmark.circle.fill")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(ReadingDeskTheme.statusPositive)
                    .frame(minHeight: 44)
                }
            }
        } label: { Label("阅读记录", systemImage: "book") }
    }

    private var publishingActions: some View {
        GroupBox {
            VStack(spacing: 10) {
                Button { viewModel.saveDraft() } label: {
                    Label("保存整期草稿", systemImage: "square.and.arrow.down")
                        .frame(maxWidth: .infinity)
                }
                .frame(minHeight: 44)
                .disabled(viewModel.isBusy || !viewModel.hasUnsavedChanges)

                Button { viewModel.previewPublish() } label: {
                    Label("预览发布", systemImage: "doc.text.magnifyingglass")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(ReadingDeskTheme.accent)
                .frame(minHeight: 44)
                .disabled(viewModel.isBusy || !viewModel.canPublishSelectedIssue)

                Text(viewModel.canPublishSelectedIssue
                     ? "仅在确认差异后由 Python 后端写入 Obsidian。"
                     : "当前报纸可阅读、校对和保存；目前仅中国建设报支持发布。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } label: { Label("保存与发布", systemImage: "lock.shield") }
    }
}

private struct OCRStatusBadge: View {
    let status: OCRReviewStatus

    var body: some View {
        Label(status.label, systemImage: status.symbolName)
            .font(.caption.weight(.semibold))
            .foregroundStyle(
                status == .confirmed
                    ? ReadingDeskTheme.statusPositive
                    : ReadingDeskTheme.statusAttention
            )
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(ReadingDeskTheme.card, in: Capsule())
            .accessibilityLabel("OCR 校对状态：\(status.label)")
    }
}

private struct DailyBannerCarousel: View {
    let date: String?
    let day: DailyReadingDay?
    let weather: LocalWeatherSummary
    let sourceName: String?
    let readCount: Int
    let compact: Bool
    @State private var index = 0
    @State private var hoverPaused = false
    @State private var userPaused = false
    @FocusState private var focusedControl: FocusTarget?
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private enum FocusTarget: Hashable {
        case previous
        case pause
        case next
    }

    private let slides = ["今日工作台", "八报进度", "本地天气"]
    private let artwork = ["banner-morning-city", "banner-reading-desk", "banner-weather"]

    var body: some View {
        ZStack {
            BannerArtwork(name: artwork[index])
            LinearGradient(
                colors: reduceTransparency ? [.black.opacity(0.48), .black.opacity(0.48)] : [.black.opacity(0.5), .clear, .black.opacity(0.18)],
                startPoint: .leading,
                endPoint: .trailing
            )
            if compact {
                VStack(alignment: .leading, spacing: 4) {
                    bannerSummary(iconSize: 38, iconFontSize: 22, spacing: 10)
                    carouselControls
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            } else {
                HStack(spacing: 16) {
                    bannerSummary(iconSize: 54, iconFontSize: 30, spacing: 16)
                    Spacer()
                    carouselControls
                }
                .padding(.horizontal, 18)
            }
        }
        .frame(maxWidth: .infinity)
        .frame(height: compact ? 128 : 84)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay { RoundedRectangle(cornerRadius: 14).stroke(ReadingDeskTheme.border) }
        .onHover { hoverPaused = $0 }
        .task(id: "\(reduceMotion)-\(userPaused)-\(hoverPaused)-\(focusedControl != nil)") {
            guard CarouselPlaybackPolicy.shouldAutoAdvance(
                reduceMotion: reduceMotion,
                isUserPaused: userPaused,
                isHovering: hoverPaused,
                hasKeyboardFocus: focusedControl != nil
            ) else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 8_000_000_000) }
                catch { return }
                guard !Task.isCancelled else { return }
                showSlide(index + 1)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("信息横幅")
    }

    private func bannerSummary(iconSize: CGFloat, iconFontSize: CGFloat, spacing: CGFloat) -> some View {
        HStack(spacing: spacing) {
            Image(systemName: symbol)
                .font(.system(size: iconFontSize, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: iconSize, height: iconSize)
                .background(Color.black.opacity(0.2), in: RoundedRectangle(cornerRadius: 11))
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                Text(slides[index])
                    .font(.title3.weight(.bold))
                    .foregroundStyle(.white)
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.white.opacity(0.94))
                    .lineLimit(2)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(slides[index])：\(message)")
        .accessibilityValue("第 \(index + 1) 张，共 \(slides.count) 张")
    }

    private var carouselControls: some View {
        HStack(spacing: 7) {
            Button { showSlide(index + slides.count - 1) } label: {
                Image(systemName: "chevron.left")
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .focused($focusedControl, equals: .previous)
            .accessibilityLabel("上一张信息横幅")

            Text("\(index + 1)/\(slides.count)")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.white)
                .accessibilityHidden(true)

            Button { userPaused.toggle() } label: {
                Image(systemName: isEffectivelyPaused ? "play.fill" : "pause.fill")
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .focused($focusedControl, equals: .pause)
            .disabled(reduceMotion)
            .accessibilityLabel(pauseControlLabel)
            .accessibilityValue(isEffectivelyPaused ? "已暂停" : "播放中")

            Button { showSlide(index + 1) } label: {
                Image(systemName: "chevron.right")
                    .frame(width: 32, height: 32)
                    .contentShape(Rectangle())
            }
            .focused($focusedControl, equals: .next)
            .accessibilityLabel("下一张信息横幅")
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white)
        .padding(8)
        .background(Color.black.opacity(0.22), in: Capsule())
    }

    private var symbol: String {
        switch index {
        case 1: return "chart.bar.doc.horizontal"
        case 2: return "cloud.sun"
        default: return "sunrise.fill"
        }
    }

    private var message: String {
        switch index {
        case 1:
            guard let day else { return "等待读报数据；缺报会明确标记为当日未获取。" }
            return "已获取 \(day.completedCount)/8 份 · 已读完 \(readCount)/8 份"
        case 2:
            return weather.displayText
        default:
            return "\(date ?? "暂无日期") · \(sourceName ?? "选择报纸开始阅读")"
        }
    }

    private var isEffectivelyPaused: Bool {
        reduceMotion || userPaused
    }

    private var pauseControlLabel: String {
        if reduceMotion { return "自动轮播已因减少动态效果停止" }
        return userPaused ? "继续自动轮播" : "暂停自动轮播"
    }

    private func showSlide(_ requestedIndex: Int) {
        let nextIndex = (requestedIndex + slides.count) % slides.count
        if reduceMotion {
            index = nextIndex
        } else {
            withAnimation(.easeInOut(duration: 0.35)) {
                index = nextIndex
            }
        }
    }
}

private struct BannerArtwork: View {
    let name: String

    var body: some View {
        GeometryReader { proxy in
            if let url = ReadDailyResource.url(forResource: name, withExtension: "svg"),
               let image = NSImage(contentsOf: url) {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(width: proxy.size.width, height: proxy.size.height)
                    .clipped()
                    .accessibilityHidden(true)
            } else {
                ReadingDeskTheme.bannerStart
            }
        }
    }
}
