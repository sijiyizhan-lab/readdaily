import AppKit
import CoreGraphics
import Dispatch
import Foundation
@testable import ConstructionReadingDesk
import Testing

@Suite("页面图像后台仓库")
struct PageImageRepositoryTests {
    @Test("旧缩略图未释放时新主图已经开始解码")
    func selectedPageDoesNotWaitForSuspendedThumbnail() async throws {
        let sources = try TemporaryPageSources(names: ["old.jpg", "selected.jpg"])
        defer { sources.remove() }
        let decoder = ControlledPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })

        let oldKey = decoder.key(path: sources.path("old.jpg"), page: 0, size: 180)
        let selectedKey = decoder.key(path: sources.path("selected.jpg"), page: 0, size: 1_600)
        let old = Task {
            await repository.image(
                imagePath: sources.path("old.jpg"),
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 180,
                priority: .utility
            )
        }
        await decoder.waitUntilStarted(oldKey)

        let selected = Task {
            await repository.image(
                imagePath: sources.path("selected.jpg"),
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 1_600,
                priority: .userInitiated
            )
        }
        await decoder.waitUntilStarted(selectedKey)

        #expect(await decoder.startCount(oldKey) == 1)
        #expect(await decoder.startCount(selectedKey) == 1)
        await decoder.complete(selectedKey, with: testPageImage(width: 1_600, height: 2_200))
        #expect(await selected.value != nil)

        await decoder.complete(oldKey, with: testPageImage(width: 180, height: 240))
        _ = await old.value
    }

    @Test("后台解码挂起时主线程仍可处理下一次交互")
    @MainActor
    func suspendedDecodeDoesNotBlockMainActor() async throws {
        let sources = try TemporaryPageSources(names: ["page.png"])
        defer { sources.remove() }
        try onePixelPNG().write(to: URL(fileURLWithPath: sources.path("page.png")), options: .atomic)
        let gate = DefaultDecoderGate()
        let repository = PageImageRepository(decoder: { request in
            dispatchPrecondition(condition: .notOnQueue(.main))
            await gate.pauseBeforeDecode()
            return PageImageDecoder.decode(request).map(SendablePageImage.init)
        })

        let load = Task { @MainActor in
            await repository.image(
                imagePath: sources.path("page.png"),
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 1_600
            )
        }
        await gate.waitUntilStarted()

        var interactionHandled = false
        await Task { @MainActor in
            interactionHandled = true
        }.value
        #expect(interactionHandled)

        await gate.resumeDecode()
        #expect(await load.value != nil)
    }

    @Test("被取消的唯一请求不会写缓存")
    func cancelledDecodeResultIsDiscarded() async throws {
        let sources = try TemporaryPageSources(names: ["cancelled.jpg"])
        defer { sources.remove() }
        let decoder = ControlledPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let key = decoder.key(path: sources.path("cancelled.jpg"), page: 0, size: 1_600)

        let cancelled = Task {
            await repository.image(
                imagePath: sources.path("cancelled.jpg"),
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 1_600
            )
        }
        await decoder.waitUntilStarted(key)
        cancelled.cancel()
        await decoder.complete(key, with: testPageImage(width: 1_600, height: 2_200))
        #expect(await cancelled.value == nil)

        let retry = Task {
            await repository.image(
                imagePath: sources.path("cancelled.jpg"),
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 1_600
            )
        }
        await decoder.waitUntilStarted(key, count: 2)
        #expect(await decoder.startCount(key) == 2)
        await decoder.complete(key, with: testPageImage(width: 1_600, height: 2_200))
        #expect(await retry.value != nil)
    }

    @Test("取消一个同键等待者不会终止其他等待者")
    func cancellingOneCoalescedWaiterKeepsSharedDecodeAlive() async throws {
        let sources = try TemporaryPageSources(names: ["shared.jpg"])
        defer { sources.remove() }
        let decoder = ControlledPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let path = sources.path("shared.jpg")
        let key = decoder.key(path: path, page: 0, size: 1_600)

        let first = Task { await repository.image(imagePath: path, pdfPath: nil, pageIndex: 0, targetPixels: 1_600) }
        await decoder.waitUntilStarted(key)
        let second = Task { await repository.image(imagePath: path, pdfPath: nil, pageIndex: 0, targetPixels: 1_600) }
        await waitForWaiterCount(2, repository: repository, imagePath: path, pageIndex: 0, targetPixels: 1_600)

        first.cancel()
        await waitForWaiterCount(1, repository: repository, imagePath: path, pageIndex: 0, targetPixels: 1_600)
        await decoder.complete(key, with: testPageImage(width: 1_600, height: 2_200))

        #expect(await first.value == nil)
        #expect(await second.value != nil)
        #expect(await decoder.startCount(key) == 1)

        #expect(await repository.image(imagePath: path, pdfPath: nil, pageIndex: 0, targetPixels: 1_600) != nil)
        #expect(await decoder.startCount(key) == 1)
    }

    @Test("缩略图解码并发有上限且已取消排队任务不会启动")
    func utilityDecodeQueueIsBoundedAndCancellationAware() async throws {
        let sources = try TemporaryPageSources(names: ["first.jpg", "queued.jpg", "after.jpg"])
        defer { sources.remove() }
        let decoder = ControlledPageImageDecoder()
        let repository = PageImageRepository(
            interactiveDecodeLimit: 1,
            utilityDecodeLimit: 1,
            decoder: { request in await decoder.decode(request) }
        )
        let firstPath = sources.path("first.jpg")
        let queuedPath = sources.path("queued.jpg")
        let firstKey = decoder.key(path: firstPath, page: 0, size: 180)
        let queuedKey = decoder.key(path: queuedPath, page: 0, size: 180)

        let first = Task {
            await repository.image(
                imagePath: firstPath,
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 180,
                priority: .utility
            )
        }
        await decoder.waitUntilStarted(firstKey)
        let queued = Task {
            await repository.image(
                imagePath: queuedPath,
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 180,
                priority: .utility
            )
        }
        await waitForWaiterCount(1, repository: repository, imagePath: queuedPath, pageIndex: 0, targetPixels: 180)

        #expect(await decoder.startCount(queuedKey) == 0)
        queued.cancel()
        await waitForWaiterCount(0, repository: repository, imagePath: queuedPath, pageIndex: 0, targetPixels: 180)
        await decoder.complete(firstKey, with: testPageImage(width: 180, height: 240))

        #expect(await first.value != nil)
        #expect(await queued.value == nil)
        #expect(await decoder.startCount(queuedKey) == 0)

        let afterPath = sources.path("after.jpg")
        let afterKey = decoder.key(path: afterPath, page: 0, size: 180)
        let after = Task {
            await repository.image(
                imagePath: afterPath,
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 180,
                priority: .utility
            )
        }
        await decoder.waitUntilStarted(afterKey)
        await decoder.complete(afterKey, with: testPageImage(width: 180, height: 240))
        #expect(await after.value != nil)
    }

    @Test("相同版本页面和尺寸直接命中缓存")
    func identicalRequestUsesCache() async throws {
        let sources = try TemporaryPageSources(names: ["cached.jpg"])
        defer { sources.remove() }
        let decoder = RecordingPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let path = sources.path("cached.jpg")

        let first = try #require(await repository.image(imagePath: path, pdfPath: nil, pageIndex: 0, targetPixels: 800))
        let second = try #require(await repository.image(imagePath: path, pdfPath: nil, pageIndex: 0, targetPixels: 800))

        #expect(first.image === second.image)
        #expect(await decoder.requests.count == 1)
    }

    @Test("同版低清缓存会在高清解码期间立即作为占位并无闪白替换")
    @MainActor
    func cachedThumbnailIsShownUntilHighResolutionDecodeFinishes() async throws {
        let sources = try TemporaryPageSources(names: ["progressive.jpg"])
        defer { sources.remove() }
        let decoder = ControlledPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let path = sources.path("progressive.jpg")
        let thumbnailKey = decoder.key(path: path, page: 0, size: 180)
        let highResolutionKey = decoder.key(path: path, page: 0, size: 1_600)

        let thumbnailTask = Task {
            await repository.image(
                imagePath: path,
                pdfPath: nil,
                pageIndex: 0,
                targetPixels: 180,
                priority: .utility
            )
        }
        await decoder.waitUntilStarted(thumbnailKey)
        let thumbnailImage = testPageImage(width: 180, height: 240)
        await decoder.complete(thumbnailKey, with: thumbnailImage)
        _ = await thumbnailTask.value

        let loader = PageImageLoadCoordinator<NSImage>()
        let highResolutionTask = Task { @MainActor in
            await loader.load(
                requestID: "progressive-1600",
                placeholder: {
                    await repository.cachedLowerResolutionImage(
                        imagePath: path,
                        pdfPath: nil,
                        pageIndex: 0,
                        targetPixels: 1_600
                    )?.image
                },
                operation: {
                    await repository.image(
                        imagePath: path,
                        pdfPath: nil,
                        pageIndex: 0,
                        targetPixels: 1_600
                    )?.image
                }
            )
        }
        await decoder.waitUntilStarted(highResolutionKey)

        let displayedThumbnail = try #require(loader.value)
        #expect(displayedThumbnail === thumbnailImage.image)
        #expect(loader.isLoading)

        let highResolutionImage = testPageImage(width: 1_600, height: 2_200)
        await decoder.complete(highResolutionKey, with: highResolutionImage)
        await highResolutionTask.value

        let displayedHighResolution = try #require(loader.value)
        #expect(displayedHighResolution === highResolutionImage.image)
        #expect(!loader.isLoading)
        #expect(await decoder.startCount(thumbnailKey) == 1)
        #expect(await decoder.startCount(highResolutionKey) == 1)
    }

    @Test("低清占位严格绑定文件版本与版次")
    func cachedThumbnailDoesNotCrossPageOrFileRevision() async throws {
        let sources = try TemporaryPageSources(names: ["revision-preview.jpg"])
        defer { sources.remove() }
        let decoder = RecordingPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let path = sources.path("revision-preview.jpg")

        _ = await repository.image(
            imagePath: path,
            pdfPath: nil,
            pageIndex: 0,
            targetPixels: 180
        )
        #expect(await repository.cachedLowerResolutionImage(
            imagePath: path,
            pdfPath: nil,
            pageIndex: 0,
            targetPixels: 1_600
        ) != nil)
        #expect(await repository.cachedLowerResolutionImage(
            imagePath: path,
            pdfPath: nil,
            pageIndex: 1,
            targetPixels: 1_600
        ) == nil)

        try FileManager.default.setAttributes(
            [.modificationDate: Date(timeIntervalSinceNow: 20)],
            ofItemAtPath: path
        )
        #expect(await repository.cachedLowerResolutionImage(
            imagePath: path,
            pdfPath: nil,
            pageIndex: 0,
            targetPixels: 1_600
        ) == nil)
        #expect(await decoder.requests.count == 1)
    }

    @Test("默认 ImageIO 解码器可读取真实图片")
    func defaultDecoderReadsImageData() async throws {
        let sources = try TemporaryPageSources(names: ["pixel.png"])
        defer { sources.remove() }
        let png = try #require(Data(base64Encoded:
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        try png.write(to: URL(fileURLWithPath: sources.path("pixel.png")), options: .atomic)
        let repository = PageImageRepository()

        let loaded = try #require(await repository.image(
            imagePath: sources.path("pixel.png"),
            pdfPath: nil,
            pageIndex: 0,
            targetPixels: 180
        ))

        #expect(loaded.image.size.width > 0)
        #expect(loaded.image.size.height > 0)
    }

    @Test("文件版本、页码或目标尺寸变化都会缓存未命中")
    func revisionPageAndSizeParticipateInCacheKey() async throws {
        let sources = try TemporaryPageSources(names: ["revision.jpg"])
        defer { sources.remove() }
        let decoder = RecordingPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let path = sources.path("revision.jpg")

        _ = await repository.image(imagePath: path, pdfPath: nil, pageIndex: 0, targetPixels: 180)
        _ = await repository.image(imagePath: path, pdfPath: nil, pageIndex: 1, targetPixels: 180)
        _ = await repository.image(imagePath: path, pdfPath: nil, pageIndex: 1, targetPixels: 1_600)
        try FileManager.default.setAttributes(
            [.modificationDate: Date(timeIntervalSinceNow: 20)],
            ofItemAtPath: path
        )
        _ = await repository.image(imagePath: path, pdfPath: nil, pageIndex: 1, targetPixels: 1_600)

        #expect(await decoder.requests.count == 4)
        #expect(Set(await decoder.requests.map(\.pageIndex)) == [0, 1])
        #expect(Set(await decoder.requests.map(\.targetPixels)) == [180, 1_600])
        #expect(Set(await decoder.requests.map(\.modificationRevision)).count == 2)
    }

    @Test("缩略图路径缺失时按实际 PDF 生成缓存键并回退")
    func missingImageFallsBackToExistingPDF() async throws {
        let sources = try TemporaryPageSources(names: ["issue.pdf"])
        defer { sources.remove() }
        let decoder = RecordingPageImageDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })
        let missingImage = sources.path("missing.jpg")
        let pdf = sources.path("issue.pdf")

        #expect(await repository.image(
            imagePath: missingImage,
            pdfPath: pdf,
            pageIndex: 3,
            targetPixels: 800
        ) != nil)
        let request = try #require(await decoder.requests.first)

        #expect(request.sourceKind == .pdf)
        #expect(request.sourcePath == pdf)
        #expect(request.pageIndex == 3)
        #expect(request.targetPixels == 800)
    }

    @Test("默认 PDFKit 解码器可从缺失缩略图回退到真实 PDF")
    func defaultDecoderRendersPDFFallback() async throws {
        let sources = try TemporaryPageSources(names: ["real.pdf"])
        defer { sources.remove() }
        let pdfURL = URL(fileURLWithPath: sources.path("real.pdf"))
        try writeOnePagePDF(to: pdfURL)
        let repository = PageImageRepository()

        let loaded = try #require(await repository.image(
            imagePath: sources.path("missing.jpg"),
            pdfPath: pdfURL.path,
            pageIndex: 0,
            targetPixels: 800
        ))

        #expect(loaded.image.size.width > 0)
        #expect(loaded.image.size.height > 0)
    }

    @Test("缩略图存在但解码失败时继续回退 PDF")
    func corruptImageFallsBackToPDF() async throws {
        let sources = try TemporaryPageSources(names: ["corrupt.jpg", "issue.pdf"])
        defer { sources.remove() }
        let decoder = ImageFailureThenPDFDecoder()
        let repository = PageImageRepository(decoder: { request in await decoder.decode(request) })

        #expect(await repository.image(
            imagePath: sources.path("corrupt.jpg"),
            pdfPath: sources.path("issue.pdf"),
            pageIndex: 2,
            targetPixels: 800
        ) != nil)

        #expect(await decoder.sourceKinds == [.image, .pdf])
    }
}

