"""Local rendering engine. No web or third-party Python dependencies."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import uuid
from fractions import Fraction

import color_core as core

ROOT = Path(__file__).resolve().parent
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
FRAME_RATES = {"source", "24000/1001", "24", "25", "30000/1001", "30", "50", "60000/1001", "60", "120"}


class EncodingError(RuntimeError):
    def __init__(self, message, details):
        super().__init__(message)
        self.details = details


def nvenc_failure(text):
    return bool(re.search(r"nvenc|nvcuda|CUDA_ERROR", text, re.I) and re.search(
        r"nvenc API|driver does not support|minimum required nvidia|no capable devices|cannot load|unsupported device|CUDA_ERROR|error while opening encoder|initialize.*encoder|openencodesession", text, re.I))


def encoder_error(text, encoder):
    if encoder == "nvenc" and nvenc_failure(text):
        if re.search(r"nvenc API|minimum required nvidia|driver does not support", text, re.I):
            return "当前 NVIDIA 驱动与硬件编码器接口不兼容"
        return "当前环境无法使用 NVIDIA HEVC 10-bit 编码"
    if re.search(r"no space left|disk full|not enough space", text, re.I):
        return "输出磁盘空间不足，请清理空间或更换保存位置"
    if re.search(r"permission denied|access is denied", text, re.I):
        return "无法写入输出文件，请选择有写入权限的保存位置"
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('["')]
    relevant = [line for line in lines if re.search(r"error|invalid|failed|not found|cannot|no such", line, re.I)]
    detail = (relevant or lines or ["请查看详细日志"])[0]
    return "视频转码失败：" + detail[:240]
BUILTINS = [
    {"id": "original", "name": "官方还原", "tag": "REC.709", "description": "大疆官方技术 LUT，准确还原色彩", "stage": "builtin"},
    {"id": "dji_reference", "name": "大疆参考增强", "tag": "REFERENCE", "description": "参考本次大疆样片，增强色彩浓度与明暗层次，可调节强度", "stage": "builtin"},
    {"id": "soft", "name": "柔和日光", "tag": "SOFT", "description": "稍低对比，保留通透和自然肤色", "stage": "builtin"},
    {"id": "warm", "name": "暖调胶片", "tag": "WARM", "description": "温暖的高光，柔和的色彩", "stage": "builtin"},
    {"id": "cool", "name": "冷调纪实", "tag": "COOL", "description": "克制饱和度，微冷的画面", "stage": "builtin"},
    {"id": "mono", "name": "黑白银盐", "tag": "MONO", "description": "细腻灰阶与稍强的明暗关系", "stage": "builtin"},
]


def read_cube(content):
    if len(content) > 32 * 1024 * 1024:
        raise ValueError("LUT 文件不能超过 32 MB")
    text = content.decode("utf-8-sig")
    size, low, high, values = None, (0., 0., 0.), (1., 1., 1.), []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        key = fields[0]
        if key == "LUT_1D_SIZE":
            raise ValueError("请使用纯 3D .cube LUT，暂不支持含 1D shaper 的 LUT")
        if key == "TITLE":
            continue
        if key == "LUT_3D_SIZE":
            size = int(fields[1])
            if not 2 <= size <= 65:
                raise ValueError("3D LUT 网格大小应在 2–65 之间")
        elif key in {"DOMAIN_MIN", "DOMAIN_MAX"}:
            vector = tuple(float(x) for x in fields[1:])
            if len(vector) != 3 or not all(math.isfinite(x) for x in vector):
                raise ValueError("LUT DOMAIN 格式不正确")
            if key == "DOMAIN_MIN":
                low = vector
            else:
                high = vector
        else:
            if len(fields) != 3:
                raise ValueError("无法识别的 LUT 行：" + line[:60])
            vector = tuple(float(x) for x in fields)
            if not all(math.isfinite(x) for x in vector):
                raise ValueError("LUT 含有无效数值")
            values.append(vector)
    if size is None or len(values) != size ** 3 or any(a >= b for a, b in zip(low, high)):
        raise ValueError("LUT 网格数据不完整或 DOMAIN 不正确")
    return size, low, high, values


def builtin_color(name, rgb):
    r, g, b = rgb
    y = .2126 * r + .7152 * g + .0722 * b
    if name == "mono":
        v = (y - .5) * 1.08 + .5
        return (v, v, v)
    contrast, saturation = {"soft": (.93, .94), "warm": (1.035, .92), "cool": (1.025, .86)}.get(name, (1., 1.))
    result = [((y + (c - y) * saturation) - .5) * contrast + .5 for c in rgb]
    if name == "soft":
        result = [c * .985 + .012 for c in result]
    if name == "warm":
        result = [result[0] * 1.025 + .007, result[1] * 1.005, result[2] * .965]
    if name == "cool":
        result = [result[0] * .98, result[1] * 1.005, result[2] * 1.025 + .004]
    return tuple(result)


class Renderer:
    def __init__(self, ffmpeg, ffprobe, cache):
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.lut_lock = threading.Lock()
        self.preview_slots = threading.Semaphore(2)
        self.preview_locks = {}
        self.lock = threading.Lock()
        self.thread_budget = max(2, min(8, (os.cpu_count() or 4) // 2))
        self.nvenc = {"available": None, "reason": "尚未检测"}
        self.official = core.prepare_lut()
        if core.digest(self.official) != core.LUT_SHA256:
            raise RuntimeError("官方还原 LUT 校验失败")

    def check_nvenc(self):
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                "color=black:size=640x360:rate=30", "-frames:v", "2", "-vf", "format=p010le", "-c:v", "hevc_nvenc",
                "-profile:v", "main10", "-preset", "p4", "-f", "null", "-"]
        try:
            result = subprocess.run(args, capture_output=True, timeout=15, creationflags=NO_WINDOW)
            text = result.stderr.decode("utf-8", errors="replace")
            status = {"available": result.returncode == 0,
                      "reason": "NVIDIA HEVC 10-bit 编码可用" if result.returncode == 0 else encoder_error(text, "nvenc")}
        except (OSError, subprocess.TimeoutExpired):
            status = {"available": False, "reason": "NVIDIA 编码检测未通过"}
        with self.lock:
            self.nvenc = status
        return dict(status)

    def inspect(self, path):
        data, video = core.probe(self.ffprobe, Path(path))
        fps = video.get("avg_frame_rate", "0/1")
        try:
            numerator, denominator = fps.split("/")
            fps = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            fps = 0
        duration = core.video_duration(data, video)
        audio_bitrate = sum(int(s.get("bit_rate") or 192000) for s in data.get("streams", []) if s.get("codec_type") == "audio")
        source_bitrate = int(video.get("bit_rate") or 0)
        if not source_bitrate and duration:
            source_bitrate = max(0, int(Path(path).stat().st_size * 8 / duration) - audio_bitrate)
        return {"duration": duration, "width": video.get("width", 0), "source_bitrate": source_bitrate,
                "height": video.get("height", 0), "fps": round(fps, 3),
                "pix_fmt": video.get("pix_fmt", ""), "video": video,
                "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}

    def style_lut(self, look, strength, amount_scale=1.):
        strength = max(0, min(100, int(strength)))
        if look["id"] == "original" or not strength:
            return None
        custom = self.official if look["id"] == "dji_reference" else look.get("path")
        key = hashlib.sha256((look["id"] + str(strength) + (core.digest(Path(custom)) if custom else "v1") + ("reference-085" if look["id"] == "dji_reference" else "") + (f"scale-{amount_scale}" if amount_scale != 1. else "")).encode()).hexdigest()
        target = self.cache / (key + ".cube")
        with self.lut_lock:
            if target.exists():
                return target
            if custom:
                size, low, high, values = read_cube(Path(custom).read_bytes())
            else:
                size, low, high, values = 17, (0., 0., 0.), (1., 1., 1.), None
            lines = [f"LUT_3D_SIZE {size}", "DOMAIN_MIN " + " ".join(map(str, low)),
                     "DOMAIN_MAX " + " ".join(map(str, high))]
            i, amount = 0, strength / 100 * amount_scale
            if look["id"] == "dji_reference":
                amount *= .85
            for b in range(size):
                for g in range(size):
                    for r in range(size):
                        rgb = tuple(low[k] + x / (size - 1) * (high[k] - low[k]) for k, x in enumerate((r, g, b)))
                        grade = values[i] if values is not None else builtin_color(look["id"], rgb)
                        output = [a * (1 - amount) + c * amount for a, c in zip(rgb, grade)]
                        if not custom:
                            output = [max(0, min(1, c)) for c in output]
                        lines.append(" ".join(f"{v:.7f}" for v in output))
                        i += 1
            target.write_text("\n".join(lines) + "\n", encoding="ascii")
        return target

    def enhancement_lut(self, look, settings):
        if not settings.get("enhance_enabled") or (look["id"] == "dji_reference" or look.get("includes_reference")):
            return None
        strength = int(settings.get("enhance_strength", 100))
        if not strength:
            return None
        if look["id"] == "original":
            if settings.get("profile", "dlogm") != "dlogm":
                return None
            source = self.official
        elif look.get("stage") == "technical":
            source = Path(look["path"])
        else:
            source = self.style_lut(look, settings.get("strength", 100))
            if source is None:
                return None
        # Repeat the selected transform, not a fixed DJI look for every filter.
        repeat = {"id": "enhance-" + look["id"], "path": str(source)}
        return self.style_lut(repeat, strength, amount_scale=.85)

    def graph(self, media, look, settings, directory):
        import shutil
        video = media["video"]
        matrix = {"bt709": "709", "bt2020nc": "2020_ncl", "bt470bg": "470bg", "smpte170m": "170m"}.get(video.get("color_space"), "709")
        value_range = settings.get("range", "auto")
        if value_range == "auto":
            value_range = "full" if video.get("color_range") == "pc" else "limited"
        parts = [f"zscale=matrixin={matrix}:rangein={value_range}:matrix=gbr:range=full", "format=gbrp16le"]
        technical = look.get("stage") == "technical"
        if technical:
            shutil.copyfile(look["path"], Path(directory) / "technical.cube")
            parts.append("lut3d=file=technical.cube:interp=tetrahedral")
        else:
            if settings.get("profile", "dlogm") == "dlogm":
                shutil.copyfile(self.official, Path(directory) / "base.cube")
                parts.append("lut3d=file=base.cube:interp=tetrahedral")
            style = self.style_lut(look, settings.get("strength", 100))
            if style:
                shutil.copyfile(style, Path(directory) / "style.cube")
                parts.append("lut3d=file=style.cube:interp=tetrahedral")
        enhancement = self.enhancement_lut(look, settings)
        if enhancement:
            shutil.copyfile(enhancement, Path(directory) / "enhancement.cube")
            parts.append("lut3d=file=enhancement.cube:interp=tetrahedral")
        fmt = "yuv420p" if settings.get("encoder") == "h264" else "yuv420p10le"
        parts.extend(["zscale=matrixin=gbr:rangein=full:matrix=709:range=limited:dither=error_diffusion", "format=" + fmt])
        if settings.get("encoder") == "nvenc":
            parts.append("format=p010le")
        parts.append("setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709")
        return ",".join(parts)

    def preview(self, media, look, settings, second=0, before=False, width=1280):
        path = Path(media["path"])
        stamp = (path.stat().st_size, path.stat().st_mtime_ns)
        key = hashlib.sha256(json.dumps([media["id"], stamp, look["id"], look.get("revision", 0), settings, round(second, 2), before, width], sort_keys=True).encode()).hexdigest()
        target = self.cache / (key + ".jpg")
        with self.lock:
            slot = self.preview_locks.setdefault(key, threading.Lock())
        with slot:
            if target.exists():
                return target
            with self.preview_slots, tempfile.TemporaryDirectory(prefix="action4_preview_") as directory:
                scale = f"scale=w={width}:h={int(width * 9 / 16)}:force_original_aspect_ratio=decrease:flags=bilinear"
                graph = scale if before else scale + "," + self.graph(media, look, settings, directory)
                graph += ",format=yuvj420p"
                temporary = target.with_name(key + ".partial.jpg")
                try:
                    args = [self.ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-y", "-threads", "2", "-filter_threads", "2", "-ss", str(max(0, second)),
                            "-i", str(path), "-vf", graph, "-frames:v", "1", "-q:v", "3", str(temporary)]
                    result = subprocess.run(args, cwd=directory, capture_output=True, timeout=90, creationflags=NO_WINDOW)
                    if result.returncode or not temporary.exists():
                        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-800:] or "此时间点没有可预览的画面")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        return target

    def render(self, media, look, settings, target, log_path, cancelled, progress, process_changed, notice=None):
        requested = settings["encoder"]
        effective = dict(settings)
        reason = ""
        if requested in {"auto", "nvenc"}:
            with self.lock:
                capabilities = dict(self.nvenc)
            if requested == "auto":
                effective["encoder"] = "nvenc" if capabilities["available"] else "hevc"
            if capabilities["available"] is False:
                reason = capabilities["reason"]
                effective["encoder"] = "hevc"
        if reason and notice:
            notice(reason + "，已自动改用 CPU H.265（10-bit）", "hevc")
        try:
            result = self._render_once(media, look, effective, target, log_path, cancelled, progress, process_changed)
        except EncodingError as error:
            if effective["encoder"] != "nvenc" or not nvenc_failure(error.details):
                raise
            if cancelled.is_set():
                raise InterruptedError("已取消")
            import shutil
            shutil.copyfile(log_path, Path(log_path).with_suffix(".nvenc.log"))
            reason = str(error)
            if re.search(r"nvenc API|driver does not support|no capable devices|cannot load|CUDA_ERROR", error.details, re.I):
                with self.lock:
                    self.nvenc = {"available": False, "reason": reason}
            effective["encoder"] = "hevc"
            progress(0., 0., None)
            if notice:
                notice(reason + "，已自动改用 CPU H.265（10-bit）", "hevc")
            result = self._render_once(media, look, effective, target, log_path, cancelled, progress, process_changed)
        return dict(result, encoder_used=effective["encoder"], requested_encoder=requested, fallback_reason=reason)

    def encode_arguments(self, media, settings):
        encoder = settings["encoder"]
        if encoder == "nvenc":
            args = ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-profile:v", "main10", "-tag:v", "hvc1"]
        elif encoder == "h264":
            args = ["-c:v", "libx264", "-preset", "fast", "-threads", str(self.thread_budget)]
        else:
            args = ["-c:v", "libx265", "-preset", "fast", "-threads", str(self.thread_budget),
                    "-x265-params", f"pools={self.thread_budget}:frame-threads=2", "-profile:v", "main10", "-tag:v", "hvc1"]
        bitrate = int(media.get("source_bitrate") or media.get("video", {}).get("bit_rate") or 0)
        if settings.get("rate_mode", "source") == "source" and bitrate > 0:
            bitrate = max(64000, bitrate)
            args += ["-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.2)), "-bufsize", str(bitrate * 2)]
        elif encoder == "nvenc":
            args += ["-cq", str(settings["quality"]), "-b:v", "0"]
        else:
            args += ["-crf", str(settings["quality"])]
        return args

    def _render_once(self, media, look, settings, target, log_path, cancelled, progress, process_changed):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.stem + ".partial.mp4")
        duration = media.get("duration", 0)
        if settings.get("preview"):
            duration = min(duration, settings["preview"]) if duration else settings["preview"]
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="action4_render_") as directory, Path(log_path).open("w", encoding="utf-8") as log:
                graph = self.graph(media, look, settings, directory)
                fps = settings.get("fps", "source")
                if fps not in FRAME_RATES:
                    raise ValueError("不支持的导出帧率")
                if fps != "source":
                    fps_filter = f"fps=fps={fps}:round=near"
                    # Drop early to avoid grading unused frames; duplicate only after grading.
                    if float(Fraction(fps)) <= float(media.get("fps") or 0):
                        graph = fps_filter + "," + graph
                    else:
                        graph += "," + fps_filter
                args = [self.ffmpeg, "-hide_banner", "-nostdin", "-n", "-loglevel", "warning", "-stats_period", "0.5",
                        "-threads", "4", "-filter_threads", str(self.thread_budget),
                        "-progress", "pipe:1", "-nostats", "-i", media["path"], "-map", "0:v:0", "-map", "0:a?",
                        "-map_metadata", "0", "-map_chapters", "0", "-vf", graph]
                args += self.encode_arguments(media, settings)
                args += ["-c:a", "aac", "-b:a", "192k"] if settings.get("audio_aac") else ["-c:a", "copy"]
                args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
                         "-fps_mode", "passthrough" if fps == "source" else "cfr", "-movflags", "+faststart"]
                if fps != "source":
                    args += ["-r", fps]
                if settings.get("preview"):
                    args += ["-t", str(settings["preview"])]
                args.append(str(temporary))
                log.write(json.dumps(args, ensure_ascii=False) + "\n")
                log.flush()
                if cancelled.is_set():
                    raise InterruptedError("已取消")
                process = subprocess.Popen(args, cwd=directory, stdout=subprocess.PIPE, stderr=log, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                process_changed(process)
                elapsed, speed = 0., 0.
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us" and value.lstrip("-").isdigit():
                        elapsed = max(0., int(value) / 1e6)
                    if key == "speed":
                        try:
                            speed = float(value.rstrip("x"))
                        except ValueError:
                            pass
                    if key == "progress":
                        progress(min(.99, elapsed / duration) if duration else 0., speed,
                                 max(0., duration - elapsed) / speed if speed > 0 else None)
                    if cancelled.is_set() and process.poll() is None:
                        process.terminate()
                code = process.wait()
            if cancelled.is_set():
                raise InterruptedError("已取消")
            if code:
                raw = Path(log_path).read_text(encoding="utf-8", errors="replace")
                details = raw.split("\n", 1)[-1]
                raise EncodingError(encoder_error(details, settings["encoder"]), details)
            data, video = core.probe(self.ffprobe, temporary)
            actual = core.video_duration(data, video)
            if (video.get("color_space") != "bt709" or video.get("color_transfer") != "bt709"
                    or video.get("color_primaries") != "bt709" or not temporary.stat().st_size):
                raise RuntimeError("输出文件的色彩标记检查未通过")
            if actual and duration and abs(actual - duration) > max(1., duration * .001):
                raise RuntimeError("输出时长与原片不符")
            if target.exists():
                raise RuntimeError("目标文件已经存在，请重新创建任务")
            temporary.rename(target)
            return {"bytes": target.stat().st_size, "pix_fmt": video.get("pix_fmt"), "duration": actual,
                    "output_fps": video.get("avg_frame_rate"), "output_frames": video.get("nb_frames")}
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process and process.stdout:
                process.stdout.close()
            process_changed(None)
            temporary.unlink(missing_ok=True)
