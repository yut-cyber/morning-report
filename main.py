#!/usr/bin/env python3
"""AI 每日早报生成器
抓取 GitHub Trending / HuggingFace Papers / HackerNews
-> OpenRouter 免费模型生成中文一句话摘要(无 key 时用 MyMemory 免费翻译兜底)
-> 生成响应式 HTML 看板 -> PushPlus 推送个人微信
任何环节失败都会尝试推送告警微信, 绝不悄悄漏跑。
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ---------- 配置 ----------
BJT = timezone(timedelta(hours=8))
NOW = datetime.now(BJT)
DATE_STR = NOW.strftime("%Y-%m-%d")
WEEK_CN = "一二三四五六日"[NOW.weekday()]
TIME_STR = NOW.strftime("%H:%M")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "google/gemma-4-31b-it:free"
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").rstrip("/")

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(ROOT, "docs")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 20

AI_KEYWORDS = [
    "ai", "gpt", "llm", "openai", "anthropic", "claude", "gemini", "deepseek",
    "qwen", "model", "agent", "neural", "diffusion", "transformer", "inference",
    "fine-tune", "rag", "multimodal", "robot", "token", "prompt",
]


# ---------- 数据抓取 ----------
def fetch_github_trending(limit=10):
    resp = requests.get("https://github.com/trending", headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for row in soup.select("article.Box-row")[:limit]:
        a = row.select_one("h2 a")
        if not a:
            continue
        name = "/".join(a.get_text(strip=True).split()).replace(" / ", "/")
        url = "https://github.com" + a.get("href", "")
        desc_el = row.select_one("p")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        lang_el = row.select_one('[itemprop="programmingLanguage"]')
        lang = lang_el.get_text(strip=True) if lang_el else ""
        muted = row.select("a.Link--muted")
        stars = muted[0].get_text(strip=True) if muted else ""
        today = ""
        span = row.select_one("span.d-inline-block.float-sm-right")
        if span:
            m = re.search(r"([\d,]+)\s+stars?\s+today", span.get_text())
            if m:
                today = m.group(1)
        items.append({"name": name, "url": url, "desc": desc, "lang": lang, "stars": stars, "today": today})
    return items


def fetch_hf_papers(limit=6):
    resp = requests.get("https://huggingface.co/papers", headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items, seen = [], set()
    for a in soup.select('a[href^="/papers/"]'):
        href = a.get("href", "")
        title = " ".join(a.get_text(" ", strip=True).split())
        if not title or len(title) < 10 or href in seen:
            continue
        seen.add(href)
        items.append({"title": title, "url": "https://huggingface.co" + href})
        if len(items) >= limit:
            break
    return items


def fetch_hackernews(limit=8):
    resp = requests.get(
        "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30",
        headers=UA, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    ai_hits, other_hits = [], []
    for h in hits:
        title = h.get("title") or ""
        url = h.get("url") or ("https://news.ycombinator.com/item?id=" + str(h.get("objectID", "")))
        text = title.lower()
        (ai_hits if any(k in text for k in AI_KEYWORDS) else other_hits).append(
            {"title": title, "url": url, "points": h.get("points", 0)}
        )
    picked = (ai_hits + other_hits)[:limit]
    for it in picked:
        text = it["title"].lower()
        it["tag"] = "AI" if any(k in text for k in AI_KEYWORDS) else "TECH"
    return picked


# ---------- 中文摘要层 ----------
def llm_summarize(trending, papers, news):
    """OpenRouter 免费模型: 全板块中文标题+一句话摘要。失败返回 None。"""
    if not OPENROUTER_API_KEY:
        print("[llm] 未配置 OPENROUTER_API_KEY, 使用免费翻译兜底")
        return None
    lines = ["GitHub Trending:"]
    for i, t in enumerate(trending):
        lines.append(f"{i}. {t['name']} | {t['desc'][:160]}")
    lines.append("\nHuggingFace Papers:")
    for i, p in enumerate(papers):
        lines.append(f"{i}. {p['title']}")
    lines.append("\nHackerNews:")
    for i, n in enumerate(news):
        lines.append(f"{i}. {n['title']}")
    prompt = (
        "你是资深技术编辑, 为中文技术早报撰写内容。根据以下素材, 输出严格 JSON(不要输出其他任何内容):\n"
        '{"trending": [{"summary": "..."}], "papers": [{"title_cn": "...", "summary": "..."}], '
        '"news": [{"title_cn": "...", "summary": "..."}]}\n'
        "要求:\n"
        "1. 数组顺序、数量必须与输入一一对应;\n"
        "2. summary 为简体中文一句话(<=40字), 说清这个项目/论文/新闻是做什么的、为什么值得关注;\n"
        "3. title_cn 为中文短标题(<=18字), 专有名词可保留英文;\n"
        "4. 合并语义重复的内容(在对应 summary 中注明'与X相关'), 不要遗漏。\n\n"
        + "\n".join(lines)
    )
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 3000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r"\{[\s\S]*\}", content)
        data = json.loads(m.group(0))
        return data
    except Exception as e:
        print(f"[llm] 摘要失败, 使用兜底: {e}")
        return None


_mm_cache = {}

def translate_zh(text):
    """无 OpenRouter key 时的兜底: MyMemory 免费翻译(无需密钥)。失败返回原文。"""
    if not text or re.search(r"[一-鿿]", text):
        return text
    if text in _mm_cache:
        return _mm_cache[text]
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text[:450], "langpair": "en|zh-CN"},
            headers=UA, timeout=10,
        )
        resp.raise_for_status()
        zh = resp.json().get("responseData", {}).get("translatedText", "").strip()
        if not zh or "MYMEMORY WARNING" in zh.upper() or zh.lower() == text.lower():
            zh = text
    except Exception:
        zh = text
    _mm_cache[text] = zh
    return zh


def apply_summaries(trending, papers, news):
    data = llm_summarize(trending, papers, news)
    ts = data.get("trending", []) if data else []
    ps = data.get("papers", []) if data else []
    ns = data.get("news", []) if data else []
    for i, t in enumerate(trending):
        if i < len(ts) and ts[i].get("summary"):
            t["summary"] = ts[i]["summary"]
        else:
            t["summary"] = translate_zh(t["desc"]) if t["desc"] else "暂无描述, 点击查看仓库详情"
    for i, p in enumerate(papers):
        if i < len(ps) and ps[i].get("title_cn"):
            p["title_cn"] = ps[i]["title_cn"]
            p["summary"] = ps[i].get("summary", "")
        else:
            p["title_cn"] = translate_zh(p["title"])
            p["summary"] = ""
    for i, n in enumerate(news):
        if i < len(ns) and ns[i].get("title_cn"):
            n["title_cn"] = ns[i]["title_cn"]
            n["summary"] = ns[i].get("summary", "")
        else:
            n["title_cn"] = translate_zh(n["title"])
            n["summary"] = ""
    return trending, papers, news


# ---------- HTML 模板 ----------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 每日早报 · {date}</title>
<style>
:root {{
  --ink: #14161A;
  --paper: #FFFFFF;
  --bg: #F6F5F1;
  --red: #E8442E;
  --blue: #1D4ED8;
  --yellow: #FFC53D;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
  background: var(--bg); color: var(--ink);
  -webkit-font-smoothing: antialiased;
}}
a {{ color: inherit; text-decoration: none; }}
.wrap {{ max-width: 1160px; margin: 0 auto; padding: 0 28px; }}

/* ===== 头部 ===== */
.masthead {{ background: var(--paper); border-bottom: 4px solid var(--ink); position: relative; overflow: hidden; }}
.masthead-top {{ height: 10px; background: repeating-linear-gradient(90deg, var(--red) 0 60px, var(--ink) 60px 120px, var(--yellow) 120px 180px, var(--ink) 180px 240px); }}
.masthead-inner {{ display:flex; justify-content:space-between; align-items:flex-end; padding: 44px 0 36px; gap: 32px; flex-wrap: wrap; }}
.kicker {{ display:inline-flex; align-items:center; gap:10px; font-size:13px; font-weight:800; letter-spacing:.35em; color: var(--red); margin-bottom: 14px; }}
.kicker::before {{ content:""; width:34px; height:4px; background: var(--red); display:inline-block; }}
h1 {{ font-size: clamp(46px, 7.5vw, 88px); font-weight: 900; line-height: 1.02; letter-spacing: .02em; }}
h1 .thin {{ font-weight: 200; }}
.masthead-meta {{ display:flex; flex-direction:column; gap:10px; align-items:flex-start; }}
.meta-chip {{ border: 2.5px solid var(--ink); background: var(--paper); padding: 8px 16px; font-size: 14px; font-weight: 800; box-shadow: 4px 4px 0 var(--ink); }}
.meta-chip.hot {{ background: var(--ink); color: #fff; }}
.deco {{ position:absolute; right: -30px; top: 8px; opacity:.9; pointer-events:none; }}

/* ===== 板块标题 ===== */
.section {{ margin-top: 56px; }}
.sec-head {{ display:flex; align-items:center; gap:16px; margin-bottom: 26px; }}
.sec-no {{ background: var(--red); color:#fff; font-weight:900; font-size:20px; padding:6px 14px; box-shadow: 4px 4px 0 var(--ink); }}
.sec-title {{ font-size: clamp(26px, 3.4vw, 38px); font-weight:900; letter-spacing:.04em; }}
.sec-en {{ font-size:12px; font-weight:800; letter-spacing:.3em; color:#8a8f98; text-transform:uppercase; margin-left:auto; }}

/* ===== Trending 卡片 ===== */
.tcard {{ display:flex; gap:0; background: var(--paper); border: 2.5px solid var(--ink); box-shadow: 6px 6px 0 var(--ink); margin-bottom: 20px; transition: transform .15s ease, box-shadow .15s ease; }}
.tcard:hover {{ transform: translate(-2px,-2px); box-shadow: 9px 9px 0 var(--ink); }}
.tcard-rank {{ flex: 0 0 86px; display:flex; align-items:center; justify-content:center; font-size: 40px; font-weight: 900; color: var(--red); border-right: 2.5px solid var(--ink); background: #FFF7F5; font-variant-numeric: tabular-nums; }}
.tcard-body {{ padding: 20px 24px; flex:1; min-width:0; }}
.tcard-head {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:8px; }}
.tcard-name {{ font-family: "SFMono-Regular", Consolas, "Courier New", monospace; font-size: 19px; font-weight: 800; }}
.tcard-name:hover {{ color: var(--blue); text-decoration: underline; }}
.tcard-sum {{ font-size: 15px; line-height: 1.7; color: #2b2f36; margin-bottom: 12px; }}
.tcard-meta {{ display:flex; gap:10px; flex-wrap:wrap; }}
.chip {{ display:inline-block; font-size:12px; font-weight:800; padding:4px 12px; border:2px solid var(--ink); }}
.chip-lang {{ background: var(--yellow); }}
.chip-star {{ background: var(--ink); color:#fff; border-color: var(--ink); }}
.chip-today {{ background: var(--red); color:#fff; border-color: var(--red); }}
.chip-plain {{ background:#fff; }}

/* ===== 论文 / 快讯卡片 ===== */
.grid {{ display:grid; gap:20px; }}
.grid-3 {{ grid-template-columns: repeat(3, 1fr); }}
.grid-2 {{ grid-template-columns: repeat(2, 1fr); }}
.pcard {{ display:block; background: var(--paper); border: 2.5px solid var(--ink); box-shadow: 6px 6px 0 var(--ink); padding: 22px; transition: transform .15s ease, box-shadow .15s ease; }}
.pcard:hover {{ transform: translate(-2px,-2px); box-shadow: 9px 9px 0 var(--ink); }}
.ptag {{ display:inline-block; font-size:11px; font-weight:900; letter-spacing:.2em; padding:4px 10px; margin-bottom:12px; background: var(--blue); color:#fff; }}
.ptag.red {{ background: var(--red); }}
.ptag.dark {{ background: var(--ink); }}
.pcard h3 {{ font-size:17px; font-weight:900; line-height:1.45; margin-bottom:8px; }}
.pcard p {{ font-size:13.5px; line-height:1.65; color:#3a3f47; }}
.pcard .src {{ display:block; margin-top:12px; font-size:11px; font-weight:800; letter-spacing:.15em; color:#8a8f98; }}

/* ===== 页脚 ===== */
.footer {{ margin-top: 72px; background: var(--ink); color:#fff; }}
.footer-inner {{ display:flex; justify-content:space-between; align-items:center; gap:20px; padding: 30px 0; flex-wrap:wrap; }}
.footer .big {{ font-size:22px; font-weight:900; letter-spacing:.06em; }}
.footer .small {{ font-size:12px; color:#9aa0a8; line-height:1.8; }}
.footer .red {{ color: var(--red); }}

@media (max-width: 860px) {{
  .grid-3 {{ grid-template-columns: 1fr 1fr; }}
  .grid-2 {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 600px) {{
  .grid-3 {{ grid-template-columns: 1fr; }}
  .tcard-rank {{ flex-basis: 60px; font-size: 28px; }}
  .masthead-inner {{ padding: 30px 0 26px; }}
}}
@media print {{
  .tcard, .pcard {{ box-shadow:none; break-inside: avoid; }}
}}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead-top"></div>
  <svg class="deco" width="260" height="150" viewBox="0 0 260 150" fill="none">
    <circle cx="210" cy="40" r="26" fill="#E8442E"/>
    <rect x="120" y="90" width="46" height="46" fill="#FFC53D" stroke="#14161A" stroke-width="4"/>
    <path d="M30 120 L70 60 L110 120 Z" fill="none" stroke="#14161A" stroke-width="5"/>
    <line x1="0" y1="30" x2="90" y2="30" stroke="#14161A" stroke-width="5"/>
  </svg>
  <div class="wrap masthead-inner">
    <div>
      <div class="kicker">DAILY BRIEFING</div>
      <h1>AI 每日<span class="thin">早报</span></h1>
    </div>
    <div class="masthead-meta">
      <span class="meta-chip hot">{date} · 星期{week}</span>
      <span class="meta-chip">生成时间 {time} (北京时间)</span>
      <span class="meta-chip">GitHub · HuggingFace · HackerNews</span>
    </div>
  </div>
</header>

<main class="wrap">

  <section class="section">
    <div class="sec-head">
      <span class="sec-no">01</span>
      <h2 class="sec-title">GitHub 热门项目</h2>
      <span class="sec-en">Trending Top 10</span>
    </div>
    {trending_html}
  </section>

  <section class="section">
    <div class="sec-head">
      <span class="sec-no">02</span>
      <h2 class="sec-title">HuggingFace 每日论文</h2>
      <span class="sec-en">Daily Papers</span>
    </div>
    <div class="grid grid-3">
      {papers_html}
    </div>
  </section>

  <section class="section">
    <div class="sec-head">
      <span class="sec-no">03</span>
      <h2 class="sec-title">AI / 技术快讯</h2>
      <span class="sec-en">HackerNews Digest</span>
    </div>
    <div class="grid grid-2">
      {news_html}
    </div>
  </section>

</main>

<footer class="footer">
  <div class="wrap footer-inner">
    <div>
      <div class="big">AI 每日早报 <span class="red">●</span></div>
      <div class="small">自动化生成 · GitHub Actions 每日 10:00 (北京时间) · 摘要由开源模型生成</div>
    </div>
    <div class="small">数据源: GitHub Trending / HuggingFace Papers / HackerNews<br>{date} · 星期{week}</div>
  </div>
</footer>

</body>
</html>
"""