private actor DefaultDecoderGate {
    private var started = false
    private var startWaiters: [CheckedContinuation<Void, Never>] = []
    private var decodeContinuation: CheckedContinuation<Void, Never>?

    func pauseBeforeDecode() async {
        started = true
        let waiting = startWaiters
        startWaiters.removeAll()
        for continuation in waiting { continuation.resume() }
        await withCheckedContinuation { continuation in
            decodeContinuation = continuation
        }
    }

    func waitUntilStarted() async {
        guard !started else { return }
        await withCheckedContinuation { continuation in
            startWaiters.append(continuation)
        }
    }

    func resumeDecode() {
        decodeContinuation?.resume()
        decodeContinuation = nil
    }
}

private actor ControlledPageImageDecoder {
    private var starts: [String: Int] = [:]
    private var startWaiters: [String: [(Int, CheckedContinuation<Void, Never>)]] = [:]
    private var completions: [String: [CheckedContinuation<SendablePageImage?, Never>]] = [:]

    nonisolated func key(path: String, page: Int, size: Int) -> String {
        "\(path)|\(page)|\(size)"
    }

    func decode(_ request: PageImageDecodeRequest) async -> SendablePageImage? {
        let requestKey = key(path: request.sourcePath, page: request.pageIndex, size: request.targetPixels)
        starts[requestKey, default: 0] += 1
        if let waiting = startWaiters.removeValue(forKey: requestKey) {
            var pending: [(Int, CheckedContinuation<Void, Never>)] = []
            for (target, continuation) in waiting {
                if starts[requestKey, default: 0] >= target {
                    continuation.resume()
                } else {
                    pending.append((target, continuation))
                }
            }
            if !pending.isEmpty { startWaiters[requestKey] = pending }
        }
        return await withCheckedContinuation { continuation in
            completions[requestKey, default: []].append(continuation)
        }
    }

    func waitUntilStarted(_ requestKey: String, count: Int = 1) async {
        guard starts[requestKey, default: 0] < count else { return }
        await withCheckedContinuation { continuation in
            startWaiters[requestKey, default: []].append((count, continuation))
        }
    }

    func startCount(_ requestKey: String) -> Int {
        starts[requestKey, default: 0]
    }

    func complete(_ requestKey: String, with image: SendablePageImage?) {
        guard var waiting = completions[requestKey], !waiting.isEmpty else {
            Issue.record("没有等待完成的图像解码：\(requestKey)")
            return
        }
        let continuation = waiting.removeFirst()
        completions[requestKey] = waiting
        continuation.resume(returning: image)
    }
}

