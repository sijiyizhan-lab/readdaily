#!/usr/bin/env swift
// vocr.swift — macOS Vision OCR 命令行工具（版面图文字提取 / 期号识别）
// 构建: swiftc -O -o bin/vocr scripts/vocr.swift   （install.sh 自动构建）
// 用法: vocr <image> [--crop-top F]   F=裁剪顶部比例（0-1），用于放大报头区 OCR
import Foundation
import Vision
import ImageIO
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 2 else { print("usage: vocr <image> [--crop-top F]"); exit(2) }
let path = args[1]
var cropTop: CGFloat = 0
if let ix = args.firstIndex(of: "--crop-top"), ix + 1 < args.count {
    cropTop = CGFloat(Double(args[ix+1]) ?? 0)
}
guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { exit(3) }
let cg: CGImage
if cropTop > 0 && cropTop < 1 {
    let h = CGFloat(img.height)
    // CGImage cropping: origin is the top-left corner of the image
    let rect = CGRect(x: 0, y: 0, width: CGFloat(img.width), height: h * cropTop)
    guard let c = img.cropping(to: rect) else { exit(4) }
    cg = c
} else {
    cg = img
}
let req = VNRecognizeTextRequest { r, _ in
    guard let obs = r.results as? [VNRecognizedTextObservation] else { return }
    for o in obs {
        if let t = o.topCandidates(1).first { print(t.string) }
    }
}
req.recognitionLevel = .accurate
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.usesLanguageCorrection = false
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try? handler.perform([req])
