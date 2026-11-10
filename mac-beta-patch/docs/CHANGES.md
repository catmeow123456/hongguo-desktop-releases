# 修改清单

日期：2026-11-08
基线：Mac 1.0.9 beta.2（`mac-v1.0.9-beta.2`，Apple Silicon）

## 1. 登录持久化

- 新增 `app/bridge.py`，在 `127.0.0.1:8787` 实现设备码 / 二维码流程。
- 账号保存在 `~/Library/Application Support/HongguoBridge/account.json`，权限 `0600`；后续启动直接复用。
- `bin/launch.sh` 负责启动桥接、App（带 `HONGGUO_DESKTOP_AUTH_BASE_URL` 等环境变量）和 `autologin.js`。
- `autologin.js` 通过 System Events 点击「扫码登录 → 获取登录二维码」；桥接从本地账号即时返回，不再展示二维码。

## 2. 搜索修复

内置搜索无法在外部修复：它由闭源 Rust 直接请求上游，失败时只记录
`search_request_failed reason=decode status=200`，不会回退到 Python 后端。

新增：

- `app/lab.html`：单文件搜索页，调用桥接代理的 `/app/search/page`、`/app/episodes`、`/app/stream`。
- 后端 `guest_search_fallback.py` + `desktop_guest_search.py`：原签名路径被上游拒绝（`code 100103`）时，改用匿名设备与 version-3 搜索签名帧，仍能拿到结果。
- 页面「加载更多」按 `series_id` 去重；若整页都是重复项则停止并提示。

验证：`闪婚` 返回 6 部，加载更多后 40 部；`总裁`、`离婚`、`重生` 均返回结果。

## 3. 收藏

- `bridge.py` 的 `POST /favorite` 校验 `series_id`、限制字段长度，并 upsert 到：
  `~/Library/Application Support/cn.guoban.desktop-companion/guoban.sqlite3` → `favorites`。
- App 只在启动时读取收藏，因此页面提示「重启 App 生效」，并提供 `/restart-app`。

验证：写入《闪婚，她甜的过头》后重启 App，收藏页出现该剧，详情显示「已收藏」。

## 4. App 内播放

- 原 `/playback` 只带 `ep=1` 调 `/play`，返回 1 集，App 认为全剧只有 1 集，播放一直转圈。
- 现在先调 `/episodes` 得到真实集数，再调 `/play?ep=N` 取当前集；`total` 返回完整集数。
- 可选 `vid` 路径继续走 `/video_url`。
- 自动连播经常失败：后端 `/stream` 首次请求会内联完成 CDN 下载 + 解密 +
  H.264 转码，冷缓存的下一集经常超出播放器等待。现在 `/playback` 返回前
  会在后台用 `Range: bytes=0-0` 的 1 字节请求预热下一集（420s 超时，失败
  30s 后重试一次），播放器切集时缓存已就绪、秒切。

验证：收藏中《闪婚，她甜的过头》显示「第 1/78 集」，第 1 集 02:16、第 2 集 02:09，均可播放与切集。

## 5. 发现片单 / 详情本地仿真（2026-11-09）

官网 `hongguoduanju.com` 的榜单页与搜索页已改为客户端渲染（`content` 为空、
无 `_ROUTER_DATA`），App 的 Rust 抓取解析不到数据，导致发现页只显示
「更新失败，保留上次片单」。

排查结论：

- App 有两类 HTTP 客户端。官网抓取（片单/详情）接受任意 URL；搜索客户端
  强制 `https` 且使用编译内置的根证书（rustls + webpki-roots），既不读
  钥匙串，也不接受纯 http，因此**搜索通道在本地无解**，保持原状。
- 官网抓取通道改为等长 URL 补丁：`https://hongguoduanju.com`（25 字节）→
  `http://127.0.0.1:8787/abc`（25 字节），拼接出的
  `/abc/rank/*`、`/abc/detail` 落到桥接仿真路由。

新增：

- `bin/patch_urls.py`：幂等二进制补丁（先备份到 `$STATE/backup`，替换
  4 处 URL 常量后 ad-hoc 重签），由 `install.sh` 自动调用。
- `bridge.py` 仿真层：`rank_page()` 按 App 解析器的选择器生成榜单 HTML
  （`article[aria-labelledby=rank-title-N]` + 对应 `h2 id`、
  `a[href^='/detail?series_id=']`、`aria-label="播放第 1 集 …"`、
  `p[class*='categories'] span[class*='description']`、
  `span.episode`「更新至N集 全共N集」、`_ROUTER_DATA` + `req.webID`）；
  `detail_page()` 输出详情字段群与 `video_data`；另含 `/novelfm` 搜索
  仿真（当前仅调试用，搜索客户端不会连 http，见上）。

验证（本机 1.0.9 beta.2）：点「刷新片单」后 `/abc/rank/hot-drama → 200`，
发现页横幅由「更新失败」变为正常，卡片标题/简介/集数解析成功；点击卡片
打开详情并起播（`/app/episodes → 200`，播放器正常渲染）。

排查用的按 PID AX 自动化小工具收进 `bin/ax/`（winid / axdump / axpress /
axsearch / axconfirm，见其 README）：不抢焦点，可在用户使用其他窗口时
驱动和检查 App UI；需要给终端授予「辅助功能」权限。

2026-11-09 收尾清理：移除排查期的临时脚手架——`/tmp` 请求记录、
novelfm 响应模式调试开关、以及最终方案用不上的 TLS 监听（:8888）；
最终桥接只保留 8787 纯 HTTP 一条通道。

## 6. 桥接守护与日志（2026-11-10）

- 排查「发现片单暂时加载失败」复发：桥接进程被外部静默杀死（日志无
  traceback），App 重启后连不上 8787，片单请求直接失败。
- 修复：`bin/cn.guoban.hongguo-bridge.plist.in` + `install.sh` 注册用户级
  LaunchAgent（`~/Library/LaunchAgents/cn.guoban.hongguo-bridge.plist`，
  KeepAlive + ThrottleInterval 5s），桥接异常退出 5 秒内自动拉起；已验证
  kill 后自动恢复。`--no-start` 模式不注册。
- `bridge.py` 日志加时间戳，启动行带 pid，便于下次定位。
- 注意：直接从 Dock 启动 红果免费短剧.app 会缺 `HONGGUO_DESKTOP_*`
  环境变量（App 内播放接口可能受影响）；应从 红果搜片（其 launcher 调
  launch.sh）或 `/restart-app` 启动。

## 7. 清理

- 移除为定位搜索问题临时加在 `desktop_bootstrap.py` 的 `search_watch` 中间件（不再写 `/tmp/hg_beta/api_watch.log`）。

## 7. 安装脚本验证

- 从 `mac-v1.0.9-beta.2` DMG 复制一份干净 App，在 `--no-start` 模式下运行 `install.sh`：后端文件与 `third_party/phone-account-protocol` 复制正确，补丁模块可导入，`红果搜片.app` 生成并通过 ad-hoc 签名。
- 本机实装验证：搜索、加载更多、网页播放、写入收藏、App 内 78 集播放与切集均通过（见上文）。

## 未改动 / 已知限制

- App 内置搜索框仍然不可用（搜索客户端强制 https + 内置根证书，本地无法
  劫持；需要公网域名 + 证书中继才能打通，暂不做）。搜索请使用 红果搜片。
- App 播放页的弹幕、评论等上游能力未逐一验证。
- 依赖测试版内部文件布局；升级 App 后补丁可能需要更新。
