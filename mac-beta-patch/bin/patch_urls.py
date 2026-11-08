#!/usr/bin/env python3
"""Patch the app binary so its website-fetch URLs point at the local bridge.

The app scrapes hongguoduanju.com for 发现片单 / detail enrichment with an
HTTP client that (unlike the search client) accepts plain http:// URLs.  We
replace every 25-byte occurrence of the website base URL with an equal-length
alias that lands on the bridge's emulation routes:

    https://hongguoduanju.com   (25 bytes)
    ->  http://127.0.0.1:8787/abc   (25 bytes)

so base + "/rank/hot-drama" etc. become http://127.0.0.1:8787/abc/rank/...
which bridge.py serves with locally generated pages.

Idempotent: already-patched binaries are left untouched.  The pristine
binary is backed up to $STATE/backup/ before the first patch.  The search
endpoint (api5-sinfonlinea.novelfm.com) is intentionally NOT redirected: the
search client enforces https and rejects non-public CAs, so search stays on
its stock (broken) path; use 红果搜片 for search.

Usage: patch_urls.py <path-to-binary> <backup-dir>
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

OLD = b"https://hongguoduanju.com"
NEW = b"http://127.0.0.1:8787/abc"
assert len(OLD) == len(NEW) == 25


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: patch_urls.py <binary> <backup-dir>", file=sys.stderr)
        return 2
    binary = Path(sys.argv[1])
    backup_dir = Path(sys.argv[2])
    data = binary.read_bytes()

    if NEW in data and OLD not in data:
        print("二进制已打过补丁，跳过。")
        return 0

    hits = [i for i in range(len(data) - 25) if data[i:i + 25] == OLD]
    if not hits:
        print("未找到官网 URL 常量（App 版本可能不匹配），跳过补丁。")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:12]
    backup = backup_dir / f"companion.orig-{digest}"
    if not backup.exists():
        shutil.copy2(binary, backup)
        print(f"已备份原始二进制: {backup}")

    patched = bytearray(data)
    for i in hits:
        patched[i:i + 25] = NEW
    binary.write_bytes(bytes(patched))

    sign = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(binary.parent.parent.parent)],
        capture_output=True, text=True)
    if sign.returncode != 0:
        print("重新签名失败: " + sign.stderr.strip(), file=sys.stderr)
        return 1
    print(f"已替换 {len(hits)} 处 URL 常量并重新签名。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
