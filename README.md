# motion-photo-bridge

Turn **dual-file live photos** (a `JPG` + a short `MP4` that share a filename) into **standard Android Motion Photos** — a single JPEG with the clip embedded — and wire the whole thing into [Immich](https://immich.app).

Built for phones that store live photos as two loose files with no standard metadata linking them: most **vivo / iQOO** models, some OPPO/realme builds, and anything else that ships `IMG_20260101_120000.jpg` next to `IMG_20260101_120000.mp4`.

## The problem

Immich recognises two kinds of motion photo:

| Kind | How it is detected |
| --- | --- |
| Apple Live Photo | `HEIC` + `MOV` paired by the `ContentIdentifier` metadata |
| Android Motion Photo | one JPEG containing an embedded video (`Camera:MotionPhoto` XMP or `MicroVideo` tags) |

A vivo live photo is **neither**. It is two plain files with a private `vivo Live Photo ID` that Immich never reads. The result:

- the timeline shows a photo **and** a 2-second clip as two separate assets;
- downloading gives you two files instead of one moving picture;
- on Windows the clip often plays as a **black frame with audio**, because vivo records it in full-range `yuvj420p` and the hardware decoder refuses it.

## What this tool does

```
JPG + MP4 ──► optional transcode ──► merge ──► single Motion Photo JPEG
                                                (original pixels + EXIF untouched)
```

1. **Pair** — same directory, same stem, clip shorter than 10 s.
2. **Transcode** (optional) — re-encode full-range clips to limited-range 4:2:0 BT.709 so they play in Windows browsers and the Photos app. Costs a tiny amount of quality; skips clean clips entirely.
3. **Merge** — append the clip to the JPEG and write the `Container:Directory` XMP block. The photo's pixels and EXIF segment are copied byte-for-byte.

## Install

```bash
git clone https://github.com/<you>/motion-photo-bridge
cd motion-photo-bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Requires `ffmpeg` and `ffprobe` on `PATH`.

## Use it offline

```bash
mpb scan  ~/Pictures/phone         # show what would be paired
mpb convert ~/Pictures/phone -o ~/Pictures/fixed --transcode
```

`convert` writes `<stem>.jpg` containing the clip. The originals are never modified.

## Wire it into Immich

Immich does not let third-party code run inside the server, but since **v3.0** it ships workflows that can call a webhook when an asset is created. That is the supported extension point:

```
phone uploads JPG + MP4
        │
        ▼
Immich  ── Workflow (AssetCreate, filename matches \.mp4$) ──► POST
        │                                                      │
        ▼                                                      ▼
   asset in library                          motion-photo-bridge serve
                                                        │
                                          finds the same-stem image,
                                          links it via the Immich API
                                          (Immich hides the clip itself)
```

1. Run the receiver:

   ```bash
   mpb serve --port 9100
   ```

   It needs an Immich API key for the account that owns the photos, with `asset.read` and `asset.update`. Put it in the environment as `API_KEY`, and point `IMMICH_URL` at your instance.

2. From that same Immich account, create the workflow in **Utilities → Workflows** and paste [`docs/immich-workflow.json`](docs/immich-workflow.json). Point its `url` at an address Immich can reach — on a Docker bridge network that is usually `http://172.17.0.1:9100/…`.

3. Take a live photo. Within a few seconds the timeline shows one picture with a motion button, and the separate clip disappears.

> Workflows are scoped to their owner, so the workflow must be created by the account that uploads the photos — not by an admin on its behalf.

## Notes and limits

- **Original files are never deleted.** `convert` writes new files; the webhook only changes Immich's link and visibility fields through the official API.
- **EXIF is preserved.** The merge copies the JPEG's original EXIF segment unchanged, so camera, timestamp and GPS survive.
- **Real videos are safe.** Only clips under 10 s that share a filename stem with a photo are touched; anything else is reported and skipped.
- **Ordering.** If a clip lands before its photo, the receiver reports `defer`. Run `mpb convert` or a periodic sweep to catch stragglers.
- **Not a plugin.** Immich has no third-party plugin loader yet; this uses its workflow webhook, which is part of the product.

## Credits

- Merging is delegated to [`oneone1995/android-motion-photo-merger`](https://github.com/oneone1995/android-motion-photo-merger) (MIT), which does the JPEG/XMP work correctly.
- Thanks to the Immich maintainers for shipping workflows and a webhook action instead of forcing forks.

## License

MIT
