#!/usr/bin/env python3
"""Verify an applied pair: asset state, playback, and what a download archive contains."""
import io
import json
import os
import sys
import urllib.request
import zipfile

BASE = "http://localhost:2283"
EMAIL = "vivocamera@duruonanni.net"
PASSWORD = os.environ["IMMICH_VIVO_PASSWORD"]
IMG = sys.argv[1] if len(sys.argv) > 1 else "1d9bcc78-a325-4331-9bff-cef0d7e2c4f1"
VID = sys.argv[2] if len(sys.argv) > 2 else "3cd472cf-c536-4cbd-8533-a36cd1f35794"


def req(path, data=None, token=None, raw=False):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body,
                               headers={"Content-Type": "application/json",
                                        "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.read() if raw else json.loads(resp.read())


tok = req("/api/auth/login", {"email": EMAIL, "password": PASSWORD})["accessToken"]

img = req(f"/api/assets/{IMG}", token=tok)
print(f"图片: {img['originalFileName']}  size={img.get('exifInfo', {}).get('fileSizeInByte')}")
print(f"       livePhotoVideoId={img.get('livePhotoVideoId')}")

try:
    data = req(f"/api/assets/{VID}/video/playback", token=tok, raw=True)
    print(f"视频 playback: OK ({len(data)/1e6:.2f} MB 转码流)")
except Exception as exc:  # noqa: BLE001
    print(f"视频 playback: {exc}")

try:
    data = req(f"/api/assets/{VID}/original", token=tok, raw=True)
    print(f"视频原始文件: {len(data)/1e6:.2f} MB")
except Exception as exc:  # noqa: BLE001
    print(f"视频原始文件: {exc}")

blob = req("/api/download/archive", {"assetIds": [IMG]}, token=tok, raw=True)
zf = zipfile.ZipFile(io.BytesIO(blob))
print(f"\n下载归档: {len(zf.namelist())} 个文件")
for info in zf.infolist():
    print(f"  - {info.filename}  {info.file_size/1e6:.2f} MB")
