#!/usr/bin/env python3
"""Tech Digest Agent — lives on a Fly.io Sprite.

Fetches tech-news RSS/Atom feeds, summarizes them into a Traditional Chinese
(zh-TW) daily digest with an LLM reached through the Sprites Ollama connector
(the Sprite never holds the Ollama API key; the gateway attaches it), and
writes a static HTML page that a tiny web server on the same Sprite serves.

Environment:
  GATEWAY_BASE   e.g. https://api.sprites.dev/v1/gateway/ollama/<connection_id>
                 (when unset, the agent runs in fallback mode: titles only)
  OLLAMA_MODEL   preferred model name; auto-picked from the connector if unset
  OUT_DIR        output directory (default: ./public)
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

FEEDS = [
    # Frontier AI labs & big-tech research (official)
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("Google DeepMind Blog", "https://deepmind.google/blog/rss.xml"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("Google Research Blog", "https://research.google/blog/rss/"),
    ("Mistral AI News", "https://mistral.ai/news/rss"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/"),
    ("Apple ML Research", "https://machinelearning.apple.com/rss.xml"),
    ("NVIDIA AI Blog", "https://blogs.nvidia.com/blog/category/deep-learning/feed/"),
    ("AWS Machine Learning Blog", "https://aws.amazon.com/blogs/machine-learning/feed/"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
    # Research preprints
    ("arXiv cs.AI", "https://export.arxiv.org/rss/cs.AI"),
    ("arXiv cs.CL", "https://export.arxiv.org/rss/cs.CL"),
    # Authoritative coverage & curation
    ("Hacker News", "https://hnrss.org/frontpage?points=80"),
    ("Ars Technica (AI)", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("Import AI", "https://importai.substack.com/feed"),
]
MAX_ITEMS_PER_FEED = 6
MAX_ITEMS_TOTAL = 60
TAIPEI = timezone(timedelta(hours=8))


def _load_env_file():
    """Load KEY=VALUE pairs from gateway.env next to this script (used when the
    process environment does not carry them, e.g. under sprite-env services)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gateway.env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env_file()

PREFERRED_MODELS = [
    "deepseek-v4.1-flash:1017",
    "gpt-oss:120b",
    "deepseek-v3.1:671b",
    "qwen3:235b",
    "llama3.3:70b",
    "gpt-oss:20b",
]


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "TechDigestAgent/1.0 (+https://fly.io/hacksgiving/)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":  # some feeds (e.g. DeepMind) answer gzip without asking
        import gzip
        raw = gzip.decompress(raw)
    return raw


def strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


def parse_date(raw):
    """Parse an RSS (RFC 822) or Atom (ISO 8601) date string to an aware datetime."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_feed(source, raw):
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items
    for el in root.iter():
        if strip_ns(el.tag) not in ("item", "entry"):
            continue
        title, link, summary = "", "", ""
        dates = {}
        for child in el:
            t = strip_ns(child.tag)
            if t == "title":
                title = (child.text or "").strip()
            elif t == "link":
                link = (child.text or "").strip() or (child.get("href") or "")
            elif t in ("description", "summary", "content"):
                summary = re.sub("<[^>]+>", " ", child.text or "")
                summary = re.sub(r"\s+", " ", summary).strip()
            elif t in ("pubDate", "published", "updated", "date"):
                dates[t] = (child.text or "").strip()
        if title and link:
            dt = None
            for key in ("published", "pubDate", "date", "updated"):
                dt = parse_date(dates.get(key, ""))
                if dt:
                    break
            items.append({"source": source, "title": title, "link": link,
                          "summary": summary[:400],
                          "date": dt.isoformat() if dt else ""})
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return items


def format_date(iso):
    """Format a stored ISO date in Taipei time for display; '' when unknown."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return ""
    return dt.astimezone(TAIPEI).strftime("%Y/%m/%d %H:%M")