private actor RecordingPageImageDecoder {
    private(set) var requests: [PageImageDecodeRequest] = []

    func decode(_ request: PageImageDecodeRequest) -> SendablePageImage? {
        requests.append(request)
        return testPageImage(width: request.targetPixels, height: request.targetPixels)
    }
}

private actor ImageFailureThenPDFDecoder {
    private(set) var sourceKinds: [PageImageSourceKind] = []

    func decode(_ request: PageImageDecodeRequest) -> SendablePageImage? {
        sourceKinds.append(request.sourceKind)
        guard request.sourceKind == .pdf else { return nil }
        return testPageImage(width: request.targetPixels, height: request.targetPixels)
    }
}

private struct TemporaryPageSources {
    let directory: URL

    init(names: [String]) throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("readdaily-page-images-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        for name in names {
            let url = directory.appendingPathComponent(name)
            guard FileManager.default.createFile(atPath: url.path, contents: Data("source".utf8)) else {
                throw TemporaryPageSourceError.creationFailed(url.path)
            }
        }
    }

    func path(_ name: String) -> String {
        directory.appendingPathComponent(name).path
    }

    func remove() {
        try? FileManager.default.removeItem(at: directory)
    }
}

private enum TemporaryPageSourceError: Error {
    case creationFailed(String)
}

