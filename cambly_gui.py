#!/usr/bin/env python3
"""Cambly 课程导出 — 浏览器标签页版 GUI

启动:
    python3 cambly_gui.py

实现:
- 用 stdlib http.server 起本地 HTTP(127.0.0.1,OS 自动分配空闲端口)
- 自动用默认浏览器打开 http://127.0.0.1:PORT/
- 前端是纯 HTML/CSS/JS(本文件末尾),5 个 URL 槽 + 下载路径 + 实时日志
- 后端把 cambly_export.py 当子进程跑,实时把日志进 buffer
- 前端每 500ms 拉一次新日志,实现"准实时"刷新
- 跨平台零编译依赖:只依赖 cambly_export.py 已要的 playwright

设计取舍:
- 没用 SSE/WS,改用 500ms 轮询 — 实现简单、跨浏览器一致、对这个场景延迟可接受
- 文件夹选择用文本输入 + "在文件管理器中打开"按钮,而不是浏览器 picker
  (浏览器出于安全不会给 JS 完整绝对路径,macOS native picker 需要额外进程,得不偿失)
- 关浏览器 = 关 server(Ctrl+C 终止 server 后,子进程也会被清理)
"""

from __future__ import annotations

import collections
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent.resolve()
EXPORT_SCRIPT = HERE / "cambly_export.py"
DEFAULT_OUT = Path.home() / "Documents" / "CamblyNotes"


