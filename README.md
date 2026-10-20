# motion-photo-bridge

**Live photos from vivo / iQOO phones don't work in Immich. This fixes that — including the photos already in your library.**

![Motion Photo demo](docs/demo.gif)

*One `.jpg`. Click it and the clip inside plays. (The demo uses a generated clip, not a personal photo.)*

## The problem

Immich recognises two kinds of live photo:

| Kind | How it is detected |
| --- | --- |
| Apple Live Photo | `HEIC` + `MOV` paired by `ContentIdentifier` metadata |
| Android Motion Photo | a single JPEG with the video embedded (`Camera:MotionPhoto` XMP or `MicroVideo`) |
| **vivo / iQOO live photo** | **neither** — two plain files joined only by a private `vivo Live Photo ID` that Immich never reads |

So a vivo live photo lands as **two assets**: the picture, plus a 2-second clip sitting next to it
in the timeline. Downloading gives you two files instead of one moving picture. And on Windows the
clip often plays as a **black frame with audio**, because vivo records it full-range (`yuvj420p`)
and the hardware decoder refuses it.

The same shape (a `JPG` and a same-stem `MP4`) shows up on some OPPO / realme builds.

## What this does

```
JPG + MP4 ──► optional transcode ──► merge ──► one Motion Photo JPEG
                                               (pixels + EXIF copied byte-for-byte)
```

1. **Pair** — same folder, same filename stem, clip ≤ 10 s.
2. **Transcode** (optional) — re-encode full-range clips to limited-range 4:2:0 BT.709 so Windows
   browsers and the Photos app play them instead of showing black. Clean clips are skipped.
3. **Merge** — append the clip to the JPEG and write the `Container:Directory` XMP block.
4. **Link** — in Immich the image gets `livePhotoVideoId` and the clip is hidden, which is exactly
   what Immich does for a phone it recognises.

## Install

```bash
git clone https://github.com/duruonanni/motion-photo-bridge
cd motion-photo-bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

`ffmpeg` and `ffprobe` must be on `PATH`.

## Inspect a file, offline

Open [`docs/viewer.html`](docs/viewer.html) in a browser and drop a JPEG on it. It tells you whether
the file is a Motion Photo, which spec it uses and how big the embedded clip is — and lets you play
it. No server, no upload, works straight from `file://`.

## Convert files before uploading

```bash
mpb scan    ~/Pictures/phone                       # what would be paired
mpb convert ~/Pictures/phone -o ~/Pictures/fixed --transcode
```

`convert` writes `<stem>.jpg` containing the clip. **The originals are never modified.**

## Fix a library you already have

This is the part most tools skip. If the split pairs are already imported you do **not** have to
delete and re-upload — asset IDs, albums, tags, favourites and dates all survive.

```bash
# one pair
python3 tools/apply_pair.py --image-id <asset-uuid> --dry-run
python3 tools/apply_pair.py --image-id <asset-uuid>

# the whole library, resumable
python3 tools/apply_batch.py --owner you@example.com --dry-run
python3 tools/apply_batch.py --owner you@example.com
```

Per pair it:

1. backs the original `jpg` + `mp4` up;
2. transcodes + merges;
3. replaces the image's file **in place** — the path and the asset ID never change;
4. moves the clip into Immich's `encoded-video/` folder, the folder Immich treats as "embedded
   video", so downloads skip it;
5. updates `checksum`, `originalPath`, `duration` and `fileSizeInByte` to match the new file.

It is **idempotent and resumable**: a pair whose clip already lives under `encoded-video/` is
skipped, so you can stop and restart freely. Every pair is backed up first, and rolling back means
copying the backup over the file again.

These scripts drive Immich through `docker compose exec … psql`, so they expect a local Compose
deployment. Point them elsewhere with `IMMICH_COMPOSE_DIR`, `IMMICH_MEDIA_DIR` and
`MPB_BACKUP_DIR`.

Verify a pair afterwards:

```bash
python3 tools/verify_apply.py <image-uuid> <video-uuid>
```

It reports the asset state and the contents of the download archive — you want to see **one** file
there.

## Keep it automatic

Immich has no third-party plugin loader yet, but since **v3.0** it ships workflows with a webhook
action, and that is the supported extension point:

```
phone uploads JPG + MP4
        │
        ▼
Immich ── Workflow (AssetCreate, filename matches \.mp4$) ──► POST
        │                                                     │
        ▼                                                     ▼
   asset in library                       motion-photo-bridge serve
                                                 (finds the same-stem image,
                                                  links it through the Immich
                                                  API; Immich then hides the clip)
```

1. Run the receiver:

   ```bash
   mpb serve --port 9100
   ```

   It needs an Immich API key for the account that owns the photos (`asset.read` +
   `asset.update`) in `API_KEY`, and `IMMICH_URL` pointing at your instance.

