# 红果 Mac 测试版本地补丁

这是 [`waligoraamodio288-rgb/hongguo-desktop-releases`](https://github.com/waligoraamodio288-rgb/hongguo-desktop-releases) 的个人 fork，存放针对 **Mac 1.0.9 beta.2（Apple Silicon）** 的本地修复。上游是发行说明与安装包仓库，不包含源码；这里的补丁以可复用的 overlay 形式整理，不改动上游 README 的发行内容。

## 解决的问题

| 问题 | 原因 | 本补丁的做法 |
| --- | --- | --- |
| 每次打开 App 都要重新扫码登录 | 测试版只实现了 OAuth 设备码协议，没有配套的授权服务 | 在 `127.0.0.1:8787` 实现设备码/二维码桥接，并保存会话；启动时自动恢复 |
| App 内置搜索几乎必失败（HTTP 200 但 JSON 解码失败） | 闭源 Rust 直接请求 novelfm / hongguoduanju 两个已失效的第三方接口，完全绕过自带 Python 后端 | 提供独立的「红果搜片」页面，走 App 自带且可用的后端；内置搜索本身无法在外部修复 |
| 注入收藏后点播一直「正在加载」 | 桥接的 `/playback` 只请求第 1 集，App 误以为全剧只有 1 集 | `/playback` 先取 `/episodes` 得到真实集数，再取对应集的播放地址 |
| 想看的剧搜不到，也没法加入 App 收藏 | 内置搜索不可用 | 搜索页支持播放、切集，并可把剧写入 App 的 `favorites` 表 |

## 目录

```
mac-beta-patch/
├── install.sh                     # 一键安装到当前用户环境
├── app/
│   ├── bridge.py                  # 登录桥接 + 后端代理 + 收藏写入 + 本地页面服务
│   └── lab.html                   # 红果搜片（单文件，无外部依赖）
├── bin/
│   ├── launch.sh                  # 启动桥接 / App / 自动登录
│   ├── autologin.js               # System Events 自动点击登录
│   └── search-app/                # 「红果搜片.app」模板
├── backend/                       # 写入 App 自带 Python 后端的兼容层
│   ├── desktop_bootstrap.py       # 装载下列补丁
│   ├── guest_search_fallback.py   # 匿名设备搜索兜底（/search、/search/page）
│   ├── desktop_guest_search.py    # 搜索请求签名与解析
│   ├── desktop_public_player.py   # 公开访客播放兼容
│   ├── fresh_player.py            # 空媒体响应的匿名设备恢复
│   ├── desktop_fresh_playback.py
│   ├── desktop_spare_player.py
│   └── desktop_playback_request.py
├── third_party/
│   └── phone-account-protocol/    # 设备/签名组件，保留原 THIRD_PARTY_NOTICES
├── docs/
│   ├── CHANGES.md                 # 修改清单与验证记录
│   └── SECURITY.md                # 数据与隐私边界
└── bin/smoke-test.sh              # 安装后的只读冒烟检查
```

## 安装

前置：macOS 14+、Apple Silicon、已安装 **红果免费短剧 1.0.9 beta.2**，以及 Python 3.10+（脚本只使用标准库）。

```bash
git clone git@github.com:catmeow123456/hongguo-desktop-releases.git
cd hongguo-desktop-releases/mac-beta-patch
bash install.sh
```

安装脚本会：

1. 在 `~/Library/Application Support/HongguoBridge/` 建立运行目录与 Python venv；
2. 把桥接、搜索页和启动脚本复制进去；
3. 备份并覆盖 App 的 `Contents/Resources/backend/` 中对应的 Python 文件；
4. 在 `/Applications/` 生成 ad-hoc 签名的 `红果搜片.app`；
5. 启动桥接（如果 App 已在运行，需要退出重开才能加载后端补丁）。

安装后打开 **红果搜片**，或直接访问 <http://127.0.0.1:8787/lab>。

## 使用

- 搜索：输入剧名，结果来自 App 自带后端；「加载更多」会去重后追加。
- 播放：点封面或「播放」，右侧选集可直接切集；视频经 Range 代理，支持拖动。
- 收藏：卡片上的「收藏」会写入 App 的 `favorites` 表。App 只在启动时读取收藏，所以页面会提示点「重启 App 生效」。
- 登录：第一次需要扫码；之后 `launch.sh` 会调用 `autologin.js` 自动恢复账号。
- App 内置搜索框仍然是坏的，这是闭源二进制的行为；搜索请用「红果搜片」。

## 冒烟检查

```bash
bash bin/smoke-test.sh
```

检查桥接健康、搜索、剧集列表、视频 Range 响应。脚本只读，不写账号或收藏。

## 边界

- 只支持本机回环访问，不对外提供服务。
- 补丁依赖测试版的目录结构与 Python 后端；App 升级后可能失效，需要重新核对。
- `third_party/phone-account-protocol/` 来自测试版安装包，保留其 `THIRD_PARTY_NOTICES.md` 与 MIT 许可；本项目不重新分发 App 本体或视频内容。
- 本项目与红果官方、上游发行方均无隶属关系；仅供个人学习与自用。
