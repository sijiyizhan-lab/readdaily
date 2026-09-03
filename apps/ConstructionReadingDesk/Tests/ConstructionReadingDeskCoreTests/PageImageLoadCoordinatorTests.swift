@testable import ConstructionReadingDesk
import Testing

@Suite("页面图像请求协调")
struct PageImageLoadCoordinatorTests {
    @Test("延迟返回的旧版图不会覆盖已完成的新选择")
    @MainActor
    func delayedOldRequestCannotReplaceNewImage() async {
        let delayedA = SuspendedValue<String>()
        let loader = PageImageLoadCoordinator<String>()

        let requestA = Task { @MainActor in
            await loader.load(requestID: "A") {
                await delayedA.wait()
            }
        }
        while !(await delayedA.isWaiting) {
            await Task.yield()
        }

        let requestB = Task { @MainActor in
            await loader.load(requestID: "B") {
                "B image"
            }
        }
        await requestB.value

        #expect(loader.value == "B image")
        #expect(loader.isLoading == false)

        await delayedA.resume(returning: "A image")
        await requestA.value

        #expect(loader.value == "B image")
        #expect(loader.isLoading == false)
    }

    @Test("开始新请求会立即清除旧版图")
    @MainActor
    func beginningNewRequestClearsPreviousImage() async {
        let delayedB = SuspendedValue<String>()
        let loader = PageImageLoadCoordinator<String>()

        await loader.load(requestID: "A") {
            "A image"
        }
        #expect(loader.value == "A image")

        let requestB = Task { @MainActor in
            await loader.load(requestID: "B") {
                await delayedB.wait()
            }
        }
        while !(await delayedB.isWaiting) {
            await Task.yield()
        }

        #expect(loader.value == nil)
        #expect(loader.isLoading == true)

        await delayedB.resume(returning: "B image")
        await requestB.value

        #expect(loader.value == "B image")
        #expect(loader.isLoading == false)
    }

    @Test("已取消请求即使底层操作继续返回也不会写入")
    @MainActor
    func cancelledRequestCannotWriteImage() async {
        let delayedImage = SuspendedValue<String>()
        let loader = PageImageLoadCoordinator<String>()

        let request = Task { @MainActor in
            await loader.load(requestID: "cancelled") {
                await delayedImage.wait()
            }
        }
        while !(await delayedImage.isWaiting) {
            await Task.yield()
        }

        request.cancel()
        await delayedImage.resume(returning: "late image")
        await request.value

        #expect(loader.value == nil)
        #expect(loader.isLoading == false)
    }

    @Test("迟到的旧请求低清占位不会覆盖新页面")
    @MainActor
    func delayedOldPlaceholderCannotReplaceNewImage() async {
        let delayedPlaceholder = SuspendedValue<String>()
        let loader = PageImageLoadCoordinator<String>()

        let requestA = Task { @MainActor in
            await loader.load(
                requestID: "A",
                placeholder: { await delayedPlaceholder.wait() },
                operation: { "A high resolution" }
            )
        }
        while !(await delayedPlaceholder.isWaiting) {
            await Task.yield()
        }

        await loader.load(requestID: "B") {
            "B high resolution"
        }
        #expect(loader.value == "B high resolution")
        #expect(!loader.isLoading)

        await delayedPlaceholder.resume(returning: "A thumbnail")
        await requestA.value

        #expect(loader.value == "B high resolution")
        #expect(!loader.isLoading)
    }
}

private actor SuspendedValue<Value> {
    private var continuation: CheckedContinuation<Value?, Never>?

    var isWaiting: Bool {
        continuation != nil
    }

    func wait() async -> Value? {
        await withCheckedContinuation { continuation in
            self.continuation = continuation
        }
    }

    func resume(returning value: Value?) {
        continuation?.resume(returning: value)
        continuation = nil
    }
}
