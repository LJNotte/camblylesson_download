#!/usr/bin/env python3
"""Cambly 课程导出 — 跨平台 GUI 包装。

启动:
    python3 cambly_gui.py

依赖(同 requirements.txt):
    pip install -r requirements.txt
    python3 -m playwright install chromium   # 首次

实现要点:
- 用 pywebview 起一个原生窗口(macOS WKWebView / Windows WebView2 / Linux WebKit2GTK)
- 把 cambly_export.py 当子进程跑,实时流 stdout 到 GUI 日志区
- 前端是内嵌的 HTML/CSS/JS,5 个 URL 槽 + 1 个下载路径 + 日志面板
- 子进程跑完会更新状态徽章,失败/成功都看得到
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import webview
except ImportError:
    print(
        "[fatal] 缺少 pywebview。安装:\n"
        "    pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(2)

HERE = Path(__file__).parent.resolve()
EXPORT_SCRIPT = HERE / "cambly_export.py"
DEFAULT_OUT = Path.home() / "Documents" / "CamblyNotes"


class CamblyAPI:
    """暴露给前端 JS 的 API。pywebview 会从 worker 线程调这些方法。"""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._poll_thread: threading.Thread | None = None
        self._last_exit_code: int | None = None
        self._last_total: int = 0
        self._last_done: int = 0

    # ---------- 窗口绑定 ----------

    def bind_window(self, window: "webview.Window") -> None:
        self._window = window
        # 启动日志轮询线程(单例)
        self._poll_thread = threading.Thread(target=self._poll_logs, daemon=True)
        self._poll_thread.start()

    # ---------- 给 JS 用的同步 API ----------

    def default_output_dir(self) -> str:
        return str(DEFAULT_OUT)

    def select_folder(self, current: str = "") -> str:
        """弹出原生文件夹选择对话框。返回选中的绝对路径,空串 = 取消。"""
        if not self._window:
            return ""
        try:
            result = self._window.create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=str(current) if current else str(Path.home()),
            )
        except Exception as e:  # noqa: BLE001
            self._log_queue.put(("err", f"[选目录异常] {e}"))
            return ""
        if not result:
            return ""
        # pywebview 4+: result 是 str;旧版/多选是 tuple
        if isinstance(result, tuple):
            return str(result[0]) if result else ""
        return str(result)

    def get_state(self) -> dict[str, Any]:
        """给前端轮询用。"""
        with self._proc_lock:
            running = self._proc is not None and self._proc.poll() is None
        return {
            "running": running,
            "last_exit_code": self._last_exit_code,
            "total": self._last_total,
            "done": self._last_done,
        }

    def start_export(self, urls: list[str], out_dir: str, login: bool = False) -> dict[str, Any]:
        """启动子进程跑导出。空 URL 槽会被过滤。返回 {ok, pid?, error?}。"""
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

        # 强制子进程无缓冲,实时看到日志
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [sys.executable, "-u", str(EXPORT_SCRIPT), *urls, "--out", str(out_path)]
        if login:
            cmd.append("--login")

        # 重置状态
        self._last_exit_code = None
        self._last_total = len(urls)
        self._last_done = 0

        self._log_queue.put(("info", f"启动子进程(共 {len(urls)} 条)"))
        self._log_queue.put(("info", f"输出目录: {out_path}"))
        if login:
            self._log_queue.put(("warn", "登录模式:会弹浏览器,登完回终端按 Enter,不要在这里操作"))
        self._log_queue.put(("info", "---"))

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
            if self._proc is None or self._proc.poll() is not None:
                return {"ok": False, "error": "没有任务在跑"}
            try:
                self._proc.terminate()
                self._log_queue.put(("warn", "[用户取消] 正在终止子进程..."))
                return {"ok": True}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)}

    # ---------- 内部:子进程输出读取 ----------

    def _read_output(self, proc: subprocess.Popen) -> None:
        """在线程里读子进程输出,推到 queue。同时按行解析进度。"""
        assert proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, ""):
                if not raw:
                    break
                line = raw.rstrip("\n")
                # 检测 "[i/total]" 进度行,更新 done 计数
                # 形如: "[1/3] https://..."
                if line.startswith("[") and "/" in line.split("]", 1)[0] + "]":
                    head = line.split("]", 1)[0]  # "[1/3"
                    if "/" in head:
                        try:
                            cur = int(head.lstrip("[").split("/", 1)[0])
                            self._last_done = cur
                        except ValueError:
                            pass
                self._log_queue.put(("out", line))
        except Exception as e:  # noqa: BLE001
            self._log_queue.put(("err", f"[读取子进程输出异常] {e}"))
        finally:
            rc = proc.wait()
            self._last_exit_code = rc
            self._log_queue.put(("done", f"子进程退出 returncode={rc}"))
            with self._proc_lock:
                self._proc = None

    # ---------- 内部:日志推送到 JS ----------

    def _poll_logs(self) -> None:
        """守护线程:把 queue 里的日志/状态事件推给前端。"""
        while True:
            try:
                kind, msg = self._log_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._window:
                continue
            try:
                if kind == "done":
                    self._window.evaluate_js(
                        f"window.__onProcessDone({json.dumps(msg)});"
                    )
                else:
                    self._window.evaluate_js(
                        f"window.__appendLog({json.dumps(kind)}, {json.dumps(msg)});"
                    )
            except Exception:  # noqa: BLE001
                # 窗口可能已关闭
                pass


# ---------- 内嵌前端 ----------

HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
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
  html, body { margin: 0; padding: 0; height: 100%; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }
  body { padding: 16px; overflow: auto; }
  h1 { font-size: 18px; margin: 0 0 4px; font-weight: 600; }
  .sub { color: var(--text-dim); font-size: 12px; margin-bottom: 14px; }
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
  .status .badge.idle { background: var(--border); color: var(--text-dim); }
  .status .badge.running { background: #fff3cd; color: #856404; }
  .status .badge.done { background: #d4edda; color: #155724; }
  .status .badge.error { background: #f8d7da; color: #721c24; }
  @media (prefers-color-scheme: dark) {
    .status .badge.running { background: #3a2f00; color: #ffd966; }
    .status .badge.done { background: #1b3a1f; color: #b5e7b8; }
    .status .badge.error { background: #3a1a1a; color: #ffb3b3; }
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
    height: 280px;
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
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
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
      <button id="pickDir" type="button">选择...</button>
    </div>
    <div class="hint">不存在会自动创建。建议放在 iCloud / OneDrive 同步目录方便多端看。</div>
  </div>

  <div class="actions">
    <button class="primary" id="startBtn" type="button">开始导出</button>
    <button class="danger" id="cancelBtn" type="button" disabled>取消</button>
    <label class="checkbox"><input type="checkbox" id="loginMode">首次登录模式(弹浏览器手动登录)</label>
    <span class="status"><span class="badge idle" id="statusBadge">空闲</span></span>
  </div>

  <div class="panel" style="margin-top: 14px;">
    <h2>实时日志</h2>
    <div id="log"></div>
  </div>

<script>
  const $ = (id) => document.getElementById(id);
  const statusBadge = $('statusBadge');
  const startBtn = $('startBtn');
  const cancelBtn = $('cancelBtn');
  const logEl = $('log');
  const outDirInput = $('outDir');
  const loginMode = $('loginMode');

  let pollTimer = null;
  let expectedTotal = 0;

  function setStatus(kind, text) {
    statusBadge.className = 'badge ' + kind;
    statusBadge.textContent = text;
  }

  function appendLog(kind, msg) {
    const line = document.createElement('span');
    line.className = 'log-line log-' + kind;
    line.textContent = msg;
    logEl.appendChild(line);
    logEl.appendChild(document.createTextNode('\n'));
    logEl.scrollTop = logEl.scrollHeight;
  }
  window.__appendLog = appendLog;

  window.__onProcessDone = function (msg) {
    // 由 Python 端在子进程退出时调用
    cancelBtn.disabled = true;
    startBtn.disabled = false;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    // 稍等一拍让 is_running 状态稳定
    setTimeout(async () => {
      const s = await window.pywebview.api.get_state();
      if (s.last_exit_code === 0) {
        setStatus('done', '完成 ✓');
        appendLog('done', '全部完成');
      } else {
        setStatus('error', '失败 (code=' + s.last_exit_code + ')');
        appendLog('err', '部分或全部失败,查看上方日志');
      }
    }, 300);
  };

  function getUrls() {
    return Array.from(document.querySelectorAll('#urls input')).map(i => i.value.trim()).filter(Boolean);
  }

  $('pickDir').addEventListener('click', async () => {
    const cur = outDirInput.value;
    const picked = await window.pywebview.api.select_folder(cur);
    if (picked) outDirInput.value = picked;
  });

  startBtn.addEventListener('click', async () => {
    const urls = getUrls();
    if (urls.length === 0) {
      appendLog('warn', '[提示] 至少填一个 URL');
      return;
    }
    if (urls.length > 5) {
      appendLog('warn', '[提示] 最多 5 个,已截断');
      urls.length = 5;
    }
    const outDir = outDirInput.value.trim();
    const login = loginMode.checked;
    expectedTotal = urls.length;
    startBtn.disabled = true;
    cancelBtn.disabled = false;
    setStatus('running', `运行中 (0/${expectedTotal})`);
    const r = await window.pywebview.api.start_export(urls, outDir, login);
    if (!r.ok) {
      appendLog('err', '[启动失败] ' + r.error);
      startBtn.disabled = false;
      cancelBtn.disabled = true;
      setStatus('error', '启动失败');
      return;
    }
    appendLog('info', '子进程已启动,pid=' + r.pid);
    // 启动状态轮询,更新 (done/total) 显示
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const s = await window.pywebview.api.get_state();
      if (s.running) {
        setStatus('running', `运行中 (${s.done}/${s.total})`);
      }
    }, 800);
  });

  cancelBtn.addEventListener('click', async () => {
    const r = await window.pywebview.api.cancel_export();
    if (!r.ok) appendLog('warn', '[取消] ' + r.error);
  });

  // 启动时加载默认输出目录
  window.addEventListener('pywebviewready', async () => {
    outDirInput.value = await window.pywebview.api.default_output_dir();
    appendLog('info', 'Cambly GUI 就绪。');
    appendLog('info', '提示:首次使用请勾选"登录模式",或先在终端跑一次 --login。');
  });
</script>
</body>
</html>
"""


def main() -> int:
    if not EXPORT_SCRIPT.exists():
        print(f"[fatal] 找不到 {EXPORT_SCRIPT}", file=sys.stderr)
        return 2

    api = CamblyAPI()
    window = webview.create_window(
        title="Cambly 课程导出",
        html=HTML,
        width=780,
        height=740,
        resizable=True,
        js_api=api,
    )
    api.bind_window(window)
    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
