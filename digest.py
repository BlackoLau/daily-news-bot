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

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]
EMOJIS   = {"国际要闻": "🌐", "香港新闻": "🏙️", "汇丰银行": "🏦", "AI动态": "🤖"}


# ── 工具：把發布時間轉成「X 小時前」──────────────────────────────
def relative_time(pub):
    if pub is None:
        return "時間未知"
    now = datetime.now(timezone.utc)
    diff = now - pub
    minutes = int(diff.total_seconds() / 60)
    if minutes < 60:
        return f"{minutes} 分鐘前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小時前"
    days = hours // 24
    return f"{days} 天前"


# ── RSS 抓取 ────────────────────────────────────────────────────
def fetch_rss(url, max_items=10):
    """抓取 RSS，返回帶有來源和發布時間的文章列表"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  RSS fetch error: {e}")
        return []

    items = []
    for entry in feed.entries:
        # 解析發布時間
        pub_dt = None
        try:
            pub_dt = email.utils.parsedate_to_datetime(entry.get("published", ""))
            if pub_dt < cutoff:
                continue  # 過濾掉48小時以前的文章
        except Exception:
            pass  # 無法解析日期則不過濾，保留文章

        # 去掉標題末尾的「- 來源名」（Google News RSS 的格式）
        raw_title = entry.get("title", "")
        title = re.sub(r"\s*-\s*[^-]+$", "", raw_title).strip()

        # 來源名稱：優先從 entry.source 取，備選從標題末尾提取
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


# ── Gemini 摘要 ─────────────────────────────────────────────────
def summarize(category, items):
    """讓 Gemini 篩選並撰寫摘要，返回帶來源和時間的結果"""
    if not items:
        return [{"headline": "暫無新消息", "summary": "過去48小時未找到相關報道。",
                 "source": "", "pub_str": ""}]

    # 給每條新聞加上編號，讓 Gemini 引用 idx
    numbered = "\n".join(
        f"[{i}] {it['title']}" + (f"（{it['source']}）" if it["source"] else "")
        for i, it in enumerate(items)
    )

    prompt = f"""你是專業新聞編輯。以下是「{category}」最新新聞標題（過去48小時），每條前面有編號 [N]。請：
1. 從中篩選最值得關注的 3-5 條（去除重複、低價值內容）
2. 為每條撰寫 2-3 句繁體中文摘要，說明事件背景、重點和影響
3. 只返回 JSON，格式如下（idx 必須填原文編號）：
{{"items":[{{"idx":0,"headline":"原標題","summary":"摘要"}}]}}
4. 不要任何 Markdown 圍欄或額外說明

新聞標題：
{numbered}"""

    for attempt in range(3):
        try:
            resp = model.generate_content(prompt)
            text = re.sub(r"```json\s*|```\s*", "", resp.text.strip()).strip()
            parsed = json.loads(text)["items"]

            result = []
            for item in parsed:
                idx = item.get("idx")
                orig = items[idx] if (isinstance(idx, int) and 0 <= idx < len(items)) else None
                result.append({
                    "headline": item.get("headline", ""),
                    "summary":  item.get("summary", ""),
                    "source":   orig["source"] if orig else "",
                    "pub_str":  orig["pub_str"] if orig else "",
                })
            return result

        except Exception as e:
            print(f"  Gemini attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

    # 降級：直接用標題，保留來源和時間
    return [{
        "headline": it["title"],
        "summary":  "（摘要生成失敗）",
        "source":   it["source"],
        "pub_str":  it["pub_str"],
    } for it in items[:3]]


# ── Telegram 推送 ───────────────────────────────────────────────
def _esc(text):
    """MarkdownV2 特殊字符轉義"""
    return re.sub(r"([_*\[\]()~`>#+=|{}.!\-\\])", r"\\\1", str(text))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def build_message(date, weekday, digest):
    lines = [f"📰 *{_esc(date)}（週{weekday}）每日要聞*"]
    lines.append("_由 Google News \\+ Gemini 免費版整理_\n")

    for category, items in digest.items():
        emoji = EMOJIS.get(category, "📌")
        lines.append(f"*{emoji} {_esc(category)}*")

        for i, item in enumerate(items, 1):
            # 標題
            lines.append(f"{i}\\. *{_esc(item['headline'])}*")

            # 來源 + 相對時間（斜體小字）
            meta_parts = []
            if item.get("source"):
                meta_parts.append(item["source"])
            if item.get("pub_str"):
                meta_parts.append(item["pub_str"])
            if meta_parts:
                lines.append(f"_📡 {_esc(' · '.join(meta_parts))}_")

            # 摘要
            lines.append(_esc(item["summary"]))
            lines.append("")  # 條目間空行

        lines.append("")  # 類別間空行

    lines.append("_💬 直接回覆此訊息可向 AI 追問_")
    return "\n".join(lines)


# ── 主程序 ──────────────────────────────────────────────────────
def main():
    now     = datetime.now(timezone.utc)
    date    = now.strftime("%Y-%m-%d")
    weekday = WEEKDAYS[now.weekday()]

    digest = {}
    for category, url in RSS_FEEDS.items():
        print(f"\n[{category}] 抓取 RSS...")
        items = fetch_rss(url)
        print(f"  找到 {len(items)} 篇，正在生成摘要...")
        digest[category] = summarize(category, items)
        time.sleep(1)  # 避免觸發 Gemini rate limit

    message = build_message(date, weekday, digest)
    print("\n發送到 Telegram...")
    result = send_telegram(message)
    print(f"✅ 成功，message_id={result['result']['message_id']}")


if __name__ == "__main__":
    main()
