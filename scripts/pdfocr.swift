#!/usr/bin/env swift
import AppKit
import Foundation
import PDFKit
import Vision

struct PageResult: Codable {
    let number: Int
    let image: String
    let text: String
    let characters: Int
}

struct Manifest: Codable {
    let page_count: Int
    let pages: [PageResult]
}

enum PDFOCRError: LocalizedError {
    case usage
    case cannotOpen(String)
    case cannotRender(Int)
    case cannotEncode(Int)

    var errorDescription: String? {
        switch self {
        case .usage:
            return "用法：pdfocr <input.pdf> <output-directory> [--accurate|--fast]"
        case let .cannotOpen(path):
            return "无法打开 PDF：\(path)"
        case let .cannotRender(page):
            return "无法渲染第 \(page) 页"
        case let .cannotEncode(page):
            return "无法编码第 \(page) 页图像"
        }
    }
}

func pageImage(_ page: PDFPage, number: Int) throws -> CGImage {
    let bounds = page.bounds(for: .mediaBox)
    let targetWidth: CGFloat = 2400
    let scale = max(1, targetWidth / max(bounds.width, 1))
    let targetSize = NSSize(width: bounds.width * scale, height: bounds.height * scale)
    let thumbnail = page.thumbnail(of: targetSize, for: .mediaBox)
    var rect = NSRect(origin: .zero, size: thumbnail.size)
    guard let image = thumbnail.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        throw PDFOCRError.cannotRender(number)
    }
    return image
}

func recognizedText(from image: CGImage, accurate: Bool) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = accurate ? .accurate : .fast
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    let observations = (request.results ?? []).sorted { left, right in
        let verticalDelta = abs(left.boundingBox.maxY - right.boundingBox.maxY)
        if verticalDelta > 0.015 {
            return left.boundingBox.maxY > right.boundingBox.maxY
        }
        return left.boundingBox.minX < right.boundingBox.minX
    }
    return observations.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

func writeJPEG(_ image: CGImage, to url: URL, page: Int) throws {
    let representation = NSBitmapImageRep(cgImage: image)
    guard let data = representation.representation(
        using: .jpeg,
        properties: [.compressionFactor: 0.9]
    ) else {
        throw PDFOCRError.cannotEncode(page)
    }
    try data.write(to: url, options: .atomic)
}

func run() throws {
    let arguments = CommandLine.arguments
    guard arguments.count >= 3 else { throw PDFOCRError.usage }
    let inputURL = URL(fileURLWithPath: arguments[1]).standardizedFileURL
    let outputURL = URL(fileURLWithPath: arguments[2]).standardizedFileURL
    let accurate = !arguments.contains("--fast")
    guard let document = PDFDocument(url: inputURL) else {
        throw PDFOCRError.cannotOpen(inputURL.path)
    }

    let pagesURL = outputURL.appendingPathComponent("pages", isDirectory: true)
    let textURL = outputURL.appendingPathComponent("text", isDirectory: true)
    try FileManager.default.createDirectory(at: pagesURL, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: textURL, withIntermediateDirectories: true)

    var results: [PageResult] = []
    for index in 0..<document.pageCount {
        guard let page = document.page(at: index) else { continue }
        let number = index + 1
        let image = try pageImage(page, number: number)
        let imageName = String(format: "%02d版.jpg", number)
        let textName = String(format: "edition_%02d.txt", number)
        try writeJPEG(image, to: pagesURL.appendingPathComponent(imageName), page: number)

        let embedded = page.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let text = embedded.count >= 80 ? embedded : try recognizedText(from: image, accurate: accurate)
        try text.write(
            to: textURL.appendingPathComponent(textName),
            atomically: true,
            encoding: .utf8
        )
        results.append(
            PageResult(
                number: number,
                image: "pages/\(imageName)",
                text: "text/\(textName)",
                characters: text.count
            )
        )
    }

    let encoded = try JSONEncoder().encode(Manifest(page_count: document.pageCount, pages: results))
    FileHandle.standardOutput.write(encoded)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

do {
    try run()
} catch {
    let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}
