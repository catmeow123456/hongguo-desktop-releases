// axdump.swift — 按 PID dump 辅助功能（AX）树：角色、标题、描述、值。
// 用来找按钮的准确标题，再交给 axpress / axsearch 使用。
// 用法: swift axdump.swift <pid> | grep "t='"
import ApplicationServices
import Foundation

guard CommandLine.arguments.count > 1, let pid = pid_t(CommandLine.arguments[1]) else {
    print("usage: axdump.swift <pid>")
    exit(2)
}

func describe(_ el: AXUIElement, _ depth: Int, _ out: inout [String]) {
    if depth > 12 { return }
    var role: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXRoleAttribute as CFString, &role)
    var title: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXTitleAttribute as CFString, &title)
    var desc: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXDescriptionAttribute as CFString, &desc)
    var val: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXValueAttribute as CFString, &val)
    let r = role as? String ?? "?"
    let t = (title as? String ?? "").prefix(24)
    let d = (desc as? String ?? "").prefix(24)
    let v = (val as? String ?? "").prefix(24)
    out.append(String(repeating: " ", count: depth * 2) + "\(r) t='\(t)' d='\(d)' v='\(v)'")
    var kids: AnyObject?
    AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &kids)
    if let arr = kids as? [AXUIElement] {
        for k in arr { describe(k, depth + 1, &out) }
    }
}

var out: [String] = []
describe(AXUIElementCreateApplication(pid), 0, &out)
print(out.joined(separator: "\n"))
