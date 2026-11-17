import os
import random
import requests

TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

ORIGIN = "SHA"
MAX_BUDGET = 900       # 预算上限（人民币）
ONE_WAY = True         # True=单程盲盒, False=往返盲盒
DIRECT_ONLY = True     # True=仅看直飞

def get_city_names():
    """拉取 Travelpayouts 官方简体中文城市字典"""
    resp = requests.get("https://api.travelpayouts.com/data/zh-hans/cities.json", timeout=20)
    resp.raise_for_status()
    return {item["code"]: item.get("name") or item["code"] for item in resp.json()}

def explore_deals():
    url = "https://api.travelpayouts.com/aviasales/v3/search_by_price_range"
    params = {
        "origin": ORIGIN,
        "destination": "-",          # '-' 代表全网任意目的地
        "value_min": 100,
        "value_max": MAX_BUDGET,
        "one_way": "true" if ONE_WAY else "false",
        "direct": "true" if DIRECT_ONLY else "false",
        "market": "us",
        "currency": "cny",
        "limit": 50,
        "token": TP_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("data", [])

def main():
    cities = get_city_names()
    deals = explore_deals()
    if not deals:
        print("本次预算内未发现盲盒特价")
        return

    # 每个目的地只保留最便宜的一班
    best_by_dest = {}
    for d in deals:
        dest = d["destination"]
        if dest not in best_by_dest or d["price"] < best_by_dest[dest]["price"]:
            best_by_dest[dest] = d

    sorted_deals = sorted(best_by_dest.values(), key=lambda x: x["price"])
    top_deals = sorted_deals[:5]
    pool = sorted_deals[5:]
    lucky_deals = random.sample(pool, min(3, len(pool))) if pool else []

    trip_type = "单程" if ONE_WAY else "往返"
    lines = [f"🎲 本周上海出发{trip_type}捡漏（≤¥{MAX_BUDGET}）\n", "🏆 全网最低 Top 5："]
    for d in top_deals:
        cname = cities.get(d["destination"], d["destination"])
        dep = d["departure_at"][:10]
        link = "https://www.aviasales.com" + d.get("link", "").split("?")[0]
        lines.append(f"• {cname}({d['destination']}) | ¥{d['price']} | {dep} | {d.get('airline','')}\n  {link}")

    if lucky_deals:
        lines.append("\n🎁 随机盲盒灵感：")
        for d in lucky_deals:
            cname = cities.get(d["destination"], d["destination"])
            dep = d["departure_at"][:10]
            link = "https://www.aviasales.com" + d.get("link", "").split("?")[0]
            lines.append(f"• {cname}({d['destination']}) | ¥{d['price']} | {dep}\n  {link}")

    requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": "\n".join(lines), "disable_web_page_preview": True},
        timeout=15,
    ).raise_for_status()

if __name__ == "__main__":
    main()
