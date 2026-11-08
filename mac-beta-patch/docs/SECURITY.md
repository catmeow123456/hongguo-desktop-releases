# 数据与隐私边界

- 仓库不包含账号、Cookie、设备标识、API key、播放地址或数据库文件。
- 运行时账号只保存在本机：
  `~/Library/Application Support/HongguoBridge/account.json`（`0600`）。
- 桥接只绑定 `127.0.0.1`，没有对外监听，也没有遥测上报。
- 搜索页只代理本机 App 后端；后端 API key 在 App 每次启动时生成，桥接从进程环境读取，不落盘到本项目。
- 收藏写入的是 App 自己的 SQLite：
  `~/Library/Application Support/cn.guoban.desktop-companion/guoban.sqlite3`。
- `third_party/phone-account-protocol/` 来自测试版安装包，保留 `THIRD_PARTY_NOTICES.md` 和 `LICENSE.ssovit`；不使用真实账号做测试。
- 不要把 `~/Library/Application Support/HongguoBridge/` 或 App 数据目录提交到仓库。
