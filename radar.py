import json
import os
import statistics
from datetime import date, datetime, timedelta, timezone
import requests

TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")  # 可选：部署好看板后把网址填进 GitHub Secrets

ORIGIN = "SHA"
OUTPUT_FILE = "docs/data.json"
TP_MARKET = "us"

# 重点深度扫描城市（用于拉取未来6个月多日期数据，供看板筛选日期 & 计算均价基准）
DEEP_SCAN_DESTS = ["OSA", "TYO", "FUK", "SEL", "HKG", "BKK", "SIN", "KUL", "TPE", "KMG"]
DEEP_MONTHS = 6

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
            if zh_name:
                mapping[code] = zh_name
            elif code not in mapping and item.get("name"):
                mapping[code] = item["name"]
    except Exception as e:
        print(f"城市字典下载降级: {e}")
    return mapping

def get_next_months(n):
    today = date.today()
    y, m = today.year, today.month
    res = []
    for _ in range(n):
        res.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return res

def build_link(origin, dest, dep_date, ret_date=None, raw_link=""):
    clean = (raw_link or "").split("?")[0]
    if clean.startswith("http"):
        return clean
    if clean.startswith("/"):
        return "https://www.aviasales.com" + clean
    if clean:
        return "https://www.aviasales.com/search/" + clean
    if len(dep_date) >= 10:
        d_part = f"{dep_date[8:10]}{dep_date[5:7]}"
        if ret_date and len(ret_date) >= 10:
            r_part = f"{ret_date[8:10]}{ret_date[5:7]}"
            return f"https://www.aviasales.com/search/{origin}{d_part}{dest}{r_part}1"
        return f"https://www.aviasales.com/search/{origin}{d_part}{dest}1"
    return "https://www.aviasales.com/"

def fetch_prices_for_dates(params):
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    base = {"origin": ORIGIN, "market": TP_MARKET, "currency": "cny", "token": TP_TOKEN}
    base.update(params)
    try:
        resp = requests.get(url, params=base, timeout=20)
        if resp.status_code != 200:
            print(f"prices_for_dates {resp.status_code}: {resp.text[:160]}")
            return []
        return resp.json().get("data", []) or []
    except Exception as e:
        print(f"prices_for_dates 异常: {e}")
        return []