class CamblyAPI:
    """后端业务逻辑。HTTP handler 只负责协议,真活儿在这。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._login_proc: subprocess.Popen | None = None
        self._login_proc_lock = threading.Lock()
        self._log_buffer: collections.deque = collections.deque(maxlen=5000)
        self._log_lock = threading.Lock()
        self._last_exit_code: int | None = None
        self._last_total: int = 0
        self._last_done: int = 0
        self._started_at: float | None = None
        self._finished_at: float | None = None

    # ---------- 对外 API ----------

    def default_output_dir(self) -> str:
        return str(DEFAULT_OUT)

    def start_export(
        self, urls: list[str], out_dir: str, login: bool = False
    ) -> dict[str, Any]:
        urls = [u.strip() for u in (urls or []) if u and u.strip()]
        if not urls:
            return {"ok": False, "error": "至少要填一个 URL"}

        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": False, "error": "已有任务在跑,请先取消或等待完成"}

        if not EXPORT_SCRIPT.exists():
            return {"ok": False, "error": f"找不到 {EXPORT_SCRIPT},请确认在同一目录运行"}

        out_dir = (out_dir or "").strip() or str(DEFAULT_OUT)
        out_path = Path(out_dir).expanduser()
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"无法创建输出目录: {e}"}

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [sys.executable, "-u", str(EXPORT_SCRIPT), *urls, "--out", str(out_path)]
        if login:
            cmd.append("--login")

        self._last_exit_code = None
        self._last_total = len(urls)
        self._last_done = 0
        self._started_at = time.time()
        self._finished_at = None

        self._push_log("info", f"启动子进程(共 {len(urls)} 条)")
        self._push_log("info", f"输出目录: {out_path}")
        if login:
            self._push_log(
                "warn",
                "登录模式:Playwright 会弹浏览器,登完回终端按 Enter,日志会接着输出",
            )
        self._push_log("info", "---")

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(HERE),
                env=env,
            )
        except OSError as e:
            self._proc = None
            return {"ok": False, "error": f"启动子进程失败: {e}"}

        threading.Thread(target=self._read_output, args=(self._proc,), daemon=True).start()
        return {"ok": True, "pid": self._proc.pid, "total": len(urls)}

    def cancel_export(self) -> dict[str, Any]:
        with self._proc_lock:
            if self._proc is None or self._proc.poll() is None:
                return {"ok": False, "error": "没有任务在跑"}
            try:
                self._proc.terminate()
                self._push_log("warn", "[用户取消] 正在终止子进程...")
                return {"ok": True}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)}

    def get_state(self) -> dict[str, Any]:
        with self._proc_lock:
            running = self._proc is not None and self._proc.poll() is None
        with self._login_proc_lock:
            login_running = self._login_proc is not None and self._login_proc.poll() is None
            login_pid = self._login_proc.pid if self._login_proc else None
        elapsed = None
        if self._started_at is not None:
            end = self._finished_at or time.time()
            elapsed = round(end - self._started_at, 1)
        return {
            "running": running,
            "last_exit_code": self._last_exit_code,
            "total": self._last_total,
            "done": self._last_done,
            "elapsed": elapsed,
            "login_open": login_running,
            "login_pid": login_pid,
        }

    def start_login_window(self) -> dict[str, Any]:
        """弹一个持久化登录浏览器(GUI 模式专用),等 SIGTERM 关闭。"""
        with self._login_proc_lock:
            if self._login_proc is not None and self._login_proc.poll() is None:
                return {"ok": False, "error": "登录窗口已经在开着了"}

        if not EXPORT_SCRIPT.exists():
            return {"ok": False, "error": f"找不到 {EXPORT_SCRIPT}"}

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [sys.executable, "-u", str(EXPORT_SCRIPT), "--login-keep-open"]

        self._push_log("info", "正在打开登录浏览器...")
        self._push_log("info", "(cookie 改动会自动落盘,无需按 Enter)")

        try:
            self._login_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(HERE),
                env=env,
            )
        except OSError as e:
            self._login_proc = None
            return {"ok": False, "error": f"启动登录浏览器失败: {e}"}

        # 后台线程读 stdout,推到 log buffer
        threading.Thread(
            target=self._read_output_to_buffer, args=(self._login_proc, "login"),
            daemon=True,
        ).start()
        # 另一个线程专门等子进程退出,清理状态(意外关浏览器 / 崩溃)
        threading.Thread(target=self._watch_login_proc, daemon=True).start()

        return {"ok": True, "pid": self._login_proc.pid}

    def close_login_window(self) -> dict[str, Any]:
        """SIGTERM 登录浏览器子进程。"""
        with self._login_proc_lock:
            if self._login_proc is None or self._login_proc.poll() is not None:
                return {"ok": False, "error": "登录窗口没开"}
            try:
                self._login_proc.terminate()
                self._push_log("info", "[登录] 正在关闭登录浏览器...")
                return {"ok": True}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)}

    def get_logs(self, since: float = 0.0) -> dict[str, Any]:
        with self._log_lock:
            logs = [
                {"ts": ts, "kind": k, "msg": m}
                for ts, k, m in self._log_buffer
                if ts > since
            ]
        return {"logs": logs}

    def reveal(self, path: str) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.exists():
            return {"ok": False, "error": f"路径不存在: {p}"}
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif sys.platform.startswith("win"):
                # Windows 上 explorer 直接接受路径,逗号分隔能"选中"
                subprocess.Popen(["explorer", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return {"ok": True}
        except OSError as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def shutdown(self) -> None:
        """清理:终止子进程。"""
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        with self._login_proc_lock:
            if self._login_proc is not None and self._login_proc.poll() is None:
                try:
                    self._login_proc.terminate()
                except Exception:  # noqa: BLE001
                    pass

    # ---------- 内部:子进程输出 ----------

    def _read_output(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, ""):
                if not raw:
                    break
                line = raw.rstrip("\n")
                # 解析 "[i/total]" 进度行,更新 done 计数
                if line.startswith("["):
                    head = line.split("]", 1)[0]
                    if "/" in head:
                        try:
                            cur = int(head.lstrip("[").split("/", 1)[0])
                            self._last_done = cur
                        except ValueError:
                            pass
                self._push_log("out", line)
        except Exception as e:  # noqa: BLE001
            self._push_log("err", f"[读取子进程输出异常] {e}")
        finally:
            rc = proc.wait()
            self._last_exit_code = rc
            self._finished_at = time.time()
            self._push_log("done", f"子进程退出 returncode={rc}")
            with self._proc_lock:
                self._proc = None

    def _read_output_to_buffer(self, proc: subprocess.Popen, label: str) -> None:
        """通用 stdout 读取(不做进度解析,只推 log buffer)。
        用于登录子进程这种不需要进度跟踪的场景。
        """
        assert proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, ""):
                if not raw:
                    break
                self._push_log("out", raw.rstrip("\n"))
        except Exception as e:  # noqa: BLE001
            self._push_log("err", f"[{label} 子进程输出异常] {e}")
        finally:
            try:
                proc.wait()
            except Exception:  # noqa: BLE001
                pass

    def _watch_login_proc(self) -> None:
        """等登录子进程退出(用户手动关浏览器 / 崩溃 / SIGTERM),清理状态。"""
        proc = self._login_proc
        if proc is None:
            return
        try:
            rc = proc.wait()
            self._push_log("info", f"[登录] 登录浏览器已退出 (returncode={rc})")
        except Exception as e:  # noqa: BLE001
            self._push_log("err", f"[登录] 等子进程退出时异常: {e}")
        finally:
            with self._login_proc_lock:
                self._login_proc = None

    def _push_log(self, kind: str, msg: str) -> None:
        with self._log_lock:
            self._log_buffer.append((time.time(), kind, msg))


# ---------- HTTP 层 ----------


class CamblyServer(ThreadingHTTPServer):
    """带 api 引用的 HTTP server,handler 通过 self.server.api 访问后端。"""

    def __init__(self, addr: tuple[str, int], handler_cls: type, api: CamblyAPI) -> None:
        super().__init__(addr, handler_cls)
        self.api = api
        self.allow_reuse_address = True
        self.daemon_threads = True


class CamblyHandler(BaseHTTPRequestHandler):
    server_version = "CamblyGUI/1.0"

    # 静默 BaseHTTPRequestHandler 默认的 stderr access log
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # ---- helpers ----

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ----

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        api = self.server.api  # type: ignore[attr-defined]
        if path in ("/", "/index.html"):
            self._html(HTML.encode("utf-8"))
        elif path == "/api/default_output_dir":
            self._json({"path": api.default_output_dir()})
        elif path == "/api/state":
            self._json(api.get_state())
        elif path == "/api/logs":
            qs = parse_qs(url.query)
            try:
                since = float(qs.get("since", ["0"])[0] or "0")
            except ValueError:
                since = 0.0
            self._json(api.get_logs(since))
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        data = self._read_json()
        api = self.server.api  # type: ignore[attr-defined]
        if path == "/api/start_export":
            self._json(
                api.start_export(
                    data.get("urls", []),
                    data.get("out_dir", ""),
                    bool(data.get("login", False)),
                )
            )
        elif path == "/api/cancel_export":
            self._json(api.cancel_export())
        elif path == "/api/reveal":
            self._json(api.reveal(data.get("path", "")))
        elif path == "/api/login_open":
            self._json(api.start_login_window())
        elif path == "/api/login_close":
            self._json(api.close_login_window())
        else:
            self.send_error(404)


# ---------- 前端(内嵌) ----------


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cambly 课程导出</title>
<style>
  :root {
    --bg: #f7f7f8;
    --panel: #ffffff;
    --border: #e3e3e8;
    --text: #1f2328;
    --text-dim: #6e7681;
    --accent: #2e7d32;
    --accent-hover: #1b5e20;
    --danger: #c62828;
    --warn: #b26a00;
    --info: #1565c0;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", "Roboto Mono", Consolas, "PingFang SC", monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1a1b1e;
      --panel: #25272b;
      --border: #3a3d42;
      --text: #e6e6e6;
      --text-dim: #9aa0a6;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; min-height: 100%; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }
  body { padding: 16px; max-width: 900px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
  .sub { color: var(--text-dim); font-size: 12px; margin-bottom: 16px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
  .panel h2 { font-size: 12px; margin: 0 0 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; }
  .url-row { display: flex; gap: 6px; align-items: center; margin-bottom: 6px; }
  .url-row input { flex: 1; }
  .url-row .label { font-size: 12px; color: var(--text-dim); width: 18px; text-align: right; flex-shrink: 0; }
  input[type="text"] {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    font-size: 12px;
  }
  input[type="text"]:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: var(--accent); }
  input[type="text"].invalid { border-color: var(--danger); }
  .dir-row { display: flex; gap: 6px; }
  .dir-row input { flex: 1; }
  button {
    padding: 7px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel);
    color: var(--text);
    cursor: pointer;
    font-size: 13px;
    font-family: var(--sans);
    transition: background 0.1s;
    white-space: nowrap;
  }
  button:hover:not(:disabled) { background: var(--border); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.primary { background: var(--accent); color: white; border-color: var(--accent); font-weight: 600; }
  button.primary:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }
  button.danger { background: var(--danger); color: white; border-color: var(--danger); }
  .actions { display: flex; gap: 10px; margin-top: 12px; align-items: center; flex-wrap: wrap; }
  .status { font-size: 12px; color: var(--text-dim); margin-left: auto; display: flex; align-items: center; gap: 6px; }
  .status .badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-weight: 500; }
  .status .badge.idle    { background: var(--border); color: var(--text-dim); }
  .status .badge.running { background: #fff3cd; color: #856404; }
  .status .badge.done    { background: #d4edda; color: #155724; }
  .status .badge.error   { background: #f8d7da; color: #721c24; }
  @media (prefers-color-scheme: dark) {
    .status .badge.running { background: #3a2f00; color: #ffd966; }
    .status .badge.done    { background: #1b3a1f; color: #b5e7b8; }
    .status .badge.error   { background: #3a1a1a; color: #ffb3b3; }
  }
  #log {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.55;
    height: 320px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-line { display: block; }
  .log-info { color: #9cdcfe; }
  .log-out  { color: #d4d4d4; }
  .log-warn { color: #dcdcaa; }
  .log-err  { color: #f48771; }
  .log-done { color: #b5cea8; font-weight: 600; }
  .hint { color: var(--text-dim); font-size: 11px; margin-top: 4px; }
  .checkbox { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-dim); cursor: pointer; user-select: none; }
  .checkbox input { margin: 0; cursor: pointer; }
  .toast {
    position: fixed; top: 16px; right: 16px;
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 13px;
    background: var(--panel);
    border: 1px solid var(--border);
    box-shadow: 0 4px 16px rgba(0,0,0,0.12);
    opacity: 0; transform: translateY(-8px);
    transition: opacity 0.2s, transform 0.2s;
    pointer-events: none;
    max-width: 360px;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.error { border-color: var(--danger); color: var(--danger); }
  .toast.success { border-color: var(--accent); color: var(--accent); }
  .login-status { font-size: 12px; }
  .login-status.open { color: var(--accent); font-weight: 500; }
</style>
</head>
<body>
  <h1>Cambly 课程导出</h1>
  <div class="sub">粘贴最多 5 个课程 URL,选个输出目录,点开始。空槽会被忽略。</div>

  <div class="panel">
    <h2>课程链接</h2>
    <div id="urls">
      <div class="url-row"><span class="label">1</span><input type="text" data-idx="0" placeholder="https://www.cambly.com/en/student/progress/past-lesson?lessonV2Id=...&lang=zh_CN"></div>
      <div class="url-row"><span class="label">2</span><input type="text" data-idx="1" placeholder="(可选)"></div>
      <div class="url-row"><span class="label">3</span><input type="text" data-idx="2" placeholder="(可选)"></div>
      <div class="url-row"><span class="label">4</span><input type="text" data-idx="3" placeholder="(可选)"></div>
      <div class="url-row"><span class="label">5</span><input type="text" data-idx="4" placeholder="(可选)"></div>
    </div>
  </div>

  <div class="panel">
    <h2>下载路径(课程 md 落到这里)</h2>
    <div class="dir-row">
      <input type="text" id="outDir" />
      <button id="revealBtn" type="button" title="在文件管理器(Finder/Explorer)中打开">在文件中打开</button>
    </div>
    <div class="hint">不存在会自动创建。建议放在 iCloud / OneDrive 同步目录方便多端看。</div>
  </div>

  <div class="actions">
    <button class="primary" id="startBtn" type="button">开始导出</button>
    <button class="danger" id="cancelBtn" type="button" disabled>取消</button>
    <button id="loginOpenBtn" type="button" title="打开持久化登录窗口,登录/切换账号用">登录 / 切换账号</button>
    <button id="loginCloseBtn" type="button" disabled title="关闭登录窗口">关闭登录窗口</button>
    <span class="status"><span class="badge idle" id="statusBadge">空闲</span> <span id="elapsedInfo" style="font-variant-numeric: tabular-nums;"></span> · <span id="loginStatus" class="login-status">登录窗口: 未开</span></span>
  </div>

  <div class="panel" style="margin-top: 14px;">
    <h2>实时日志</h2>
    <div id="log"></div>
  </div>

  <div class="toast" id="toast"></div>

<script>
  const $ = (id) => document.getElementById(id);
  const statusBadge = $('statusBadge');
  const startBtn = $('startBtn');
  const cancelBtn = $('cancelBtn');
  const logEl = $('log');
  const outDirInput = $('outDir');
  const loginOpenBtn = $('loginOpenBtn');
  const loginCloseBtn = $('loginCloseBtn');
  const loginStatus = $('loginStatus');
  const elapsedInfo = $('elapsedInfo');
  const revealBtn = $('revealBtn');
  const toast = $('toast');

  let lastTs = 0;
  let pollTimer = null;
  let statePollTimer = null;
  let expectedTotal = 0;

  function setStatus(kind, text) {
    statusBadge.className = 'badge ' + kind;
    statusBadge.textContent = text;
  }

  function showToast(msg, kind) {
    toast.textContent = msg;
    toast.className = 'toast show' + (kind ? ' ' + kind : '');
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => { toast.className = 'toast'; }, 3000);
  }

  function appendLog(kind, msg) {
    const line = document.createElement('span');
    line.className = 'log-line log-' + kind;
    line.textContent = msg;
    logEl.appendChild(line);
    logEl.appendChild(document.createTextNode('\n'));
    logEl.scrollTop = logEl.scrollHeight;
  }

  function fmtElapsed(s) {
    if (s == null) return '';
    if (s < 60) return s.toFixed(1) + 's';
    const m = Math.floor(s / 60);
    const r = (s % 60).toFixed(0);
    return m + 'm' + r + 's';
  }

  async function pollLogs() {
    try {
      const r = await fetch('/api/logs?since=' + lastTs);
      const data = await r.json();
      for (const item of data.logs) {
        appendLog(item.kind, item.msg);
        if (item.ts > lastTs) lastTs = item.ts;
      }
    } catch (e) {
      // 静默,等下次轮询
    }
  }

  async function pollState() {
    try {
      const s = await (await fetch('/api/state')).json();
      elapsedInfo.textContent = s.elapsed != null ? '(' + fmtElapsed(s.elapsed) + ')' : '';

      // 登录窗口状态
      if (s.login_open) {
        loginStatus.textContent = '登录窗口: 已打开 (PID ' + s.login_pid + ')';
        loginStatus.className = 'login-status open';
        loginOpenBtn.disabled = true;
        loginCloseBtn.disabled = false;
        // 登录窗口占用 user_data_dir,导出要先关
        if (!s.running) {
          startBtn.disabled = true;
          startBtn.title = '请先关闭登录窗口再导出(避免 user_data_dir 冲突)';
        }
      } else {
        loginStatus.textContent = '登录窗口: 未开';
        loginStatus.className = 'login-status';
        loginOpenBtn.disabled = false;
        loginCloseBtn.disabled = true;
        if (!s.running) {
          startBtn.title = '';
        }
      }

      if (s.running) {
        setStatus('running', '运行中 (' + s.done + '/' + s.total + ')');
        startBtn.disabled = true;
        startBtn.title = s.login_open ? '请先关闭登录窗口' : '';
      } else {
        if (s.last_exit_code === 0) {
          setStatus('done', '完成 ✓');
        } else if (s.last_exit_code != null) {
          setStatus('error', '失败 (code=' + s.last_exit_code + ')');
        } else {
          setStatus('idle', '空闲');
        }
        if (cancelBtn.disabled === false) {
          cancelBtn.disabled = true;
          if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
          if (s.last_exit_code === 0) {
            showToast('导出完成', 'success');
          } else if (s.last_exit_code != null) {
            showToast('部分或全部失败,查看日志', 'error');
          }
        }
        // 收尾时再恢复 start(若登录窗口没开)
        if (!s.login_open) {
          startBtn.disabled = false;
        }
      }
    } catch (e) { /* 静默 */ }
  }

  function getUrls() {
    return Array.from(document.querySelectorAll('#urls input'))
      .map(i => i.value.trim())
      .filter(Boolean);
  }

  startBtn.addEventListener('click', async () => {
    const urls = getUrls();
    if (urls.length === 0) { showToast('至少填一个 URL', 'error'); return; }
    if (urls.length > 5) { showToast('最多 5 个,已截断', 'error'); urls.length = 5; }
    const outDir = outDirInput.value.trim();
    if (!outDir) { outDirInput.classList.add('invalid'); showToast('输出目录不能为空', 'error'); return; }
    outDirInput.classList.remove('invalid');
    expectedTotal = urls.length;
    startBtn.disabled = true;
    cancelBtn.disabled = false;
    setStatus('running', '运行中 (0/' + expectedTotal + ')');
    try {
      const r = await (await fetch('/api/start_export', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({urls, out_dir: outDir, login: false}),
      })).json();
      if (!r.ok) {
        showToast('[启动失败] ' + r.error, 'error');
        startBtn.disabled = false;
        cancelBtn.disabled = true;
        setStatus('error', '启动失败');
        return;
      }
      pollLogs();
    } catch (e) {
      showToast('[网络错误] ' + e, 'error');
      startBtn.disabled = false;
      cancelBtn.disabled = true;
    }
  });

  cancelBtn.addEventListener('click', async () => {
    try {
      const r = await (await fetch('/api/cancel_export', {method: 'POST'})).json();
      if (!r.ok) showToast('[取消] ' + r.error, 'error');
    } catch (e) { showToast('[网络错误] ' + e, 'error'); }
  });

  loginOpenBtn.addEventListener('click', async () => {
    loginOpenBtn.disabled = true;
    try {
      const r = await (await fetch('/api/login_open', {method: 'POST'})).json();
      if (!r.ok) {
        showToast('[打开失败] ' + r.error, 'error');
        loginOpenBtn.disabled = false;
      } else {
        showToast('登录窗口已打开 (PID ' + r.pid + '),登完点「关闭登录窗口」', 'success');
      }
    } catch (e) {
      showToast('[网络错误] ' + e, 'error');
      loginOpenBtn.disabled = false;
    }
  });

  loginCloseBtn.addEventListener('click', async () => {
    try {
      const r = await (await fetch('/api/login_close', {method: 'POST'})).json();
      if (!r.ok) showToast('[关闭失败] ' + r.error, 'error');
    } catch (e) { showToast('[网络错误] ' + e, 'error'); }
  });

  revealBtn.addEventListener('click', async () => {
    const p = outDirInput.value.trim();
    if (!p) { showToast('路径为空', 'error'); return; }
    try {
      const r = await (await fetch('/api/reveal', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: p}),
      })).json();
      if (!r.ok) showToast('[打开失败] ' + r.error, 'error');
    } catch (e) { showToast('[网络错误] ' + e, 'error'); }
  });

  // 启动
  (async () => {
    try {
      const r = await (await fetch('/api/default_output_dir')).json();
      outDirInput.value = r.path;
    } catch (e) { outDirInput.value = ''; }
    appendLog('info', 'Cambly GUI 就绪。');
    appendLog('info', '提示:首次使用或要切换账号,点「登录 / 切换账号」按钮,在弹出的浏览器里登完再点「关闭登录窗口」。');
    pollTimer = setInterval(pollLogs, 500);
    statePollTimer = setInterval(pollState, 700);
    pollState();
  })();
</script>
</body>
</html>
"""


