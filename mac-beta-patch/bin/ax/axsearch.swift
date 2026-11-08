// axsearch.swift — 对第一个 AXTextField 设值并触发搜索，不抢焦点。
// 找得到「搜索」按钮就 AXPress，否则对输入框发 AXConfirm。
// 用法: swift axsearch.swift <pid> <关键词>
import ApplicationServices
import AppKit

guard CommandLine.arguments.count > 2,
      let pid = Int32(CommandLine.arguments[1]) else {
    print("usage: axsearch.swift <pid> <query>")
    exit(2)
}
let query = CommandLine.arguments[2]

func attr(_ el: AXUIElement, _ key: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, key as CFString, &value) == .success else { return nil }
    return value as AnyObject?
}

var field: AXUIElement?
var button: AXUIElement?
func walk(_ el: AXUIElement, _ depth: Int) {
    if depth > 14 { return }
    let role = attr(el, kAXRoleAttribute) as? String ?? ""
    let desc = attr(el, kAXDescriptionAttribute) as? String ?? ""
    let title = attr(el, kAXTitleAttribute) as? String ?? ""
    if field == nil && role == "AXTextField" { field = el }
    if button == nil && (desc.contains("搜索") || title == "搜索") && role == "AXButton" { button = el }
    if let children = attr(el, kAXChildrenAttribute) as? [AXUIElement] {
        for child in children { walk(child, depth + 1) }
    }
}

walk(AXUIElementCreateApplication(pid), 0)
if field == nil { print("no field"); exit(1) }
let setErr = AXUIElementSetAttributeValue(field!, kAXValueAttribute as CFString, query as CFTypeRef)
print("set value err=\(setErr.rawValue)")
if let b = button {
    let perr = AXUIElementPerformAction(b, kAXPressAction as CFString)
    print("pressed search err=\(perr.rawValue)")
} else {
    let cerr = AXUIElementPerformAction(field!, "AXConfirm" as CFString)
    print("confirm err=\(cerr.rawValue)")
}
