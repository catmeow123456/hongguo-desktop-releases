// axconfirm.swift — 对第一个 AXTextField 依次尝试 AXConfirm / AXPress。
// 部分输入框（如红果的搜索框）不响应回车事件但认 AXConfirm。
// 用法: swift axconfirm.swift <pid>
import ApplicationServices
import AppKit

guard CommandLine.arguments.count > 1,
      let pid = Int32(CommandLine.arguments[1]) else {
    print("usage: axconfirm.swift <pid>")
    exit(2)
}

func attr(_ el: AXUIElement, _ key: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, key as CFString, &value) == .success else { return nil }
    return value as AnyObject?
}

var field: AXUIElement?
func walk(_ el: AXUIElement, _ depth: Int) {
    if depth > 14 { return }
    if field == nil && (attr(el, kAXRoleAttribute) as? String ?? "") == "AXTextField" { field = el }
    if let children = attr(el, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children { walk(child, depth + 1) }
    }
}

walk(AXUIElementCreateApplication(pid), 0)
if field == nil { print("no field"); exit(1) }
for action in ["AXConfirm", "AXPress"] {
    let err = AXUIElementPerformAction(field!, action as CFString)
    print(action, "err=\(err.rawValue)")
    if err.rawValue == 0 { break }
}
