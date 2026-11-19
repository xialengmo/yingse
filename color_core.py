#!/usr/bin/env python3
"""Batch DJI Osmo Action 4 D-Log M -> Rec.709. Python 3.9+, standard library only."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

BASE = Path(__file__).resolve().parent
LUT_URL = ("https://terra-1-g.djicdn.com/851d20f7b9f64838a34cd02351370894/203/"
           "DJI%20OSMO%20Action%204%20D-Log%20M%20to%20Rec.709%20V1.cube")
LUT_SHA256 = "b18162854ab47702068410c33afa98a8cb6eef159fc5a04ce0e65fad0fd8947e"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
EXTENSIONS = {".mp4", ".mov", ".m4v"}


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(url, target):
    """Download to a unique temporary file; never accept partial downloads."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".download")
    last = 0.0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Action4-Restore/1.0"})
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as out:
            total = int(response.headers.get("Content-Length", 0))
            count = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                out.write(block)
                count += len(block)
                if time.monotonic() - last >= 2:
                    print(f"\r下载 {target.name}: {count / 1048576:.1f} MB"
                          + (f" / {total / 1048576:.1f} MB" if total else ""), end="", flush=True)
                    last = time.monotonic()
            if total and count != total:
                raise RuntimeError("下载不完整，请重新运行。")
        temporary.replace(target)
        print()
    finally:
        temporary.unlink(missing_ok=True)