def collect_items():
    per_feed = []
    for source, url in FEEDS:
        try:
            per_feed.append(parse_feed(source, fetch(url)))
        except Exception as e:  # a dead feed must not kill the digest
            print(f"[warn] feed {source} failed: {e}", file=sys.stderr)
            per_feed.append([])
    # Round-robin interleave so every source is represented before the cap.
    all_items = []
    idx = 0
    while len(all_items) < MAX_ITEMS_TOTAL:
        progressed = False
        for items in per_feed:
            if idx < len(items):
                all_items.append(items[idx])
                progressed = True
                if len(all_items) >= MAX_ITEMS_TOTAL:
                    break
        if not progressed:
            break
        idx += 1
    return all_items


def build_rss(items, now):
    """RSS 2.0 feed of every collected item, published as feed.xml."""
    from email.utils import format_datetime
    from xml.sax.saxutils import escape
    pub = format_datetime(now.astimezone(timezone.utc))
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             '<title>科技新訊摘要｜全部新聞</title>',
             '<link>https://tech-digest-b6vs6.sprites.app/</link>',
             '<description>Tech Digest Agent 收集的全部 AI 科技新聞（原始清單，每 6 小時更新）</description>',
             '<language>zh-Hant-TW</language>',
             f'<lastBuildDate>{pub}</lastBuildDate>']
    for it in items:
        item_pub = pub
        if it.get("date"):
            try:
                item_pub = format_datetime(datetime.fromisoformat(it["date"]).astimezone(timezone.utc))
            except Exception:
                pass
        parts.append('<item>'
                     f'<title>{escape(it["title"])}</title>'
                     f'<link>{escape(it["link"])}</link>'
                     f'<guid isPermaLink="true">{escape(it["link"])}</guid>'
                     f'<description>{escape(it["summary"])}</description>'
                     f'<category>{escape(it["source"])}</category>'
                     f'<pubDate>{item_pub}</pubDate>'
                     '</item>')
    parts.append('</channel></rss>')
    return "".join(parts)


def _opencc_bin():
    cand = os.environ.get("OPENCC_CLI")
    if cand and os.path.exists(cand):
        return cand
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "node_modules", ".bin", "opencc-cli")
    if os.path.exists(local):
        return local
    import shutil
    return shutil.which("opencc-cli")


def convert_tw2(texts):
    """Force-convert texts to Taiwan Traditional Chinese with @willh/opencc-cli
    (cn -> tw2). Falls back to the originals when the CLI is unavailable, so a
    missing tool never breaks the digest."""
    texts = list(texts)
    if not texts:
        return texts
    binary = _opencc_bin()
    if not binary:
        print("[warn] opencc-cli not found; skipping tw2 conversion", file=sys.stderr)
        return texts
    sep = "\n@@@TW2SPLIT@@@\n"
    try:
        proc = subprocess.run([binary, "-f", "cn", "-t", "tw2"],
                              input=sep.join(texts), capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "")[:200])
        parts = proc.stdout.split("@@@TW2SPLIT@@@")
        if len(parts) != len(texts):
            raise RuntimeError(f"split mismatch {len(parts)} != {len(texts)}")
        return [p.strip("\n") for p in parts]
    except Exception as e:
        print(f"[warn] tw2 conversion failed: {e}", file=sys.stderr)
        return texts


def gateway_json(path, payload=None):
    base = os.environ["GATEWAY_BASE"].rstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def pick_model():
    want = os.environ.get("OLLAMA_MODEL")
    try:
        tags = gateway_json("/api/tags")
        names = [m.get("name", "") for m in tags.get("models", [])]
    except Exception as e:
        print(f"[warn] /api/tags failed: {e}", file=sys.stderr)
        names = []
    if want:
        return want
    for pref in PREFERRED_MODELS:
        if pref in names:
            return pref
    return names[0] if names else PREFERRED_MODELS[0]


