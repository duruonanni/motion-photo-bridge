#!/usr/bin/env python3
"""Immich vivo 动态照片 —— Workflow Webhook 接收服务。

架构:
    Immich Workflow(AssetCreate) --webhook--> 本服务
    本服务找到同名的 jpg/png,把视频关联到图片并隐藏视频。

为什么用 Webhook 而不是定时任务:
  - 资产入库即触发,不用轮询、不用等 6 小时;
  - 不入侵 Immich 源码,升级不失效;
  - 逻辑集中在一个可测试的小服务里。

启动:
    python3 immich_vivo_webhook.py --port 9100
环境变量:
    IMMICH_COMPOSE_DIR  Immich compose 目录(默认 ~/hermes-workspace/projects/immich)
    VIVO_OWNER_EMAIL    处理的属主(默认 vivocamera@duruonanni.net)
    API_KEY             可选;设置后改用官方 API 写入(推荐,会触发 websocket 事件)
    IMMICH_URL          配合 API_KEY 使用(默认 http://localhost:2283)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMPOSE_DIR = Path(os.environ.get("IMMICH_COMPOSE_DIR", Path.home() / "hermes-workspace/projects/immich"))
OWNER_EMAIL = os.environ.get("VIVO_OWNER_EMAIL", "vivocamera@duruonanni.net")
API_KEY = os.environ.get("API_KEY", "").strip()
IMMICH_URL = os.environ.get("IMMICH_URL", "http://localhost:2283").rstrip("/")
MAX_MOTION_MS = 10_000

IMAGE_EXT = re.compile(r"\.(jpg|jpeg|png|heic|heif)$", re.I)
VIDEO_EXT = re.compile(r"\.(mp4|mov|3gp)$", re.I)

log = logging.getLogger("vivo-link")


def sql(query: str) -> list[list[str]]:
    """执行 SQL,返回行(每行是字段列表)。"""
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "database",
         "psql", "-U", "postgres", "immich", "-t", "-A", "-F", "|", "-c", query],
        cwd=COMPOSE_DIR, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed: {proc.stderr.strip()[:300]}")
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line]


def q(value: str) -> str:
    """SQL 字符串字面量转义。"""
    return "'" + value.replace("'", "''") + "'"


def stem_of(name: str) -> str:
    return IMAGE_EXT.sub("", VIDEO_EXT.sub("", name)).lower()


def link_via_sql(image_id: str, video_id: str) -> None:
    sql(f"""UPDATE asset SET "livePhotoVideoId"={q(video_id)}
            WHERE id={q(image_id)} AND "livePhotoVideoId" IS NULL;""")
    sql(f"""UPDATE asset SET visibility='hidden'
            WHERE id={q(video_id)} AND visibility<>'hidden';""")


def link_via_api(image_id: str, video_id: str) -> None:
    body = json.dumps({"livePhotoVideoId": video_id}).encode()
    req = urllib.request.Request(
        f"{IMMICH_URL}/api/assets/{image_id}", data=body, method="PUT",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"API returned {resp.status}")
    # 官方 onBeforeLink 会自动隐藏视频;再兜一次以防版本差异
    body = json.dumps({"visibility": "hidden"}).encode()
    req = urllib.request.Request(
        f"{IMMICH_URL}/api/assets/{video_id}", data=body, method="PUT",
        headers={"Content-Type": "application/json", "x-api-key": API_KEY},
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:  # noqa: BLE001
        pass


def handle_asset(asset: dict) -> str:
    """返回处理结果字符串(用于日志/响应)。"""
    asset_id = asset.get("id") or ""
    name = asset.get("originalFileName") or ""
    kind = asset.get("type") or ""
    owner = asset.get("ownerId") or ""

    if kind != "VIDEO" or not VIDEO_EXT.search(name):
        return "skip: not a video"
    if not asset_id:
        return "skip: missing id"

    stem = stem_of(name)
    rows = sql(f"""
        SELECT v.duration, u.email FROM asset v JOIN "user" u ON v."ownerId"=u.id
        WHERE v.id={q(asset_id)};""")
    if not rows:
        return "skip: video row not found"
    duration = int(float(rows[0][0] or 0))
    email = rows[0][1]
    if email != OWNER_EMAIL:
        return f"skip: owner {email} not handled"
    if duration > MAX_MOTION_MS:
        return f"skip: duration {duration}ms > {MAX_MOTION_MS} (real video)"

    # 找同属主、同 stem、还没关联的图片
    images = sql(f"""
        SELECT a.id FROM asset a
        WHERE a.type='IMAGE' AND a."ownerId"={q(owner)}
          AND a."livePhotoVideoId" IS NULL
          AND lower(regexp_replace(a."originalFileName", '\\.(jpg|jpeg|png|heic|heif)$', '')) = {q(stem)}
        LIMIT 1;""")
    if not images:
        return f"defer: no unlinked image yet for stem '{stem}'"

    image_id = images[0][0]
    if API_KEY:
        link_via_api(image_id, asset_id)
        backend = "api"
    else:
        link_via_sql(image_id, asset_id)
        backend = "sql"

    log.info("linked image=%s video=%s (%s, %.1fs)", image_id, asset_id, backend, duration / 1000)
    return f"linked: image={image_id} video={asset_id} backend={backend}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        log.debug(fmt, *args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "backend": "api" if API_KEY else "sql",
                             "owner": OWNER_EMAIL})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/immich/vivo-link"):
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # noqa: BLE001
            return self._send(400, {"error": f"bad json: {exc}"})

        asset = ((payload.get("data") or {}).get("asset") or payload.get("asset") or {})
        try:
            result = handle_asset(asset)
        except Exception as exc:  # noqa: BLE001
            log.exception("handle failed")
            return self._send(500, {"error": str(exc)})
        log.info("webhook %s -> %s", asset.get("originalFileName"), result)
        self._send(200, {"result": result})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("listening on %s:%s (backend=%s, owner=%s)",
             args.host, args.port, "api" if API_KEY else "sql", OWNER_EMAIL)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
