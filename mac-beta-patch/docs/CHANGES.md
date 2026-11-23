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

## 7. 发现片单韧性重构（2026-11-10）

「发现片单暂时加载失败」toast 复发的根因不止桥接被杀一个：

- App 重启时捆绑后端一起重启，启动初期的片单请求落在未就绪的后端上，
  `rank_items()` 拿到空数据，桥接仍返回 200 + **0 张卡片**，App 解析空列表
  即弹 toast。现在 `backend_get_retry()` 短重试一次。
- 上游 `category/landpage` 分类接口已死（返回非 JSON），`/browse` 的
  short_play / ai_series 两块榜单永远为空——这两个 slug 在二进制里是
  编译死的，App 请求必失败。已把 4 个 slug 全部重映射到健康的
  `/rank?board=recommend|hot|new`。
- `rank_page()` 重写为 stale-while-revalidate：新鲜缓存直接秒回；过期缓存
  先回后由后台线程刷新；无缓存才同步抓取并落盘
  （`$STATE/cache/rank-<slug>.html`，TTL 600s）。桥接启动 15s 后还会预热
  全部 slug。效果：App 的片单请求永远立即拿到有内容的页面，任何
  桥接/后端/上游故障都不再产生空列表。

验证：4 个 slug 均 200 且含卡片；二次请求 2–7ms（缓存命中）。

- 2026-11-23 起此注意事项已失效：见第 8 节，Dock/Finder 直启也可用。

## 8. Dock 直启、签名列表与缓存清理（2026-11-23）

- **Dock/Finder 直接启动**：`install.sh` 向 App 的 `Info.plist` 注入
  `LSEnvironment`（与 `launch.sh` 导出的同一组变量），LaunchServices 会在
  从 Dock/Finder/`open` 启动时应用；原 plist 备份在
  `$STATE/backup/Info.plist.orig`。桥接新增 `watch_app()` 线程（每 3s 轮询
  App pid），出现新进程即自动跑一次 `autologin.js`，并暴露
  `POST /autologin`。已用 `open -b cn.guoban.desktop-companion` 验证：
  环境变量到位、自动登录成功。
- **签名列表请求**：第 7 节「`/browse` 分类接口已死」的判断有误。真实
  原因是上游开始校验分类/榜单接口的签名，而后端只在 `HG_SIGN_LIST=1`
  时才对这些请求签名（否则报 "Upstream did not return JSON"）。已在
  `launch.sh`、`install.sh`（新装 + 旧装补键）和已安装的 plist 中加入该
  变量。实测 short_play / ai_series 返回真实数据。
- **榜单重新映射**：`hot-real-drama` → `/browse?genre=short_play`、
  `hot-ai-drama` → `/browse?genre=ai_series`（按 hot_score），失败时回退到
  `/rank` 的 new / recommend；`hot-drama`、`hot-comic-drama` 不变。实测
  真人榜、AI 榜均为对应类型的剧集；缓存命中 4–5ms。
- **流缓存 LRU**：本机文件系统不维护 atime，桥接在每次播放时
  `touch` 对应剧集，再按 mtime 以整集（`{vid}_*`）为单位淘汰。上限
  `HONGGUO_STREAM_CACHE_MAX_GB`（默认 8），超过 30 天淘汰，30 分钟内用过的
  不动，顺带清理超过 1 天的 `.partial`。桥接启动 60s 后首跑，之后每 30 分钟。
- **移植 Windows 1.0.138 的剧集缓存校验**：`backend/hongguo.py` 与
  `backend/desktop_episode_cache.py`（与 1.0.138 对应文件一致，仅换行符
  不同）。`get_episodes` 命中缓存前先校验，空/坏的剧集列表不再被缓存。
- **未移植**：1.0.138 的播放相关改动（见「已知限制」）。

## 9. 清理

- 移除为定位搜索问题临时加在 `desktop_bootstrap.py` 的 `search_watch` 中间件（不再写 `/tmp/hg_beta/api_watch.log`）。

## 10. 安装脚本验证

- 从 `mac-v1.0.9-beta.2` DMG 复制一份干净 App，在 `--no-start` 模式下运行 `install.sh`：后端文件与 `third_party/phone-account-protocol` 复制正确，补丁模块可导入，`红果搜片.app` 生成并通过 ad-hoc 签名。
- 本机实装验证：搜索、加载更多、网页播放、写入收藏、App 内 78 集播放与切集均通过（见上文）。

## 11. 播放 SDK v5 移植（2026-11-24）

上游 11 月中旬起拒绝旧版播放签名（`multi_video_model` 回 200 空 body），
Windows 1.0.138 新增 SDK v5 签名栈应对。移植方式：

- **原样移植**（与 1.0.138 逐字节一致，仅换行符）：`backend/
  desktop_sdk_player_v5.py`、`desktop_codec_fallback.py`、
  `desktop_codec_urls.py`、`desktop_range_reader.py` 和 12 个
  `phone_*_v5.py` 协议模块。素材（`libmetasec_ml.so` 4.5MB +
  配置）首次播放时从官方 APK CDN 按哈希校验下载，缓存到后端数据目录。