def summarize(items):
    """Return a list of digest entries via the Ollama connector, or None."""
    if not os.environ.get("GATEWAY_BASE"):
        return None
    model = pick_model()
    listing = "\n".join(
        f"{i+1}. [{it['source']}] {it['title']}\n   {it['summary']}\n   {it['link']}"
        for i, it in enumerate(items))
    prompt = (
        "你是一位台灣科技編輯。以下是今天的英文科技新聞清單。請挑出最重要的 9 則，"
        "用台灣繁體中文寫成每日摘要。每一則包含：title（繁中標題，忠實原意）、"
        "summary（2 到 3 句繁中摘要，說明發生什麼事、為什麼重要）、"
        "source（原來源名稱）、link（原連結）。\n"
        "只輸出 JSON 陣列，不要加任何說明文字或 markdown 圍欄。格式：\n"
        '[{"title": "...", "summary": "...", "source": "...", "link": "..."}]\n\n'
        f"新聞清單：\n{listing}")
    try:
        resp = gateway_json("/api/chat", {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        })
        text = resp.get("message", {}).get("content", "")
        m = re.search(r"\[.*\]", text, re.S)
        entries = json.loads(m.group(0)) if m else []
        clean = [{"title": str(e.get("title", "")).strip(),
                  "summary": str(e.get("summary", "")).strip(),
                  "source": str(e.get("source", "")).strip(),
                  "link": str(e.get("link", "")).strip()}
                 for e in entries if e.get("title")]
        flat = []
        for e in clean:
            flat += [e["title"], e["summary"]]
        conv = convert_tw2(flat)
        for i, e in enumerate(clean):
            e["title"], e["summary"] = conv[2 * i], conv[2 * i + 1]
        print(f"[info] summarized with model {model}: {len(clean)} entries")
        return clean or None
    except Exception as e:
        print(f"[warn] summarize failed: {e}", file=sys.stderr)
        return None


SOURCE_COLORS = {
    "OpenAI News": "#0a7d62",
    "Google DeepMind Blog": "#7b5cff",
    "Google AI Blog": "#4285f4",
    "Google Research Blog": "#34a853",
    "Mistral AI News": "#ff7000",
    "Microsoft Research": "#0067b8",
    "Apple ML Research": "#555555",
    "NVIDIA AI Blog": "#76b900",
    "AWS Machine Learning Blog": "#e88b00",
    "Hugging Face Blog": "#e8a100",
    "arXiv cs.AI": "#b31b1b",
    "arXiv cs.CL": "#8c2f39",
    "Hacker News": "#ff6600",
    "Ars Technica (AI)": "#01a3c4",
    "TechCrunch AI": "#0a9e01",
    "The Verge AI": "#5200ff",
    "Simon Willison": "#0b7285",
    "Import AI": "#946b00",
}

# Lighter variants for dark mode, where the base color would be too dark
# against the dark card surface (e.g. The Verge's #5200ff).
SOURCE_COLORS_DARK = {
    "OpenAI News": "#2fbf9b",
    "Apple ML Research": "#c9c9c9",
    "Microsoft Research": "#4db2ff",
    "arXiv cs.AI": "#ff8a80",
    "arXiv cs.CL": "#e59aa4",
    "TechCrunch AI": "#4be15a",
    "The Verge AI": "#b79cff",
    "Simon Willison": "#45c9dd",
    "Import AI": "#eec04a",
}


def badge_style(source):
    c = SOURCE_COLORS.get(source, "#6b675f")
    d = SOURCE_COLORS_DARK.get(source)
    return f"--c:{c};--cd:{d}" if d else f"--c:{c}"