def find_tools(explicit=None, offline=False):
    names = ("ffmpeg.exe", "ffprobe.exe") if os.name == "nt" else ("ffmpeg", "ffprobe")
    candidates = []
    if explicit:
        location = Path(explicit).expanduser().resolve()
        directory = location if location.is_dir() else location.parent
        candidates.append((directory / names[0], directory / names[1]))
    else:
        candidates.extend([(BASE / "tools" / names[0], BASE / "tools" / names[1]),
                           (BASE / names[0], BASE / names[1])])
        installed = shutil.which(names[0])
        if installed:
            candidates.append((Path(installed), Path(installed).with_name(names[1])))
    for pair in candidates:
        if all(p.is_file() for p in pair):
            return tuple(str(p.resolve()) for p in pair)
    if explicit or offline or os.name != "nt":
        raise RuntimeError("找不到 ffmpeg / ffprobe。把两个程序放入 tools 文件夹，"
                           "或用 --ffmpeg 指定它们所在的文件夹。")
    print("首次运行：下载便携版 FFmpeg（约 100 MB），只保存到脚本的 tools 文件夹。")
    print("下载来源：" + FFMPEG_URL)
    with tempfile.TemporaryDirectory(prefix="action4_setup_") as temp:
        archive = Path(temp) / "ffmpeg.zip"
        with urllib.request.urlopen(FFMPEG_URL + ".sha256", timeout=60) as response:
            checksum_text = response.read(4096).decode("ascii")
        match = re.search(r"\b[0-9a-fA-F]{64}\b", checksum_text)
        if not match:
            raise RuntimeError("无法读取 FFmpeg 发布方校验值。")
        download(FFMPEG_URL, archive)
        if digest(archive).lower() != match.group(0).lower():
            raise RuntimeError("FFmpeg SHA256 校验失败，请重试或手动安装。")
        tools = BASE / "tools"
        tools.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as package:
            for name in names:
                members = [m for m in package.namelist() if m.endswith("/bin/" + name)]
                if len(members) != 1:
                    raise RuntimeError("下载包中找不到唯一的 " + name)
                with package.open(members[0]) as src, (tools / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            for member in package.namelist():
                if Path(member).name.lower() in {"license", "license.txt", "readme.txt"}:
                    with package.open(member) as src, (tools / Path(member).name).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    return find_tools(offline=True)


def prepare_lut(custom=None, offline=False):
    lut = Path(custom).expanduser().resolve() if custom else BASE / "Action4_DLogM_Rec709.cube"
    if not lut.is_file():
        if custom or offline:
            raise RuntimeError("找不到 LUT 文件：" + str(lut))
        print("下载大疆官方 Action 4 D-Log M 转 Rec.709 LUT。")
        download(LUT_URL, lut)
    if not custom and digest(lut) != LUT_SHA256:
        raise RuntimeError("官方 LUT 校验失败。请移走现有 LUT 后重试，或用 --lut 指定自己的 LUT。")
    text = lut.read_text(encoding="utf-8-sig")
    if not re.search(r"^\s*LUT_3D_SIZE\s+\d+", text, re.MULTILINE):
        raise RuntimeError("需要包含 LUT_3D_SIZE 的 3D .cube 文件。")
    return lut


def inside(path, directory):
    return path == directory or directory in path.parents


def collect(source, output, recursive=True):
    if source.is_file():
        return [source] if source.suffix.lower() in EXTENSIONS else []
    found = []
    for parent, directories, files in os.walk(source, followlinks=False):
        here = Path(parent)
        directories[:] = sorted(d for d in directories
                                if not inside((here / d).resolve(), output)
                                and d not in {"_Rec709", "_Rec709_preview", ".logs"}
                                and not (here / d).is_symlink())
        for name in sorted(files):
            p = here / name
            if (p.suffix.lower() in EXTENSIONS and not p.is_symlink()
                    and "_rec709" not in p.stem.lower()
                    and ".partial" not in p.stem.lower()):
                found.append(p)
        if not recursive:
            break
    return found


def probe(executable, path):
    result = subprocess.run([executable, "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError("读取视频信息失败：" + result.stderr.strip()[-800:])
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("文件中没有视频轨。")
    return data, video


def video_duration(data, video):
    for value in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            seconds = float(value)
            if seconds > 0 and math.isfinite(seconds):
                return seconds
        except (TypeError, ValueError):
            pass
    return 0.0


def filters(video, options):
    # The LUT handles the transfer curve and gamut. Zscale only handles the YUV/RGB
    # matrix and sample range, with 16-bit RGB between decoding and the LUT.
    matrix = {"bt709": "709", "bt2020nc": "2020_ncl", "fcc": "fcc",
              "smpte170m": "170m", "bt470bg": "470bg", "smpte240m": "smpte240m"}
    input_matrix = matrix.get(video.get("color_space"), "709")
    input_range = options.input_range
    if input_range == "auto":
        input_range = "full" if video.get("color_range") == "pc" else "limited"
    pixel_format = "yuv420p" if options.encoder == "h264" else "yuv420p10le"
    hardware_format = ",format=p010le" if options.encoder == "nvenc" else ""
    return (f"zscale=matrixin={input_matrix}:rangein={input_range}:matrix=gbr:range=full,"
            "format=gbrp16le,lut3d=file=lut.cube:interp=tetrahedral,"
            "zscale=matrixin=gbr:rangein=full:matrix=709:range=limited:dither=error_diffusion,"
            f"format={pixel_format}{hardware_format},"
            "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709")


def encode_arguments(options):
    if options.encoder == "hevc":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", str(options.quality),
                "-profile:v", "main10", "-tag:v", "hvc1"]
    if options.encoder == "h264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(options.quality)]
    return ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(options.quality),
            "-b:v", "0", "-profile:v", "main10", "-tag:v", "hvc1"]


def convert(ffmpeg, ffprobe, source, target, log_path, lut_directory, options):
    data, video = probe(ffprobe, source)
    duration = video_duration(data, video)
    if options.preview:
        duration = min(duration, options.preview) if duration else options.preview
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.stem + "." + uuid.uuid4().hex + ".partial.mp4")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-n", "-loglevel", "warning",
            "-stats_period", "1", "-progress", "pipe:1", "-nostats", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0",
            "-vf", filters(video, options)]
    args += encode_arguments(options)
    args += ["-c:a", "aac", "-b:a", "192k"] if options.audio_aac else ["-c:a", "copy"]
    args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             "-color_range", "tv", "-fps_mode", "passthrough", "-movflags", "+faststart"]
    if options.preview:
        args += ["-t", str(options.preview)]
    args += [str(temporary)]
    process = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            log.write(json.dumps({"source": str(source), "command": args,
                                  "input_video": video}, ensure_ascii=False) + "\n\n")
            log.flush()
            process = subprocess.Popen(args, cwd=lut_directory, stdout=subprocess.PIPE,
                                       stderr=log, encoding="utf-8", errors="replace")
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and value.lstrip("-").isdigit():
                    seconds = max(0, int(value) / 1000000)
                    status = f"{min(100, seconds / duration * 100):5.1f}%" if duration else f"{seconds:.1f} 秒"
                    print("\r  处理进度 " + status + "     ", end="", flush=True)
            code = process.wait()
        print()
        if code:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"FFmpeg 退出码 {code}。日志：{log_path}\n{tail}")
        result, result_video = probe(ffprobe, temporary)
        if (not temporary.stat().st_size or not result_video.get("width")
                or result_video.get("color_space") != "bt709"
                or result_video.get("color_transfer") != "bt709"
                or result_video.get("color_primaries") != "bt709"):
            raise RuntimeError("输出视频验证失败，未生成最终文件。")
        actual_duration = video_duration(result, result_video)
        if duration and actual_duration and abs(actual_duration - duration) > max(1.0, duration * 0.001):
            raise RuntimeError(f"输出时长异常：预期 {duration:.2f} 秒，实际 {actual_duration:.2f} 秒。")
        # On Windows rename refuses to replace an existing destination.
        if target.exists():
            raise RuntimeError("目标文件已由其他任务生成，已保留现有文件。")
        temporary.rename(target)
        return {"duration": actual_duration, "pix_fmt": result_video.get("pix_fmt"),
                "codec": result_video.get("codec_name"), "bytes": target.stat().st_size}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Action 4 D-Log M 批量还原至 Rec.709；保留原片。")
    parser.add_argument("input", nargs="?", help="视频文件或文件夹（默认包含子文件夹）")
    parser.add_argument("-o", "--output", help="输出文件夹，默认为输入目录下的 _Rec709")
    parser.add_argument("--encoder", choices=["hevc", "h264", "nvenc"], default="hevc",
                        help="hevc=10-bit CPU（默认）；h264=8-bit兼容；nvenc=10-bit NVIDIA")
    parser.add_argument("--quality", type=int, choices=range(0, 52), metavar="0-51", default=18,
                        help="CPU 使用 CRF，NVIDIA 使用 CQ；越小质量越高，默认 18")
    parser.add_argument("--preview", type=float, default=0, metavar="秒", help="仅转每个视频开头若干秒")
    parser.add_argument("--limit", type=int, metavar="数量", help="最多处理几个视频，例如 --limit 1")
    parser.add_argument("--lut", help="自定义 3D .cube 路径，默认使用附带的官方 Action 4 LUT")
    parser.add_argument("--ffmpeg", help="ffmpeg.exe 的路径或同时含 ffmpeg、ffprobe 的文件夹")
    parser.add_argument("--input-range", choices=["auto", "full", "limited"], default="auto",
                        help="默认读取视频范围标签；无标签时按 limited 处理")
    parser.add_argument("--audio-aac", action="store_true", help="音轨无法直接封装时转 AAC 192k")
    parser.add_argument("--no-recursive", action="store_true", help="只处理当前文件夹")
    parser.add_argument("--offline", action="store_true", help="禁止自动下载依赖和 LUT")
    parser.add_argument("--dry-run", action="store_true", help="只列出计划，不下载、不转码")
    options = parser.parse_args()
    if not math.isfinite(options.preview) or options.preview < 0:
        parser.error("--preview 必须是非负有限秒数")
    if options.limit is not None and options.limit < 1:
        parser.error("--limit 必须大于 0")
    raw = options.input or input("请拖入视频文件夹或输入路径，然后回车：\n> ").strip().strip('"')
    if not raw:
        parser.error("需要输入文件夹或视频路径")
    source = Path(raw).expanduser().resolve()
    if not source.exists():
        parser.error("输入路径不存在：" + str(source))
    root = source if source.is_dir() else source.parent
    default_name = "_Rec709_preview" if options.preview else "_Rec709"
    output = Path(options.output).expanduser().resolve() if options.output else root / default_name
    if inside(root, output):
        parser.error("输出目录不能等于输入目录或是它的上级目录")
    videos = collect(source, output, not options.no_recursive)
    if options.limit:
        videos = videos[:options.limit]
    if not videos:
        print("未找到 MP4 / MOV / M4V 原始视频。")
        return 0
    suffix = "_Rec709_preview" if options.preview else "_Rec709"
    # Keep the original extension in the output name to avoid MP4/MOV stem collisions.
    plan = [(p, output / p.relative_to(root).parent / (p.name + suffix + ".mp4")) for p in videos]
    print(f"共 {len(plan)} 个视频；编码 {options.encoder}；质量 {options.quality}；输出：{output}")
    if options.dry_run:
        for p, target in plan:
            print(("[跳过] " if target.exists() else "[转码] ") + str(p) + " -> " + str(target))
        return 0
    ffmpeg, ffprobe = find_tools(options.ffmpeg, options.offline)
    capabilities = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                                  encoding="utf-8", errors="replace")
    if capabilities.returncode or not re.search(r"\bzscale\b", capabilities.stdout):
        raise RuntimeError("此 FFmpeg 缺少 zscale / libzimg。请使用 Gyan essentials 构建，"
                           "或用 --ffmpeg 指定包含该滤镜的版本。")
    lut = prepare_lut(options.lut, options.offline)
    logs = output / ".logs"
    logs.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    report = logs / (run_id + ".jsonl")
    counts = {"ok": 0, "skip": 0, "failed": 0}
    with tempfile.TemporaryDirectory(prefix="action4_lut_") as temp, report.open("a", encoding="utf-8") as journal:
        # A simple relative LUT name avoids FFmpeg quoting bugs with Windows drive
        # letters, Chinese paths, apostrophes, commas and brackets.
        shutil.copyfile(lut, Path(temp) / "lut.cube")
        for index, (p, target) in enumerate(plan, 1):
            print(f"\n[{index}/{len(plan)}] {p.relative_to(root)}")
            record = {"input": str(p), "output": str(target), "encoder": options.encoder,
                      "quality": options.quality, "lut_sha256": digest(lut)}
            if target.exists():
                print("  已存在，跳过。更换 LUT 或参数时，请指定新的输出目录。")
                record["status"] = "skip"
            else:
                identity = hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:12]
                log_path = logs / (run_id + "-" + identity + ".log")
                try:
                    record.update(convert(ffmpeg, ffprobe, p, target, log_path, temp, options))
                    record["status"] = "ok"
                    print("  已完成：" + str(target))
                except KeyboardInterrupt:
                    record.update(status="cancelled", error="用户中止；当前临时文件已清理")
                    journal.write(json.dumps(record, ensure_ascii=False) + "\n")
                    journal.flush()
                    print("\n已中止。已完成的文件保留，再次运行会跳过。")
                    return 130
                except Exception as error:
                    record.update(status="failed", error=str(error), log=str(log_path))
                    print("  失败：" + str(error))
            counts[record["status"]] += 1
            journal.write(json.dumps(record, ensure_ascii=False) + "\n")
            journal.flush()
    print(f"\n结束：成功 {counts['ok']}，跳过 {counts['skip']}，失败 {counts['failed']}。")
    print("日志：" + str(report))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中止。")
        sys.exit(130)
    except Exception as error:
        print("错误：" + str(error), file=sys.stderr)
        sys.exit(2)