def build_html(trending, papers, news):
    t_html = []
    for i, t in enumerate(trending, 1):
        chips = []
        if t.get("lang"):
            chips.append(f'<span class="chip chip-lang">{escape(t["lang"])}</span>')
        if t.get("stars"):
            chips.append(f'<span class="chip chip-star">★ {escape(t["stars"])}</span>')
        if t.get("today"):
            chips.append(f'<span class="chip chip-today">今日 +{escape(t["today"])}</span>')
        t_html.append(
            '<article class="tcard">'
            f'<div class="tcard-rank">{i:02d}</div>'
            '<div class="tcard-body">'
            f'<div class="tcard-head"><a class="tcard-name" href="{escape(t["url"])}" target="_blank" rel="noopener">{escape(t["name"])}</a></div>'
            f'<p class="tcard-sum">{escape(t.get("summary", ""))}</p>'
            f'<div class="tcard-meta">{"".join(chips)}</div>'
            '</div></article>'
        )

    p_html = []
    for p in papers:
        p_html.append(
            f'<a class="pcard" href="{escape(p["url"])}" target="_blank" rel="noopener">'
            '<span class="ptag">PAPER</span>'
            f'<h3>{escape(p.get("title_cn") or p["title"])}</h3>'
            f'<p>{escape(p.get("summary", ""))}</p>'
            '<span class="src">HUGGINGFACE.CO</span></a>'
        )

    n_html = []
    for n in news:
        tag_cls = "red" if n.get("tag") == "AI" else "dark"
        n_html.append(
            f'<a class="pcard" href="{escape(n["url"])}" target="_blank" rel="noopener">'
            f'<span class="ptag {tag_cls}">{escape(n.get("tag", "TECH"))}</span>'
            f'<h3>{escape(n.get("title_cn") or n["title"])}</h3>'
            f'<p>{escape(n.get("summary", ""))}</p>'
            '<span class="src">NEWS.YCOMBINATOR.COM</span></a>'
        )

    return HTML_TEMPLATE.format(
        date=DATE_STR, week=WEEK_CN, time=TIME_STR,
        trending_html="\n".join(t_html),
        papers_html="\n".join(p_html),
        news_html="\n".join(n_html),
    )


