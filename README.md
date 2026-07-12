# Cambly 课程工作流

两件套,把 Cambly 课程从"网页里翻不到"变成"Obsidian 里可复习":

| 工具 | 作用 | 形态 |
|------|------|------|
| **`cambly_export.py`** | 把 Cambly 网页版 past-lesson 导出为结构化 Markdown(元信息 + AI 反馈 + Transcript + Chat + Slides) | CLI 脚本 |
| **`cambly_gui.py`** | `cambly_export.py` 的 GUI 外壳,粘贴 1-5 个 URL + 选下载路径,实时看日志 | 跨平台桌面窗口(pywebview) |
| **`cambly-review` skill** | 对导出的 Markdown 做 5 维结构化 review(词汇 / 方法论 / 注意事项 / 纠错 / 句式),产出 `*-review.md` 配合 Obsidian 使用 | Mavis / Claude Code / Codex skill |

**典型工作流**:
```
Cambly 网页 ──[cambly_export.py]──> 单节课 .md ──[cambly-review]──> 复习笔记 .md
```

---

# 一、`cambly_export.py` — 导出工具

把 Cambly 网页版已结束课程(past-lesson)导出为结构化 Markdown,
落到 `Tutor-Date-Duration` 自动命名的文件夹里。

> **CLI 工具 + 可选 GUI 外壳**(`cambly_gui.py`),一次可传多个 URL 串行导出。
> 想要可视化界面就装 `pywebview` 跑 `cambly_gui.py`;只想要命令行也行,不影响。

## 1. 安装(一次性)

需要 Python 3.9+。

```bash
cd /Users/llazuli/Documents/MiniMax/Cambly
pip3 install --user -r requirements.txt
python3 -m playwright install chromium
```

> 第一次跑 `--login` 时,工具会用 Playwright 自带的 Chromium 弹窗,
> 不会动你日常 Chrome 的登录态。Cookie 缓存在 `~/.cambly_export/chrome-profile/`。

## 2. 第一次跑:登录 Cambly

Cambly 整站都要登录,先手动登一次让 cookie 落盘:

```bash
python3 cambly_export.py "https://www.cambly.com/en/student/progress/past-lesson?lessonV2Id=你的lessonV2Id&lang=zh_CN" --login
```

流程:弹 Chromium 窗口 → 手动登录 → 看到课程页 → 回终端按 Enter。

之后所有导出都不用再登(cookie 失效时再跑一次 `--login`)。

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

# 二、`cambly_gui.py` — GUI 外壳(可选)

> 把 `cambly_export.py` 包了一层,可视化填 URL + 选下载路径 + 实时看日志。
> **CLI 完全不受影响**,不装 `pywebview` 也能用 `cambly_export.py`。

## 1. 安装 GUI 依赖(一次性)

GUI 需要 `pywebview`,在已装好 `cambly_export.py` 依赖的基础上加一行:

```bash
pip install -r requirements.txt
```