# ---------- main ----------


def main() -> int:
    if not EXPORT_SCRIPT.exists():
        print(f"[fatal] 找不到 {EXPORT_SCRIPT}", file=sys.stderr)
        print(f"[fatal] 请在 {HERE} 目录运行,或确认 cambly_export.py 在同一目录", file=sys.stderr)
        return 2

    api = CamblyAPI()
    server = CamblyServer(("127.0.0.1", 0), CamblyHandler, api)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    print("=" * 60)
    print(f"[cambly-gui] 本地服务已启动: {url}")
    print(f"[cambly-gui] 1.5 秒后自动用默认浏览器打开...")
    print(f"[cambly-gui] 如果没自动打开,请手动访问上面的 URL")
    print(f"[cambly-gui] Ctrl+C 退出(同时会终止正在跑的导出子进程)")
    print("=" * 60)

    def open_browser() -> None:
        time.sleep(1.5)
        try:
            opened = webbrowser.open(url)
            if not opened:
                print("[cambly-gui] webbrowser.open() 返回 False,可能没找到默认浏览器", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[cambly-gui] 自动打开浏览器失败: {e}", file=sys.stderr)
            print(f"[cambly-gui] 请手动打开: {url}", file=sys.stderr)

    threading.Thread(target=open_browser, daemon=True).start()

    # SIGINT / SIGTERM 优雅关闭
    def handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        print(f"\n[cambly-gui] 收到信号 {signum},正在关闭...")
        api.shutdown()
        # 给 server.shutdown 一点时间完成当前请求
        threading.Thread(target=server.shutdown, daemon=True).start()

    if sys.platform != "win32":
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[cambly-gui] 收到 Ctrl+C,正在关闭...")
        api.shutdown()
    finally:
        server.server_close()
        print("[cambly-gui] 已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
