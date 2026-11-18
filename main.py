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
# 免费模型不稳定(限流/下线/输出为空), 依次回退尝试
FALLBACK_MODELS = [
    OPENROUTER_MODEL,
    "nvidia/nemotron-3-super-120b-a12b:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "liquid/lfm-2.5-2.6b:free",
    "google/gemma-4-31b-it:free",
]
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
        href = a.get("href", "").strip("/")
        # 直接取 href(形如 owner/repo), 避免文本里已有的 "/" 被再次拼接成双斜杠
        name = re.sub(r"/+", "/", href)
        url = "https://github.com/" + name
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
        data = None
        for model in FALLBACK_MODELS:
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 4000,
                    },
                    timeout=120,
                )
                if resp.status_code == 429:
                    print(f"[llm] {model} 限流(429), 跳过")
                    continue
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if not content and msg.get("reasoning"):
                    # 推理模型可能把正文写进 reasoning, 尝试提取
                    content = msg["reasoning"].strip()
                if not content:
                    print(f"[llm] {model} 返回空内容, 跳过")
                    continue
                m = re.search(r"\{[\s\S]*\}", content)
                if not m:
                    print(f"[llm] {model} 未返回JSON, 跳过")
                    continue
                try:
                    # raw_decode: 解析第一个完整 JSON 对象, 忽略尾部多余文本
                    data = json.JSONDecoder().raw_decode(m.group(0))[0]
                except json.JSONDecodeError:
                    print(f"[llm] {model} JSON 解析失败, 跳过")
                    continue
                if data.get("trending") or data.get("papers") or data.get("news"):
                    print(f"[llm] 使用模型: {model}")
                    return data
                print(f"[llm] {model} JSON 内容为空, 跳过")
            except Exception as me:
                print(f"[llm] {model} 失败: {me}")
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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23E8442E'/%3E%3Crect x='14' y='16' width='36' height='6' fill='%23fff'/%3E%3Crect x='14' y='29' width='36' height='4' fill='%23fff'/%3E%3Crect x='14' y='38' width='24' height='4' fill='%23fff'/%3E%3Crect x='14' y='47' width='30' height='4' fill='%23fff'/%3E%3C/svg%3E">
<title>AI 每日早报 · {date}</title>
<style>
:root {{
  --ink: #2B2A27;
  --ink-soft: #55534E;
  --muted: #8A867E;
  --paper: #FFFFFF;
  --bg: #F7F6F3;
  --line: #E6E2DA;
  --accent: #8A6A4F;
  --accent-soft: #F2EDE7;
  --serif: "Songti SC", "STSong", "Noto Serif SC", "Source Han Serif SC", "Noto Serif CJK SC", SimSun, Georgia, serif;
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
.masthead {{ background: var(--paper); border-bottom: 1px solid var(--line); position: relative; overflow: hidden; }}
.masthead-top {{ height: 3px; background: var(--accent); opacity: .45; }}
.masthead-inner {{ display:flex; justify-content:space-between; align-items:flex-end; padding-top: 56px; padding-bottom: 40px; gap: 40px; flex-wrap: wrap; }}
.kicker {{ display:inline-flex; align-items:center; font-size:12px; font-weight:600; letter-spacing:.42em; color: var(--muted); margin-bottom: 18px; }}
.kicker::before {{ content:""; width:24px; height:1px; background: var(--accent); display:inline-block; margin-right: 12px; }}
h1 {{ font-family: var(--serif); font-size: clamp(44px, 6.6vw, 78px); font-weight: 600; line-height: 1.08; letter-spacing: .06em; }}
h1 .thin {{ font-weight: 300; color: var(--ink-soft); }}
.masthead-meta {{ display:flex; flex-direction:column; gap:8px; align-items:flex-start; padding-bottom: 8px; }}
.meta-chip {{ border:none; background:none; padding:0; font-size: 13px; font-weight: 500; letter-spacing:.04em; color: var(--muted); box-shadow:none; }}
.meta-chip.hot {{ color: var(--ink); font-weight: 600; }}
.deco {{ display:none; }}

/* ===== 板块标题 ===== */
.section {{ margin-top: 72px; }}
.sec-head {{ display:flex; align-items:baseline; gap:18px; margin-bottom: 28px; padding-bottom: 16px; border-bottom: 1px solid var(--line); }}
.sec-no {{ font-family: var(--serif); font-size:16px; font-weight:600; color: var(--accent); letter-spacing:.08em; }}
.sec-title {{ font-family: var(--serif); font-size: clamp(26px, 3.2vw, 34px); font-weight:600; letter-spacing:.06em; color: var(--ink); }}
.sec-en {{ font-size:11px; font-weight:600; letter-spacing:.28em; color: var(--muted); text-transform:uppercase; margin-left:auto; }}

/* ===== Trending 卡片 ===== */
.tcard {{ display:flex; gap:0; background: transparent; border-bottom: 1px solid var(--line); padding: 24px 0; transition: background .15s ease; }}
.tcard:hover {{ background: #FBFAF8; }}
.tcard-rank {{ flex: 0 0 74px; display:flex; align-items:flex-start; justify-content:flex-start; font-family: var(--serif); font-size: 26px; font-weight: 500; color: #C9C4BA; padding-top: 2px; font-variant-numeric: tabular-nums; }}
.tcard-body {{ padding-right: 8px; flex:1; min-width:0; }}
.tcard-head {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:8px; }}
.tcard-name {{ font-size: 18px; font-weight: 700; color: var(--ink); letter-spacing:.01em; }}
.tcard-name:hover {{ color: var(--accent); }}
.tcard-sum {{ font-size: 14.5px; line-height: 1.85; color: var(--ink-soft); margin-bottom: 12px; max-width: 80ch; }}
.tcard-meta {{ display:flex; gap:8px; flex-wrap:wrap; }}
.chip {{ display:inline-block; font-size:11.5px; font-weight:500; padding:3px 9px; border:1px solid var(--line); color: var(--muted); background:#FCFBF9; letter-spacing:.02em; }}
.chip-lang {{ background: var(--accent-soft); border-color: transparent; color: #7A5C43; }}
.chip-star {{ background: transparent; border-color: transparent; color: var(--ink-soft); }}
.chip-today {{ background: transparent; border-color: #E3D5C8; color: var(--accent); }}
.chip-plain {{ background: transparent; }}

/* ===== 论文 / 快讯卡片 ===== */
.grid {{ display:grid; gap:18px; }}
.grid-3 {{ grid-template-columns: repeat(3, 1fr); }}
.grid-2 {{ grid-template-columns: repeat(2, 1fr); }}
.pcard {{ display:block; background: var(--paper); border: 1px solid var(--line); padding: 26px 24px; transition: border-color .15s ease, background .15s ease; }}
.pcard:hover {{ border-color: #D8D2C6; background: #FCFBF9; }}
.ptag {{ display:inline-block; font-size:10.5px; font-weight:600; letter-spacing:.22em; padding:0; margin-bottom:14px; background:none; color: var(--muted); }}
.ptag.red {{ background:none; color: var(--muted); }}
.ptag.dark {{ background:none; color: var(--muted); }}
.pcard h3 {{ font-family: var(--serif); font-size:17px; font-weight:600; line-height:1.6; margin-bottom:10px; letter-spacing:.02em; }}
.pcard p {{ font-size:13.5px; line-height:1.8; color: var(--ink-soft); }}
.pcard .src {{ display:block; margin-top:16px; font-size:10.5px; font-weight:500; letter-spacing:.16em; color:#B3AEA4; }}

/* ===== 页脚 ===== */
.footer {{ margin-top: 88px; background: var(--paper); border-top: 1px solid var(--line); color: var(--ink); }}
.footer-inner {{ display:flex; justify-content:space-between; align-items:center; gap:20px; padding: 38px 0; flex-wrap:wrap; }}
.footer .big {{ font-family: var(--serif); font-size:20px; font-weight:600; letter-spacing:.08em; }}
.footer .small {{ font-size:12px; color: var(--muted); line-height:1.9; }}
.footer .red {{ color: var(--accent); }}

@media (max-width: 860px) {{
  .grid-3 {{ grid-template-columns: 1fr 1fr; }}
  .grid-2 {{ grid-template-columns: 1fr; }}
  .masthead-inner {{ flex-direction: column; align-items: flex-start; gap: 20px; padding-top: 40px; padding-bottom: 28px; }}
  .masthead-meta {{ padding-bottom: 0; }}
  .meta-chip {{ max-width: 100%; overflow-wrap: anywhere; }}
}}
@media (max-width: 600px) {{
  .grid-3 {{ grid-template-columns: 1fr; }}
  .tcard-rank {{ flex-basis: 60px; font-size: 28px; }}
  .masthead-inner {{ padding-top: 30px; padding-bottom: 26px; }}
}}
@media print {{
  .tcard, .pcard {{ box-shadow:none; break-inside: avoid; }}
}}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead-top"></div>
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
      <div class="small">自动化生成 · 每日 10:00 前后自动更新 · 摘要由开源模型生成</div>
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
def push_wechat(title, content, template="markdown"):
    if not PUSHPLUS_TOKEN:
        print("[push] 未配置 PUSHPLUS_TOKEN, 跳过推送")
        return False
    for attempt in range(3):
        try:
            resp = requests.post(
                "http://www.pushplus.plus/send",
                json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": template},
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


def build_push_card(trending, papers, news):
    """微信推送 HTML 卡片(报纸风, 全内联样式, 匹配看板配色)"""
    INK, PAPER, BG, RED, BLUE, YELLOW, MUTED = "#14161A", "#FFFFFF", "#F6F5F1", "#E8442E", "#1D4ED8", "#FFC53D", "#8a8f98"

    def chip(text, bg, color=INK, border="transparent"):
        return (f'<span style="display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;'
                f'border-radius:4px;background:{bg};color:{color};border:1px solid {border};margin:2px 4px 2px 0;">{text}</span>')

    H = []
    # 头部
    H.append(
        f'<div style="max-width:640px;margin:0 auto;background:{BG};border-radius:12px;overflow:hidden;'
        f'font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;border:1px solid #e5e2da;">'
        f'<div style="background:{INK};padding:22px 20px 18px;">'
        f'<div style="color:{YELLOW};font-size:11px;font-weight:800;letter-spacing:4px;">DAILY BRIEFING</div>'
        f'<div style="color:{PAPER};font-size:24px;font-weight:900;margin-top:6px;">AI 每日早报 <span style="color:{RED};">●</span></div>'
        f'<div style="margin-top:10px;">'
        + chip(f"{DATE_STR} · 星期{WEEK_CN}", RED, PAPER)
        + chip(f"{TIME_STR} 生成", "#2a2d34", "#cfd3da")
        + '</div></div>'
    )
    # 看板按钮
    if DASHBOARD_URL:
        H.append(
            f'<div style="padding:14px 16px;background:{PAPER};border-bottom:1px dashed #d8d4c8;">'
            f'<a href="{DASHBOARD_URL}" style="display:block;text-align:center;background:{INK};color:{YELLOW};'
            f'font-weight:800;font-size:15px;padding:10px;border-radius:6px;text-decoration:none;">📰 查看完整看板</a></div>'
        )
    # GitHub 热门
    H.append(f'<div style="padding:6px 16px 16px;">')
    H.append(f'<div style="margin:14px 0 2px;font-size:17px;font-weight:900;color:{INK};">🔥 GitHub 热门</div>')
    for i, t in enumerate(trending, 1):
        chips = ""
        if t.get("lang"):
            chips += chip(escape(t["lang"]), YELLOW)
        if t.get("stars"):
            chips += chip(f"★ {escape(t['stars'])}", INK, PAPER)
        if t.get("today"):
            chips += chip(f"今日 +{escape(t['today'])}", RED, PAPER)
        summary = escape(t.get("summary", ""))
        H.append(
            f'<div style="background:{PAPER};border:1px solid {INK};border-radius:8px;padding:12px 14px;margin-top:10px;">'
            f'<div><span style="color:{RED};font-weight:900;font-size:15px;margin-right:8px;">{i:02d}</span>'
            f'<a href="{escape(t["url"])}" style="font-family:Consolas,monospace;font-weight:800;font-size:15px;color:{INK};text-decoration:none;">{escape(t["name"])}</a></div>'
            + (f'<div style="margin-top:6px;">{chips}</div>' if chips else "")
            + f'<div style="margin-top:8px;font-size:13px;color:#3a3f47;line-height:1.65;">{summary}</div></div>'
        )
    # 每日论文
    H.append(f'<div style="margin:20px 0 2px;font-size:17px;font-weight:900;color:{INK};">📄 每日论文</div>')
    for p in papers[:4]:
        title = escape(p.get("title_cn") or p["title"])
        summary = escape(p.get("summary", ""))
        summary_html = f'<div style="margin-top:6px;font-size:12.5px;color:#3a3f47;line-height:1.6;">{summary}</div>' if summary else ""
        H.append(
            f'<div style="background:{PAPER};border:1px solid {INK};border-radius:8px;padding:12px 14px;margin-top:10px;">'
            f'<div>{chip("PAPER", BLUE, PAPER)}'
            f'<a href="{escape(p["url"])}" style="font-weight:800;font-size:14px;color:{INK};text-decoration:none;line-height:1.5;">{title}</a></div>'
            f'{summary_html}</div>'
        )
    # 快讯
    H.append(f'<div style="margin:20px 0 2px;font-size:17px;font-weight:900;color:{INK};">⚡ 技术 / AI 快讯</div>')
    for n in news[:5]:
        title = escape(n.get("title_cn") or n["title"])
        is_ai = n.get("tag") == "AI"
        tag = chip(n.get("tag", "TECH"), RED if is_ai else INK, PAPER)
        H.append(
            f'<div style="background:{PAPER};border:1px solid {INK};border-radius:8px;padding:10px 14px;margin-top:10px;">'
            f'<div>{tag}'
            f'<a href="{escape(n["url"])}" style="font-weight:700;font-size:14px;color:{INK};text-decoration:none;line-height:1.5;">{title}</a></div></div>'
        )
    H.append('</div>')
    # 页脚
    H.append(
        f'<div style="background:{INK};color:{MUTED};font-size:11px;padding:14px 16px;line-height:1.8;">'
        f'数据源: GitHub Trending · HuggingFace Papers · HackerNews<br>'
        f'摘要由开源模型生成 · {DATE_STR} 星期{WEEK_CN}</div></div>'
    )
    return "".join(H)


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

    card = build_push_card(trending, papers, news)
    ok = push_wechat(f"AI 早报 {DATE_STR}", card, template="html")
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