依赖差异:
- **macOS**:用系统 WKWebView,需要 `pyobjc`。**系统自带 Python 3.9 装不上**,
  请用 [python.org Python 3.12+](https://www.python.org/downloads/macos/) 或 Homebrew Python。
  这是 macOS 上所有原生 GUI 都绕不开的老问题(同 Tk)。
- **Windows**:用 WebView2(Win10 1903+ / Win11 自带),`pywebview` 在 PyPI 有现成 wheel,
  `pip install` 直接成功。
- **Linux**:用 WebKit2GTK,需要系统包 `python3-gi gir1.2-webkit2-4.0` 等。

> **不强求装 GUI**。你只是想可视化导出这一步,不需要 GUI 的话,继续用 CLI 即可。

## 2. 启动

```bash
python3 cambly_gui.py
```

会弹出一个原生窗口:

```
┌────────────────────────────────────────┐
│  Cambly 课程导出                        │
├────────────────────────────────────────┤
│  课程链接                                │
│  1 [https://www.cambly.com/...past-...] │
│  2 [(可选)]                              │
│  3 [(可选)]                              │
│  4 [(可选)]                              │
│  5 [(可选)]                              │
├────────────────────────────────────────┤
│  下载路径                                │
│  [/Users/.../Documents/CamblyNotes] [选择]│
├────────────────────────────────────────┤
│  [开始导出] [取消]  ☐ 首次登录模式       │
│                            状态: 空闲    │
├────────────────────────────────────────┤
│  实时日志                                │
│  ┌────────────────────────────────────┐ │
│  │ 启动子进程(共 2 条)                  │ │
│  │ 输出目录: /Users/.../CamblyNotes   │ │
│  │ ---                                │ │
│  │ [1/2] https://www.cambly.com/...   │ │
│  │ → 外教: Dennis D, 日期: ...        │ │
│  │ ✓ 已保存 → /Users/.../.../xxx.md  │ │
│  │ ...                                │ │
│  └────────────────────────────────────┘ │
└────────────────────────────────────────┘
```

## 3. 操作流程

1. 粘贴 1-5 个课程 URL(空槽自动忽略)
2. 选个下载路径(默认 `~/Documents/CamblyNotes`,不存在会自动创建)
3. 第一次用?勾上「首次登录模式」再点「开始导出」,会弹浏览器让你登一次
4. 不勾登录模式时,直接复用 `~/.cambly_export/chrome-profile/` 里已有的 cookie
5. 跑的时候状态徽章会从「空闲」→「运行中 (1/3)」→「完成 ✓」,日志区实时刷新
6. 跑一半想停?点「取消」,子进程会被 `terminate`

> 跟 CLI 完全等价:底层就是 `python3 cambly_export.py URL1 URL2 ... --out <路径>`。
> 你也可以先在终端 `--login` 一次,再回 GUI 跑,cookie 是共享的。

## 4. 实现要点

- `pywebview` 起原生窗口(macOS WKWebView / Windows WebView2 / Linux WebKit2GTK)
- 把 `cambly_export.py` 当子进程跑,`PYTHONUNBUFFERED=1` 保证日志实时流到 GUI
- 5 个 URL 槽是固定 HTML 元素,空字符串被过滤,超过 5 个会自动截断并提示
- 输出目录不存在会自动 `mkdir -p`,失败会立刻报错
- 前端用暗色/亮色自适应(`prefers-color-scheme`),跟系统主题走
- 状态徽章 4 种:idle / running(done/total) / done / error

---

# 三、`cambly-review` skill — 复习笔记生成器

**前提**:你已经在用 [Mavis / Claude Code / Codex](https://github.com)
或其他支持 skill 加载的 agent 客户端。

对 `cambly_export.py` 导出的单节课 Markdown 做 5 维结构化复习笔记,
在同目录产出 `<原文件名>-review.md`,可直接放进 Obsidian vault 复习。

## 1. 安装(分享给对方后)

skill 文件夹结构:
```
cambly-review/
└── SKILL.md
```

对方把整个 `cambly-review/` 放到自己客户端的 skill 目录即可,具体路径看客户端:
- Mavis: `~/.mavis/skills/cambly-review/`
- Claude Code: `~/.claude/skills/cambly-review/`
- Codex / OpenCode: 各自的 `skills/` 目录

> skill 本身**无外部依赖**,纯文本规则 + LLM 推理,不需要 pip install 任何东西。

## 2. 触发方式

在 agent 客户端对话里,用以下任一关键词触发:

| 关键词 | 场景 |
|--------|------|
| `review`、`梳理`、`整理` | 给一节 / 一堆课做 review |
| `/ob-secure + 课程分析` | 显式要求按 ob-secure 五规则产出 |
| `cambly-review` | 直接念 skill 名 |
| `把这堆课都过一遍` | 文件夹批量模式 |

英文 trigger 同理:`review my Cambly lesson`、`review 整个文件夹`。

## 3. 两种使用模式

### 3.1 单文件模式

直接给一节课的路径:

```
帮我 review 一下这节课
/Users/llazuli/Documents/CamblyNotes/Dennis D-July 1st, 2026-60 minutes/Dennis D-July 1st, 2026-60 minutes.md
```

产出:`<同目录>/Dennis D-July 1st, 2026-60 minutes-review.md`,通常 250-450 行。

### 3.2 文件夹增量模式(推荐)

传一个**外层大文件夹**(里面是一堆 `<外教>-<日期>-<N> minutes/` 子文件夹,
每节课一个):

```
把这堆课都过一遍
/Users/llazuli/Documents/CamblyNotes/
```

skill 会:
1. 扫一层子文件夹
2. **跳过已经有 `*-review.md` 的子文件夹**(增量,不覆盖)
3. 列出"已跳过 X / 待处理 Y"清单
4. 1-2 节直接做;≥ 3 节先跟你确认一句
5. 每节独立 review(不合并成一个文件),按时间倒序输出

> 想重做某节课的 review?明确说"重做 / redo",skill 会覆盖并在 final reply 顶部红字提示。

## 4. 产出格式(每节 review 的内部结构)

每份 `<原文件名>-review.md` 严格按 ob-secure 五规则,10 个固定 section:

1. **frontmatter** — `date / tutor / duration / date_of_lesson / topic` 5 个字段
2. **目录** — Obsidian 锚点链接
3. **概念总览** — 5 行表格,维度 ↔ 核心要点 ↔ 预期效果
4. **一、重点词汇拓展** — 词 / 释义 / 词性 / 用法 / 例句;≥ 5 个词时额外加"同义辨析"
5. **二、思维方法论** — 外教反复用的结构 / 模板 / 比喻 / 口诀
6. **三、注意事项** — Callout `[!danger]` / `[!warning]` / `[!tip]`,外教说"don't..." 的雷区
7. **四、表达不严谨之处** — 3 个子小节:Cambly AI 标注 / 外教当场纠正 / 自查发现(自查必列)
8. **五、可复用句式 / 模板** — 完整句子,标"面试 / 日常 / 商务"用途
9. **勘误与版本记录** — `~~删除线~~` 标记对原笔记的修正(正文不删任何内容)
10. **Agent 可读总结** — SKILL / DOMAIN / TRIGGER_WHEN / STANCE + 优先级表 + 决策点 + 局限

风格:中文为主(用户工作语言),英文例句和模板保留原文;每节至少 1 个 Callout;表格优于段落。

## 5. 完整示例

### 单节 review

```
Dennis D-July 1st, 2026-60 minutes/
  ├── Dennis D-July 1st, 2026-60 minutes.md         (cambly_export.py 产物)
  └── Dennis D-July 1st, 2026-60 minutes-review.md  (cambly-review 产物,新增)
```

### 批量 review(增量)

```
CamblyNotes/
  ├── Schalk-June 5th, 2026-30 minutes/
  │     ├── Schalk-June 5th, 2026-30 minutes.md
  │     └── Schalk-June 5th, 2026-30 minutes-review.md       ← 已存在,跳过
  ├── Schalk-June 26th, 2026-30 minutes/                     ← 待处理
  ├── Dennis D-July 1st, 2026-60 minutes/                    ← 待处理
  ├── Mia-June 18th, 2026-30 minutes/                        ← 待处理
  └── Tom-July 3rd, 2026-45 minutes/                         ← 待处理
```

skill 跑完后:
- 1 份已跳过(Schalk June 5th)
- 4 份新增 review(各自独立)
- final reply 顶部列出"已跳过 1 / 本次新增 4"

## 6. skill 边界(什么时候不该用)

- ❌ **通用 markdown 编辑 / 翻译 / 初学者词汇表** — 用通用 skill
- ❌ **TOEFL / IELTS 标准化备考** — 用专门的备考 skill
- ❌ **课堂外聊天记录** — 没结构化教学内容
- ❌ **短对话** — 没结构化教学内容
- ❌ **用户自写的中文笔记**(不是 cambly 导出格式) — skill 识别不到 Lesson ID 段,会拒绝

---

# 四、常见问题

**Q: 跑出来 feedback 是空的,markdown 里提示「反馈面板还未生成完成」。**
A: Cambly AI 反馈是异步生成的。脚本最多等 30s。再跑一次通常就有了。

**Q: 提示「未登录(cookie 失效)」。**
A: cookie 过期了。重跑一次 `--login` 重新登录。

**Q: transcript 数字跟我看到的不太一样。**
A: 默认把 559 个 ASR 片段按「相邻同 speaker」合并到 ~540 个 turn。
没做"按停顿重新对齐",那是另一个活儿,需要的话告诉我。

**Q: cambly-review 跟 cambly_export.py 必须配对用吗?**
A: 不必须。`cambly_export.py` 是导出,`cambly-review` 是复习笔记生成器。
你完全可以只跑导出,或者手动写笔记后用 `cambly-review` 整理。

**Q: 能不能并行跑(快一点)?**
A: 现在是串行(一条完成再下一条),因为 Playwright 单浏览器实例 + cookie 共享。
真要并行需要每个 worker 独立 user-data-dir,改动不小,先不做。

**Q: 能用系统的 Chrome 吗?**
A: 现在固定用 Playwright 自带的 Chromium(`python3 -m playwright install chromium` 装的)。
不依赖系统 Chrome,跨平台一致。

**Q: Cambly 改版后工具坏了怎么办?**
A: 没有公开 API,全靠 Playwright 模拟浏览器 + DOM 解析。
改版后用 `--debug-html` 和 `--screenshots` 把当前页面结构捞出来,再调 `cambly_export.py` 里的 `JS_FEEDBACK / JS_TRANSCRIPT / JS_CHAT / JS_SLIDES`。

**Q: cambly-review 跑出来后,词汇/方法论太少,显得内容单薄。**
A: 自由聊天课词汇密度本来就低(可能只有 1-2 个生词),不要硬凑。
skill 的"Agent 可读总结"里会标注"本节课偏自由聊天,词汇密度低"。

---

# 五、已知限制

## `cambly_export.py`
- **每条约 30-60s**,瓶颈是 Cambly AI 反馈异步生成(等 30s) + 页面渲染
- **单浏览器实例**,串行跑多条
- **依赖前端 DOM 结构**,Cambly 改版会失效
- **transcript 合并粒度**:同 speaker 相邻片段合并,不识别"说话停顿"

## `cambly_gui.py`
- **macOS 上需要 Python 3.12+**(pyobjc 编译问题,跟 Tk 8.5 同一类坑)
- **Windows 上需要 Win10 1903+ / Win11**(用系统 WebView2)
- **Linux 需要系统包** `python3-gi gir1.2-webkit2-4.0` 等
- **不是独立打包**:`pywebview` 走系统 WebView,不内置 Chromium,体量小
- **底层仍是 CLI**:GUI 失败时直接退回去用 `cambly_export.py` 就行

## `cambly-review` skill
- **依赖 LLM 质量**:本质是结构化 prompt,需要 gpt-4 / sonnet / 同级模型才能稳定产出 5 维
- **ASR 转写误差**:Cambly 的 transcript 是 ASR 生成的,噪声大时需 skill 自行判断"推测"
- **不覆盖已有 review**:增量模式下默认跳过,不会无声覆盖
- **不是真人批改**:自查发现是 skill 根据 transcript 推理的,不能替代真人外教反馈

---

# 六、项目结构

```
Cambly/
├── cambly_export.py     # CLI 工具 + 结构化提取 + markdown 渲染
├── cambly_gui.py        # (可选) GUI 外壳,pywebview 单文件
├── requirements.txt     # playwright + pywebview
├── README.md            # 本文件
└── .gitignore           # .worktrees/ __pycache__/ ...

# 配合使用的 skill(分享出去时一起打包):
cambly-review/
└── SKILL.md             # 5 维结构化 review skill
```
