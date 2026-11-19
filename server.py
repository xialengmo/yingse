"""Action 4 local color workspace. Python 3.9+, no pip installation needed."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid

from engine import BUILTINS, Renderer, ROOT, read_cube, FRAME_RATES
import color_core as core

TERMINAL = {"done", "failed", "cancelled", "interrupted"}
DEFAULT_SETTINGS = {"profile": "dlogm", "encoder": "auto", "quality": 18, "rate_mode": "source", "fps": "source", "range": "auto", "preview": 0, "audio_aac": False}


def pause_render_process(process, paused):
    """Suspend only our FFmpeg child, keeping its encoder state for resume."""
    if process.poll() is not None:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        action = getattr(ctypes.WinDLL("ntdll"), "NtSuspendProcess" if paused else "NtResumeProcess")
        action.argtypes = [wintypes.HANDLE]
        action.restype = wintypes.LONG
        status = action(int(process._handle))
        if status < 0:
            if process.poll() is not None:
                return False
            raise OSError(f"系统未能{'暂停' if paused else '恢复'}转码（0x{status & 0xffffffff:08X}），请重试")
    else:
        import signal
        os.kill(process.pid, signal.SIGSTOP if paused else signal.SIGCONT)
    return True


def native_desktop_available():
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    buffer = ctypes.create_unicode_buffer(256)
    required = wintypes.DWORD()
    desktop = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    success = user.GetUserObjectInformationW(desktop, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(required))
    return not (success and buffer.value.startswith("CodexSandboxDesktop"))


class Workspace:
    def __init__(self, data, explicit=None):
        self.data = Path(data).resolve()
        for sub in ["cache", "luts", "logs", "imports", "render-cache"]:
            (self.data / sub).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.dialog_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metadata")
        key = self.data / "session.key"
        self.token = key.read_text(encoding="ascii").strip() if key.exists() else secrets.token_urlsafe(32)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{30,100}", self.token):
            self.token = secrets.token_urlsafe(32)
        key.write_text(self.token, encoding="ascii")
        self.renderer = None
        self.ready = "loading"
        self.setup_error = ""
        self.explicit = explicit
        self.media, self.jobs, self.custom = [], [], []
        self.builtin_edits = {}
        self.output = str(ROOT / "exports")
        self.settings = dict(DEFAULT_SETTINGS)
        self.revision = 0
        self.media_revision = 1
        self.instance = uuid.uuid4().hex
        self.save_pending = threading.Event()
        self.flow = {"paused": False, "reason": "", "pending_files": 0, "pending_bytes": 0}
        self.current_process = None
        self.process_paused = False
        self.queue_paused = False
        self.current_dialog = None
        self.cancel = threading.Event()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.load()
        threading.Thread(target=self.persist_worker, daemon=True, name="workspace-save").start()
        threading.Thread(target=self.prepare, daemon=True, name="prepare").start()
        threading.Thread(target=self.worker, daemon=True, name="render-queue").start()

    def load(self):
        file = self.data / "workspace.json"
        if not file.exists():
            return
        try:
            saved = json.loads(file.read_text(encoding="utf-8"))
            self.media = saved.get("media", [])
            self.jobs = saved.get("jobs", [])
            self.custom = [x for x in saved.get("custom", []) if Path(x.get("path", "")).is_file()]
            self.builtin_edits = saved.get("builtin_edits", {})
            self.settings.update({k: v for k, v in saved.get("settings", {}).items() if k in DEFAULT_SETTINGS})
            self.output = saved.get("output", self.output)
            self.queue_paused = bool(saved.get("queue_paused", False))
            for job in self.jobs:
                if not job.get("look_snapshot"):
                    try:
                        job["look_snapshot"] = self.get_look(job["filter"])
                    except StopIteration:
                        pass
                if job["status"] in {"running", "queued"}:
                    job.update(status="interrupted", error="服务上次退出时任务尚未完成，可重试")
                    temporary = Path(job["output"]).with_name("." + Path(job["output"]).stem + ".partial.mp4")
                    temporary.unlink(missing_ok=True)
            for item in self.media:
                item.setdefault("enhance_enabled", False)
                item.setdefault("enhance_strength", 100)
                if not Path(item["path"]).is_file():
                    item.update(status="missing", error="文件已移动或不再可用")
        except (ValueError, KeyError, OSError) as error:
            print("Unable to load previous workspace:", error, flush=True)

    def save(self, deferred=False):
        if deferred:
            self.save_pending.set()
            return
        with self.save_lock:
            self.save_pending.clear()
            with self.lock:
                self.revision += 1
                snapshot = copy.deepcopy({"media": self.media, "jobs": self.jobs, "custom": self.custom, "builtin_edits": self.builtin_edits,
                                          "settings": self.settings, "output": self.output, "queue_paused": self.queue_paused})
            temporary = self.data / "workspace.tmp"
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.data / "workspace.json")

    def persist_worker(self):
        while not self.stop.is_set():
            if self.save_pending.wait(.5):
                if self.stop.wait(.75):
                    break
                try:
                    self.save()
                except OSError as error:
                    print("Workspace save pending:", error, flush=True)
                    self.save_pending.set()

    def prepare(self):
        try:
            ffmpeg, ffprobe = core.find_tools(self.explicit)
            result = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode or not all(re.search(r"\b" + f + r"\b", result.stdout) for f in ["zscale", "lut3d"]):
                raise RuntimeError("FFmpeg 需要包含 zscale 和 lut3d，请使用 tools 文件夹中的便携版")
            self.renderer = Renderer(ffmpeg, ffprobe, self.data / "cache")
            self.renderer.check_nvenc()
            with self.lock:
                self.ready = "ready"
            for item in self.media:
                if item.get("status") == "checking":
                    self.pool.submit(self.inspect_media, item["id"])
            self.wake.set()
        except Exception as error:
            with self.lock:
                self.ready, self.setup_error = "error", str(error)

    def state(self, compact=False, since=None, instance=None):
        with self.lock:
            unchanged = compact and since == self.media_revision and instance == self.instance
            media_keys = {"id", "path", "name", "bytes", "status", "duration", "filter", "strength", "enhance_enabled", "enhance_strength", "width", "height", "fps", "pix_fmt", "audio", "source_bitrate", "error"}
            media = None if unchanged else [{k: v for k, v in x.items() if k in media_keys} for x in self.media] if compact else self.media
            jobs = [{k: v for k, v in j.items() if k not in {"look_snapshot", "settings"} and not (k == "output" and j.get("filename"))} for j in self.jobs] if compact else self.jobs
            payload = copy.deepcopy({"product": "Action4 Studio", "version": "1.11", "ready": self.ready,
                                  "setup_error": self.setup_error, "media": media, "jobs": jobs,
                                  "looks": self.looks(), "base_look": self.get_look("original"),
                                  "removed_looks": [{"id": x["id"], "name": x["name"]} for x in self.looks(True) if x.get("hidden") or x.get("deleted")], "settings": self.settings, "output": self.output,
                                  "default_output": str(ROOT / "exports"),
                                  "nvenc": self.renderer.nvenc if self.renderer else {"available": None, "reason": "正在检测"},
                                  "revision": self.revision,
                                  "instance": self.instance, "media_revision": self.media_revision, "media_unchanged": unchanged,
                                  "flow": self.flow, "queue_paused": self.queue_paused})
            if unchanged:
                payload.pop("media", None)
            return payload

    def get_media(self, identity):
        with self.lock:
            return copy.deepcopy(next(x for x in self.media if x["id"] == identity))

    def get_look(self, identity):
        with self.lock:
            return copy.deepcopy(next(x for x in self.looks(True) if x["id"] == identity))

    def looks(self, include_removed=False):
        result = []
        for base in BUILTINS:
            look = dict(base, is_builtin=True, default_strength=100, revision=0)
            look.update(self.builtin_edits.get(base["id"], {}))
            if include_removed or not look.get("hidden"):
                result.append(look)
        for item in self.custom:
            if include_removed or not item.get("deleted"):
                look = dict(is_builtin=False, default_strength=100, revision=0)
                look.update(item)
                result.append(look)
        return result

    def delete_look(self, identity):
        original = self.get_look(identity)
        with self.lock:
            if original["is_builtin"]:
                saved = dict(self.builtin_edits.get(identity, {}))
                saved.update(hidden=True, revision=original.get("revision", 0) + 1)
                self.builtin_edits[identity] = saved
            else:
                next(x for x in self.custom if x["id"] == identity)["deleted"] = True
            for item in self.media:
                if item["filter"] == identity:
                    item.update(filter="original", strength=100)
            self.media_revision += 1
        self.save()
        return {"ok": True}

    def restore_looks(self, identity=None):
        with self.lock:
            for key, value in self.builtin_edits.items():
                if identity is None or key == identity:
                    value["hidden"] = False
            for item in self.custom:
                if identity is None or item["id"] == identity:
                    item["deleted"] = False
        self.save()
        return {"ok": True}

    def add_paths(self, paths, recursive=True, deferred=False):
        candidates = []
        for value in paths:
            path = Path(str(value).strip().strip('"')).expanduser().resolve()
            if not path.exists():
                raise ValueError("路径不存在：" + str(path))
            if path.is_file():
                candidates.append(path)
            else:
                for parent, directories, files in os.walk(path, followlinks=False):
                    here = Path(parent)
                    directories[:] = sorted(d for d in directories if d not in {"_Rec709", "_Rec709_preview", ".logs", ".runtime", "tools"}
                                            and not (here / d).is_symlink()
                                            and (here / d).resolve() != Path(self.output).resolve()
                                            and (here / d).resolve() != (ROOT / "exports").resolve()
                                            and (here / d).resolve() != self.data)
                    candidates.extend(here / name for name in sorted(files) if "_rec709" not in Path(name).stem.lower())
                    if not recursive:
                        break
        added, identities = [], []
        with self.lock:
            known = {os.path.normcase(x["path"]): x for x in self.media}
            for p in candidates:
                if p.suffix.lower() not in core.EXTENSIONS or p.is_symlink():
                    continue
                key = os.path.normcase(str(p))
                if key in known:
                    identities.append(known[key]["id"])
                    continue
                if len(self.media) >= 5000:
                    raise ValueError("一个工作区最多容纳 5000 个视频")
                item = {"id": uuid.uuid4().hex, "path": str(p), "name": p.name, "bytes": p.stat().st_size,
                        "status": "checking", "duration": 0, "filter": "original", "strength": 100,
                        "enhance_enabled": False, "enhance_strength": 100}
                self.media.append(item)
                known[key] = item
                added.append(item["id"])
                identities.append(item["id"])
            if added:
                self.media_revision += 1
        self.save(deferred=deferred)
        if self.renderer:
            for identity in added:
                self.pool.submit(self.inspect_media, identity)
        return {"ids": identities, "added": len(added)}

    def inspect_media(self, identity):
        if self.stop.is_set():
            return
        try:
            item = self.get_media(identity)
            result = self.renderer.inspect(item["path"])
            with self.lock:
                target = next(x for x in self.media if x["id"] == identity)
                target.update(result, status="ready", error="")
                self.media_revision += 1
        except StopIteration:
            return
        except Exception as error:
            with self.lock:
                target = next((x for x in self.media if x["id"] == identity), None)
                if target:
                    target.update(status="error", error="读取视频信息超时，请检查文件是否可访问" if isinstance(error, subprocess.TimeoutExpired) else str(error))
                    self.media_revision += 1
        self.save(deferred=True)

    def native_dialog(self, kind):
        if kind not in {"files", "folder", "output"}:
            raise ValueError("未知的文件选择类型")
        if not native_desktop_available():
            raise ValueError("当前环境无法显示系统窗口，请输入本地路径，或使用浏览器选择")
        if not self.dialog_lock.acquire(blocking=False):
            raise ValueError("已有一个文件选择窗口，请先完成选择")
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen([sys.executable, str(ROOT / "picker.py"), kind], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
            self.current_dialog = process
            stdout, stderr = process.communicate(timeout=300)
            if process.returncode:
                raise ValueError("无法打开系统选择窗口，请使用“输入本地路径”或拖入文件")
            selected = json.loads(stdout.decode("utf-8"))
            if kind == "output":
                if selected:
                    self.set_output(selected)
                return {"output": self.output}
            return self.add_paths(selected if kind == "files" else [selected]) if selected else {"ids": [], "added": 0}
        finally:
            if self.current_dialog and self.current_dialog.poll() is None:
                self.current_dialog.kill()
                self.current_dialog.wait()
            self.current_dialog = None
            self.dialog_lock.release()

    def apply_look(self, body):
        look = self.get_look(body["filter"])
        strength = int(body.get("strength", look.get("default_strength", 100)))
        if not 0 <= strength <= 100:
            raise ValueError("滤镜强度应在 0–100 之间")
        with self.lock:
            for item in self.media:
                if item["id"] in body.get("ids", []):
                    item.update(filter=look["id"], strength=100 if look["stage"] == "technical" else strength)
            self.media_revision += 1
        self.save()
        return {"ok": True}

    def apply_enhancement(self, body):
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("增强开关必须为开启或关闭")
        strength = int(body.get("strength", 100))
        if not 0 <= strength <= 100:
            raise ValueError("增强强度应在 0–100 之间")
        with self.lock:
            for item in self.media:
                if item["id"] in body.get("ids", []):
                    item.update(enhance_enabled=enabled, enhance_strength=strength)
            self.media_revision += 1
        self.save()
        return {"ok": True}

    def add_lut(self, content, name, stage):
        if stage not in {"creative", "technical"}:
            raise ValueError("请选择正确的 LUT 用途")
        read_cube(content)
        identity = "lut-" + uuid.uuid4().hex
        path = self.data / "luts" / (identity + ".cube")
        path.write_bytes(content)
        item = {"id": identity, "name": Path(name).stem[:55] or "自定义 LUT", "tag": "CUSTOM", "stage": stage,
                "path": str(path), "description": "替代官方还原" if stage == "technical" else "在色彩还原后叠加"}
        with self.lock:
            self.custom.append(item)
        self.save()
        return item

    def output_directory(self, value):
        raw = str(value).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1].strip()
        if not raw or any(c in raw for c in ('\r', '\n', '\x00')):
            raise ValueError("请输入一个完整的文件夹路径")
        output = Path(os.path.expandvars(raw)).expanduser()
        if not output.is_absolute():
            raise ValueError("请填写完整路径，例如 D:\\视频成片")
        output = output.resolve()
        if output.exists() and not output.is_dir():
            raise ValueError("该路径指向文件，请选择文件夹")
        try:
            output.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=output, prefix=".color-studio-write-") as probe:
                probe.write(b"ok")
                probe.flush()
        except OSError as error:
            raise ValueError("这个位置无法写入，请检查目录权限或选择其他文件夹：" + str(output)) from error
        return output

    def set_output(self, value):
        output = self.output_directory(value)
        with self.lock:
            self.output = str(output)
        self.save()
        return {"output": str(output)}

    def enqueue(self, body):
        if self.ready != "ready":
            raise ValueError("处理引擎尚未就绪")
        chosen = body.get("ids", [])
        if not chosen:
            raise ValueError("请先勾选视频")
        chosen = set(chosen)
        with self.lock:
            checking = sum(x["id"] in chosen and x["status"] == "checking" for x in self.media)
        if checking:
            raise ValueError(f"还有 {checking} 段素材正在读取，请等待后再开始处理")
        options = dict(self.settings)
        options.update({k: v for k, v in body.get("settings", {}).items() if k in DEFAULT_SETTINGS})
        if options["encoder"] not in {"auto", "hevc", "h264", "nvenc"} or options["profile"] not in {"dlogm", "rec709"}:
            raise ValueError("编码或输入色彩类型不正确")
        if options["range"] not in {"auto", "full", "limited"}:
            raise ValueError("输入范围不正确")
        if options["rate_mode"] not in {"source", "quality"}:
            raise ValueError("码率模式不正确")
        if options["fps"] not in FRAME_RATES:
            raise ValueError("不支持的导出帧率")
        options["quality"] = int(options["quality"])
        options["preview"] = float(options["preview"])
        if not 0 <= options["quality"] <= 40 or not math.isfinite(options["preview"]) or not 0 <= options["preview"] <= 60:
            raise ValueError("质量或预览时长不正确")
        managed = bool(body.get("browser_target")) and body.get("browser_stream_version") == 2
        output = self.output_directory(str(self.data / "render-cache") if managed else body.get("output", self.output))
        jobs = []
        batch_id = uuid.uuid4().hex
        with self.lock:
            for item in self.media:
                if item["id"] not in chosen or item["status"] != "ready":
                    continue
                look = self.get_look(item["filter"])
                settings = dict(options, strength=item["strength"],
                                enhance_enabled=item.get("enhance_enabled", False),
                                enhance_strength=item.get("enhance_strength", 100))
                if any(j["media_id"] == item["id"] and j["filter"] == look["id"] and j["settings"] == settings
                       and j.get("look_snapshot", {}).get("revision", 0) == look.get("revision", 0)
                       and Path(j["output"]).parent == output and j.get("browser_target") == body.get("browser_target") and j["status"] in {"queued", "running"} for j in self.jobs):
                    continue
                identity = uuid.uuid4().hex
                filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(item["name"]).stem)[:90]
                final_name = filename + "_" + ("preview_" if options["preview"] else "") + identity[:8] + ".mp4"
                target = output / (identity + ".render.mp4" if managed else final_name)
                label = "原色保留" if look["id"] == "original" and options["profile"] == "rec709" else look["name"]
                job = {"id": identity, "batch": batch_id, "media_id": item["id"], "name": item["name"], "filter": look["id"],
                       "filter_name": label, "settings": settings, "output": str(target), "status": "queued",
                       "progress": 0., "speed": 0., "eta": None, "created": time.time(), "duration": min(item["duration"], options["preview"]) if options["preview"] else item["duration"]}
                job["look_snapshot"] = copy.deepcopy(look)
                if managed:
                    job.update(temporary_output=True, filename=final_name, cache_removed=False)
                browser_target = body.get("browser_target")
                if browser_target:
                    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", str(browser_target)):
                        raise ValueError("保存目录标识不正确")
                    job.update(browser_target=str(browser_target), browser_destination=str(body.get("browser_destination", "文件夹"))[:150], browser_saved=False)
                self.jobs.append(job)
                jobs.append(identity)
            if not jobs:
                raise ValueError("所选视频仍在读取、不可用，或已经在等待队列中")
            self.settings.update(options)
            if not managed:
                self.output = str(output)
        self.save()
        self.wake.set()
        return {"ids": jobs}

    def cancel_job(self, identity):
        with self.lock:
            job = next(x for x in self.jobs if x["id"] == identity)
            if job["status"] == "queued":
                job.update(status="cancelled", error="已取消")
            elif job["status"] == "running":
                self.cancel.set()
                self.terminate_current()
        self.save()
        return {"ok": True}

    def set_queue_paused(self, paused):
        if not isinstance(paused, bool):
            raise ValueError("暂停状态不正确")
        with self.lock:
            if paused != self.queue_paused:
                if paused and not any(j["status"] in {"queued", "running"} for j in self.jobs):
                    return {"ok": True, "paused": False}
                if self.current_process and self.process_paused != paused:
                    changed = pause_render_process(self.current_process, paused)
                    self.process_paused = paused and changed
                self.queue_paused = paused
        self.save()
        self.wake.set()
        return {"ok": True, "paused": paused}

    def terminate_current(self):
        with self.lock:
            process = self.current_process
            if process and process.poll() is None:
                try:
                    if self.process_paused:
                        pause_render_process(process, False)
                except OSError:
                    # Windows can terminate a suspended child even if resume fails.
                    if os.name != "nt":
                        raise
                finally:
                    self.process_paused = False
                    process.terminate()

    def cancel_all(self):
        with self.lock:
            count = 0
            for job in self.jobs:
                if job["status"] == "queued":
                    job.update(status="cancelled", error="已取消", eta=None, finished=time.time())
                    count += 1
                elif job["status"] == "running":
                    self.cancel.set()
                    count += 1
            # Cancel queued jobs before releasing the worker; no next job may start.
            self.queue_paused = False
            self.terminate_current()
            self.flow = {"paused": False, "reason": "", "pending_files": 0, "pending_bytes": 0}
        self.save()
        self.wake.set()
        return {"ok": True, "count": count}

    def retry(self, identity):
        with self.lock:
            job = next(x for x in self.jobs if x["id"] == identity)
            if job["status"] not in {"failed", "cancelled", "interrupted"}:
                raise ValueError("只有失败、中止或取消的任务可以重试")
            media = next((x for x in self.media if x["id"] == job["media_id"]), None)
            if not media or not Path(media["path"]).is_file():
                raise ValueError("原片已移出素材库或被移动，请重新导入后创建任务")
            if Path(job["output"]).exists():
                raise ValueError("目标文件已存在，请重新创建导出任务")
            job.update(status="queued", progress=0., error="", eta=None, speed=0., notice="")
        self.save()
        self.wake.set()
        return {"ok": True}

    def reveal_output(self, identity=None):
        with self.lock:
            folder = Path(self.output)
            if identity:
                folder = Path(next(j for j in self.jobs if j["id"] == identity)["output"]).parent
            completed = copy.deepcopy([j for j in self.jobs if j["status"] == "done" and Path(j["output"]).parent == folder])
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ValueError("无法访问输出目录，请更换保存位置：" + str(folder)) from error
        opened = False
        reason = "当前后台环境无法唤起资源管理器，可复制路径打开，或直接下载下面的结果。"
        if os.name == "nt" and native_desktop_available():
            try:
                os.startfile(str(folder))
                opened = True
            except OSError:
                reason = "系统未能打开资源管理器，可复制路径打开，或直接下载下面的结果。"
        files = [{"id": j["id"], "name": Path(j["output"]).name, "bytes": Path(j["output"]).stat().st_size,
                  "download": "/api/download/" + j["id"]} for j in reversed(completed) if Path(j["output"]).is_file()]
        return {"path": str(folder), "opened": opened, "reason": "" if opened else reason, "files": files}

    def cleanup_render_cache(self, job):
        if not job.get("temporary_output") or not job.get("browser_saved") or job.get("cache_removed"):
            return False
        base = (self.data / "render-cache").resolve()
        target = Path(job["output"]).resolve()
        # Only delete this app's exact per-job cache file, never the delivered filename.
        if (base.parent != self.data.resolve() or target.parent != base
                or target.name != job["id"] + ".render.mp4" or target.name == job.get("browser_filename")):
            return False
        try:
            target.unlink(missing_ok=True)
        except OSError:
            return False
        with self.lock:
            job.update(cache_removed=True)
        return True

    def worker(self):
        while not self.stop.is_set():
            self.wake.wait(1)
            with self.lock:
                saved = [j for j in self.jobs if j.get("temporary_output") and j.get("browser_saved") and not j.get("cache_removed")]
            if any([self.cleanup_render_cache(j) for j in saved]):
                self.save()
            if self.ready != "ready":
                continue
            with self.lock:
                if self.queue_paused:
                    self.wake.clear()
                    continue
                job = next((j for j in self.jobs if j["status"] == "queued"), None)
                if not job:
                    self.flow = {"paused": False, "reason": "", "pending_files": 0, "pending_bytes": 0}
                    self.wake.clear()
                    continue
                pending = [j for j in self.jobs if j.get("temporary_output") and j["status"] == "done" and not j.get("cache_removed")]
                pending_bytes = sum(j.get("bytes", 0) for j in pending)
                reason = ""
                if len(pending) >= 2 or pending_bytes >= 1024 ** 3:
                    reason = "等待浏览器保存成片" if any(not j.get("browser_saved") for j in pending) else "等待临时缓存清理"
                source = next((m for m in self.media if m["id"] == job["media_id"]), None)
                estimate = max(256 * 1024 ** 2, int((source or {}).get("bytes", 0) * min(1., job.get("duration", 0) / max(1., (source or {}).get("duration", 1))) * 1.5))
                if shutil.disk_usage(self.data).free < estimate + 256 * 1024 ** 2:
                    reason = "临时磁盘空间不足，释放空间后自动继续"
                self.flow = {"paused": bool(reason), "reason": reason, "pending_files": len(pending), "pending_bytes": pending_bytes}
                if reason:
                    self.wake.clear()
                    continue
                self.cancel.clear()
                job.update(status="running", started=time.time())
            self.save()
            def progress(value, speed, eta):
                with self.lock:
                    job.update(progress=value, speed=speed, eta=eta)
            def process_changed(process):
                with self.lock:
                    self.current_process = process
                    self.process_paused = False
                    if process:
                        if self.cancel.is_set() or self.stop.is_set():
                            self.terminate_current()
                        elif self.queue_paused:
                            self.process_paused = pause_render_process(process, True)
            def notice(message, encoder):
                with self.lock:
                    job.update(notice=message, actual_encoder=encoder, progress=0., speed=0., eta=None)
            try:
                media = self.get_media(job["media_id"])
                look = copy.deepcopy(job.get("look_snapshot")) or self.get_look(job["filter"])
                log = self.data / "logs" / (job["id"] + ".log")
                result = self.renderer.render(media, look, job["settings"], job["output"], log, self.cancel, progress, process_changed, notice)
                with self.lock:
                    job.update(result, status="done", progress=1., eta=0., finished=time.time())
            except InterruptedError:
                with self.lock:
                    job.update(status="cancelled", error="已取消，原片保留", eta=None)
            except Exception as error:
                with self.lock:
                    job.update(status="failed", error=str(error), eta=None)
            finally:
                self.save()


class Handler(BaseHTTPRequestHandler):
    server_version = "Action4Studio/1.11"

    @property
    def app(self):
        return self.server.workspace

    def log_message(self, fmt, *args):
        if args and "200" not in str(args):
            print(fmt % args, flush=True)

    def json(self, value, status=200):
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_file(self, path, download=False, download_name=None):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=86400" if path.suffix == ".jpg" else "no-cache")
        if download:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(download_name or path.name))
        self.end_headers()
        with path.open("rb") as file:
            shutil.copyfileobj(file, self.wfile)

    def valid_host(self):
        host = self.headers.get("Host", "")
        return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        try:
            if not self.valid_host():
                return self.json({"error": "此工作台仅供本机访问"}, 403)
            parsed = urlsplit(self.path)
            route, query = parsed.path, parse_qs(parsed.query)
            if route == "/api/ping":
                return self.json({"product": "Action4 Studio", "version": "1.11"})
            if route == "/api/state":
                since = int(query["since"][0]) if query.get("since") else None
                return self.json(self.app.state(query.get("compact", ["0"])[0] == "1", since, query.get("instance", [None])[0]))
            if route == "/":
                content = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("__SESSION_TOKEN__", self.app.token).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                return self.wfile.write(content)
            if route in {"/app.js", "/browser-output.js", "/style.css"}:
                return self.send_file(ROOT / "web" / route.lstrip("/"))
            if route == "/api/preview":
                if not self.app.renderer:
                    return self.json({"error": "处理引擎正在准备"}, 503)
                item = self.app.get_media(query["id"][0])
                if item["status"] != "ready":
                    return self.json({"error": "视频尚不可用"}, 422)
                look = self.app.get_look(query.get("filter", [item["filter"]])[0])
                settings = dict(self.app.settings, strength=int(query.get("strength", [item["strength"]])[0]))
                settings["profile"] = query.get("profile", ["dlogm"])[0]
                settings["range"] = query.get("range", ["auto"])[0]
                enhance = query.get("enhance", ["1" if item.get("enhance_enabled") else "0"])[0]
                if enhance not in {"0", "1"}:
                    raise ValueError("增强开关不正确")
                settings["enhance_enabled"] = enhance == "1"
                settings["enhance_strength"] = int(query.get("enhance_strength", [item.get("enhance_strength", 100)])[0])
                if not 0 <= settings["enhance_strength"] <= 100:
                    raise ValueError("增强强度应在 0–100 之间")
                if settings["profile"] not in {"dlogm", "rec709"}:
                    raise ValueError("输入色彩类型不正确")
                if settings["range"] not in {"auto", "full", "limited"}:
                    raise ValueError("输入色彩范围不正确")
                width = max(160, min(1440, int(query.get("width", [1280])[0])))
                second = float(query.get("time", [0])[0])
                if not math.isfinite(second):
                    raise ValueError("时间不正确")
                second = max(0., min(second, max(0, item["duration"] - .05)))
                result = self.app.renderer.preview(item, look, settings, second, query.get("before", ["0"])[0] == "1", width)
                return self.send_file(result)
            if route.startswith("/api/download/"):
                identity = route.rsplit("/", 1)[1]
                with self.app.lock:
                    job = copy.deepcopy(next(j for j in self.app.jobs if j["id"] == identity and j["status"] == "done"))
                if job.get("cache_removed"):
                    return self.json({"error": "成片已保存到所选目录，后台临时缓存已清理"}, 410)
                return self.send_file(Path(job["output"]), download=True, download_name=job.get("filename"))
            return self.json({"error": "未找到页面"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (ValueError, KeyError, StopIteration, FileNotFoundError) as error:
            self.json({"error": str(error) or "文件或任务不存在"}, 400)
        except Exception as error:
            self.json({"error": str(error)}, 500)

    def do_POST(self):
        try:
            if not self.valid_host() or self.headers.get("X-Workspace-Token") != self.app.token:
                return self.json({"error": "请从工作台页面发起操作，刷新后重试"}, 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.json({"error": "来源不正确"}, 403)
            parsed = urlsplit(self.path)
            route, query = parsed.path, parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", "0"))
            if route == "/api/upload":
                if not 0 < length <= 1024 ** 4:
                    raise ValueError("视频文件大小不正确")
                if shutil.disk_usage(self.app.data).free < length + 256 * 1024 * 1024:
                    raise ValueError("本地磁盘空间不足，请改用系统文件选择直接引用原片")
                name = Path(unquote(self.headers.get("X-File-Name", "video.mp4"))).name
                if Path(name).suffix.lower() not in core.EXTENSIONS:
                    raise ValueError("请选择 MP4、MOV 或 M4V 视频")
                folder = self.app.data / "imports" / uuid.uuid4().hex
                folder.mkdir()
                target = folder / name
                try:
                    with target.open("wb") as file:
                        remaining = length
                        while remaining:
                            block = self.rfile.read(min(1024 * 1024, remaining))
                            if not block:
                                raise ValueError("导入中断，请重新拖入视频")
                            file.write(block)
                            remaining -= len(block)
                    return self.json(self.app.add_paths([str(target)], deferred=True))
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
            if route == "/api/luts":
                if not 0 < length <= 32 * 1024 * 1024:
                    raise ValueError("LUT 文件需小于 32 MB")
                return self.json(self.app.add_lut(self.rfile.read(length), unquote(self.headers.get("X-File-Name", "custom.cube")), query.get("stage", ["creative"])[0]))
            if length > 2 * 1024 * 1024:
                raise ValueError("请求过大")
            body = json.loads(self.rfile.read(length) or b"{}")
            if route == "/api/looks/delete":
                return self.json(self.app.delete_look(body["id"]))
            if route == "/api/looks/restore":
                return self.json(self.app.restore_looks(body.get("id")))
            if route == "/api/import":
                return self.json(self.app.add_paths(body.get("paths", []), body.get("recursive", True)))
            if route == "/api/picker":
                return self.json(self.app.native_dialog(body.get("kind", "files")))
            if route == "/api/apply":
                return self.json(self.app.apply_look(body))
            if route == "/api/enhance":
                return self.json(self.app.apply_enhancement(body))
            if route == "/api/output":
                return self.json(self.app.set_output(body.get("path", "")))
            if route == "/api/browser-saved":
                with self.app.lock:
                    job = next(j for j in self.app.jobs if j["id"] == body.get("id"))
                    if job["status"] != "done" or job.get("browser_target") != body.get("target"):
                        raise ValueError("任务尚未完成或保存目录不匹配")
                    if job.get("temporary_output") and (body.get("name") != job.get("filename") or body.get("bytes") != job.get("bytes")):
                        raise ValueError("成片保存信息不完整，临时缓存已保留")
                    job.update(browser_saved=True, browser_filename=str(body.get("name", "")), browser_saved_at=time.time())
                self.app.save()
                if self.app.cleanup_render_cache(job):
                    self.app.save()
                self.app.wake.set()
                return self.json({"ok": True, "cache_removed": job.get("cache_removed", False)})
            if route == "/api/jobs":
                return self.json(self.app.enqueue(body))
            if route == "/api/queue/pause":
                return self.json(self.app.set_queue_paused(body.get("paused")))
            if route == "/api/queue/cancel":
                return self.json(self.app.cancel_all())
            if route == "/api/cancel":
                return self.json(self.app.cancel_job(body["id"]))
            if route == "/api/retry":
                return self.json(self.app.retry(body["id"]))
            if route == "/api/remove":
                identities = set(body.get("ids", []))
                with self.app.lock:
                    active = {j["media_id"] for j in self.app.jobs if j["status"] in {"queued", "running"}}
                    if identities & active:
                        raise ValueError("请先取消这些视频的待处理任务")
                    self.app.media[:] = [x for x in self.app.media if x["id"] not in identities]
                    self.app.media_revision += 1
                self.app.save()
                return self.json({"ok": True})
            if route == "/api/clear-jobs":
                with self.app.lock:
                    self.app.jobs[:] = [j for j in self.app.jobs if j["status"] not in TERMINAL or (j.get("temporary_output") and j["status"] == "done" and not j.get("cache_removed"))]
                self.app.save()
                return self.json({"ok": True})
            if route == "/api/reveal":
                return self.json(self.app.reveal_output(body.get("id")))
            if route == "/api/setup":
                if self.app.ready == "error":
                    self.app.ready = "loading"
                    threading.Thread(target=self.app.prepare, daemon=True).start()
                return self.json({"ok": True})
            if route == "/api/shutdown":
                self.app.stop.set()
                self.app.cancel.set()
                self.app.terminate_current()
                if self.app.current_dialog and self.app.current_dialog.poll() is None:
                    self.app.current_dialog.terminate()
                self.json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.json({"error": "未知操作"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, StopIteration, FileNotFoundError) as error:
            self.json({"error": str(error) or "文件或任务不存在"}, 400)
        except Exception as error:
            self.json({"error": str(error)}, 500)


def main():
    parser = argparse.ArgumentParser(description="Action4 Studio 本地色彩工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--data", default=str(ROOT / ".runtime"))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = Workspace(args.data, args.ffmpeg)
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("找不到可用的本地端口")
    server.daemon_threads = True
    server.workspace = app
    url = "http://127.0.0.1:" + str(server.server_port)
    (app.data / "server.json").write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    print("Action4 Studio: " + url, flush=True)
    if not args.no_browser:
        import webbrowser
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        app.cancel.set()
        app.terminate_current()
        app.pool.shutdown(wait=False, cancel_futures=True)
        app.save()
        server.server_close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    main()
