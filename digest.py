# ── Gemini 摘要（全部類別一次調用）──────────────────────────────
def summarize_all(feeds_items):
    """把所有類別合併成一次 Gemini 調用，節省配額"""
    sections = []
    for category, items in feeds_items.items():
        if not items:
            continue
        numbered = "\n".join(
            f"[{i}] {it['title']}" + (f"（{it['source']}）" if it["source"] else "")
            for i, it in enumerate(items)
        )
        sections.append(f"=== {category} ===\n{numbered}")

    prompt = f"""你是專業新聞編輯。以下是四個類別的最新新聞標題（過去48小時），每條前面有編號 [N]。請：
1. 每個類別篩選最值得關注的 3-5 條
2. 為每條撰寫 2-3 句繁體中文摘要
3. 只返回 JSON，格式如下：
{{"国际要闻":[{{"idx":0,"headline":"標題","summary":"摘要"}}],"香港新闻":[...],"汇丰银行":[...],"AI动态":[...]}}
4. 不要任何 Markdown 圍欄或額外說明

{chr(10).join(sections)}"""

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


# ── 主程序 ──────────────────────────────────────────────────────
def main():
    now     = datetime.now(timezone.utc)
    date    = now.strftime("%Y-%m-%d")
    weekday = WEEKDAYS[now.weekday()]
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

