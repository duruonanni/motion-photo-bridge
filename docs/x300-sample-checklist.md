# 拿到 X300 / X500 原始动态照片后要核对的清单

目的：确认 vivo 新一代（X300 起）的标准单文件实现，与我们的合并产物差在哪。

## 1. 文件形态
- [ ] 是单个 `.jpg`，还是同名 `.jpg` + `.mp4` 两个文件？
- [ ] `ls -l`：JPEG 里是否已包含 2~4MB 的尾部数据（说明视频内嵌）
- [ ] 二进制里 `ftyp` 出现次数与位置（新机型预期：私有 JSON 双尾，`ftyp` 在其后）

## 2. XMP
```bash
python3 - <<'PY'
import pathlib,re
d=pathlib.Path("sample.jpg").read_bytes(); i=d.find(b"<x:xmpmeta")
print(d[i:d.find(b"</x:xmpmeta>")+12].decode("utf8","replace"))
PY
```
- [ ] 命名空间前缀（`Camera:` 还是 `GCamera:`）
- [ ] `MotionPhotoPresentationTimestampUs` 是真实值还是 `-1`
- [ ] Primary 项是否带 `Item:Length="0"` / `Item:Padding`
- [ ] 有没有 `GainMap` 项、`hdrgm:Version`
- [ ] 有没有 vivo 私有字段（`com.android.camera.livephoto` 等）

## 3. 段结构
- [ ] 是否带 MPF（`MPF\0`）
- [ ] 是否带 APP1 之外的厂商段

## 4. 视频
```bash
ffprobe -v error -show_entries format=format_name:format_tags:stream=codec_name,width,height sample.mp4
```
- [ ] 编码（HEVC / H.264）、分辨率、时长
- [ ] MP4 里的设备标签（`com.android.manufacturer` 等）

## 5. 对照
把上述结果和我们 `03-out` 里的产物逐项对比，差异项决定要不要加 `--xiaomi` / `--vivo-new` 之类的输出选项。

## 已确认（无需样张即成立）
- X300 系列 = 标准单文件 Google Motion Photo；XMP 前缀 vivo 写 `GCamera`
- 时间戳 `MotionPhotoPresentationTimestampUs` = `0`
- 三个身份字段：`xmlns:VCamera="http://ns.vivo.com/photos/1.0/camera/"`、
  `VMotionPhotoVersion=1`、`VMotionPhotoSource=1`、`VMediaKitVersion`
- Primary 项不带 Length/Padding；MotionPhoto 项带 `Item:Padding="0"`
- 真机文件另带 C2PA 相机签名（`c2pa.created` + vivo 证书链），第三方无法复制
- 参考实现（协议事实来源，代码未复制）：live-photo-box `VivoLivePhotoProtocol.cs`、
  XHS_Downloader_Android `docs/LIVE_PHOTO_COMPATIBILITY.md`

## 结论（2026-10-22 实测）
- `--target vivo` 形态：小米相册认（有实况标记、可播放）
- 小红书仍不认（小米手机上传，仍为静态图；同机直出照片可正常发动态图）
- 判定：平台侧不看 XMP 形态，看捕获来源（真机文件含 C2PA 签名链，第三方无法生成）
- 处置：不再投入逆向；发平台用手机相册原图，工具专注 Immich
