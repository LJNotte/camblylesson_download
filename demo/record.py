#!/usr/bin/env python3
"""录制 cambly_gui demo 视频。

完全用假数据:加载 demo.html(自包含,无后端),
Playwright 录屏到 webm。约 30 秒。
"""
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent.resolve()
HTML_PATH = HERE / "demo.html"
OUTPUT_DIR = HERE / "video"
OUTPUT_DIR.mkdir(exist_ok=True)

VIDEO_PATH = OUTPUT_DIR / "demo.webm"

# 等待 demo 走完的时间(略大于 demo 自身 ~30s,留 buffer)
DEMO_DURATION_S = 35


def main() -> int:
    if not HTML_PATH.exists():
        print(f"[fatal] 找不到 {HTML_PATH}", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 900, "height": 800},
            record_video_dir=str(OUTPUT_DIR),
            record_video_size={"width": 900, "height": 800},
        )
        page = context.new_page()
        # 在导航前重命名 video 文件(Playwright 生成的临时名,后改)
        page.goto(f"file://{HTML_PATH}")
        # 跑完 demo
        time.sleep(DEMO_DURATION_S)
        # 关页面,video 才会 finalize
        page.close()
        context.close()
        browser.close()

    # Playwright 把录的视频放到 OUTPUT_DIR,文件名形如 <uuid>.webm
    # 找最新的那个,改名
    webm_files = sorted(OUTPUT_DIR.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not webm_files:
        print("[fatal] 没找到 webm 输出", file=sys.stderr)
        return 1
    latest = webm_files[-1]
    if latest.resolve() != VIDEO_PATH.resolve():
        latest.rename(VIDEO_PATH)
    print(f"[ok] 视频已保存: {VIDEO_PATH}")
    print(f"     大小: {VIDEO_PATH.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
