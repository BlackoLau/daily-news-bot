"""
每日要聞摘要推送
GitHub Actions 排程執行：每天 GMT 00:00
依賴：feedparser, requests, google-generativeai
"""
import os, re, json, time, email.utils
import feedparser, requests
import google.generativeai as genai
from datetime import datetime, timezone, timedelta

# ── 設定（從 GitHub Secrets 讀取）──────────────────────────────
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ── Google News RSS 搜索 URL ────────────────────────────────────
RSS_FEEDS = {
    "国际要闻": (
        "https://news.google.com/rss/search"
        "?q=world+news+when:2d"
        "&hl=en&gl=US&ceid=US:en"
    ),
    "香港新闻": (
        "https://news.google.com/rss/search"
        '?q="Hong+Kong"+government+OR+policy+OR+law+OR+economy+when:2d'
        "&hl=zh-TW&gl=HK&ceid=HK:zh-Hant"
    ),
    "汇丰银行": (
        "https://news.google.com/rss/search"
        '?q="HSBC"+"Hong+Kong"+when:2d'
        "&hl=en&gl=HK&ceid=HK:en"
    ),
    "AI动态": (
        "https://news.google.com/rss/search"
        "?q=artificial+intelligence+OR+%22large+language+model%22+OR+OpenAI+OR+Anthropic+OR+Gemini+when:2d"
        "&hl=en&gl=US&ceid=US:en"
    ),
}

WEEKDAYS  = ["一", "二", "三", "四", "五", "六", "日"]
EMOJIS    = {"国际要闻": "🌐", "香港新闻": "🏙️", "汇丰银行": "🏦", "AI动态": "🤖"}
TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16749490, 16478047]


# ── 相對時間 ────────────────────────────────────────────────────
def relative_time(pub):
    if pub is None:
        return "時間未知"
    now = datetime.now(timezone.utc)
    minutes = int((now - pub).total_seconds() / 60)
    if minutes < 60:
        return f"{minutes} 分鐘前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小時前"
    return f"{hours // 24} 天前"


# ── RSS 抓取 ────────────────────────────────────────────────────
def fetch_rss(url, max_items=10):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  RSS fetch error: {e}")
        return []

    items = []
    for entry in feed.entries:
        pub_dt = None
        try:
            pub_dt = email.utils.parsedate_to_datetime(entry.get("published", ""))
            if pub_dt < cutoff:
                continue
        except Exception:
            pass

        raw_title = entry.get("title", "")
        title = re.sub(r"\s*-\s*[^-]+$", "", raw_title).strip()

        source = ""
        if entry.get("source"):
            source = entry["source"].get("title", "")
        if not source:
            m = re.search(r"-\s*([^-]+)$", raw_title)
            if m:
                source = m.group(1).strip()

        items.append({
            "title":   title,
            "source":  source,
            "pub_dt":  pub_dt,
            "pub_str": relative_time(pub_dt),
        })
        if len(items) >= max_items:
            break

    return items


