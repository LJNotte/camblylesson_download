# Cambly Lesson Exporter

把 Cambly 网页版已结束课程(past-lesson)导出为结构化 Markdown:
**课程总结 + AI 反馈 + 语音转文字 + 课堂聊天 + 课件**,落到 `Tutor-Date-Duration` 自动命名的文件夹里。

> **CLI-only 工具**,无 GUI,一次可传多个 URL 串行导出。

---

## 1. 安装(一次性)

需要 Python 3.9+。

```bash
cd /Users/llazuli/Documents/MiniMax/Cambly
pip3 install --user -r requirements.txt
python3 -m playwright install chromium
```

> 第一次跑 `--login` 时,工具会用 Playwright 自带的 Chromium 弹窗,
> 不会动你日常 Chrome 的登录态。Cookie 缓存在 `~/.cambly_export/chrome-profile/`。

---

## 2. 第一次跑:登录 Cambly

Cambly 整站都要登录,先手动登一次让 cookie 落盘:

```bash
python3 cambly_export.py "https://www.cambly.com/en/student/progress/past-lesson?lessonV2Id=你的lessonV2Id&lang=zh_CN" --login
```

流程:弹 Chromium 窗口 → 手动登录 → 看到课程页 → 回终端按 Enter。

之后所有导出都不用再登(cookie 失效时再跑一次 `--login`)。

---

## 3. 日常使用

### 3.1 导出单条课程

```bash
python3 cambly_export.py "<cambly课程url>" --out ~/Documents/CamblyNotes
```

`<cambly课程url>` 形如:
```
https://www.cambly.com/en/student/progress/past-lesson?lessonV2Id=6a43e8da323f6f28ee64d728&lang=zh_CN
```

> `lessonV2Id` 在 Cambly 网页版 past-lesson 页面 URL 里。
> 不带 `--out` 默认输出到当前目录。

### 3.2 批量导出(串行)

```bash
python3 cambly_export.py \
    "<url1>" "<url2>" "<url3>" \
    --out ~/Documents/CamblyNotes
```

每条独立文件夹,前一条跑完再跑下一条(每条约 30-60s,主要等 Cambly AI 反馈异步生成)。

### 3.3 运行时的输出

```
============================================================
[1/3] https://www.cambly.com/.../past-lesson?lessonV2Id=xxxx
============================================================
[info] 正在打开课程页: https://www.cambly.com/...
[scrape] 正在浏览器中跑结构化提取...
[scrape] feedback: ready=True, 4 categories
[scrape] transcript: 559 fragments → 542 turns
[scrape] chat: speaker='Leon', links=2
[scrape] slides: unavailable=False
   → 外教: Dennis D, 日期: July 1st, 2026, 时长: 60 分钟
   ✓ 已保存 → /Users/.../CamblyNotes/Dennis D-July 1st, 2026-60 minutes/Dennis D-July 1st, 2026-60 minutes.md

============================================================
[done] 全部 3 条导出完成。输出目录: /Users/llazuli/Documents/CamblyNotes
============================================================
```

---

## 4. 全部参数

| 参数 | 作用 |
|------|------|
| `urls` (位置参数,必填,可多个) | Cambly 课程 URL,空格分隔 |
| `--login` | 弹浏览器手动登录(首次或 cookie 失效时) |
| `--out <dir>` | 输出目录(默认当前目录) |
| `--debug-html <file>` | 把抓到的页面 HTML 落盘(只对第 1 个 URL 生效,排查用) |
| `--screenshots <dir>` | 把每个 tab 截图落盘(同上,只对第 1 个 URL) |

```bash
# 出问题时排查:把 DOM 和每个 tab 的截图都存下来
python3 cambly_export.py "<url>" --debug-html ./debug.html --screenshots ./shots

# 走完一次登录流程(用同一份 cookie)
python3 cambly_export.py "<url>" --login --out ~/Documents/CamblyNotes
```

---

## 5. 输出结构

```
<--out 指定的目录>/
  Dennis D-July 1st, 2026-60 minutes/
    Dennis D-July 1st, 2026-60 minutes.md
  Sara K-June 28th, 2026-30 minutes/
    Sara K-June 28th, 2026-30 minutes.md
  ...
```

文件夹 / 文件名都来自 feedback tab 的「From Cambly」上方的教师信息:
**外教姓名 - 课程日期 - 时长**。三者任一缺失会回退到 `Unknown XXX`。

每份 `.md` 包含 5 个 section:
1. 元信息(Lesson ID、URL、外教、日期、时长、Speaking%/WPM/Unique words)
2. **课程总结 · AI 反馈**(From Cambly,按 Grammar/Vocabulary/Pronunciation/... 分类)
3. **语音转文字(Transcript)** — 559 个 ASR 片段合并为 ~540 个 turn,按 speaker 标注
4. **课堂聊天(Chat)** — 课内聊天窗的文本 + 链接
5. **课件(Slides)** — 课件文本(无课件则标注「未使用」)

---

## 6. 常见问题

**Q: 跑出来 feedback 是空的,markdown 里提示「反馈面板还未生成完成」。**
A: Cambly AI 反馈是异步生成的。脚本最多等 30s。再跑一次通常就有了。

**Q: 提示「未登录(cookie 失效)」。**
A: cookie 过期了。重跑一次 `--login` 重新登录。

**Q: transcript 数字跟我看到的不太一样。**
A: 默认把 559 个 ASR 片段按「相邻同 speaker」合并到 ~540 个 turn。
没做"按停顿重新对齐",那是另一个活儿,需要的话告诉我。

**Q: 能不能并行跑(快一点)?**
A: 现在是串行(一条完成再下一条),因为 Playwright 单浏览器实例 + cookie 共享。
真要并行需要每个 worker 独立 user-data-dir,改动不小,先不做。

**Q: 能用系统的 Chrome 吗?**
A: 现在固定用 Playwright 自带的 Chromium(`python3 -m playwright install chromium` 装的)。
不依赖系统 Chrome,跨平台一致。

**Q: Cambly 改版后工具坏了怎么办?**
A: 没有公开 API,全靠 Playwright 模拟浏览器 + DOM 解析。
改版后用 `--debug-html` 和 `--screenshots` 把当前页面结构捞出来,再调 `cambly_export.py` 里的 `JS_FEEDBACK / JS_TRANSCRIPT / JS_CHAT / JS_SLIDES`。

---

## 7. 已知限制

- **CLI-only**,无 GUI(个人偏好,见 agent memory)
- **每条约 30-60s**,瓶颈是 Cambly AI 反馈异步生成(等 30s) + 页面渲染
- **单浏览器实例**,串行跑多条
- **依赖前端 DOM 结构**,Cambly 改版会失效
- **transcript 合并粒度**:同 speaker 相邻片段合并,不识别"说话停顿"

---

## 8. 项目结构

```
Cambly/
├── cambly_export.py     # CLI 工具 + 结构化提取 + markdown 渲染
├── requirements.txt     # playwright
└── README.md            # 本文件
```
