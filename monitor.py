import json
import os
from datetime import date
import requests

HISTORY_FILE = "history.json"
TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")  # 可选：留空则仅使用 Travelpayouts 推送
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

MONTHS_AHEAD = 12
PRICE_DROP_STEP = 30          # 同日期降价至少 30 元才再次推送
MAX_VERIFY_PER_ROUTE = 3      # 每条航线每次最多用 SerpApi 复核前 3 个最低价，省额度

# 4 个单向监控方向（可按直飞/中转和税费差异微调阈值）
WATCH_ROUTES = [
    {"name": "上海 -> 大阪", "from": "SHA", "to": "OSA", "max_price": 600, "max_stops": 0},
    {"name": "大阪 -> 上海", "from": "OSA", "to": "SHA", "max_price": 750, "max_stops": 0},
    {"name": "上海 -> 东京", "from": "SHA", "to": "TYO", "max_price": 650, "max_stops": 0},
    {"name": "东京 -> 上海", "from": "TYO", "to": "SHA", "max_price": 800, "max_stops": 0},
]

# 仅当 Travelpayouts 未返回具体机场时的兜底映射
CITY_TO_AIRPORTS = {
    "SHA": "PVG,SHA",
    "OSA": "KIX,ITM",
    "TYO": "NRT,HND",
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

def scan_oneway_month(origin, destination, month, direct_only):
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": month,
        "one_way": "true",
        "direct": "true" if direct_only else "false",
        "currency": "cny",
        "sorting": "price",
        "limit": 1000,
        "token": TP_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("data", [])

def verify_with_serpapi(origin_airport, dest_airport, dep_date, max_stops):
    """用具体机场代码在 Google Flights 上复核实时价格"""
    if not SERPAPI_KEY:
        return None
    params = {
        "engine": "google_flights",
        "departure_id": origin_airport,
        "arrival_id": dest_airport,
        "outbound_date": dep_date,
        "currency": "CNY",
        "hl": "zh-cn",
        "type": 2,  # 单程
        "stops": 1 if max_stops == 0 else 0,  # SerpApi: 1=仅直飞, 0=不限
        "api_key": SERPAPI_KEY,
    }
    try:
        resp = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
        if resp.status_code != 200:
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
    except Exception:
        return None

def send_notification(text):
    if TG_BOT_TOKEN and TG_CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    if FEISHU_WEBHOOK:
        requests.post(FEISHU_WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=15)

def main():
    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)

    # 自动清理已过期的历史记录
    today_str = date.today().isoformat()
    history = {k: v for k, v in history.items() if v.get("dep_date", "9999-99-99") >= today_str}

    months = get_next_months(MONTHS_AHEAD)
    updated = False

    for route in WATCH_ROUTES:
        direct_only = (route["max_stops"] == 0)
        candidates = []

        for month in months:
            try:
                items = scan_oneway_month(route["from"], route["to"], month, direct_only)
            except Exception as e:
                print(f"[{route['name']}] {month} 查询失败: {e}")
                continue

            for item in items:
                if item.get("transfers", 0) > route["max_stops"]:
                    continue
                if item["price"] <= route["max_price"]:
                    dep_date = item["departure_at"][:10]
                    if dep_date < today_str:
                        continue
                    candidates.append({
                        "dep_date": dep_date,
                        "price": item["price"],
                        "origin_airport": item.get("origin_airport") or CITY_TO_AIRPORTS[route["from"]],
                        "dest_airport": item.get("destination_airport") or CITY_TO_AIRPORTS[route["to"]],
                        "airline": item.get("airline", ""),
                        "transfers": item.get("transfers", 0),
                        "link": "https://www.aviasales.com" + item.get("link", ""),
                    })

        # 按价格从低到高排序，同一天只保留最低价
        candidates.sort(key=lambda x: x["price"])
        seen_dates = set()
        unique_deals = []
        for c in candidates:
            if c["dep_date"] in seen_dates:
                continue
            seen_dates.add(c["dep_date"])

            key = f"{route['from']}-{route['to']}-{c['dep_date']}"
            last_price = history.get(key, {}).get("price")
            if last_price is None or c["price"] <= last_price - PRICE_DROP_STEP:
                c["key"] = key
                unique_deals.append(c)

        if not unique_deals:
            print(f"[{route['name']}] 未来一年暂无新特价")
            continue

        # 组装推送列表（若配置了 SerpApi，对最便宜的前几个做实价复核）
        lines = []
        verified_count = 0
        for d in unique_deals:
            final_price = d["price"]
            url_to_show = d["link"]

            if SERPAPI_KEY and verified_count < MAX_VERIFY_PER_ROUTE:
                verified_count += 1
                real = verify_with_serpapi(d["origin_airport"], d["dest_airport"], d["dep_date"], route["max_stops"])
                if real:
                    if real["price"] > route["max_price"] * 1.1:
                        print(f"[{route['name']}] {d['dep_date']} 缓存过期 (¥{d['price']} -> 实时¥{real['price']})")
                        continue
                    final_price = real["price"]
                    if real["url"]:
                        url_to_show = real["url"]

            history[d["key"]] = {"price": final_price, "dep_date": d["dep_date"]}
            updated = True

            # 消息中最多展示前 8 条，避免过长
            if len(lines) < 8:
                stop_txt = "直飞" if d["transfers"] == 0 else f"{d['transfers']}经停"
                lines.append(
                    f"• {d['dep_date']} | ¥{final_price} | {d['origin_airport']}→{d['dest_airport']} ({d['airline']} {stop_txt})\n  {url_to_show}"
                )

        if lines:
            extra = f"\n（另有 {len(unique_deals) - len(lines)} 个特价日期已同步记录）" if len(unique_deals) > len(lines) else ""
            msg = (
                f"✈️ 单程特价汇总：{route['name']}（≤¥{route['max_price']}）\n"
                + "\n".join(lines)
                + extra
            )
            send_notification(msg)

    if updated:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
