# Cambly Lesson Exporter

把 Cambly 网页版已结束课程（past-lesson）导出成结构化 Markdown。

> **用法**：传 1 个或多个 Cambly 课程链接，按 `Tutor-Date-Duration` 自动建文件夹，
> 课程总结 + 语音转文字（542 个 turn）落到对应文件夹里。

## 一次性安装

```bash
pip3 install --user -r requirements.txt
python3 -m playwright install chromium
```

## 第一次跑：登录 Cambly

Cambly 整站都要登录，工具用独立的 user-data-dir 持久化 cookie：

```bash
python3 cambly_export.py "https://www.cambly.com/en/student/progress/past-lesson?lessonV2Id=xxx&lang=zh_CN" --login
```

会弹个 Chromium 窗口 → 手动登录 → 看到课程页后回终端按 Enter。Cookie 缓存在
`~/.cambly_export/chrome-profile/`，下次不用再登。

> 用的是 Playwright 自带的 Chromium（不是系统 Chrome），所以不动你日常 Chrome 的登录态。

## 日常跑

```bash
# 1 条
python3 cambly_export.py "<url>" --out ~/Documents/CamblyNotes

# 多条（串行跑，每条独立文件夹）
python3 cambly_export.py "<url1>" "<url2>" "<url3>" --out ~/Documents/CamblyNotes
```

跑动期间 terminal 会实时打印：

```
============================================================
[1/3] https://www.cambly.com/.../past-lesson?lessonV2Id=xxxx
============================================================
   → 外教: Dennis D, 日期: July 1st, 2026, 时长: 60 分钟
   ✓ 已保存 → /Users/.../CamblyNotes/Dennis D-July 1st, 2026-60 minutes/Dennis D-July 1st, 2026-60 minutes.md

============================================================
[2/3] ...
============================================================

============================================================
[done] 全部 3 条导出完成。输出目录: /Users/.../CamblyNotes
============================================================
```

## 输出结构

`<输出目录>/<Tutor>-<Date>-<Duration minutes>/<同名>.md`

例：
```
~/Documents/CamblyNotes/
  Dennis D-July 1st, 2026-60 minutes/
    Dennis D-July 1st, 2026-60 minutes.md
  Sara K-June 28th, 2026-30 minutes/
    Sara K-June 28th, 2026-30 minutes.md
```

文件夹名 / 文件名都来自 feedback tab 的「From Cambly BETA」上方的教师信息
（外教姓名、课程日期、时长）。

## 反查 DOM（出问题时用）

```bash
python3 cambly_export.py "<url>" --debug-html ./debug.html --screenshots ./shots
```

## 项目结构

```
Cambly/
├── cambly_export.py     # CLI 工具
├── requirements.txt     # playwright
└── README.md
```

## 已知坑

- **没用 GUI**：CLI-only。一次跑 N 条，串行（一个完成再下一个），约 30-60s/条（要等 Cambly AI 生成反馈）
- **Cambly 没公开 API**，全靠 Playwright 模拟浏览器；前端改版会失效
- **AI feedback 是异步生成**，脚本最多等 30s；失败再跑一次
- **transcript 合并**：把 559 个 ASR 片段合并成 ~540 个 turn（相邻同 speaker 合并）。如需更精细的"按停顿重新对齐"是另一个活儿