# ---------- 微信推送 ----------
def push_wechat(title, content):
    if not PUSHPLUS_TOKEN:
        print("[push] 未配置 PUSHPLUS_TOKEN, 跳过推送")
        return False
    for attempt in range(3):
        try:
            resp = requests.post(
                "http://www.pushplus.plus/send",
                json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "markdown"},
                timeout=TIMEOUT,
            )
            data = resp.json()
            if data.get("code") == 200:
                print("[push] 微信推送成功")
                return True
            print(f"[push] 第{attempt + 1}次推送失败: {data.get('msg')}")
        except Exception as e:
            print(f"[push] 第{attempt + 1}次推送异常: {e}")
        time.sleep(3)
    return False


def build_push_markdown(trending, papers, news):
    lines = [f"## AI 每日早报 {DATE_STR} 星期{WEEK_CN}", ""]
    if DASHBOARD_URL:
        lines.append(f"[查看完整看板]({DASHBOARD_URL})")
        lines.append("")
    lines.append("**GitHub 热门**")
    for i, t in enumerate(trending, 1):
        lines.append(f"{i}. [{t['name']}]({t['url']}) — {t.get('summary', '')}")
    lines.append("")
    lines.append("**每日论文**")
    for p in papers[:4]:
        lines.append(f"- [{p.get('title_cn') or p['title']}]({p['url']})")
    lines.append("")
    lines.append("**快讯**")
    for n in news[:5]:
        lines.append(f"- [{n.get('title_cn') or n['title']}]({n['url']})")
    return "\n".join(lines)


