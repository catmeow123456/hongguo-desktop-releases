// winid.swift — 列出红果相关窗口（windowId、标题、尺寸），配合
// `screencapture -x -l <id> out.png` 可截取被其他窗口遮挡的窗口。
// 用法: swift winid.swift
import CoreGraphics
import Foundation

let opts: CGWindowListOption = [.optionAll]
if let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] {
    for w in list {
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        if owner.contains("hongguo") || owner.contains("红果") {
            let id = w[kCGWindowNumber as String] as? Int ?? 0
            let name = w[kCGWindowName as String] as? String ?? ""
            let bounds = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
            print("\(id)\t\(name)\t\(bounds)")
        }
    }
}
