#!/usr/bin/env python3
"""vidcut 网页编辑器：在浏览器里改 project.json、实时预览、出草稿。只用 Python 标准库。

用法：
  python3 web.py                 # 打开 work/ 工作目录
  python3 web.py -w work_real    # 其他工作目录
然后浏览器打开 http://127.0.0.1:8765

接口：
  GET  /api/state      项目 + 媒体信息（摘要、转录）+ 渲染状态
  PUT  /api/project    校验后保存 project.json，旧版本存到 history/
  POST /api/render     后台渲染 {"draft": true|false}
  GET  /api/render     渲染进度
  GET  /media/<id>/<proxy.mp4|thumb.jpg|kf_N.jpg>   代理与缩略图（支持 Range，可拖动进度）
  GET  /out/<文件名>    渲染结果
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
INDEX = HERE / "web" / "index.html"
SCHEMA_VERSION = 2
CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".srt": "text/plain; charset=utf-8"}


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


class Editor:
    def __init__(self, work: Path):
        self.work = work
        self.project_file = work / "project.json"
        self.lock = threading.Lock()
        self.render = {"status": "idle", "draft": True, "log": [], "output": None}

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
            media[mid]["transcript"] = load_json(d / "transcript.json", {}).get("segments", [])
        return {"work": str(self.work), "project": load_json(self.project_file),
                "media": media, "render": self.render}

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
        tracks = p.get("tracks")
        if not (isinstance(tracks, list) and tracks and tracks[0].get("kind") == "video"):
            return errs + ["第一条轨道必须是 video"]
        seen = set()
        for i, it in enumerate(tracks[0].get("items", [])):
            tag = f"第 {i + 1} 项（{it.get('name') or it.get('id')}）"
            if it.get("id") in seen:
                errs.append(f"{tag}: id 重复")
            seen.add(it.get("id"))
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
            if self.project_file.exists():
                hist = self.work / "history"
                hist.mkdir(exist_ok=True)
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                shutil.copy2(self.project_file, hist / f"project-{stamp}.json")
            tmp = self.project_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2))
            tmp.replace(self.project_file)
        return dt.datetime.now().isoformat(timespec="seconds")

    def start_render(self, draft: bool) -> bool:
        with self.lock:
            if self.render["status"] == "running":
                return False
            self.render = {"status": "running", "draft": draft, "log": [], "output": None}
        threading.Thread(target=self._render, args=(draft,), daemon=True).start()
        return True

    def _render(self, draft: bool) -> None:
        cmd = [sys.executable, str(HERE / "vidcut.py"), "-w", str(self.work), "render"]
        if draft:
            cmd.append("--draft")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.rstrip()
            if line and "punycode" not in line and "trace-deprecation" not in line:
                self.render["log"].append(line)
            if (m := re.search(r"完成：(\S+\.mp4)", line)):
                self.render["output"] = "/out/" + Path(m.group(1)).name
        ok = proc.wait() == 0 and self.render["output"]
        self.render["status"] = "done" if ok else "failed"


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

    def send_file(self, path: Path, cache: bool):
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
        path = unquote(urlparse(self.path).path)
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
        elif path == "/api/render":
            self.send_json(ed.render)
        elif (m := re.fullmatch(r"/media/(m_[0-9a-f]{16})/(proxy\.mp4|thumb\.jpg|kf_\d+\.jpg)", path)):
            if m.group(1) not in ed.media():
                return self.send_json({"error": "unknown media"}, 404)
            self.send_file(ed.work / "derived" / m.group(1) / m.group(2), cache=True)
        elif (m := re.fullmatch(r"/out/([^/\\]+\.(?:mp4|srt))", path)):
            self.send_file(ed.work / "out" / m.group(1), cache=False)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
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
        if urlparse(self.path).path != "/api/render":
            return self.send_json({"error": "not found"}, 404)
        try:
            draft = bool(self.read_json().get("draft", True))
        except ValueError:
            draft = True
        if not self.editor.start_render(draft):
            return self.send_json({"error": "已有渲染在进行"}, 409)
        self.send_json(self.editor.render, 202)


def main() -> None:
    ap = argparse.ArgumentParser(description="vidcut 网页编辑器")
    ap.add_argument("-w", "--work", default="work", help="工作目录（默认 ./work）")
    ap.add_argument("--host", default="127.0.0.1", help="默认只允许本机访问")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    work = Path(args.work).resolve()
    if not (work / "project.json").exists():
        sys.exit(f"{work} 下没有 project.json，请先运行 vidcut.py roughcut")
    Handler.editor = Editor(work)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"编辑 {work / 'project.json'}\n打开 http://{args.host}:{args.port}  （Ctrl+C 退出）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