# ---------- 主流程 ----------
def main():
    print(f"=== 生成 {DATE_STR} 早报 ===")
    trending = fetch_github_trending()
    papers = fetch_hf_papers()
    news = fetch_hackernews()
    print(f"抓取完成: trending={len(trending)}, papers={len(papers)}, hn={len(news)}")

    trending, papers, news = apply_summaries(trending, papers, news)

    html = build_html(trending, papers, news)
    os.makedirs(DOCS_DIR, exist_ok=True)
    index_path = os.path.join(DOCS_DIR, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    archive_dir = os.path.join(DOCS_DIR, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    with open(os.path.join(archive_dir, f"{DATE_STR}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"页面已生成: {index_path}")

    md = build_push_markdown(trending, papers, news)
    ok = push_wechat(f"AI 早报 {DATE_STR}", md)
    if not ok and PUSHPLUS_TOKEN:
        push_wechat(f"⚠️ 早报推送异常 {DATE_STR}", "正文推送失败, 请检查 GitHub Actions 日志。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        print(err)
        if PUSHPLUS_TOKEN:
            push_wechat(
                f"⚠️ 早报生成失败 {DATE_STR}",
                f"```\n{err[-1500:]}\n```\n请检查 GitHub Actions 运行日志。",
            )
        sys.exit(1)
