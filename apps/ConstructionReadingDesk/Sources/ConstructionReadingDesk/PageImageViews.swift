import AppKit
import Combine
import ConstructionReadingDeskCore
import ImageIO
import PDFKit
import SwiftUI

@MainActor
enum ReadDailyResource {
    private static let imageCache = NSCache<NSString, NSImage>()

    static func url(forResource name: String, withExtension extensionName: String) -> URL? {
        Bundle.main.url(forResource: name, withExtension: extensionName)
    }

    static func image(forResource name: String, withExtension extensionName: String) -> NSImage? {
        let key = "\(name).\(extensionName)" as NSString
        if let cached = imageCache.object(forKey: key) { return cached }
        guard let url = url(forResource: name, withExtension: extensionName),
              let image = NSImage(contentsOf: url) else { return nil }
        imageCache.setObject(image, forKey: key)
        return image
    }
}

enum PageImageSourceKind: String, Sendable {
    case image
    case pdf
}

struct PageImageDecodeRequest: Hashable, Sendable {
    let sourcePath: String
    let sourceKind: PageImageSourceKind
    let modificationRevision: UInt64
    let fileSize: UInt64
    let pageIndex: Int
    let targetPixels: Int

    fileprivate var cacheKey: String {
        "\(sourceKind.rawValue)|\(sourcePath)|\(modificationRevision)|\(fileSize)|\(pageIndex)|\(targetPixels)"
    }

    fileprivate var sourceRevisionKey: String {
        "\(sourceKind.rawValue)|\(sourcePath)|\(modificationRevision)|\(fileSize)|\(pageIndex)"
    }
}

final class SendablePageImage: @unchecked Sendable {
    // Decoders create an immutable image representation off-main. Consumers only
    // hand it to SwiftUI on the main actor and never mutate it after publication.
    let image: NSImage

    init(_ image: NSImage) {
        self.image = image
    }
}

private actor PageImageDecodePermitPool {
    private struct Waiter {
        let id: UUID
        let continuation: CheckedContinuation<Bool, Never>
    }

    private let limit: Int
    private var available: Int
    private var waiters: [Waiter] = []

    init(limit: Int) {
        self.limit = max(limit, 1)
        available = max(limit, 1)
    }

    func acquire(waiterID: UUID) async -> Bool {
        guard !Task.isCancelled else { return false }
        if available > 0 {
            available -= 1
            return true
        }
        return await withCheckedContinuation { continuation in
            waiters.append(Waiter(id: waiterID, continuation: continuation))
        }
    }

    func cancel(waiterID: UUID) {
        guard let index = waiters.firstIndex(where: { $0.id == waiterID }) else { return }
        let waiter = waiters.remove(at: index)
        waiter.continuation.resume(returning: false)
    }

    func release() {
        if waiters.isEmpty {
            available = min(available + 1, limit)
        } else {
            let waiter = waiters.removeFirst()
            waiter.continuation.resume(returning: true)
        }
    }
}

private final class PageImageDecodeScheduler: @unchecked Sendable {
    private let interactivePool: PageImageDecodePermitPool
    private let utilityPool: PageImageDecodePermitPool

    init(interactiveLimit: Int = 2, utilityLimit: Int = 2) {
        interactivePool = PageImageDecodePermitPool(limit: interactiveLimit)
        utilityPool = PageImageDecodePermitPool(limit: utilityLimit)
    }

    func decode(
        priority: TaskPriority,
        operation: @escaping @Sendable () async -> SendablePageImage?
    ) async -> SendablePageImage? {
        let pool = priority == .utility || priority == .background ? utilityPool : interactivePool
        let waiterID = UUID()
        let acquired = await withTaskCancellationHandler {
            await pool.acquire(waiterID: waiterID)
        } onCancel: {
            Task { await pool.cancel(waiterID: waiterID) }
        }
        guard acquired else { return nil }
        guard !Task.isCancelled else {
            await pool.release()
            return nil
        }
        let decoded = await operation()
        await pool.release()
        return decoded
    }
}

enum PageImageDecoder {
    static func decode(_ request: PageImageDecodeRequest) -> NSImage? {
        switch request.sourceKind {
        case .image:
            return downsample(URL(fileURLWithPath: request.sourcePath), targetPixels: request.targetPixels)
        case .pdf:
            guard let document = PDFDocument(url: URL(fileURLWithPath: request.sourcePath)),
                  document.pageCount > 0,
                  let page = document.page(at: min(max(request.pageIndex, 0), document.pageCount - 1)) else {
                return nil
            }
            let bounds = page.bounds(for: .mediaBox)
            let longest = max(bounds.width, bounds.height)
            let scale = longest > 0 ? CGFloat(request.targetPixels) / longest : 1
            return page.thumbnail(
                of: NSSize(width: max(bounds.width * scale, 1), height: max(bounds.height * scale, 1)),
                for: .mediaBox
            )
        }
    }

