import AppKit
import Combine
import ConstructionReadingDeskCore
import ImageIO
import PDFKit
import SwiftUI

enum ReadDailyResource {
    static func url(forResource name: String, withExtension extensionName: String) -> URL? {
        Bundle.main.url(forResource: name, withExtension: extensionName)
    }
}

private actor PageImageRepository {
    static let shared = PageImageRepository()

    private let cache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.totalCostLimit = 192 * 1_024 * 1_024
        cache.countLimit = 48
        return cache
    }()

    func image(imagePath: String?, pdfPath: String?, pageIndex: Int, targetPixels: Int) -> NSImage? {
        guard let source = imagePath ?? pdfPath else { return nil }
        let modified = ((try? FileManager.default.attributesOfItem(atPath: source)[.modificationDate]) as? Date)?.timeIntervalSince1970 ?? 0
        let key = "\(source)|\(modified)|\(pageIndex)|\(targetPixels)" as NSString
        if let cached = cache.object(forKey: key) { return cached }

        let image: NSImage?
        if let imagePath, FileManager.default.fileExists(atPath: imagePath) {
            image = downsample(URL(fileURLWithPath: imagePath), targetPixels: targetPixels)
        } else if let pdfPath, let document = PDFDocument(url: URL(fileURLWithPath: pdfPath)),
                  let page = document.page(at: min(max(pageIndex, 0), max(document.pageCount - 1, 0))) {
            let bounds = page.bounds(for: .mediaBox)
            let longest = max(bounds.width, bounds.height)
            let scale = longest > 0 ? CGFloat(targetPixels) / longest : 1
            image = page.thumbnail(
                of: NSSize(width: max(bounds.width * scale, 1), height: max(bounds.height * scale, 1)),
                for: .mediaBox
            )
        } else {
            image = nil
        }

        if let image {
            let cost = max(Int(image.size.width * image.size.height * 4), 1)
            cache.setObject(image, forKey: key, cost: cost)
        }
        return image
    }

    private func downsample(_ url: URL, targetPixels: Int) -> NSImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        let options = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: max(targetPixels, 64),
            kCGImageSourceShouldCacheImmediately: true,
        ] as CFDictionary
        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else { return nil }
        return NSImage(cgImage: cgImage, size: .zero)
    }
}

@MainActor
final class PageImageLoadCoordinator<Value>: ObservableObject {
    struct Request: Equatable {
        let id: String
        let generation: UInt64
    }

    @Published private(set) var value: Value?
    @Published private(set) var isLoading = false

    private var activeRequest: Request?
    private var nextGeneration: UInt64 = 0

    func load(
        requestID: String,
        operation: () async -> Value?
    ) async {
        let request = begin(requestID: requestID)
        let loadedValue = await operation()

        guard !Task.isCancelled, activeRequest == request else {
            finishCancellation(for: request)
            return
        }

        value = loadedValue
        isLoading = false
    }

    private func begin(requestID: String) -> Request {
        nextGeneration &+= 1
        let request = Request(id: requestID, generation: nextGeneration)
        activeRequest = request
        value = nil
        isLoading = true
        return request
    }

    private func finishCancellation(for request: Request) {
        guard activeRequest == request else { return }
        isLoading = false
    }
}

struct AsyncPageImage: View {
    let imagePath: String?
    let pdfPath: String?
    let pageIndex: Int
    var targetPixels = 1_200
    var contentMode: ContentMode = .fit
    var accessibilityText = "报纸原版图"

    @StateObject private var loader = PageImageLoadCoordinator<NSImage>()

    private var loadID: String {
        "\(imagePath ?? "")|\(pdfPath ?? "")|\(pageIndex)|\(targetPixels)"
    }

    var body: some View {
        ZStack {
            ReadingDeskTheme.field
            if let image = loader.value {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: contentMode)
                    .accessibilityLabel(accessibilityText)
            } else if loader.isLoading {
                VStack(spacing: 8) {
                    ProgressView()
                    Text("正在载入版面")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "doc.questionmark")
                        .font(.title2)
                    Text("原版图不可用")
                        .font(.caption)
                }
                .foregroundStyle(.secondary)
                .accessibilityElement(children: .combine)
            }
        }
        .clipped()
        .task(id: loadID) {
            let requestID = loadID
            await loader.load(requestID: requestID) {
                await PageImageRepository.shared.image(
                    imagePath: imagePath,
                    pdfPath: pdfPath,
                    pageIndex: pageIndex,
                    targetPixels: targetPixels
                )
            }
        }
    }
}

