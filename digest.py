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
GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

# ── Google News RSS 搜索 URL ────────────────────────────────────
# 關鍵：用 when:2d 限定過去48小時；用引號做精確短語匹配
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

# ── RSS 抓取 ────────────────────────────────────────────────────
def fetch_rss(url: str, max_items: int = 10) -> list[dict]:
    """抓取 RSS 並返回過去48小時的文章"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  RSS fetch error: {e}")
        return []

    items = []
    for entry in feed.entries:
        try:
            pub = email.utils.parsedate_to_datetime(entry.get("published", ""))
            if pub < cutoff:
                continue
        except Exception:
            pass  # 無法解析日期則不過濾

        title = re.sub(r"\s*-\s*[\w\s]+$", "", entry.title)  # 去掉 "- 來源名"
        source = (entry.get("source") or {}).get("title", "")
        items.append({"title": title, "source": source, "link": entry.link})
        if len(items) >= max_items:
            break

    return items


# ── Gemini 摘要 ─────────────────────────────────────────────────
def summarize(category: str, items: list[dict]) -> list[dict]:
    """讓 Gemini 從標題中篩選並撰寫繁體中文摘要"""
    if not items:
        return [{"headline": "暫無新消息", "summary": "過去48小時未找到相關報道。"}]

    headlines = "\n".join(
        f"- {it['title']}" + (f"（{it['source']}）" if it["source"] else "")
        for it in items
    )

    prompt = f"""你是專業新聞編輯。以下是「{category}」最新新聞標題（過去48小時），請：
1. 從中篩選最值得關注的 3-5 條（去除重複、低價值內容）
2. 為每條撰寫 2-3 句繁體中文摘要，說明事件背景、重點和影響
3. 只返回 JSON，格式：{{"items":[{{"headline":"原標題","summary":"摘要"}}]}}
4. 不要任何 Markdown 圍欄或額外說明

新聞標題：
{headlines}"""

    for attempt in range(3):
        try:
            resp = model.generate_content(prompt)
            text = resp.text.strip().lstrip("```json").rstrip("```").strip()
            return json.loads(text)["items"]
        except Exception as e:
            print(f"  Gemini attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)

    return [{"headline": item["title"], "summary": "（摘要生成失敗）"} for item in items[:3]]


# ── Telegram 推送 ───────────────────────────────────────────────
def _esc(text: str) -> str:
    """MarkdownV2 轉義"""
    return re.sub(r"([_*\[\]()~`>#+=|{}.!\-\\])", r"\\\1", str(text))


def send_telegram(text: str) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def build_message(date: str, weekday: str, digest: dict) -> str:
    lines = [f"📰 *{_esc(date)}（週{weekday}）每日要聞*\n"]
    lines.append("_由 Google News \\+ Gemini 免費版整理_\n")

    for category, items in digest.items():
        emoji = EMOJIS.get(category, "📌")
        lines.append(f"\n*{emoji} {_esc(category)}*")
        for i, item in enumerate(items, 1):
            lines.append(f"{i}\\. *{_esc(item['headline'])}*")
            lines.append(_esc(item["summary"]) + "\n")

    lines.append("\n_💬 直接回覆此訊息可向 AI 追問_")
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