- **平台胶水 `backend/desktop_v5_fallback.py`**：Windows DPAPI 在 macOS
  上不存在，替换为 AES-GCM + 数据目录内 0600 密钥文件（同 DPAPI 的
  「同用户可读」威胁模型）；`unicorn`（arm64 模拟器，跑原生签名库）或
  `Crypto` 缺失时自动禁用，后端行为与移植前完全一致。
- **接线**：`fresh_player.py` 在原匿名通道失败（且原因属
  empty-response / invalid-model，与 1.0.138 的 SdkFallback 同门槛）时才
  调 v5；v5 也拿不到则抛回原错误。v5 读取成功后模型沿用原有 5 小时缓存。
- **install.sh**：向捆绑 Python 安装 `unicorn==2.1.4`（uv 优先，pip 兜
  底；失败仅禁用 v5 并提示），安装结束统一重签 App。
- 状态文件在后端数据目录：`playback-spare.dpapi`（匿名设备，一次性注册
  带重试标记，遵循上游「不自动重放」语义）、`playback-sdk-v5.dpapi`（SDK
  会话，2 次握手预算）、`playback-v5-diagnostic.json`（统计）。

**App 发现页按卡片序号请求播放的问题**：实测 App 从发现页点开卡片后，
播放请求带的是 `series_id=1/2/3...`（卡片在榜单中的位置），不是 href 里的
真实 ID；历史/续播入口才发真实 ID。桥接新增 `record_rank_served()` /
`resolve_rank_index()`：服务榜单页时记录卡片顺序，播放请求带小数字时映射
回真实 ID（第 1 集重新解析，>1 集沿用上次映射，避免榜单刷新中途换剧）。
服务超过 2 小时或映射失败则按原样透传。

验证（2026-11-24 00:30–00:36）：临时副本从零跑通（注册→下载素材→2 次握
手→取模型 5.9s）；装进 App 后：桥接 `/playback` 200（第 3/4 集）、直链
Range 请求 206 返回真 MP4；**App 内实测**：重启自动续播 200、历史入口播放
《闪婚，她甜的过头》第 1→2 集 200、流缓存新增剧集并在后台转码、v5 读取
3 模型 0 失败；卡片索引 POST `{3,1}` 映射到《和离后…完结篇》第 1 集 200。

## 12. 播放恢复与换集提速（2026-11-24）

第 11 节落地后的三个收尾补丁：

- **`POST /v5-reset`**（bridge.py）：v5 的匿名设备与 SDK 会话都是上游
  设计的一次性状态，注册时断网或保存被打断会留下标记，之后所有播放静默
  失败。该端点精确删除这 16 类状态文件（`playback-spare.*`、
  `playback-sdk-v5*`、诊断文件；保留 AES 密钥与 4.5MB 已校验素材），
  下次播放自动重新注册。`?restart=1` 会顺带重启 App（后端内存里还缓存着
  旧 Reader，需重启才会重建）。实测：重置→首次解析 11.2s 完成从零重建
  （新设备 + 2 次 SDK 握手）→ 200。
- **旧通道短路**（fresh_player.py）：上游当前对旧匿名播放通道回空
  body，且 `fetch_batch` 每次都先注册一个一次性设备再失败。失败一次后
  10 分钟内直接抛 `empty-response` 走 v5（与 v5 的入口门槛一致），每
  TTL 一次请求重新探测旧通道、上游恢复即自动回退。实测未缓存剧集解析
  10.4s → 3.7s（首次）/ 1.4s（后续）。
- **启动竞态重试**（bridge.py resolve_playback）：App 重启后的自动续播
  请求会落在还在初始化的后端上（曾实测 502）；且桥接的 3 秒端点缓存可能
  指向被杀掉的旧后端端口（RemoteDisconnected）。现在 get_json 每次调用
  重新解析端点，/play 在后端启动窗口（45s）或传输层错误时以 3s 间隔重试
  至 25s 上限，/episodes 同样有一次启动重试。实测：重启 App 后立即请求
  播放，26.4s 后返回 200（顶过整个启动窗口）。

## 13. 已知限制与遗留

- App 内置搜索框仍然不可用（搜索客户端强制 https + 内置根证书，本地无法
  劫持；需要公网域名 + 证书中继才能打通，暂不做）。搜索请使用 红果搜片。
- App 播放页的弹幕、评论等上游能力未逐一验证。
- **播放 v5 移植（2026-11-24，上游变更后恢复播放）**：上游对
  `multi_video_model` 播放接口返回 HTTP 200 + 空 body（账号通道和匿名
  备用通道都拿不到地址，`PLAY_FAILED / video_model`；与 11-23 改动无关，
  IPv4/IPv6/代理结果一致）。Windows 1.0.138 的解法是 SDK v5 签名栈，已按
  「原样移植 + 平台胶水」方式落地，见第 11 节。
- 依赖测试版内部文件布局；升级 App 后补丁可能需要更新。
