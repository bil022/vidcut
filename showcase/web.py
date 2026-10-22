#!/usr/bin/env python3
"""vidcut 网页编辑器：在浏览器里改 project.json、实时预览、出片、加入新素材。服务本身只用 Python 标准库。

用法：
  python3 web.py                 # 打开 work/ 工作目录
  python3 web.py -w work_real    # 其他工作目录
然后浏览器打开 http://127.0.0.1:8766

接口：
  GET  /api/state      项目 + 媒体信息（摘要、转录）+ 后台任务状态
  PUT  /api/project    校验后保存 project.json，旧版本存到 history/
  POST /api/render     后台渲染 {"draft": true|false}
  POST /api/update     后台加入新素材 {"folder": "可选的新目录"}（vidcut.py update）
  POST /api/auto       后台全自动 {"folder", "name", "rebuild", "final"}（vidcut.py auto）
  GET  /api/settings   读取 settings.json（不存在时生成默认值）
  PUT  /api/settings   校验后保存 settings.json，旧版本存到 history/
  GET  /api/outputs    out/ 里的视频和字幕
  GET  /api/job        后台任务进度
  GET  /media/<id>/<proxy.mp4|thumb.jpg|kf_N.jpg>   代理与缩略图（支持 Range，可拖动进度）
  GET  /out/<文件名>    渲染结果（加 ?download=1 作为附件下载）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
INDEX = HERE / "web" / "index.html"
SCHEMA_VERSION = 2
TRANSITIONS = {"cut", "fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "slideleft",
               "slideright", "smoothleft", "smoothright", "circleopen", "circleclose", "radial", "zoomin", "distance"}
CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".srt": "text/plain; charset=utf-8"}


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


class Editor:
    def __init__(self, work: Path):
        self.work = work
        self.project_file = work / "project.json"
        self.lock = threading.Lock()
        self.job = {"kind": None, "status": "idle", "draft": True, "log": [], "output": None}
        self.asr: str | None = None        # 加入新素材时用的语音识别库（uv --with 的包名）
        self.asr_model: str | None = None

    def media(self) -> dict:
        return (load_json(self.work / "media.json") or {}).get("media", {})

    def state(self) -> dict:
        media = {}
        for mid, m in self.media().items():
            d = self.work / "derived" / mid
            media[mid] = {
                k: m.get(k) for k in ("name", "duration_ms", "width", "height", "portrait",
                                      "created", "gps", "has_audio", "fps")
            }
            media[mid]["summary"] = load_json(d / "summary.json")
            tr = load_json(d / "transcript.json", {})
            media[mid]["transcript"] = tr.get("segments", [])
            media[mid]["language"] = tr.get("language")
        data = load_json(self.work / "media.json") or {}
        return {"work": str(self.work), "project": load_json(self.project_file),
                "media": media, "sources": data.get("sources", []), "job": self.job,
                "default_folder": os.environ.get("VIDCUT_DEFAULT_FOLDER", "")}

    def outputs(self) -> list[dict]:
        out = self.work / "out"
        files = [f for f in out.glob("*") if f.suffix in (".mp4", ".srt")] if out.is_dir() else []
        return [{"name": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime), "url": "out/" + quote(f.name)}
                for f in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)]

    def backup(self, path: Path, prefix: str) -> None:
        if path.exists():
            hist = self.work / "history"
            hist.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(path, hist / f"{prefix}-{stamp}.json")

    def settings(self) -> tuple[dict | None, str]:
        """读取 settings.json；不存在或缺字段时让 vidcut.py 补全默认值。"""
        self.work.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(self.vidcut_cmd() + ["settings"], capture_output=True, text=True, cwd=HERE)
        msg = (p.stdout + p.stderr).strip()
        return load_json(self.work / "settings.json"), "" if p.returncode == 0 else msg

    def save_settings(self, st: dict) -> list[str]:
        """写入后用 vidcut.py settings 校验；不通过就恢复原文件。"""
        path = self.work / "settings.json"
        with self.lock:
            old = path.read_bytes() if path.exists() else None
            self.backup(path, "settings")
            path.write_text(json.dumps(st, ensure_ascii=False, indent=2))
            p = subprocess.run(self.vidcut_cmd() + ["settings"], capture_output=True, text=True, cwd=HERE)
            if p.returncode != 0:
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(old)
                lines = [ln.strip() for ln in (p.stdout + p.stderr).splitlines()]
                return [ln for ln in lines if ln and "有问题" not in ln and "punycode" not in ln
                        and "trace-deprecation" not in ln] or ["设置无效"]
        return []

    def validate(self, p: dict) -> list[str]:
        errs: list[str] = []
        media = self.media()
        if not isinstance(p, dict) or p.get("schema_version") != SCHEMA_VERSION:
            return [f"schema_version 必须是 {SCHEMA_VERSION}"]
        cfg = p.get("project", {})
        res = cfg.get("resolution")
        if not (isinstance(res, list) and len(res) == 2 and all(isinstance(x, int) and x > 0 and x % 2 == 0 for x in res)):
            errs.append("resolution 必须是两个正偶数")
        if not (isinstance(cfg.get("fps"), int) and 1 <= cfg["fps"] <= 120):
            errs.append("fps 必须是 1–120 的整数")
        if cfg.get("portrait_mode") not in ("blur_fill", "pad"):
            errs.append("portrait_mode 只能是 blur_fill 或 pad")
        if not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
            errs.append("项目名不能为空")
        sub_cfg = cfg.get("subtitles")
        if sub_cfg is not None and not (isinstance(sub_cfg, dict) and isinstance(sub_cfg.get("burn_in", False), bool)
                                        and sub_cfg.get("show", "both") in ("both", "original", "translation")):
            errs.append("subtitles 设置无效：burn_in 为 true/false，show 为 both / original / translation")
        tracks = p.get("tracks")
        if not (isinstance(tracks, list) and tracks and tracks[0].get("kind") == "video"):
            return errs + ["第一条轨道必须是 video"]
        seen = set()
        for i, it in enumerate(tracks[0].get("items", [])):
            tag = f"第 {i + 1} 项（{it.get('name') or it.get('id')}）"
            if it.get("id") in seen:
                errs.append(f"{tag}: id 重复")
            seen.add(it.get("id"))
            tr = it.get("transition")
            if tr is not None and not (isinstance(tr, dict) and tr.get("type") in TRANSITIONS
                                       and isinstance(tr.get("duration_ms", 600), int) and 100 <= tr.get("duration_ms", 600) <= 3000):
                errs.append(f"{tag}: 转场无效（类型见 settings.json 说明，时长 0.1–3 秒）")
            if it.get("type") == "clip":
                m = media.get(it.get("media_id"))
                if m is None:
                    errs.append(f"{tag}: 找不到媒体 {it.get('media_id')}")
                    continue
                a, b = it.get("in_ms"), it.get("out_ms")
                if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= m["duration_ms"]):
                    errs.append(f"{tag}: 入点/出点必须满足 0 ≤ 入点 < 出点 ≤ {m['duration_ms']}")
                    continue
                if b - a < 200:
                    errs.append(f"{tag}: 片段至少 0.2 秒")
                v = it.get("volume_db", 0)
                if not (isinstance(v, (int, float)) and -30 <= v <= 30):
                    errs.append(f"{tag}: 音量须在 -30 到 30 dB 之间")
                fi, fo = it.get("fade_in_ms", 0), it.get("fade_out_ms", 0)
                if not (isinstance(fi, int) and isinstance(fo, int) and fi >= 0 and fo >= 0 and fi + fo <= b - a):
                    errs.append(f"{tag}: 淡入淡出必须是非负整数，且总和不超过片段长度")
                subs = it.get("subtitles")
                if subs is not None:
                    ok = isinstance(subs, list) and all(
                        isinstance(x, dict) and isinstance(x.get("start_ms"), int) and isinstance(x.get("end_ms"), int)
                        and x["start_ms"] < x["end_ms"] and isinstance(x.get("text", ""), str) and len(x.get("text", "")) <= 200
                        and isinstance(x.get("translation", ""), str) and len(x.get("translation", "")) <= 300 for x in subs)
                    if not ok:
                        errs.append(f"{tag}: 字幕格式无效（每句需要 start_ms < end_ms，原文最多 200 字，译文最多 300 字）")
                cap = it.get("caption")
                if cap is not None:
                    if not (isinstance(cap, dict) and isinstance(cap.get("text"), str) and len(cap["text"]) <= 60):
                        errs.append(f"{tag}: 画面文字最多 60 个字")
                    elif cap.get("position") not in ("bottom", "center", "top"):
                        errs.append(f"{tag}: 文字位置只能是 bottom / center / top")
                    elif not (isinstance(cap.get("start_ms"), int) and isinstance(cap.get("end_ms"), int)
                              and 0 <= cap["start_ms"] < cap["end_ms"] <= b - a):
                        errs.append(f"{tag}: 文字的显示时间须在片段范围内（0–{(b - a) / 1000:.1f}s）")
            elif it.get("type") == "title":
                if not (isinstance(it.get("text"), str) and 0 < len(it["text"].strip()) <= 40):
                    errs.append(f"{tag}: 标题文字须为 1–40 个字")
                if not (isinstance(it.get("subtitle", ""), str) and len(it.get("subtitle", "")) <= 60):
                    errs.append(f"{tag}: 副标题最多 60 个字")
                d = it.get("duration_ms")
                if not (isinstance(d, int) and 500 <= d <= 15000):
                    errs.append(f"{tag}: 标题卡时长须在 0.5–15 秒之间")
                    continue
                if it.get("background", "black") not in ("black", "blur_next"):
                    errs.append(f"{tag}: 背景只能是 black 或 blur_next")
                fi, fo = it.get("fade_in_ms", 0), it.get("fade_out_ms", 0)
                if not (isinstance(fi, int) and isinstance(fo, int) and fi >= 0 and fo >= 0 and fi + fo <= d):
                    errs.append(f"{tag}: 淡入淡出必须是非负整数，且总和不超过时长")
            elif it.get("type") == "gap":
                d = it.get("duration_ms")
                if not (isinstance(d, int) and 100 <= d <= 60000):
                    errs.append(f"{tag}: 间隔须在 0.1–60 秒之间")
                if not re.fullmatch(r"[a-z]+|0x[0-9a-fA-F]{6}", str(it.get("background", "black"))):
                    errs.append(f"{tag}: 背景色只能是颜色名或 0xRRGGBB")
            else:
                errs.append(f"{tag}: 未知类型 {it.get('type')}")
        return errs

    def save(self, p: dict) -> str:
        with self.lock:
            self.backup(self.project_file, "project")
            tmp = self.project_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2))
            tmp.replace(self.project_file)
        return dt.datetime.now().isoformat(timespec="seconds")

    def vidcut_cmd(self, with_asr: bool = False) -> list[str]:
        # 用 uv 运行，自动带上 vidcut.py 声明的依赖（Pillow 用于画标题文字）
        if shutil.which("uv"):
            cmd = ["uv", "run"]
            if with_asr and self.asr:
                cmd += ["--with", self.asr]
            return cmd + [str(HERE / "vidcut.py"), "-w", str(self.work)]
        return [sys.executable, str(HERE / "vidcut.py"), "-w", str(self.work)]

    def start_job(self, kind: str, cmd: list[str], draft: bool = True) -> bool:
        with self.lock:
            if self.job["status"] == "running":
                return False
            self.job = {"kind": kind, "status": "running", "draft": draft, "log": [], "output": None}
        threading.Thread(target=self._run_job, args=(cmd,), daemon=True).start()
        return True

    def start_render(self, draft: bool) -> bool:
        cmd = self.vidcut_cmd() + ["render"] + (["--draft"] if draft else [])
        return self.start_job("render", cmd, draft)

    def start_update(self, folder: str | None) -> bool:
        cmd = self.vidcut_cmd(with_asr=True) + ["update"]
        if folder:
            cmd.append(folder)
        if self.asr_model:
            cmd += ["--model", self.asr_model]
        return self.start_job("update", cmd)

    def start_auto(self, folder: str | None, name: str | None, rebuild: bool, final: bool) -> bool:
        cmd = self.vidcut_cmd(with_asr=True) + ["auto"]
        if folder:
            cmd.append(folder)
        if name:
            cmd += ["--name", name]
        if rebuild:
            cmd.append("--rebuild")  # vidcut.py 覆盖 project.json 前会自己备份到 history/
        if final:
            cmd.append("--final")
        if self.asr_model:
            cmd += ["--model", self.asr_model]
        return self.start_job("auto", cmd)

    def _run_job(self, cmd: list[str]) -> None:
        job = self.job
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    cwd=HERE, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except OSError as e:
            job["log"].append(f"无法启动：{e}")
            job["status"] = "failed"
            return
        for line in proc.stdout:
            line = line.rstrip()
            if line and "punycode" not in line and "trace-deprecation" not in line:
                job["log"].append(line)
            if (m := re.search(r"完成：(\S+\.mp4)", line)):
                job["output"] = "out/" + Path(m.group(1)).name
        ok = proc.wait() == 0 and (job["kind"] not in ("render",) or job["output"])
        job["status"] = "done" if ok else "failed"


class Handler(BaseHTTPRequestHandler):
    editor: Editor

    def log_message(self, fmt, *args):  # 只打印出错的请求
        if args and str(args[1])[0] in "45":
            super().log_message(fmt, *args)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 5 << 20:
            raise ValueError("请求太大")
        return json.loads(self.rfile.read(n) or b"{}")

    def send_file(self, path: Path, cache: bool, download: bool = False):
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if (m := re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())):
            s, e = m.groups()
            if s:
                start, end = int(s), min(int(e), size - 1) if e else size - 1
            elif e:
                start = max(0, size - int(e))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(1 << 16, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 浏览器拖动进度条时会主动断开旧请求

    def do_GET(self):
        url = urlparse(self.path)
        path = unquote(url.path)
        ed = self.editor
        if path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self.send_json(ed.state())
        elif path == "/api/job":
            self.send_json(ed.job)
        elif path == "/api/outputs":
            self.send_json(ed.outputs())
        elif path == "/api/settings":
            st, err = ed.settings()
            self.send_json({"settings": st, "error": err}, 200 if st else 500)
        elif (m := re.fullmatch(r"/media/(m_[0-9a-f]{16})/(proxy\.mp4|thumb\.jpg|kf_\d+\.jpg)", path)):
            if m.group(1) not in ed.media():
                return self.send_json({"error": "unknown media"}, 404)
            self.send_file(ed.work / "derived" / m.group(1) / m.group(2), cache=True)
        elif (m := re.fullmatch(r"/out/([^/\\]+\.(?:mp4|srt))", path)):
            self.send_file(ed.work / "out" / m.group(1), cache=False,
                           download="download" in parse_qs(url.query))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        if urlparse(self.path).path == "/api/settings":
            try:
                st = self.read_json()
            except ValueError as e:
                return self.send_json({"errors": [f"JSON 无效: {e}"]}, 400)
            if not isinstance(st, dict):
                return self.send_json({"errors": ["设置必须是 JSON 对象"]}, 400)
            if self.editor.job["status"] == "running":
                return self.send_json({"errors": ["后台任务进行中，请等它完成再改设置"]}, 409)
            if (errs := self.editor.save_settings(st)):
                return self.send_json({"errors": errs}, 400)
            return self.send_json({"saved_at": dt.datetime.now().isoformat(timespec="seconds")})
        if urlparse(self.path).path != "/api/project":
            return self.send_json({"error": "not found"}, 404)
        try:
            p = self.read_json()
        except ValueError as e:
            return self.send_json({"errors": [f"JSON 无效: {e}"]}, 400)
        if (errs := self.editor.validate(p)):
            return self.send_json({"errors": errs}, 400)
        self.send_json({"saved_at": self.editor.save(p)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
        except ValueError:
            body = {}
        if path == "/api/render":
            started = self.editor.start_render(bool(body.get("draft", True)))
        elif path == "/api/update":
            folder = str(body.get("folder") or "").strip() or None
            if folder and not Path(folder).expanduser().is_dir():
                return self.send_json({"error": f"目录不存在：{folder}"}, 400)
            started = self.editor.start_update(folder)
        elif path == "/api/auto":
            folder = str(body.get("folder") or "").strip() or None
            if folder and not Path(folder).expanduser().is_dir():
                return self.send_json({"error": f"目录不存在：{folder}"}, 400)
            if not folder and not (self.editor.work / "media.json").exists():
                return self.send_json({"error": "请填写素材目录"}, 400)
            name = str(body.get("name") or "").strip() or None
            started = self.editor.start_auto(folder, name, bool(body.get("rebuild")), bool(body.get("final")))
        else:
            return self.send_json({"error": "not found"}, 404)
        if not started:
            return self.send_json({"error": "已有后台任务在进行，请等它完成"}, 409)
        self.send_json(self.editor.job, 202)


def main() -> None:
    ap = argparse.ArgumentParser(description="vidcut 网页编辑器")
    ap.add_argument("-w", "--work", default="work", help="工作目录（默认 ./work）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只允许本机访问")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--asr", default="auto", choices=["auto", "mlx-whisper", "faster-whisper", "none"],
                    help="加入新素材时用的语音识别；auto = Apple Silicon 用 mlx-whisper，其他跳过转录")
    ap.add_argument("--asr-model", help="Whisper 模型（默认用 models/ 下已下载的模型）")
    args = ap.parse_args()
    work = Path(args.work).resolve()
    if not (work / "project.json").exists():
        print(f"{work} 里还没有剪辑项目：在网页上填写素材目录，点“开始自动处理”", flush=True)
    editor = Editor(work)
    if args.asr == "auto":
        apple = platform.system() == "Darwin" and platform.machine() == "arm64"
        editor.asr = "mlx-whisper" if apple else None
    elif args.asr != "none":
        editor.asr = args.asr
    local_models = sorted((HERE / "models").glob("*whisper*")) if editor.asr == "mlx-whisper" else []
    editor.asr_model = args.asr_model or (str(local_models[0]) if local_models else None)
    Handler.editor = editor
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"工作目录 {work}\n打开 http://{args.host}:{args.port}  （Ctrl+C 退出）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
