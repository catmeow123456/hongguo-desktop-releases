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

验证：收藏中《闪婚，她甜的过头》显示「第 1/78 集」，第 1 集 02:16、第 2 集 02:09，均可播放与切集。

## 5. 清理

- 移除为定位搜索问题临时加在 `desktop_bootstrap.py` 的 `search_watch` 中间件（不再写 `/tmp/hg_beta/api_watch.log`）。

## 未改动 / 已知限制

- App 内置搜索框仍然不可用。
- App 播放页的弹幕、评论等上游能力未逐一验证。
- 依赖测试版内部文件布局；升级 App 后补丁可能需要更新。
