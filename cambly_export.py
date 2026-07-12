"""Cambly lesson-page exporter (v0.3 — fully structured).

Usage:
    python3 cambly_export.py "<cambly-url>"                          # headless export
    python3 cambly_export.py "<cambly-url>" --login                 # interactive first-login
    python3 cambly_export.py "<cambly-url>" --debug-html ./debug.html
    python3 cambly_export.py "<cambly-url>" --screenshots ./shots
    python3 cambly_export.py "<cambly-url>" --out /path/to/out

Output:
    cambly_<lessonV2Id>.md  — fully structured markdown with metadata,
    feedback (AI summary + grammar), transcript (per-turn), chat, slides.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROFILE_DIR = Path.home() / ".cambly_export" / "chrome-profile"
LESSON_URL_TEMPLATE = (
    "https://www.cambly.com/en/student/progress/past-lesson"
    "?lessonV2Id={lesson_id}&lang=zh_CN"
)
LESSON_ID_RE = re.compile(r"lessonV2Id=([a-f0-9]+)", re.IGNORECASE)

TABS: tuple[str, ...] = ("feedback", "transcript", "chat", "slides")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def parse_lesson_id(url: str) -> str:
    m = LESSON_ID_RE.search(url)
    if not m:
        raise ValueError(f"无法从 URL 中提取 lessonV2Id: {url}")
    return m.group(1)


def build_lesson_url(lesson_id: str, lang: str = "zh_CN") -> str:
    return LESSON_URL_TEMPLATE.format(lesson_id=lesson_id, lang=lang)


# ---------------------------------------------------------------------------
# Browser / fetch
# ---------------------------------------------------------------------------


def open_lesson_page(playwright, lesson_id: str, *, headless: bool, debug_html: Path | None):
    """Returns ``(context, page, is_logged_in)``."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.new_page()
    url = build_lesson_url(lesson_id)
    print(f"[info] 正在打开课程页: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    is_logged_in = False
    for _ in range(40):
        try:
            tab_count = page.locator('[role="tab"]').count()
            body_text = page.locator("body").inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            tab_count = 0
            body_text = ""
        if "/login" in page.url or "登录 Cambly" in body_text:
            break
        if "past-lesson" in page.url and tab_count >= 6:
            is_logged_in = True
            break
        page.wait_for_timeout(500)

    if debug_html:
        debug_html.parent.mkdir(parents=True, exist_ok=True)
        debug_html.write_text(page.content(), encoding="utf-8")
        print(f"[info] HTML 已落盘（post-hydration 快照）: {debug_html}")

    return context, page, is_logged_in


# ---------------------------------------------------------------------------
# Per-tab JS extractors. Each is a tiny page.evaluate() so a single tab
# re-render / nav can't blow up the whole pipeline.
# ---------------------------------------------------------------------------

# Each per-tab JS is fully self-contained — no cross-call state needed.
# Python handles tab clicking so a sub-tab re-render can't kill a long
# async evaluate.

_JS_INNER = r"""
const txt = el => {
    try {
        const t = el.innerText !== undefined ? el.innerText : (el.textContent || '');
        // Collapse runs of horizontal whitespace but PRESERVE newlines so
        // a speaker name appearing on its own line stays on its own line.
        return (t || '').replace(/[ \t]+/g, ' ').trim();
    } catch (e) { return ''; }
};
// The right-side dark wrapper is `gray900BackgroundColor`. Its children are
// [top-header, tab-strip, panel-content]. We pick the child with the longest
// innerText as the actual content area — robust to Cambly reshuffling order.
function findPanel() {
    return document.querySelector('[class*="gray900BackgroundColor"]') || document.body;
}
function panelContent() {
    const root = findPanel();
    if (!root || !root.children || !root.children.length) return root || document.body;
    let best = null, bestLen = 0;
    for (const ch of root.children) {
        const t = txt(ch);
        if (t.length > bestLen) { bestLen = t.length; best = ch; }
    }
    return best || root;
}
"""

JS_FEEDBACK = _JS_INNER + r"""
const panel = panelContent();
const body = txt(panel);
const md = {};
const dateMatch = body.match(/[A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?,? \d{4}/);
if (dateMatch) {
    md.date = dateMatch[0];
    // Strip out tab-strip noise (the panel often contains both mobile and
    // desktop tab labels at the top before the actual content). Find the
    // last "Firstname L." pattern in the prefix; otherwise use the prefix
    // with tab tokens removed.
    const prefix = body.slice(0, dateMatch.index);
    const TAB_RE = /^(FEEDBACK|TRANSCRIPT|SLIDES|CHAT|feedback|transcript|slides|chat)$/;
    const lines = prefix.split('\n').map(s => s.trim()).filter(l => l && !TAB_RE.test(l));
    // Take the last 1–3 capitalized-word run before the date
    md.tutorName = lines.length ? lines[lines.length - 1] : '';
}
const sp = body.match(/Speaking percent\s*(\d+%)/);
const wpm = body.match(/Words per minute\s*(\d+)/);
const uniq = body.match(/Unique words\s*(\d+)/);
const dur = body.match(/(\d+)\s*minutes/);
md.speakingPercent = sp ? sp[1] : null;
md.wordsPerMinute = wpm ? wpm[1] : null;
md.uniqueWords = uniq ? uniq[1] : null;
md.durationMinutes = dur ? dur[1] : null;
const wk = body.match(/Deducted from the week of\s*([\d\/]+-[\d\/]+)/);
if (wk) md.week = wk[1];

const fromIdx = body.indexOf('From Cambly');
const fbText = fromIdx >= 0 ? body.slice(fromIdx) : body;
const catRegex = /(Other|Grammar|Vocabulary|Pronunciation|Fluency|Topic|Word Choice|Structure|Sentence Structure)(?=\s|$)/g;
const catPositions = [];
let m;
while ((m = catRegex.exec(fbText)) !== null) {
    catPositions.push({ name: m[1], index: m.index });
}
function parseItemBlock(block) {
    const item = {};
    const lines = {
        well:  /WHAT YOU(?:'|’)RE DOING WELL:\s*([\s\S]*?)(?=(EXPLANATION:|$))/.exec(block),
        said:  /YOU SAID:\s*([\s\S]*?)(?=(SUGGESTION:|$))/.exec(block),
        sug:   /SUGGESTION:\s*([\s\S]*?)(?=(EXPLANATION:|$))/.exec(block),
        expl:  /EXPLANATION:\s*([\s\S]*?)(?=(This is unhelpful|$))/.exec(block),
    };
    if (lines.well)  item.well        = lines.well[1].trim();
    if (lines.said)  item.youSaid     = lines.said[1].trim();
    if (lines.sug)   item.suggestion  = lines.sug[1].trim();
    if (lines.expl)  item.explanation = lines.expl[1].trim();
    return item;
}
const categories = [];
for (let i = 0; i < catPositions.length; i++) {
    const start = catPositions[i].index + catPositions[i].name.length;
    const end = i + 1 < catPositions.length ? catPositions[i+1].index : fbText.length;
    const block = fbText.slice(start, end);
    const item = parseItemBlock(block);
    if (Object.keys(item).length > 0) {
        categories.push({ category: catPositions[i].name, items: [item] });
    }
}
({ ready: !body.includes('Getting your feedback ready'), metadata: md, categories });
"""

JS_TRANSCRIPT = _JS_INNER + r"""
const panel = panelContent();
let turnsContainer = null;
let maxImgs = 0;
for (const d of panel.querySelectorAll('div')) {
    const imgs = d.querySelectorAll('img');
    if (imgs.length > maxImgs) { maxImgs = imgs.length; turnsContainer = d; }
}
const turns = [];
if (turnsContainer && maxImgs > 5) {
    turnsContainer.querySelectorAll('img').forEach(img => {
        const alt = img.alt || '';
        const speaker = alt.replace(/'?s avatar$/, '');
        const sib = img.parentElement.nextElementSibling;
        const text = sib ? txt(sib) : '';
        if (text) turns.push({ speaker, text });
    });
}
const merged = [];
for (const t of turns) {
    if (merged.length && merged[merged.length - 1].speaker === t.speaker) {
        merged[merged.length - 1].text += ' ' + t.text;
    } else {
        merged.push({ ...t });
    }
}
({ turns, mergedTurns: merged });
"""

JS_CHAT = _JS_INNER + r"""
const panel = panelContent();
const rawText = txt(panel);
const TAB_NAMES = new Set(['Chat', 'FEEDBACK', 'TRANSCRIPT', 'SLIDES', 'CHAT']);

// Look for a line that is exactly "First Last" (capitalized, 2 words,
// appears alone in the panel content). The chat panel renders the sender's
// name on its own line before the first message. Use `*` on the second word
// so single-letter initials (e.g. "Dennis D") match too.
let speaker = '';
for (const line of rawText.split('\n')) {
    const t = line.trim();
    const m = t.match(/^([A-Z][a-zA-Z]+)\s+([A-Z][a-zA-Z]*)$/);
    if (!m) continue;
    if (TAB_NAMES.has(m[1])) continue;
    if (TAB_NAMES.has(m[2])) continue;
    const joined = (m[1] + ' ' + m[2]).trim();
    if (/^(Lesson|From|Cambly|Help|Skip|Specific|Click|Go|Revisit|Deducted|Speaking|Words|Unique)$/i.test(m[1])) continue;
    speaker = joined;
    break;
}
const links = Array.from(panel.querySelectorAll('a'))
    .filter(a => {
        const href = a.href || '';
        return href &&
            !href.includes('/student/tutors/') &&
            !href.startsWith('https://www.cambly.com/');
    })
    .map(a => ({ href: a.href, text: txt(a).slice(0, 200) }));
({ speaker, text: rawText, links });
"""

JS_SLIDES = _JS_INNER + r"""
const panel = panelContent();
// strip out tab-strip noise from text
let t = txt(panel);
t = t.replace(/FEEDBACK\s+TRANSCRIPT\s+SLIDES\s+CHAT\s*/g, '');
t = t.replace(/^Slides\s+Revisit the slides from your lesson[^\n]*\n?/g, '');
({ text: t.trim(), unavailable: /Unavailable/i.test(t) });
"""


def _click_tab(page, name: str) -> None:
    """Click all role=tab elements whose visible text equals `name`.

    Cambly renders two parallel tab strips (mobile + desktop) so we click
    every match. Bounded by timeout — if no tab is found we just log.
    """
    try:
        page.get_by_role("tab", name=name, exact=True).first.click(timeout=3000)
    except Exception as e:
        print(f"[warn] click tab {name!r} failed: {e}")


def scrape_structured(page, *, screenshot_dir: Path | None = None) -> dict:
    """Walk all tabs and return structured data. Python handles tab clicks
    so each evaluate() runs in a stable page context.
    """
    print("[scrape] 正在浏览器中跑结构化提取...")
    result = {
        "url": page.url,
        "title": page.title(),
        "metadata": {},
        "transcript": {"turns": [], "mergedTurns": []},
        "chat": {"speaker": "", "text": "", "links": []},
        "slides": {"text": "", "unavailable": False},
        "feedback": {"ready": False, "categories": []},
    }

    # Always click feedback first so the panel state is normalized.
    _click_tab(page, "feedback")
    page.wait_for_timeout(1500)
    try:
        page.wait_for_function(
            """() => !document.body.innerText.includes('Getting your feedback ready')""",
            timeout=30000,
        )
    except Exception:
        pass
    fb = page.evaluate(JS_FEEDBACK)
    result["feedback"] = {"ready": fb["ready"], "categories": fb["categories"]}
    result["metadata"] = fb["metadata"]
    print(f"[scrape] feedback: ready={fb['ready']}, {len(fb['categories'])} categories")

    # TRANSCRIPT
    _click_tab(page, "transcript")
    try:
        page.wait_for_function(
            """() => document.body.innerText.includes('Skip to the most memorable')""",
            timeout=10000,
        )
    except Exception:
        pass
    tr = page.evaluate(JS_TRANSCRIPT)
    result["transcript"] = tr
    print(f"[scrape] transcript: {len(tr['turns'])} fragments → {len(tr['mergedTurns'])} turns")

    # CHAT
    _click_tab(page, "chat")
    try:
        page.wait_for_function(
            """() => document.body.innerText.includes('specific parts of your lesson')""",
            timeout=10000,
        )
    except Exception:
        pass
    chat = page.evaluate(JS_CHAT)
    result["chat"] = chat
    print(f"[scrape] chat: speaker={chat['speaker']!r}, links={len(chat['links'])}")

    # SLIDES
    _click_tab(page, "slides")
    try:
        page.wait_for_function(
            """() => /Revisit the slides|Lesson Slide Preview Unavailable/.test(document.body.innerText)""",
            timeout=10000,
        )
    except Exception:
        pass
    sl = page.evaluate(JS_SLIDES)
    result["slides"] = sl
    print(f"[scrape] slides: unavailable={sl['unavailable']}")

    if screenshot_dir:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        for tab in ("feedback", "transcript", "chat", "slides"):
            _click_tab(page, tab)
            page.wait_for_timeout(800)
            try:
                page.screenshot(path=str(screenshot_dir / f"{tab}.png"), full_page=False)
            except Exception:
                pass

    return result


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def fmt_unknown(value, fallback="(未提供)"):
    if value is None or value == "":
        return fallback
    return value


def render_markdown(lesson_id: str, data: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md = data.get("metadata", {}) or {}

    out = []
    out.append("# Cambly 课程导出\n")
    out.append(f"- **Lesson ID**: `{lesson_id}`")
    out.append(f"- **课程 URL**: {fmt_unknown(data.get('url'))}")
    out.append(f"- **外教**: {fmt_unknown(md.get('tutorName'))}")
    if md.get("date"):
        out.append(f"- **日期**: {md['date']}")
    if md.get("durationMinutes"):
        out.append(f"- **时长**: {md['durationMinutes']} 分钟")
    if md.get("week"):
        out.append(f"- **扣课时**: {md['week']}")
    stats_line = []
    if md.get("speakingPercent"):
        stats_line.append(f"Speaking {md['speakingPercent']}")
    if md.get("wordsPerMinute"):
        stats_line.append(f"WPM {md['wordsPerMinute']}")
    if md.get("uniqueWords"):
        stats_line.append(f"Unique words {md['uniqueWords']}")
    if stats_line:
        out.append(f"- **表现指标**: " + " · ".join(stats_line))
    out.append(f"- **导出时间**: {now}")
    out.append("\n---\n")

    # ---- Feedback / AI summary ----
    fb = data.get("feedback", {}) or {}
    out.append("## 课程总结 · AI 反馈（From Cambly）\n")
    if not fb.get("ready"):
        out.append("> ⚠️ 反馈面板在抓取时还未生成完成（Cambly AI 异步加载）。再跑一次可能就有了。\n")
    cats = fb.get("categories", []) or []
    if not cats:
        out.append("_（无内容）_\n")
    else:
        for cat in cats:
            out.append(f"### {cat['category']}\n")
            for item in cat.get("items", []) or []:
                if item.get("well"):
                    out.append(f"- **What you're doing well**: {item['well']}")
                if item.get("youSaid"):
                    out.append(f"- **You said**: `{item['youSaid']}`")
                if item.get("suggestion"):
                    out.append(f"- **Suggestion**: `{item['suggestion']}`")
                if item.get("explanation"):
                    out.append(f"- **Explanation**: {item['explanation']}")
            out.append("")
    out.append("---\n")

    # ---- Transcript ----
    tr = data.get("transcript", {}) or {}
    merged = tr.get("mergedTurns", []) or []
    out.append("## 语音转文字（Transcript）\n")
    out.append(f"共 {len(tr.get('turns', []))} 个语音片段，合并为 **{len(merged)}** 个发言轮次。\n")
    if not merged:
        out.append("_（无内容）_\n")
    else:
        for t in merged:
            speaker = t.get("speaker", "未知")
            text = t.get("text", "").strip()
            out.append(f"- **{speaker}**: {text}")
        out.append("")
    out.append("---\n")

    # ---- Chat ----
    chat = data.get("chat", {}) or {}
    out.append("## 课堂聊天（Chat）\n")
    speaker = chat.get("speaker") or ""
    body = chat.get("text", "")
    body = re.sub(r"FEEDBACK\s+TRANSCRIPT\s+SLIDES\s+CHAT\s*", "", body)
    body = re.sub(
        r"^Chat\s+Go to specific parts of your lesson by clicking any message or emoji\.\s*",
        "", body,
    )
    # Drop the speaker name from the body if it appears first
    if speaker:
        body = re.sub(rf"^{re.escape(speaker)}\s*", "", body.strip(), count=1)
    if not body:
        out.append("_（无聊天内容）_\n")
    else:
        if speaker:
            out.append(f"**{speaker}** 在课内聊天窗发出的内容：\n")
        out.append("```\n")
        out.append(body.strip())
        out.append("\n```\n")
    links = chat.get("links", []) or []
    if links:
        out.append("\n链接：\n")
        for l in links:
            label = l.get("text", "").strip() or l.get("href", "")
            out.append(f"- [{label}]({l.get('href')})")
        out.append("")
    out.append("---\n")

    # ---- Slides ----
    slides = data.get("slides", {}) or {}
    out.append("## 课件（Slides）\n")
    sl_text = (slides.get("text") or "").strip()
    sl_text = re.sub(r"FEEDBACK\s+TRANSCRIPT\s+SLIDES\s+CHAT\s*", "", sl_text)
    sl_text = re.sub(
        r"^Slides\s+Revisit the slides from your lesson to strengthen your learning\.\s*",
        "", sl_text,
    )
    if slides.get("unavailable") or "Unavailable" in sl_text:
        out.append("_（本节课未使用课件）_\n")
    elif not sl_text:
        out.append("_（无内容）_\n")
    else:
        out.append("```\n" + sl_text + "\n```\n")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Reusable helpers (used by both the CLI and the GUI)
# ---------------------------------------------------------------------------


def folder_name_for(metadata: dict) -> str:
    """Compute the per-lesson folder name: `Tutor-Date-Duration` (one level deep).
    Uses only safe filename chars. Falls back gracefully on missing fields."""
    tutor = (metadata.get("tutorName") or "Unknown Tutor").strip()
    date = (metadata.get("date") or "Unknown Date").strip()
    duration = metadata.get("durationMinutes")
    duration_part = f"{duration} minutes" if duration else "Unknown Duration"
    raw = f"{tutor}-{date}-{duration_part}"
    # Replace any path-unsafe characters
    return re.sub(r"[\\/:\"*?<>|]+", "-", raw).strip()


def run_for_url(url: str, *, headless: bool = True, screenshots_dir=None,
                debug_html=None, on_progress=None) -> dict:
    """Scrape a single Cambly URL and return a structured dict.

    on_progress: optional callable(str) for logging without touching stdout.
    Returns: {"ok": True, "lesson_id": ..., "data": ..., "url": ...}
             or: {"ok": False, "error": "...", "url": ...}
    """
    log = on_progress or (lambda s: print(s))
    try:
        lesson_id = parse_lesson_id(url)
    except ValueError as e:
        return {"ok": False, "error": f"无法解析 lessonV2Id: {e}", "url": url}

    log(f"  → 打开 {url}")
    debug_html_path = Path(debug_html).expanduser().resolve() if debug_html else None
    shots_path = Path(screenshots_dir).expanduser().resolve() if screenshots_dir else None

    with sync_playwright() as p:
        context, page, is_logged_in = open_lesson_page(
            p, lesson_id, headless=headless, debug_html=debug_html_path,
        )
        if not is_logged_in:
            context.close()
            return {"ok": False, "error": "未登录（cookie 失效）", "url": url}

        try:
            data = scrape_structured(page, screenshot_dir=shots_path)
        finally:
            context.close()

    return {
        "ok": True,
        "lesson_id": lesson_id,
        "url": url,
        "data": data,
    }


def save_lesson(result: dict, output_dir: str | Path) -> dict:
    """Save a successful run result to disk under <output_dir>/<folder>/<name>.md.

    The folder name is derived from the lesson's metadata
    (tutor / date / duration). Returns the folder + file path.
    """
    if not result.get("ok"):
        raise ValueError(f"run_for_url returned failed result: {result}")
    lesson_id = result["lesson_id"]
    data = result["data"]
    metadata = data.get("metadata", {}) or {}

    folder = folder_name_for(metadata)
    base = Path(output_dir).expanduser().resolve()
    target_dir = base / folder
    target_dir.mkdir(parents=True, exist_ok=True)

    out_path = target_dir / f"{folder}.md"
    out_path.write_text(render_markdown(lesson_id, data), encoding="utf-8")

    return {
        "ok": True,
        "folder": folder,
        "path": str(out_path),
        "lesson_id": lesson_id,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cambly 课程页导出为 markdown（结构化）。支持一次传多个 URL，串行跑。",
    )
    ap.add_argument(
        "urls", nargs="+",
        help="Cambly 课程 URL（可一次传多个，串行跑）",
    )
    ap.add_argument("--login", action="store_true",
                    help="弹出浏览器让你手动登录一次（cookie 失效时也要重跑）")
    ap.add_argument("--out", default=".", help="md 输出目录（默认当前工作目录）")
    ap.add_argument("--debug-html", default=None,
                    help="把抓到的页面 HTML 落到该路径（仅对第一个 URL 生效）")
    ap.add_argument("--screenshots", default=None,
                    help="把每个 tab 截图落盘到该目录（仅对第一个 URL 生效）")
    args = ap.parse_args()

    if not args.urls:
        ap.print_help()
        return 2

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(args.urls)
    failed = 0

    for idx, url in enumerate(args.urls, 1):
        print()
        print("=" * 64)
        print(f"[{idx}/{total}] {url}")
        print("=" * 64)

        result = run_for_url(
            url,
            headless=not args.login,
            screenshots_dir=args.screenshots if idx == 1 else None,
            debug_html=args.debug_html if idx == 1 else None,
            on_progress=lambda s: print("   " + s),
        )

        if not result["ok"]:
            print(f"\n[error] {result['error']}", file=sys.stderr)
            failed += 1
            continue

        metadata = result["data"].get("metadata", {}) or {}
        folder = folder_name_for(metadata)
        print(f"   → 外教: {metadata.get('tutorName', '?')}, "
              f"日期: {metadata.get('date', '?')}, "
              f"时长: {metadata.get('durationMinutes', '?')} 分钟")

        saved = save_lesson(result, out_dir)
        print(f"   ✓ 已保存 → {saved['path']}")

    print()
    print("=" * 64)
    if failed == 0:
        print(f"[done] 全部 {total} 条导出完成。输出目录: {out_dir}")
        return 0
    print(f"[done] {total - failed} 条成功，{failed} 条失败。输出目录: {out_dir}")
    return 1 if failed == total else 0


if __name__ == "__main__":
    sys.exit(main())