# ── Gemini 摘要（全部類別一次調用，節省配額）────────────────────
def summarize_all(feeds_items):
    sections = []
    for category, items in feeds_items.items():
        if not items:
            continue
        numbered = "\n".join(
            f"[{i}] {it['title']}" + (f"（{it['source']}）" if it["source"] else "")
            for i, it in enumerate(items)
        )
        sections.append(f"=== {category} ===\n{numbered}")

    prompt = """你是專業新聞編輯。以下是四個類別的最新新聞標題（過去48小時），每條前面有編號 [N]。請：
1. 每個類別篩選最值得關注的 3-5 條
2. 為每條撰寫 2-3 句繁體中文摘要，說明事件背景、重點和影響
3. 只返回 JSON，格式如下（idx 必須填原文編號）：
{"国际要闻":[{"idx":0,"headline":"標題","summary":"摘要"}],"香港新闻":[...],"汇丰银行":[...],"AI动态":[...]}
4. 不要任何 Markdown 圍欄或額外說明

""" + "\n\n".join(sections)

    for attempt in range(3):
        try:
            resp = model.generate_content(prompt)
            text = re.sub(r"```json\s*|```\s*", "", resp.text.strip()).strip()
            parsed = json.loads(text)
            result = {}
            for category, items_raw in parsed.items():
                orig = feeds_items.get(category, [])
                result[category] = []
                for item in items_raw:
                    idx = item.get("idx")
                    o = orig[idx] if (isinstance(idx, int) and 0 <= idx < len(orig)) else None
                    result[category].append({
                        "headline": item.get("headline", ""),
                        "summary":  item.get("summary", ""),
                        "source":   o["source"] if o else "",
                        "pub_str":  o["pub_str"] if o else "",
                    })
            return result
        except Exception as e:
            print(f"  Gemini attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

    # 降級：直接用標題
    return {
        cat: [{"headline": it["title"], "summary": "（摘要生成失敗）",
               "source": it["source"], "pub_str": it["pub_str"]} for it in items[:3]]
        for cat, items in feeds_items.items()
    }


# ── Telegram 工具函數 ───────────────────────────────────────────
def _esc(text):
    return re.sub(r"([_*\[\]()~`>#+=|{}.!\-\\])", r"\\\1", str(text))


def tg_api(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def create_topic(name, color_index=0):
    result = tg_api("createForumTopic", {
        "chat_id":    TELEGRAM_CHAT_ID,
        "name":       name,
        "icon_color": TOPIC_COLORS[color_index % len(TOPIC_COLORS)],
    })
    thread_id = result["result"]["message_thread_id"]
    print(f"  Topic 已創建：{name}（thread_id={thread_id}）")
    return thread_id


def send_to_topic(text, thread_id):
    return tg_api("sendMessage", {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "message_thread_id":        thread_id,
        "text":                     text,
        "parse_mode":               "MarkdownV2",
        "disable_web_page_preview": True,
    })


# ── 消息構建 ────────────────────────────────────────────────────
def build_message(date, weekday, digest):
    lines = [f"📰 *{_esc(date)}（週{weekday}）每日要聞*"]
    lines.append("_由 Google News \\+ Gemini 免費版整理_\n")

    for category, items in digest.items():
        emoji = EMOJIS.get(category, "📌")
        lines.append(f"*{emoji} {_esc(category)}*")
        for i, item in enumerate(items, 1):
            lines.append(f"{i}\\. *{_esc(item['headline'])}*")
            meta_parts = []
            if item.get("source"):
                meta_parts.append(item["source"])
            if item.get("pub_str"):
                meta_parts.append(item["pub_str"])
            if meta_parts:
                lines.append(f"_📡 {_esc(' · '.join(meta_parts))}_")
            lines.append(_esc(item["summary"]))
            lines.append("")
        lines.append("")

    lines.append("_💬 在此 Topic 內直接發問可向 AI 追問_")
    return "\n".join(lines)


# ── 主程序 ──────────────────────────────────────────────────────
def main():
    now       = datetime.now(timezone.utc)
    date      = now.strftime("%Y-%m-%d")
    weekday   = WEEKDAYS[now.weekday()]
    color_idx = now.timetuple().tm_yday

    # 1. 抓取所有 RSS
    feeds_items = {}
    for category, url in RSS_FEEDS.items():
        print(f"\n[{category}] 抓取 RSS...")
        items = fetch_rss(url)
        print(f"  找到 {len(items)} 篇")
        feeds_items[category] = items

    # 2. 一次過生成所有摘要（只用 1 次 Gemini 配額）
    print("\n生成摘要（合併調用）...")
    digest = summarize_all(feeds_items)

    # 3. 建立今日 Topic
    print("\n建立 Telegram Topic...")
    topic_name = f"📅 {date}（週{weekday}）"
    thread_id = create_topic(topic_name, color_idx)

    # 4. 發送摘要
    message = build_message(date, weekday, digest)
    result = send_to_topic(message, thread_id)
    print(f"✅ 推送成功，message_id={result['result']['message_id']}")


if __name__ == "__main__":
    main()