private func testPageImage(width: Int, height: Int) -> SendablePageImage {
    SendablePageImage(NSImage(size: NSSize(width: width, height: height)))
}

private func onePixelPNG() throws -> Data {
    try #require(Data(base64Encoded:
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    ))
}

private func writeOnePagePDF(to url: URL) throws {
    guard let consumer = CGDataConsumer(url: url as CFURL) else {
        throw TemporaryPageSourceError.creationFailed(url.path)
    }
    var mediaBox = CGRect(x: 0, y: 0, width: 600, height: 800)
    guard let context = CGContext(consumer: consumer, mediaBox: &mediaBox, nil) else {
        throw TemporaryPageSourceError.creationFailed(url.path)
    }
    context.beginPDFPage(nil)
    context.setFillColor(CGColor(red: 0.2, green: 0.5, blue: 0.8, alpha: 1))
    context.fill(CGRect(x: 40, y: 40, width: 520, height: 720))
    context.endPDFPage()
    context.closePDF()
}

private func waitForWaiterCount(
    _ expectedCount: Int,
    repository: PageImageRepository,
    imagePath: String,
    pageIndex: Int,
    targetPixels: Int
) async {
    for _ in 0..<2_000 {
        if await repository.activeWaiterCount(
            imagePath: imagePath,
            pdfPath: nil,
            pageIndex: pageIndex,
            targetPixels: targetPixels
        ) == expectedCount {
            return
        }
        await Task.yield()
    }
    Issue.record("等待者数量未达到 \(expectedCount)")
}