def fetch_city_directions():
    """获取 v1 热门城市方向"""
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/v1/city-directions",
            params={"origin": ORIGIN, "currency": "cny", "token": TP_TOKEN},
            timeout=20,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json().get("data", {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def fetch_special_offers():
    """获取官方异常低价促销接口"""
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/get_special_offers",
            params={"origin": ORIGIN, "locale": "en", "currency": "cny", "market": TP_MARKET, "token": TP_TOKEN},
            timeout=20,
        )
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []

def main():
    cities = get_city_names()
    today_str = date.today().isoformat()
    records = {}  # 去重池

    def upsert_deal(item, one_way, is_popular=False, pop_rank=999, is_special=False, note=""):
        dest = item.get("destination")
        if isinstance(dest, dict):
            dest = dest.get("city_iata") or dest.get("iata") or ""
        if not dest or len(dest) != 3:
            return

        dep_raw = item.get("departure_at", "")
        if not dep_raw:
            return
        dep_date = dep_raw[:10]
        if dep_date < today_str:
            return

        ret_raw = item.get("return_at") or ""
        ret_date = ret_raw[:10] if (not one_way and ret_raw) else ""
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return

        transfers = item.get("transfers", 0)
        airline = item.get("airline", "")
        origin_ap = item.get("origin_airport") or ORIGIN
        dest_ap = item.get("destination_airport") or dest
        duration = item.get("duration_to") or item.get("duration") or 0
        link = build_link(ORIGIN, dest, dep_date, ret_date, item.get("link", ""))

        uid = f"{dest}|{'OW' if one_way else 'RT'}|{dep_date}|{ret_date}|{transfers}"
        existing = records.get(uid)
        if existing:
            if price < existing["price"]:
                existing["price"] = price
                existing["airline"] = airline or existing["airline"]
                existing["link"] = link
                existing["origin_airport"] = origin_ap
                existing["dest_airport"] = dest_ap
            existing["is_popular"] = existing["is_popular"] or is_popular
            existing["popular_rank"] = min(existing["popular_rank"], pop_rank)
            existing["is_special_offer"] = existing["is_special_offer"] or is_special
            if note and not existing["note"]:
                existing["note"] = note
        else:
            records[uid] = {
                "id": uid,
                "origin": ORIGIN,
                "destination": dest,
                "dest_name": cities.get(dest, dest),
                "origin_airport": origin_ap,
                "dest_airport": dest_ap,
                "one_way": one_way,
                "dep_date": dep_date,
                "ret_date": ret_date,
                "price": int(round(price)),
                "transfers": transfers,
                "airline": airline,
                "duration": duration,
                "link": link,
                "is_popular": is_popular,
                "popular_rank": pop_rank,
                "is_special_offer": is_special,
                "is_anomaly": False,
                "baseline_price": None,
                "discount_pct": 0,
                "save_amount": 0,
                "note": note,
            }

    # 1. 热门榜单 (sorting=route + unique=true)
    for ow in (True, False):
        pop_items = fetch_prices_for_dates({
            "sorting": "route", "unique": "true",
            "one_way": "true" if ow else "false", "direct": "false", "limit": 100
        })
        for idx, it in enumerate(pop_items, start=1):
            upsert_deal(it, one_way=ow, is_popular=True, pop_rank=idx)

    # 1b. v1 热门方向补充
    v1_pop = fetch_city_directions()
    for idx, (_, it) in enumerate(v1_pop.items(), start=1):
        has_ret = bool(it.get("return_at"))
        upsert_deal(it, one_way=not has_ret, is_popular=True, pop_rank=idx)

    # 2. 全网低价池（单程 & 往返）
    for ow in (True, False):
        for page in (1, 2):
            cheap_items = fetch_prices_for_dates({
                "sorting": "price", "unique": "true",
                "one_way": "true" if ow else "false", "direct": "false", "limit": 100, "page": page
            })
            for it in cheap_items:
                upsert_deal(it, one_way=ow)

    # 3. 重点城市未来6个月深度日期拉取（供看板按日期筛选 + 计算统计异常低价）
    months = get_next_months(DEEP_MONTHS)
    for dest in DEEP_SCAN_DESTS:
        for month in months:
            for ow in (True, False):
                items = fetch_prices_for_dates({
                    "destination": dest, "departure_at": month,
                    "one_way": "true" if ow else "false", "direct": "false", "sorting": "price", "limit": 100
                })
                for it in items:
                    upsert_deal(it, one_way=ow)

    # 4. 官方特价促销接口
    specials = fetch_special_offers()
    for it in specials:
        has_ret = bool(it.get("return_at"))
        upsert_deal(it, one_way=not has_ret, is_special=True, note=it.get("title", "官方促销"))

    # 5. 计算每条 (目的地, 单程/往返) 的中位数基准价，识别统计异常低价
    grouped_prices = {}
    for rec in records.values():
        gkey = (rec["destination"], rec["one_way"])
        grouped_prices.setdefault(gkey, []).append(rec["price"])

    baselines = {}
    for gkey, plist in grouped_prices.items():
        if len(plist) >= 5:
            baselines[gkey] = int(round(statistics.median(plist)))

    all_deals = list(records.values())
    for rec in all_deals:
        base_p = baselines.get((rec["destination"], rec["one_way"]))
        if base_p and base_p > 0:
            rec["baseline_price"] = base_p
            if rec["price"] < base_p:
                save = base_p - rec["price"]
                pct = round(save / base_p * 100)
                rec["save_amount"] = save
                rec["discount_pct"] = pct
                # 低于中位数 25% 以上（相当于 ≤7.5折）判定为统计异常低价
                if pct >= 25:
                    rec["is_anomaly"] = True
        if rec["is_special_offer"]:
            rec["is_anomaly"] = True

    all_deals.sort(key=lambda x: x["price"])

    now_bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M 北京时间")
    anomaly_count = sum(1 for d in all_deals if d["is_anomaly"])
    popular_count = sum(1 for d in all_deals if d["is_popular"])

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    payload = {
        "updated_at": now_bj,
        "origin": ORIGIN,
        "stats": {
            "total": len(all_deals),
            "anomalies": anomaly_count,
            "popular": popular_count,
            "min_oneway": min((d["price"] for d in all_deals if d["one_way"]), default=None),
            "min_roundtrip": min((d["price"] for d in all_deals if not d["one_way"]), default=None),
        },
        "deals": all_deals,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"已写入 {OUTPUT_FILE}：总计 {len(all_deals)} 条，异常低价 {anomaly_count} 条，热门 {popular_count} 条")

    # 6. 给 Telegram 发一条精简摘要 + 看板直达链接
    if TG_BOT_TOKEN and TG_CHAT_ID:
        radar_top = sorted(
            [d for d in all_deals if d["is_anomaly"]],
            key=lambda x: (-x["discount_pct"], x["price"])
        )[:5]
        lines = [f"📡 上海出发雷达日报（{now_bj}）", f"共收录 {len(all_deals)} 条报价，异常低价 {anomaly_count} 条\n"]
        if radar_top:
            lines.append("🔥 今日折扣力度 Top 5：")
            for d in radar_top:
                t_txt = "单程" if d["one_way"] else "往返"
                disc_txt = f" | 省{d['discount_pct']}%" if d["discount_pct"] else " | 官方促销"
                lines.append(f"• {d['dest_name']}({d['destination']}) [{t_txt}] ¥{d['price']} ({d['dep_date']}{disc_txt})")
        if DASHBOARD_URL:
            lines.append(f"\n📊 完整筛选看板：{DASHBOARD_URL}")
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": "\n".join(lines), "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()

if __name__ == "__main__":
    main()