2. From that same account, paste [`docs/immich-workflow.json`](docs/immich-workflow.json) into
   **Utilities → Workflows** and point its `url` somewhere Immich can reach — on a Docker bridge
   network that is usually `http://172.17.0.1:9100/…`.

3. Take a live photo. Within seconds the timeline shows one picture with a motion button, and the
   loose clip is gone.

> Workflows are scoped to their owner: create it with the account that uploads the photos, not as
> an admin acting on someone else's behalf.

If a clip arrives before its photo the receiver reports `defer`; the next upload or a periodic
sweep picks it up.

Test the whole chain:

```bash
IMMICH_VIVO_PASSWORD=... python3 tools/test_e2e_immich.sh
```

It uploads a real pair, waits for the link and the hidden flag, then trashes the test assets. The
clip content is randomised on purpose: identical bytes get deduplicated by Immich, no `AssetCreate`
event fires, and the test passes for the wrong reason.

## Troubleshooting

**`Ambiguous — missing comparable capture time`.** The merger pairs a JPEG with its clip by
comparing capture times, and some JPEGs carry no date at all (hand-exported frames, some gallery
re-exports). Write one in first — this refuses to touch a file that already has EXIF:

```bash
python3 tools/stamp_capture_time.py photo.jpg "2026:01:01 12:00:00"
```

**Times come out 8 hours apart.** The merger compares the JPEG's local `DateTimeOriginal` against
the clip's `creation_time` normalised to local time. Re-encoding a clip drops its original stamp, so
pass the photo's capture time when transcoding — `tools/apply_pair.py` takes it from Immich's
`dateTimeOriginal`.

**A photo edited in the phone's gallery pairs with the wrong time.** vivo's gallery re-saves an
edited live photo and rewrites the file name, the mtime and the clip's `creation_time` to the
*export* moment, while keeping the original `DateTimeOriginal`. **Trust EXIF, not the file name:**
one such photo is named `IMG_20261005_000310` but its EXIF says `2026-10-04 14:09:23` — three
seconds after `IMG_20261004_140920`, while the minute its name claims contains no photos at all.

**Windows plays a black frame (audio only).** The clip is full-range `yuvj420p` and Windows hardware
decoding chokes on it. `mpb convert --transcode` re-encodes it:

```bash
ffmpeg -i in.mp4 -vf "scale=in_range=full:out_range=limited,format=yuv420p" \
  -c:v libx264 -profile:v high -crf 20 -color_range tv \
  -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
  -c:a aac -b:a 96k -movflags +faststart out.mp4
```

Before blaming the merge, check the same file in another player: Linux/Chromium software decoding
shows these clips fine, which is what made this look like an Immich bug at first.

**`mpb convert` prints FAIL and a reason.** The upstream merger exits `0` even when it declines to
pair a file, so the CLI verifies the artefact and reports the merger's own reason (`Ambiguous`,
`Rejected`, …) instead of trusting the exit code.

## Scope and limits

- Only same-stem pairs with a clip of ≤ 10 s are touched. Anything else is reported and skipped.
- Original files are never deleted — `convert` writes new files, and the library mode keeps a backup.
- JPEG pixels and the entire EXIF segment are copied unchanged (verified by hashing the segment
  before and after: identical).
- Verified against ~880 pairs from a vivo X100 Ultra.
- No Immich source changes. This talks to the official API and to workflows, so your instance stays
  a stock Immich — including across upgrades.

## 中文说明（简要）

vivo / iQOO 的动态照片是**两个独立文件**（同名的 `.jpg` + `.mp4`），Immich 认不出来，时间线里会
变成"一张照片 + 一个小视频"，下载也是两个文件；Windows 上那小段视频还常常黑屏（vivo 录的是全
范围 `yuvj420p`）。

这个工具做三件事：把两者**合并成一个标准动态照片 JPEG**、**可选转码**修掉黑屏、并在 Immich 里
**建立关联并隐藏那个小视频**（和 Immich 对待 iPhone 动态照片的做法一致）。

**照片已经进库了也不用重传**：`tools/apply_pair.py` / `tools/apply_batch.py` 原地替换图片文件，
资产 ID、相册、标签、收藏、时间全部保留；每对先备份，可中断、可续跑。

想先看效果：把合成后的 jpg 拖进 [`docs/viewer.html`](docs/viewer.html) 就能播放里面的小视频
（纯本地，不上传）。批量处理用命令行的 `mpb scan` / `mpb convert`。

## Credits

- Merging is delegated to [`oneone1995/android-motion-photo-merger`](https://github.com/oneone1995/android-motion-photo-merger)
  (MIT) — it does the JPEG/XMP work correctly.
- Thanks to the Immich maintainers for shipping workflows with a webhook action instead of forcing
  people to fork.

## License

MIT
