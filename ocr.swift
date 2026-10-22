// Распознаёт японский текст через Apple Vision. Вывод: JSON [{text, x, y, w, h}] (координаты 0..1, y сверху вниз).
import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else { FileHandle.standardError.write("usage: ocr <image>\n".data(using: .utf8)!); exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let img = NSImage(contentsOf: url),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!); exit(1)
}
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = ["ja-JP"]
req.usesLanguageCorrection = true
req.minimumTextHeight = 0.0
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try handler.perform([req])
var out: [[String: Any]] = []
for o in (req.results ?? []) {
    guard let c = o.topCandidates(1).first, c.confidence >= 0.3 else { continue }
    let b = o.boundingBox
    out.append(["text": c.string, "x": b.minX, "y": 1.0 - b.maxY, "w": b.width, "h": b.height, "conf": c.confidence])
}
let data = try JSONSerialization.data(withJSONObject: out, options: [])
print(String(data: data, encoding: .utf8)!)
