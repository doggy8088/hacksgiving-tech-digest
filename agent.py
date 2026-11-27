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
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

FEEDS = [
    ("Hacker News", "https://hnrss.org/frontpage?points=80"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("OpenAI News", "https://openai.com/news/rss.xml"),
    ("Google AI Blog", "https://blog.google/technology/ai/rss/"),
    ("Ars Technica (AI)", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
]
MAX_ITEMS_PER_FEED = 8
MAX_ITEMS_TOTAL = 24
TAIPEI = timezone(timedelta(hours=8))

PREFERRED_MODELS = [
    "gpt-oss:120b",
    "deepseek-v3.1:671b",
    "qwen3:235b",
    "llama3.3:70b",
    "gpt-oss:20b",
]


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "TechDigestAgent/1.0 (+https://fly.io/hacksgiving/)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_ns(tag):
    return tag.rsplit("}", 1)[-1]


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
        for child in el:
            t = strip_ns(child.tag)
            if t == "title":
                title = (child.text or "").strip()
            elif t == "link":
                link = (child.text or "").strip() or (child.get("href") or "")
            elif t in ("description", "summary", "content"):
                summary = re.sub("<[^>]+>", " ", child.text or "")
                summary = re.sub(r"\s+", " ", summary).strip()
        if title and link:
            items.append({"source": source, "title": title, "link": link,
                          "summary": summary[:400]})
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return items


def collect_items():
    all_items = []
    for source, url in FEEDS:
        try:
            all_items.extend(parse_feed(source, fetch(url)))
        except Exception as e:  # a dead feed must not kill the digest
            print(f"[warn] feed {source} failed: {e}", file=sys.stderr)
    return all_items[:MAX_ITEMS_TOTAL]


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
        "你是一位台灣科技編輯。以下是今天的英文科技新聞清單。請挑出最重要的 8 則，"
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
        print(f"[info] summarized with model {model}: {len(clean)} entries")
        return clean or None
    except Exception as e:
        print(f"[warn] summarize failed: {e}", file=sys.stderr)
        return None


def render(entries, fallback_items, now):
    date_str = now.strftime("%Y-%m-%d")
    llm_mode = entries is not None
    cards = []
    if llm_mode:
        for e in entries:
            cards.append(
                f'<article><h2><a href="{html.escape(e["link"])}">{html.escape(e["title"])}</a></h2>'
                f'<p>{html.escape(e["summary"])}</p>'
                f'<div class="src">{html.escape(e["source"])}</div></article>')
    else:
        for it in fallback_items[:12]:
            cards.append(
                f'<article><h2><a href="{html.escape(it["link"])}">{html.escape(it["title"])}</a></h2>'
                f'<p>{html.escape(it["summary"][:160])}</p>'
                f'<div class="src">{html.escape(it["source"])}（原文標題，LLM 摘要待 Ollama connector 開通）</div></article>')
    mode_note = "由 Ollama connector 驅動的 LLM 整理" if llm_mode else "Fallback 模式（connector 未開通）"
    return f"""<!DOCTYPE html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>科技新訊摘要｜{date_str}</title>
<style>
 body {{ font-family: system-ui, "Noto Sans TC", sans-serif; margin: 0; background: #f6f4ef; color: #1c1a17; }}
 header {{ background: #241f4e; color: #fff; padding: 28px 20px; }}
 header h1 {{ margin: 0 0 6px; font-size: 26px; }}
 header p {{ margin: 0; opacity: .85; font-size: 14px; }}
 main {{ max-width: 760px; margin: 0 auto; padding: 20px 16px 48px; }}
 article {{ background: #fff; border-radius: 12px; padding: 18px 20px; margin: 14px 0; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
 article h2 {{ font-size: 18px; margin: 0 0 8px; line-height: 1.45; }}
 article h2 a {{ color: #241f4e; text-decoration: none; }}
 article p {{ margin: 0 0 10px; line-height: 1.7; color: #3d3a36; }}
 .src {{ font-size: 12px; color: #8a8578; }}
 footer {{ max-width: 760px; margin: 0 auto; padding: 0 16px 32px; font-size: 12px; color: #8a8578; }}
</style>
</head>
<body>
<header>
 <h1>🗞️ 科技新訊摘要</h1>
 <p>{date_str}（台北時間）・{mode_note}・住在 Fly.io Sprite 上的個人 agent 自動編製</p>
</header>
<main>
{''.join(cards)}
</main>
<footer>Built for Fly.io Hacksgiving 2026 — runs on a Sprite, summarized via the Sprites Ollama connector.</footer>
</body>
</html>"""


def main():
    out_dir = os.environ.get("OUT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"))
    os.makedirs(out_dir, exist_ok=True)
    items = collect_items()
    print(f"[info] collected {len(items)} items")
    entries = summarize(items)
    now = datetime.now(TAIPEI)
    page = render(entries, items, now)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    archive = {"date": now.strftime("%Y-%m-%d"), "generated_at": now.isoformat(),
               "llm": entries is not None, "entries": entries or [],
               "raw_items": items}
    with open(os.path.join(out_dir, f"digest-{now.strftime('%Y-%m-%d')}.json"), "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)
    print("[info] digest written to", out_dir)


if __name__ == "__main__":
    main()
