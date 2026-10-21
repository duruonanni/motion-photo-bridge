#!/usr/bin/env python3
"""Apply the bridge to one vivo pair that is already inside Immich.

Steps, all reversible:
  1. back up the original jpg + mp4
  2. transcode the clip to limited-range H.264 (fixes Windows black frames)
  3. merge jpg + clip into a single Motion Photo (EXIF untouched)
  4. atomically replace the image file
  5. move the clip to encoded-video/*-MP.mp4 so Immich skips it on download
  6. update checksums / paths in the database
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import transcode as tc  # noqa: E402
from . import vivo  # noqa: E402

MEDIA = Path(os.environ.get("IMMICH_MEDIA_DIR", "/mnt/ztb/media/immich"))
BACKUP = Path(os.environ.get("MPB_BACKUP_DIR", "/mnt/ztb/immich-backup/vivo-originals"))
COMPOSE = Path(os.environ.get("IMMICH_COMPOSE_DIR", Path.home() / "hermes-workspace/projects/immich"))


def psql(query: str) -> list[list[str]]:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "database", "psql", "-U", "postgres",
         "immich", "-t", "-A", "-F", "|", "-c", query],
        cwd=COMPOSE, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:400])
    return [line.split("|") for line in proc.stdout.strip().splitlines() if line]


def host_path(container_path: str) -> Path:
    return MEDIA / Path(container_path).relative_to("/data")


def sha1_hex(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".mpb-tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def back_up(stem: str, img_id: str, img_file: Path, img_name: str,
            vid_file: Path, backup_clip: str, create: bool = True) -> Path:
    """Pick a backup directory and create it.

    Two accounts can hold two different photos with the same filename, so a
    plain ``BACKUP/<stem>`` would silently overwrite the first one.  When the
    stem is taken by different bytes, fall back to ``<stem>__<asset-id>``.
    """
    preferred = BACKUP / stem
    pick = preferred
    if preferred.exists():
        same = True
        for source, name in ((img_file, img_name), (vid_file, backup_clip)):
            target = preferred / name
            if target.exists() and sha1_hex(target) != sha1_hex(source):
                same = False
                break
        if not same:
            pick = BACKUP / f"{stem}__{img_id[:8]}"
    if create:
        pick.mkdir(parents=True, exist_ok=True)
    return pick


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--crf", type=int, default=20)
    args = ap.parse_args(argv)

    rows = psql(f"""SELECT i.id, i."originalFileName", i."originalPath", i."ownerId",
                           v.id, v."originalPath"
                    FROM asset i JOIN asset v ON v.id = i."livePhotoVideoId"
                    WHERE i.id = '{args.image_id}' AND i.type='IMAGE';""")
    if not rows:
        raise SystemExit("image not found or has no linked video")
    img_id, img_name, img_path, owner, vid_id, vid_path = rows[0]
    img_file, vid_file = host_path(img_path), host_path(vid_path)

    print(f"图片: {img_name}  {img_file}")
    print(f"视频: {vid_file}")
    print(f"属主: {owner}")

    info = tc.probe(vid_file)
    print(f"视频参数: {info.codec} {info.width}x{info.height} {info.pix_fmt}/{info.color_range} {info.duration:.2f}s")

    # 配对闸门:vivo 的私有 livephoto id 是权威依据。两个文件都带 id 却不一致,
    # 说明这一对是靠文件名猜的(相册改名/同秒连拍都会踩到),宁可中止也别合并错。
    img_id_tag = vivo.live_photo_id(img_file)
    vid_id_tag = vivo.live_photo_id(vid_file)
    print(f"私有 ID: 图片={img_id_tag} 视频={vid_id_tag}")
    if img_id_tag and vid_id_tag and img_id_tag != vid_id_tag:
        raise SystemExit(f"ID 不一致,拒绝合并(图片 {img_id_tag} != 视频 {vid_id_tag})")

    # 用照片的拍摄时间覆盖视频里的 creation_time:部分 vivo 视频带的是"传输时间"
    # 而不是拍摄时间,会让合并工具判定为 Ambiguous。
    exif = psql(f"""SELECT to_char("dateTimeOriginal" AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                    FROM asset_exif WHERE "assetId" = '{img_id}';""")
    photo_time = exif[0][0] if exif and exif[0][0] else None
    print(f"照片拍摄时间(UTC): {photo_time}  / 视频自带: {info.creation_time}")
    if not info.needs_range_fix:
        print("→ 已经是标准范围,跳过转码")

    stem = Path(img_name).stem
    # 一律以 .mp4 落盘:合并工具只认 .mp4,而源可能是 .MOV(Apple 形态)或相册改名后的名字
    vid_name = stem + ".mp4"
    # 备份保留原始扩展名(.MOV 就存 .MOV),合并用的落盘名才是 .mp4
    backup_clip = stem + Path(vid_path).suffix
    backup_dir = back_up(stem, img_id, img_file, img_name, vid_file, backup_clip,
                         create=not args.dry_run)
    print(f"备份目录: {backup_dir}")

    if args.dry_run:
        print("\n[dry-run] 将执行: 备份 → 转码 → 合并 → 替换原图 → 视频移到 encoded-video → 更新 DB")
        return 0

    shutil.copy2(img_file, backup_dir / img_name)
    shutil.copy2(vid_file, backup_dir / backup_clip)
    print(f"✅ 已备份原始文件到 {backup_dir}")

    with tempfile.TemporaryDirectory(prefix="mpb-apply-") as td:
        work = Path(td)
        clip = vid_file
        if info.needs_range_fix:
            clip = work / "transcoded.mp4"
            tc.transcode(vid_file, clip, crf=args.crf, creation_time=photo_time)
            print(f"✅ 已转码 -> {tc.probe(clip).pix_fmt}/{tc.probe(clip).color_range}")

        pair_dir = work / "pair"
        pair_dir.mkdir()
        shutil.copy2(img_file, pair_dir / img_name)
        shutil.copy2(clip, pair_dir / vid_name)
        out_dir = work / "out"
        proc = subprocess.run(
            [sys.executable, "-m", "motion_photo_bridge._vendor.vivo_motion_photo", "convert", str(pair_dir),
             "--output", str(out_dir), "--include-probable"],
            capture_output=True, text=True,
        )
        merged = out_dir / (stem + ".jpg")
        if proc.returncode != 0 or not merged.exists():
            raise SystemExit(f"合并失败: {(proc.stderr or proc.stdout)[-500:]}")

        # 4) 替换图片文件
        atomic_copy(merged, img_file)
        print(f"✅ 图片已替换: {img_file.stat().st_size/1e6:.2f} MB")

        # 5) 视频挪到 encoded-video/<owner>/xx/yy/<vid>-MP.mp4
        enc_dir = MEDIA / "encoded-video" / owner / vid_id[:2] / vid_id[2:4]
        enc_dir.mkdir(parents=True, exist_ok=True)
        enc_file = enc_dir / f"{vid_id}-MP.mp4"
        atomic_copy(clip, enc_file)
        enc_container = f"/data/encoded-video/{owner}/{vid_id[:2]}/{vid_id[2:4]}/{vid_id}-MP.mp4"
        print(f"✅ 视频已复制到 {enc_file}")

        # 6) 更新 DB
        new_duration = int(round(tc.probe(clip).duration * 1000))
        psql(f"""UPDATE asset SET checksum=decode('{sha1_hex(img_file)}','hex'),
                        "updateId"=immich_uuid_v7(), "updatedAt"=now()
                 WHERE id='{img_id}';""")
        psql(f"""UPDATE asset_exif SET "fileSizeInByte"={img_file.stat().st_size}
                 WHERE "assetId"='{img_id}';""")
        psql(f"""UPDATE asset SET "originalPath"='{enc_container}',
                        checksum=decode('{sha1_hex(enc_file)}','hex'),
                        duration={new_duration},
                        "updateId"=immich_uuid_v7(), "updatedAt"=now()
                 WHERE id='{vid_id}';""")
        print("✅ 数据库已更新")

    # 7) 删除 upload 下的旧视频(已备份)
    vid_file.unlink(missing_ok=True)
    print(f"✅ 已移除旧视频文件 {vid_file}")

    print("\n=== 结果 ===")
    for r in psql(f"""SELECT id, type, "originalFileName", "originalPath",
                             encode(checksum,'hex'), duration, visibility
                      FROM asset WHERE id IN ('{img_id}','{vid_id}');"""):
        print(" | ".join(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
