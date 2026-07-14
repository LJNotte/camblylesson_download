# Cambly GUI Demo 视频

`cambly_gui.py` 的演示视频,展示完整使用流程。

## 文件清单

| 文件 | 用途 |
|------|------|
| `video/demo.webm` | 录屏产物(WebM 格式,~30 秒,~1.6 MB) |
| `demo_final_state.png` | demo 结束时的静态截图(快速预览) |
| `demo.html` | demo 的 HTML 源(假数据 + 自动播放脚本) |
| `record.py` | 录屏脚本(Playwright headless Chromium) |

## 隐私设计

- **全程假数据**:URL 用 `DEMO-A1B2C3D4` 这种明显假 ID,实际 `lessonV2Id` 不会泄露
- **路径用 `/Users/demo/...`**:不出现真实用户名
- **录的是 demo HTML,不是真桌面**:`file://` 加载本地自包含 HTML,无浏览器标签页、无 cookie、无真实历史
- **录屏在 headless 浏览器里完成**:`playwright.chromium.launch(headless=True)`,你屏幕上的 Chrome/Safari 不会被录到

页面顶部有黄色 banner 明确标注「这是 demo 演示,所有 URL / 路径 / 日志都是假的」。

## 怎么播放 webm

- **Chrome / Firefox / Safari 14+**:`file://.../demo/video/demo.webm` 直接拖进浏览器
- **macOS QuickTime**:webm 不原生支持,需要装 ffmpeg 或用 IINA / VLC
- **转 mp4**:`brew install ffmpeg` 后 `ffmpeg -i video/demo.webm -c:v libx264 video/demo.mp4`

## 怎么重新录

```bash
# 1. 确保 playwright + chromium 装好
pip install playwright
python3 -m playwright install chromium

# 2. 跑录屏
python3 demo/record.py
# → 生成 demo/video/demo.webm
```

录屏时长由 `record.py` 里 `DEMO_DURATION_S = 35` 控制(略大于 demo 自身 ~30s,留 buffer)。
想改 demo 内容直接编辑 `demo.html`(改 fake 数据 / 改延时 / 改文案)。
