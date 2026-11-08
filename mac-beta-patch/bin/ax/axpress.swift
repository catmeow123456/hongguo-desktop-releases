// axpress.swift — 按标题/描述按下匹配的按钮（AXPress），不抢焦点。
// 用法: swift axpress.swift <pid> <标题> [序号]
//   序号可选：同名按钮有多个时选第几个，从 0 开始（缺省第 0 个）。
import ApplicationServices
import Foundation

guard CommandLine.arguments.count > 2,
      let pid = pid_t(CommandLine.arguments[1]) else {
    print("usage: axpress.swift <pid> <title> [index]")
    exit(2)
}
let want = CommandLine.arguments[2]
let idx = CommandLine.arguments.count > 3 ? Int(CommandLine.arguments[3]) ?? 0 : 0

func find(_ el: AXUIElement, _ want: String, _ hits: inout [AXUIElement]) {
    var title: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXTitleAttribute as CFString, &title)
    var desc: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXDescriptionAttribute as CFString, &desc)
    if (title as? String ?? "") == want || (desc as? String ?? "") == want {
        hits.append(el)
    }
    var kids: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &kids)
    if let arr = kids as? [AXUIElement] {
        for k in arr { find(k, want, &hits) }
    }
}

var hits: [AXUIElement] = []
find(AXUIElementCreateApplication(pid), want, &hits)
print("matches: \(hits.count)")
guard hits.indices.contains(idx) else {
    print("index out of range")
    exit(1)
}
let err = AXUIElementPerformAction(hits[idx], kAXPressAction as CFString)
print("press[\(idx)] err=\(err.rawValue)")
exit(err == .success ? 0 : 1)