    private static func downsample(_ url: URL, targetPixels: Int) -> NSImage? {
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

actor PageImageRepository {
    static let shared = PageImageRepository()

    typealias Decoder = @Sendable (PageImageDecodeRequest) async -> SendablePageImage?

    private struct InFlightRequest {
        let id: UUID
        let task: Task<SendablePageImage?, Never>
        var waiters: Set<UUID>
    }

    private struct CachedVariantID: Hashable {
        let sourceRevisionKey: String
        let targetPixels: Int
    }

    private let cache: NSCache<NSString, NSImage>
    private let decoder: Decoder
    private let scheduler: PageImageDecodeScheduler
    private let cachedVariantLimit: Int
    private var inFlight: [String: InFlightRequest] = [:]
    // NSCache owns image lifetimes. This bounded side index stores only cache
    // keys, allowing a larger request to reuse an already-rendered thumbnail
    // as an exact-page placeholder without retaining evicted images.
    private var cachedVariantKeys: [CachedVariantID: String] = [:]
    private var cachedVariantOrder: [CachedVariantID] = []

    init(
        totalCostLimit: Int = 192 * 1_024 * 1_024,
        countLimit: Int = 48,
        interactiveDecodeLimit: Int = 2,
        utilityDecodeLimit: Int = 2,
        decoder: @escaping Decoder = { request in PageImageDecoder.decode(request).map(SendablePageImage.init) }
    ) {
        let cache = NSCache<NSString, NSImage>()
        cache.totalCostLimit = totalCostLimit
        cache.countLimit = countLimit
        self.cache = cache
        self.decoder = decoder
        cachedVariantLimit = max(countLimit > 0 ? countLimit * 2 : 96, 16)
        scheduler = PageImageDecodeScheduler(
            interactiveLimit: interactiveDecodeLimit,
            utilityLimit: utilityDecodeLimit
        )
    }

    func image(
        imagePath: String?,
        pdfPath: String?,
        pageIndex: Int,
        targetPixels: Int,
        priority: TaskPriority = .userInitiated
    ) async -> SendablePageImage? {
        let requests = resolvedRequests(
            imagePath: imagePath,
            pdfPath: pdfPath,
            pageIndex: pageIndex,
            targetPixels: targetPixels
        )
        for request in requests {
            guard !Task.isCancelled else { return nil }
            if let decoded = await image(for: request, priority: priority) { return decoded }
        }
        return nil
    }

    func cachedLowerResolutionImage(
        imagePath: String?,
        pdfPath: String?,
        pageIndex: Int,
        targetPixels: Int
    ) -> SendablePageImage? {
        let requests = resolvedRequests(
            imagePath: imagePath,
            pdfPath: pdfPath,
            pageIndex: pageIndex,
            targetPixels: targetPixels
        )
        for request in requests {
            if let image = bestCachedLowerResolutionImage(for: request) {
                return SendablePageImage(image)
            }
        }
        return nil
    }

    private func image(for request: PageImageDecodeRequest, priority: TaskPriority) async -> SendablePageImage? {
        let key = request.cacheKey
        if let cached = cache.object(forKey: key as NSString) {
            recordCachedVariant(request)
            return SendablePageImage(cached)
        }

        let waiterID = UUID()
        let operationID: UUID
        let operation: Task<SendablePageImage?, Never>
        if var existing = inFlight[key] {
            existing.waiters.insert(waiterID)
            inFlight[key] = existing
            operationID = existing.id
            operation = existing.task
        } else {
            let decoder = self.decoder
            let scheduler = self.scheduler
            let task = Task.detached(priority: priority) { () -> SendablePageImage? in
                guard !Task.isCancelled else { return nil }
                return await scheduler.decode(priority: priority) {
                    guard !Task.isCancelled else { return nil }
                    return await decoder(request)
                }
            }
            let id = UUID()
            inFlight[key] = InFlightRequest(id: id, task: task, waiters: [waiterID])
            operationID = id
            operation = task
        }

        return await withTaskCancellationHandler {
            let decoded = await operation.value
            return finish(
                decoded,
                request: request,
                operationID: operationID,
                waiterID: waiterID
            )
        } onCancel: {
            Task {
                await self.cancelWaiter(
                    key: key,
                    operationID: operationID,
                    waiterID: waiterID
                )
            }
        }
    }

    private func resolvedRequests(
        imagePath: String?,
        pdfPath: String?,
        pageIndex: Int,
        targetPixels: Int
    ) -> [PageImageDecodeRequest] {
        let candidates: [(String?, PageImageSourceKind)] = [
            (imagePath, .image),
            (pdfPath, .pdf),
        ]
        return candidates.compactMap { path, kind in
            guard let path, !path.isEmpty,
                  let attributes = try? FileManager.default.attributesOfItem(atPath: path),
                  (attributes[.type] as? FileAttributeType) != .typeDirectory else { return nil }
            let modified = (attributes[.modificationDate] as? Date)?.timeIntervalSinceReferenceDate.bitPattern ?? 0
            let fileSize = (attributes[.size] as? NSNumber)?.uint64Value ?? 0
            return PageImageDecodeRequest(
                sourcePath: path,
                sourceKind: kind,
                modificationRevision: modified,
                fileSize: fileSize,
                pageIndex: max(pageIndex, 0),
                targetPixels: max(targetPixels, 64)
            )
        }
    }

    private func finish(
        _ decoded: SendablePageImage?,
        request: PageImageDecodeRequest,
        operationID: UUID,
        waiterID: UUID
    ) -> SendablePageImage? {
        let key = request.cacheKey
        guard var inFlightRequest = inFlight[key],
              inFlightRequest.id == operationID,
              inFlightRequest.waiters.remove(waiterID) != nil else { return nil }

        let wasCancelled = Task.isCancelled
        if !wasCancelled, let decoded {
            let cost = max(Int(decoded.image.size.width * decoded.image.size.height * 4), 1)
            cache.setObject(decoded.image, forKey: key as NSString, cost: cost)
            recordCachedVariant(request: request, cacheKey: key)
        }
        if inFlightRequest.waiters.isEmpty {
            inFlight.removeValue(forKey: key)
        } else {
            inFlight[key] = inFlightRequest
        }
        return wasCancelled ? nil : decoded
    }

    private func bestCachedLowerResolutionImage(for request: PageImageDecodeRequest) -> NSImage? {
        let candidates = cachedVariantKeys.keys
            .filter {
                $0.sourceRevisionKey == request.sourceRevisionKey
                    && $0.targetPixels < request.targetPixels
            }
            .sorted { $0.targetPixels > $1.targetPixels }
        for candidate in candidates {
            guard let cacheKey = cachedVariantKeys[candidate],
                  let image = cache.object(forKey: cacheKey as NSString) else {
                removeCachedVariant(candidate)
                continue
            }
            touchCachedVariant(candidate)
            return image
        }
        return nil
    }

    private func recordCachedVariant(_ request: PageImageDecodeRequest) {
        recordCachedVariant(request: request, cacheKey: request.cacheKey)
    }

    private func recordCachedVariant(request: PageImageDecodeRequest, cacheKey: String) {
        let variant = CachedVariantID(
            sourceRevisionKey: request.sourceRevisionKey,
            targetPixels: request.targetPixels
        )
        cachedVariantKeys[variant] = cacheKey
        touchCachedVariant(variant)
        while cachedVariantOrder.count > cachedVariantLimit {
            removeCachedVariant(cachedVariantOrder[0])
        }
    }

    private func touchCachedVariant(_ variant: CachedVariantID) {
        cachedVariantOrder.removeAll { $0 == variant }
        cachedVariantOrder.append(variant)
    }

    private func removeCachedVariant(_ variant: CachedVariantID) {
        cachedVariantKeys.removeValue(forKey: variant)
        cachedVariantOrder.removeAll { $0 == variant }
    }

    private func cancelWaiter(key: String, operationID: UUID, waiterID: UUID) {
        guard var request = inFlight[key],
              request.id == operationID,
              request.waiters.remove(waiterID) != nil else { return }
        if request.waiters.isEmpty {
            inFlight.removeValue(forKey: key)
            request.task.cancel()
        } else {
            inFlight[key] = request
        }
    }

    func activeWaiterCount(
        imagePath: String?,
        pdfPath: String?,
        pageIndex: Int,
        targetPixels: Int
    ) -> Int {
        guard let request = resolvedRequests(
            imagePath: imagePath,
            pdfPath: pdfPath,
            pageIndex: pageIndex,
            targetPixels: targetPixels
        ).first else { return 0 }
        return inFlight[request.cacheKey]?.waiters.count ?? 0
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
        placeholder: (() async -> Value?)? = nil,
        operation: () async -> Value?
    ) async {
        let request = begin(requestID: requestID)
        if let placeholder {
            let placeholderValue = await placeholder()
            guard !Task.isCancelled, activeRequest == request else {
                finishCancellation(for: request)
                return
            }
            if let placeholderValue { value = placeholderValue }
        }

        let loadedValue = await operation()

        guard !Task.isCancelled, activeRequest == request else {
            finishCancellation(for: request)
            return
        }

        // Keep the exact-page low-resolution placeholder if the optional
        // high-resolution decode fails. It is more useful than flashing back
        // to an empty state and never represents a different page revision.
        if let loadedValue { value = loadedValue }
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
    var requestPriority: TaskPriority?
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
            let repository = PageImageRepository.shared
            await loader.load(
                requestID: requestID,
                placeholder: {
                    let cached = await repository.cachedLowerResolutionImage(
                        imagePath: imagePath,
                        pdfPath: pdfPath,
                        pageIndex: pageIndex,
                        targetPixels: targetPixels
                    )
                    return cached?.image
                },
                operation: {
                    let loaded = await repository.image(
                        imagePath: imagePath,
                        pdfPath: pdfPath,
                        pageIndex: pageIndex,
                        targetPixels: targetPixels,
                        priority: requestPriority ?? (targetPixels <= 320 ? .utility : .userInitiated)
                    )
                    return loaded?.image
                }
            )
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
            if let image = ReadDailyResource.image(forResource: "readdaily-icon", withExtension: "svg") {
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