def render(entries, fallback_items, now):
    date_str = now.strftime("%Y-%m-%d")
    weekday = "一二三四五六日"[now.weekday()]
    llm_mode = entries is not None
    date_by_link = {it["link"]: it.get("date", "") for it in fallback_items}
    cards = []
    if llm_mode:
        for e in entries:
            bstyle = badge_style(e["source"])
            date_txt = format_date(date_by_link.get(e["link"], ""))
            cards.append(
                f'<article class="card">'
                f'<div class="meta"><span class="badge" style="{bstyle}">{html.escape(e["source"])}</span><span class="date">{date_txt}</span></div>'
                f'<h2><a href="{html.escape(e["link"])}" target="_blank" rel="noopener noreferrer">{html.escape(e["title"])}</a></h2>'
                f'<p>{html.escape(e["summary"])}</p>'
                f'<a class="more" href="{html.escape(e["link"])}" target="_blank" rel="noopener noreferrer">閱讀原文 →</a>'
                f'</article>')
    else:
        for it in fallback_items[:12]:
            bstyle = badge_style(it["source"])
            date_txt = format_date(it.get("date", ""))
            cards.append(
                f'<article class="card">'
                f'<div class="meta"><span class="badge" style="{bstyle}">{html.escape(it["source"])}</span><span class="date">{date_txt}</span></div>'
                f'<h2><a href="{html.escape(it["link"])}" target="_blank" rel="noopener noreferrer">{html.escape(it["title"])}</a></h2>'
                f'<p>{html.escape(it["summary"][:160])}</p>'
                f'<div class="note">原文標題，LLM 摘要待 Ollama connector 開通</div></article>')
    mode_note = "由 Ollama connector 驅動的 LLM 整理" if llm_mode else "Fallback 模式（connector 未開通）"
    return f"""<!DOCTYPE html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="科技新訊摘要：每日 AI 前沿新聞，由住在 Fly.io Sprite 上的個人 agent 以 LLM 整理成台灣繁體中文。彙整 OpenAI、Google DeepMind、Mistral、Microsoft Research、arXiv 等 18 個權威來源，每 6 小時更新。">
<meta name="keywords" content="AI 新聞, 人工智慧, 科技新訊, 繁體中文摘要, OpenAI, DeepMind, Mistral, arXiv, LLM, Fly.io, Hacksgiving">
<meta name="author" content="Will Huang">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://tech-digest-b6vs6.sprites.app/">
<meta name="theme-color" content="#241f4e">
<meta property="og:type" content="website">
<meta property="og:site_name" content="科技新訊摘要">
<meta property="og:locale" content="zh_TW">
<meta property="og:title" content="科技新訊摘要｜每日 AI 前沿新聞">
<meta property="og:description" content="住在 Fly.io Sprite 上的個人 agent，每日彙整 18 個權威 AI 來源（OpenAI、Google DeepMind、Mistral、Microsoft Research、arXiv…），以 LLM 整理成台灣繁體中文摘要，每 6 小時更新。">
<meta property="og:url" content="https://tech-digest-b6vs6.sprites.app/">
<meta property="og:image" content="https://tech-digest-b6vs6.sprites.app/og-image.jpg">
<meta property="og:image:secure_url" content="https://tech-digest-b6vs6.sprites.app/og-image.jpg">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="科技新訊摘要 — 每日 AI 前沿新聞，台灣繁體中文摘要">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="科技新訊摘要｜每日 AI 前沿新聞">
<meta name="twitter:description" content="每日彙整 18 個權威 AI 來源，以 LLM 整理成台灣繁體中文摘要，每 6 小時更新。">
<meta name="twitter:image" content="https://tech-digest-b6vs6.sprites.app/og-image.jpg">
<meta name="twitter:image:alt" content="科技新訊摘要 — 每日 AI 前沿新聞，台灣繁體中文摘要">
<script type="application/ld+json">{{"@context": "https://schema.org", "@type": "WebSite", "name": "科技新訊摘要", "url": "https://tech-digest-b6vs6.sprites.app/", "inLanguage": "zh-Hant-TW", "description": "每日 AI 前沿新聞，由個人 agent 以 LLM 整理成台灣繁體中文摘要。"}}</script>
<link rel="alternate" type="application/rss+xml" title="科技新訊摘要｜全部新聞 RSS" href="feed.xml">
<title>科技新訊摘要｜{date_str}</title>
<script>(function(){{try{{var t=localStorage.getItem("td-theme")||(window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.setAttribute("data-theme",t);}}catch(e){{document.documentElement.setAttribute("data-theme","light");}}}})();</script>
<style>
 :root {{ --bg:#f4f2ec; --surface:#ffffff; --ink:#1d1a16; --sub:#57534a; --line:#e5e0d5; --brand:#241f4e; --brand2:#4a3f9e; --accent:#ffb020; --more:#4a3f9e; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14131a; --surface:#1e1c28; --ink:#f0ede6; --sub:#a49e92; --line:#322f42; --brand:#17142e; --brand2:#3d348b; --more:#a99cff; }} }}
 html[data-theme="light"] {{ --bg:#f4f2ec; --surface:#ffffff; --ink:#1d1a16; --sub:#57534a; --line:#e5e0d5; --brand:#241f4e; --brand2:#4a3f9e; --accent:#ffb020; --more:#4a3f9e; }}
 html[data-theme="dark"] {{ --bg:#14131a; --surface:#1e1c28; --ink:#f0ede6; --sub:#a49e92; --line:#322f42; --brand:#17142e; --brand2:#3d348b; --accent:#ffb020; --more:#a99cff; }}
 * {{ box-sizing:border-box; }}
 body {{ font-family: system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif; margin:0; background:var(--bg); color:var(--ink); -webkit-font-smoothing:antialiased; }}
 header {{ background:linear-gradient(135deg, var(--brand), var(--brand2)); color:#fff; padding:34px 20px 30px; }}
 .wrap {{ max-width:1120px; margin:0 auto; }}
 header .wrap {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; }}
 header h1 {{ margin:0 0 8px; font-size:clamp(24px, 4.5vw, 34px); letter-spacing:.5px; }}
 header p {{ margin:0; opacity:.88; font-size:14px; line-height:1.6; }}
 .rssbtn {{ display:inline-block; margin-top:6px; flex-shrink:0; background:var(--accent); color:#241f4e; font-weight:700; font-size:13px; padding:8px 14px; border-radius:999px; text-decoration:none; }}
 .rssbtn:hover {{ filter:brightness(1.07); }}
 main {{ max-width:1120px; margin:0 auto; padding:22px 16px 52px; display:grid; grid-template-columns:1fr; gap:16px; }}
 @media (min-width:720px) {{ main {{ grid-template-columns:1fr 1fr; }} }}
 @media (min-width:1080px) {{ main {{ grid-template-columns:1fr 1fr 1fr; }} }}
 .card {{ background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:18px 20px; display:flex; flex-direction:column; gap:10px; transition:transform .15s ease, box-shadow .15s ease; }}
 .card:hover {{ transform:translateY(-3px); box-shadow:0 10px 24px rgba(20,16,60,.12); }}
 .badge {{ display:inline-block; font-size:11px; font-weight:700; letter-spacing:.4px; color:var(--c); border:1.5px solid var(--c); padding:3px 9px; border-radius:999px; }}
 @media (prefers-color-scheme: dark) {{ .badge {{ color:var(--cd, var(--c)); border-color:var(--cd, var(--c)); }} }}
 html[data-theme="light"] .badge {{ color:var(--c); border-color:var(--c); }}
 html[data-theme="dark"] .badge {{ color:var(--cd, var(--c)); border-color:var(--cd, var(--c)); }}
 .actions {{ display:flex; gap:8px; align-items:flex-start; flex-wrap:wrap; }}
 .themebtn {{ margin-top:6px; flex-shrink:0; background:rgba(255,255,255,.14); color:#fff; border:1px solid rgba(255,255,255,.38); font-size:13px; line-height:1; padding:8px 12px; border-radius:999px; cursor:pointer; }}
 .themebtn:hover {{ background:rgba(255,255,255,.24); }}
 .meta {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
 .date {{ font-size:12px; color:var(--sub); white-space:nowrap; }}
 .card h2 {{ font-size:17.5px; margin:0; line-height:1.5; }}
 .card h2 a {{ color:var(--ink); text-decoration:none; }}
 .card h2 a:hover {{ text-decoration:underline; }}
 .card p {{ margin:0; line-height:1.75; color:var(--sub); font-size:14.5px; flex:1; }}
 .more {{ font-size:13px; font-weight:600; color:var(--more); text-decoration:none; }}
 .more:hover {{ text-decoration:underline; }}
 .note {{ font-size:12px; color:var(--sub); }}
 footer {{ max-width:1120px; margin:0 auto; padding:0 16px 36px; font-size:12.5px; color:var(--sub); line-height:1.7; }}
 footer a {{ color:inherit; }}
 .ghlink svg {{ width:18px; height:18px; fill:var(--sub); vertical-align:-3px; margin-left:6px; }}
 .ghlink:hover svg {{ fill:var(--accent); }}
 a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
</style>
</head>
<body>
<header>
 <div class="wrap">
  <div>
   <h1>🗞️ 科技新訊摘要</h1>
   <p>{date_str}（星期{weekday}，台北時間）・{mode_note}・住在 Fly.io Sprite 上的個人 agent 自動編製，每 6 小時更新</p>
  </div>
  <div class="actions">
   <a class="rssbtn" href="feed.xml">📡 RSS 訂閱（全部新聞）</a>
   <button class="themebtn" id="themeBtn" type="button" onclick="toggleTheme()" aria-label="切換深色／淺色模式" title="切換深色／淺色模式">🌙</button>
  </div>
 </div>
</header>
<main>
{''.join(cards)}
</main>
<footer>精選 9 則由 LLM 編譯；完整收集清單見 <a href="feed.xml">RSS feed</a>（18 個來源，含 OpenAI、Google DeepMind、Mistral、Microsoft Research、arXiv 等）。<br>Built for Fly.io Hacksgiving 2026 — runs on a Sprite, summarized via the Sprites Ollama connector.<br>Copyright © {now.year} <a href="https://www.facebook.com/will.fans/" target="_blank" rel="noopener noreferrer">Will 保哥</a> <a class="ghlink" href="https://github.com/doggy8088/hacksgiving-tech-digest" target="_blank" rel="noopener noreferrer" aria-label="GitHub repository" title="GitHub repository"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12"/></svg></a></footer>
<script>function syncThemeIcon(){{var b=document.getElementById("themeBtn");if(b){{b.textContent=document.documentElement.getAttribute("data-theme")==="dark"?"☀️":"🌙";}}}}function toggleTheme(){{var h=document.documentElement;var n=h.getAttribute("data-theme")==="dark"?"light":"dark";h.setAttribute("data-theme",n);try{{localStorage.setItem("td-theme",n);}}catch(e){{}}syncThemeIcon();}}syncThemeIcon();</script>
</body>
</html>"""


def main():
    out_dir = os.environ.get("OUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"))
    os.makedirs(out_dir, exist_ok=True)
    items = collect_items()
    print(f"[info] collected {len(items)} items")
    flat = []
    for it in items:
        flat += [it["title"], it["summary"]]
    conv = convert_tw2(flat)
    for i, it in enumerate(items):
        it["title"], it["summary"] = conv[2 * i], conv[2 * i + 1]
    entries = summarize(items)
    now = datetime.now(TAIPEI)
    page = render(entries, items, now)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(out_dir, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(build_rss(items, now))
    og_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "og-image.jpg")
    if os.path.exists(og_src):
        import shutil
        shutil.copy(og_src, os.path.join(out_dir, "og-image.jpg"))
    archive = {"date": now.strftime("%Y-%m-%d"), "generated_at": now.isoformat(),
               "llm": entries is not None, "entries": entries or [],
               "raw_items": items}
    with open(os.path.join(out_dir, f"digest-{now.strftime('%Y-%m-%d')}.json"), "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)
    print("[info] digest written to", out_dir)


if __name__ == "__main__":
    main()
