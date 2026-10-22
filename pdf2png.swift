// Рендерит страницы PDF в PNG: pdf2png <pdf> <outdir> [ширина]
import Foundation
import PDFKit
import AppKit

let a = CommandLine.arguments
guard a.count >= 3, let doc = PDFDocument(url: URL(fileURLWithPath: a[1])) else { print("cannot open pdf"); exit(1) }
let width = a.count > 3 ? CGFloat(Double(a[3])!) : 1800
try FileManager.default.createDirectory(atPath: a[2], withIntermediateDirectories: true)
print("pages: \(doc.pageCount)")
for i in 0..<doc.pageCount {
    let page = doc.page(at: i)!
    let r = page.bounds(for: .mediaBox)
    let size = NSSize(width: width, height: (width * r.height / r.width).rounded())
    let img = page.thumbnail(of: size, for: .mediaBox)
    guard let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { continue }
    try png.write(to: URL(fileURLWithPath: String(format: "%@/%04d.png", a[2], i + 1)))
}
