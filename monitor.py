import json
import os
from datetime import date
import requests

HISTORY_FILE = "history.json"
TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

MONTHS_AHEAD = 12
PRICE_DROP_STEP = 30
MAX_VERIFY_PER_ROUTE = 3
TP_MARKET = "us"

# 先设为 1000 方便你立刻测试收到推送；测通后可改回你自己的心理价位（如 600~800）
WATCH_ROUTES = [
    {"name": "上海 -> 大阪", "from": "SHA", "to": "OSA", "max_price": 850, "max_stops": 0},
    {"name": "大阪 -> 上海", "from": "OSA", "to": "SHA", "max_price": 850, "max_stops": 0},
    {"name": "上海 -> 东京", "from": "SHA", "to": "TYO", "max_price": 850, "max_stops": 0},
    {"name": "东京 -> 上海", "from": "TYO", "to": "SHA", "max_price": 850, "max_stops": 0},
    {"name": "上海 -> 昆明", "from": "SHA", "to": "KMG", "max_price": 500, "max_stops": 0},
    {"name": "昆明 -> 上海", "from": "KMG", "to": "SHA", "max_price": 500, "max_stops": 0},
    {"name": "上海 -> 福冈", "from": "SHA", "to": "FUK", "max_price": 850, "max_stops": 0},
    {"name": "福冈 -> 上海", "from": "FUK", "to": "SHA", "max_price": 850, "max_stops": 0},
]

CITY_TO_AIRPORTS = {
    "SHA": "PVG,SHA",
    "OSA": "KIX,ITM",
    "TYO": "NRT,HND",
    "KMG": "KMG",
    "FUK": "FUK",
}

def get_next_months(n=12):
    today = date.today()
    y, m = today.year, today.month
    months = []
    for _ in range(n):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months

def scan_oneway_month(origin, destination, month):
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "one_way": "true",
        "direct": "false",
        "market": TP_MARKET,
        "currency": "cny",
        "sorting": "price",
        "limit": 1000,
        "token": TP_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("data", [])

def verify_with_serpapi(origin_airport, dest_airport, dep_date, max_stops):
    if not SERPAPI_KEY:
        return None
    params = {
        "engine": "google_flights",
        "departure_id": origin_airport,
        "arrival_id": dest_airport,
        "outbound_date": dep_date,
        "currency": "CNY",
        "hl": "zh-cn",
        "type": 2,
        "stops": 1 if max_stops == 0 else 0,
        "api_key": SERPAPI_KEY,
    }
    try:
        resp = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
        if resp.status_code != 200:
            print(f"  [SerpApi] HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        candidates = [c for c in (data.get("best_flights", []) + data.get("other_flights", [])) if "price" in c]
        if not candidates:
            return None
        cheapest = min(candidates, key=lambda x: x["price"])
        return {
            "price": cheapest["price"],
            "url": data.get("search_metadata", {}).get("google_flights_url", ""),
        }
    except Exception as e:
        print(f"  [SerpApi] 异常: {e}")
        return None

def send_notification(text):
    sent_ok = False
    if TG_BOT_TOKEN and TG_CHAT_ID:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        print(f"  [Telegram 推送响应] HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        sent_ok = True
    if FEISHU_WEBHOOK:
        resp = requests.post(FEISHU_WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
        print(f"  [飞书推送响应] HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        sent_ok = True
    return sent_ok

def main():
    if not (FEISHU_WEBHOOK or (TG_BOT_TOKEN and TG_CHAT_ID)):
        raise RuntimeError("未检测到通知配置！请检查 GitHub Secrets 中的 TG_BOT_TOKEN 和 TG_CHAT_ID。")

    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)

    today_str = date.today().isoformat()
    history = {k: v for k, v in history.items() if v.get("dep_date", "9999-99-99") >= today_str}
    print(f"当前有效历史记录数: {len(history)}")

    months = get_next_months(MONTHS_AHEAD)
    updated = False

    for route in WATCH_ROUTES:
        raw_total = 0
        stops_ok_total = 0
        min_seen = None
        candidates = []

        for month in months:
            try:
                items = scan_oneway_month(route["from"], route["to"], month)
            except Exception as e:
                print(f"[{route['name']}] {month} 查询失败: {e}")
                continue

            raw_total += len(items)
            for item in items:
                if item.get("transfers", 0) > route["max_stops"]:
                    continue
                stops_ok_total += 1

                dep_date = item["departure_at"][:10]
                if dep_date < today_str:
                    continue

                if min_seen is None or item["price"] < min_seen[0]:
                    min_seen = (item["price"], dep_date)

                if item["price"] <= route["max_price"]:
                    short_link = item.get("link", "").split("?")[0]
                    candidates.append({
                        "dep_date": dep_date,
                        "price": item["price"],
                        "origin_airport": item.get("origin_airport") or CITY_TO_AIRPORTS[route["from"]],
                        "dest_airport": item.get("destination_airport") or CITY_TO_AIRPORTS[route["to"]],
                        "airline": item.get("airline", ""),
                        "transfers": item.get("transfers", 0),
                        "link": "https://www.aviasales.com" + short_link,
                    })

        print(f"[{route['name']}] 12个月原始缓存={raw_total}条 | 符合经停={stops_ok_total}条 | 最低价={min_seen} | 低于¥{route['max_price']}={len(candidates)}条")

        candidates.sort(key=lambda x: x["price"])
        seen_dates = set()
        unique_deals = []
        skipped_by_history = 0

        for c in candidates:
            if c["dep_date"] in seen_dates:
                continue
            seen_dates.add(c["dep_date"])

            key = f"{route['from']}-{route['to']}-{c['dep_date']}"
            last_price = history.get(key, {}).get("price")
            if last_price is None or c["price"] <= last_price - PRICE_DROP_STEP:
                c["key"] = key
                unique_deals.append(c)
            else:
                skipped_by_history += 1

        if skipped_by_history > 0:
            print(f"  -> 有 {skipped_by_history} 个日期因 history.json 已记录且未再降价而被跳过")

        if not unique_deals:
            continue

        lines = []
        pending_history = []
        verified_count = 0

        for d in unique_deals:
            final_price = d["price"]
            url_to_show = d["link"]

            if SERPAPI_KEY and verified_count < MAX_VERIFY_PER_ROUTE:
                verified_count += 1
                real = verify_with_serpapi(d["origin_airport"], d["dest_airport"], d["dep_date"], route["max_stops"])
                if real:
                    if real["price"] > route["max_price"] * 1.1:
                        print(f"  -> {d['dep_date']} 缓存过期 (¥{d['price']} -> 实时¥{real['price']})")
                        continue
                    final_price = real["price"]
                    if real["url"]:
                        url_to_show = real["url"]

            pending_history.append((d["key"], final_price, d["dep_date"]))
            if len(lines) < 8:
                stop_txt = "直飞" if d["transfers"] == 0 else f"{d['transfers']}经停"
                lines.append(
                    f"• {d['dep_date']} | ¥{final_price} | {d['origin_airport']}→{d['dest_airport']} ({d['airline']} {stop_txt})\n  {url_to_show}"
                )

        if lines:
            extra = f"\n（另有 {len(pending_history) - len(lines)} 个特价日期已同步记录）" if len(pending_history) > len(lines) else ""
            msg = (
                f"✈️ 单程特价汇总：{route['name']}（≤¥{route['max_price']}）\n"
                + "\n".join(lines)
                + extra
            )
            if send_notification(msg):
                for k, p, dt in pending_history:
                    history[k] = {"price": p, "dep_date": dt}
                updated = True

    if updated:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