struct PageEvidencePreview: View {
    let edition: EditionRecord
    @State private var showingZoom = false

    var body: some View {
        Button {
            showingZoom = true
        } label: {
            AsyncPageImage(
                imagePath: edition.imagePath,
                pdfPath: edition.pdfPath,
                pageIndex: max((edition.pageNumber ?? 1) - 1, 0),
                targetPixels: 1_600,
                accessibilityText: "第\(edition.pageNumber ?? 0)版原版图，点击放大"
            )
            .frame(maxWidth: .infinity, minHeight: 420, idealHeight: 520, maxHeight: 560)
            .background(ReadingDeskTheme.field)
        }
        .buttonStyle(.plain)
        .help("点击进入缩放预览")
        .accessibilityLabel("放大查看第\(edition.pageNumber ?? 0)版原版图")
        .sheet(isPresented: $showingZoom) {
            ZoomablePageSheet(edition: edition)
        }
    }
}

private struct ZoomablePageSheet: View {
    let edition: EditionRecord
    @Environment(\.dismiss) private var dismiss
    @State private var zoom: CGFloat = 0.55
    @State private var baseZoom: CGFloat = 0.55
    @State private var viewportSize: CGSize = .zero

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text("第\(edition.pageNumber ?? 0)版原版")
                    .font(.headline)
                Spacer()
                Button { setZoom(zoom / 1.2) } label: { Image(systemName: "minus.magnifyingglass") }
                    .keyboardShortcut("-", modifiers: .command)
                    .accessibilityLabel("缩小原版图")
                Text("\(Int(zoom * 100))%")
                    .font(.caption.monospacedDigit())
                    .frame(width: 48)
                Button { setZoom(zoom * 1.2) } label: { Image(systemName: "plus.magnifyingglass") }
                    .keyboardShortcut("+", modifiers: .command)
                    .accessibilityLabel("放大原版图")
                Button("100%") { setZoom(1) }
                Button("适合窗口") { fitToWindow() }
                    .keyboardShortcut("0", modifiers: .command)
                Button("完成") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .controlSize(.large)
            .padding(14)

            Divider()

            GeometryReader { proxy in
                ScrollView([.horizontal, .vertical]) {
                    AsyncPageImage(
                        imagePath: edition.imagePath,
                        pdfPath: edition.pdfPath,
                        pageIndex: max((edition.pageNumber ?? 1) - 1, 0),
                        targetPixels: 4_096,
                        accessibilityText: "第\(edition.pageNumber ?? 0)版原版图"
                    )
                    .frame(width: 800 * zoom, height: 1_100 * zoom)
                    .padding(24)
                    .gesture(
                        MagnificationGesture()
                            .onChanged { value in setZoom(baseZoom * value) }
                            .onEnded { _ in baseZoom = zoom }
                    )
                }
                .onAppear {
                    viewportSize = proxy.size
                    fitToWindow()
                }
                .onChange(of: proxy.size) { newSize in viewportSize = newSize }
            }
            .background(ReadingDeskTheme.canvas)
        }
        .frame(minWidth: 980, minHeight: 720)
        .onAppear { baseZoom = zoom }
    }

    private func setZoom(_ value: CGFloat) {
        zoom = min(max(value, 0.25), 4)
        baseZoom = zoom
    }

    private func fitToWindow() {
        guard viewportSize.width > 0, viewportSize.height > 0 else { return }
        let availableWidth = max(viewportSize.width - 48, 200)
        let availableHeight = max(viewportSize.height - 48, 200)
        setZoom(min(availableWidth / 800, availableHeight / 1_100))
    }
}

struct ReadDailyLogo: View {
    var size: CGFloat = 42

    var body: some View {
        Group {
            if let url = ReadDailyResource.url(forResource: "readdaily-icon", withExtension: "svg"),
               let image = NSImage(contentsOf: url) {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFit()
            } else {
                Image(systemName: "newspaper.fill")
                    .resizable()
                    .scaledToFit()
                    .padding(size * 0.18)
                    .foregroundStyle(.white)
                    .background(Color.blue.gradient, in: RoundedRectangle(cornerRadius: size * 0.2))
            }
        }
        .frame(width: size, height: size)
        .accessibilityLabel("Read Daily 标识")
    }
}
