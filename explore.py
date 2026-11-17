import os
import random
import requests

TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

ORIGIN = "SHA"
MAX_BUDGET = 1000      # 预算上限（人民币）
ONE_WAY = True         # True=单程盲盒, False=往返盲盒
DIRECT_ONLY = True     # True=仅看直飞

# 内置常用热门城市中文字典兜底（防止官方静态字典偶发 403/超时导致程序崩溃）
FALLBACK_CITIES = {
    "OSA": "大阪", "TYO": "东京", "FUK": "福冈", "SPK": "札幌", "NGO": "名古屋", "OKA": "冲绳",
    "SEL": "首尔", "PUS": "釜山", "CJU": "济州岛", "HKG": "香港", "MFM": "澳门", "TPE": "台北", "KHH": "高雄",
    "BKK": "曼谷", "CNX": "清迈", "HKT": "普吉岛", "SIN": "新加坡", "KUL": "吉隆坡", "PEN": "槟城",
    "SGN": "胡志明市", "HAN": "河内", "DAD": "岘港", "MNL": "马尼拉", "DPS": "巴厘岛", "JKT": "雅加达",
    "KMG": "昆明", "CTU": "成都", "CKG": "重庆", "CAN": "广州", "SZX": "深圳", "BJS": "北京",
    "XIY": "西安", "SYX": "三亚", "HAK": "海口", "XMN": "厦门", "TAO": "青岛", "DLC": "大连",
    "SYD": "悉尼", "MEL": "墨尔本", "LON": "伦敦", "PAR": "巴黎", "FRA": "法兰克福", "ROM": "罗马",
}

def get_city_names():
    """优先从 Travelpayouts 拉取城市字典，失败则自动使用内置中文字典"""
    mapping = dict(FALLBACK_CITIES)
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/data/en/cities.json",
            params={"token": TP_TOKEN},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        resp.raise_for_status()
        for item in resp.json():
            code = item.get("code")
            if not code:
                continue
            zh_name = item.get("name_translations", {}).get("zh-CN")
            # 如果官方字典有中文名就覆盖；没有且兜底字典里也没有，再用英文名
            if zh_name:
                mapping[code] = zh_name
            elif code not in mapping and item.get("name"):
                mapping[code] = item["name"]
    except Exception as e:
        print(f"提示：城市字典下载失败({e})，已自动降级使用内置中文字典")
    return mapping

def explore_deals():
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": ORIGIN,
        # 不传 destination 参数 + unique=true，直接拉取上海出发全网最便宜目的地榜单
        "unique": "true",
        "sorting": "price",
        "one_way": "true" if ONE_WAY else "false",
        "direct": "true" if DIRECT_ONLY else "false",
        "market": "us",
        "currency": "cny",
        "limit": 100,
        "page": 1,
        "token": TP_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code != 200:
        print(f"API 返回异常 ({resp.status_code}): {resp.text}")
    resp.raise_for_status()
    items = resp.json().get("data", [])
    # 在本地过滤出预算范围内的航班
    return [d for d in items if d.get("price", 999999) <= MAX_BUDGET]

def build_link(d):
    raw_link = d.get("link", "").split("?")[0]
    if raw_link.startswith("http"):
        return raw_link
    if raw_link.startswith("/"):
        return "https://www.aviasales.com" + raw_link
    if raw_link:
        return "https://www.aviasales.com/search/" + raw_link
    dep = d["departure_at"]
    return f"https://www.aviasales.com/search/{ORIGIN}{dep[8:10]}{dep[5:7]}{d['destination']}1"

def format_line(d, cities):
    cname = cities.get(d["destination"], d["destination"])
    dep = d["departure_at"][:10]
    ret = d.get("return_at")
    date_txt = f"{dep} ~ {ret[:10]}" if (not ONE_WAY and ret) else dep
    airline = d.get("airline", "")
    link = build_link(d)
    return f"• {cname}({d['destination']}) | ¥{d['price']} | {date_txt} | {airline}\n  {link}"

def main():
    cities = get_city_names()
    deals = explore_deals()
    print(f"本次拉取到符合预算的机票记录共 {len(deals)} 条")
    if not deals:
        print("本次预算内未发现盲盒特价")
        return

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
        lines.append(format_line(d, cities))

    if lucky_deals:
        lines.append("\n🎁 随机盲盒灵感：")
        for d in lucky_deals:
            lines.append(format_line(d, cities))

    resp = requests.post(
        f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": "\n".join(lines), "disable_web_page_preview": True},
        timeout=15,
    )
    print(f"Telegram 推送状态: HTTP {resp.status_code}")
    resp.raise_for_status()

if __name__ == "__main__":
    main()
