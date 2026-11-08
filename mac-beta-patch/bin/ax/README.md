# AX 自动化小工具（macOS）

排查/驱动红果 App UI 用的按 PID 辅助工具。全部通过 AXUIElement 按 PID
操作，**不抢焦点**——用户在其他窗口打字聊天时也能安全使用。

## 准备

- 给运行终端授予「辅助功能」权限：系统设置 → 隐私与安全性 → 辅助功能。
- 运行方式：`swift <工具>.swift <参数>`（首次即时编译稍慢）。
- 取 App PID：`pgrep -x hongguo-desktop-companion`。

## 工具

| 工具 | 用法 | 说明 |
| --- | --- | --- |
| `winid.swift` | `swift winid.swift` | 列出红果窗口的 windowId 与尺寸；配合 `screencapture -x -l <id> out.png` 可截取被遮挡的窗口 |
| `axdump.swift` | `swift axdump.swift <pid>` | dump AX 树（角色/标题/描述/值），用来找按钮标题；建议 `| grep "t='"` 过滤 |
| `axpress.swift` | `swift axpress.swift <pid> "刷新片单" [序号]` | 按标题/描述点按钮；同名按钮用序号选第几个（从 0 开始） |
| `axsearch.swift` | `swift axsearch.swift <pid> "关键词"` | 对第一个 AXTextField 设值并触发搜索（AXPress 搜索按钮，找不到就 AXConfirm） |
| `axconfirm.swift` | `swift axconfirm.swift <pid>` | 对第一个 AXTextField 依次尝试 AXConfirm / AXPress（部分输入框只认这个） |

## 典型流程

```bash
PID=$(pgrep -x hongguo-desktop-companion)
swift axdump.swift  "$PID" | grep "t='"        # 找到目标按钮的准确标题
swift axpress.swift "$PID" "发现"               # 点侧栏
swift axsearch.swift "$PID" "重生" && sleep 1 && swift axconfirm.swift "$PID"
```

注意：Tauri 应用重启后 PID 会变；AX 值设置后 App 可能立刻清空输入框，
属正常现象（以生命周期日志为准，见 docs/CHANGES.md）。
