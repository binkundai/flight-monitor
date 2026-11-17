import json
import os
import statistics
from datetime import date, datetime, timedelta, timezone
import requests

TP_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

HOME_CITY = "SHA"
OUTPUT_FILE = "docs/data.json"
TP_MARKET = "us"

# 往返停留天数范围（3-7天）
ROUNDTRIP_MIN_DAYS = 3
ROUNDTRIP_MAX_DAYS = 7

# 重点深度扫描城市（双向各扫未来6个月，保证常用城市有充足的3-7天往返与单程日期）
DEEP_SCAN_CITIES = ["OSA", "TYO", "FUK", "SEL", "HKG", "BKK", "SIN", "KMG"]
DEEP_MONTHS = 6

FALLBACK_CITIES = {
    "SHA": "上海",
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

def build_link(origin_code, dest_code, dep_date, ret_date=None, raw_link=""):
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
            return f"https://www.aviasales.com/search/{origin_code}{d_part}{dest_code}{r_part}1"
        return f"https://www.aviasales.com/search/{origin_code}{d_part}{dest_code}1"
    return "https://www.aviasales.com/"

def fetch_prices_for_dates(params):
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    base = {"market": TP_MARKET, "currency": "cny", "token": TP_TOKEN}
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

def fetch_city_directions(origin_code):
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/v1/city-directions",
            params={"origin": origin_code, "currency": "cny", "token": TP_TOKEN},
            timeout=20,
        )
        if resp.status_code != 200:
            return {}
        data = resp.json().get("data", {})
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def fetch_special_offers(origin_code):
    try:
        resp = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/get_special_offers",
            params={"origin": origin_code, "locale": "en", "currency": "cny", "market": TP_MARKET, "token": TP_TOKEN},
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
    records = {}

    def extract_iata(val, fallback=""):
        if isinstance(val, dict):
            return val.get("city_iata") or val.get("iata") or fallback
        return val or fallback

    def upsert_deal(item, one_way, direction, default_origin="", default_dest="", is_popular=False, pop_rank=999, is_special=False, note=""):
        origin_code = extract_iata(item.get("origin"), default_origin)
        dest_code = extract_iata(item.get("destination"), default_dest)
        if len(origin_code) != 3 or len(dest_code) != 3:
            return

        # 确定对端城市（出发看目的地，飞来上海看出发地）
        other_city = dest_code if direction == "outbound" else origin_code
        if other_city == HOME_CITY:
            return

        dep_raw = item.get("departure_at", "")
        if not dep_raw:
            return
        dep_date = dep_raw[:10]
        if dep_date < today_str:
            return

        ret_raw = item.get("return_at") or ""
        ret_date = ret_raw[:10] if (not one_way and ret_raw) else ""

        # 往返严格过滤为 3~7 天
        stay_days = None
        if not one_way:
            if not ret_date:
                return
            try:
                stay_days = (date.fromisoformat(ret_date) - date.fromisoformat(dep_date)).days
            except ValueError:
                return
            if stay_days < ROUNDTRIP_MIN_DAYS or stay_days > ROUNDTRIP_MAX_DAYS:
                return

        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return

        transfers = item.get("transfers", 0)
        airline = item.get("airline", "")
        origin_ap = item.get("origin_airport") or origin_code
        dest_ap = item.get("destination_airport") or dest_code
        link = build_link(origin_code, dest_code, dep_date, ret_date, item.get("link", ""))

        uid = f"{direction}|{origin_code}|{dest_code}|{'OW' if one_way else 'RT'}|{dep_date}|{ret_date}|{transfers}"
        existing = records.get(uid)
        if existing:
            if price < existing["price"]:
                existing["price"] = int(round(price))
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
                "direction": direction,         # outbound=上海出发, inbound=飞往上海
                "origin": origin_code,
                "destination": dest_code,
                "origin_name": cities.get(origin_code, origin_code),
                "dest_name": cities.get(dest_code, dest_code),
                "city": other_city,
                "city_name": cities.get(other_city, other_city),
                "origin_airport": origin_ap,
                "dest_airport": dest_ap,
                "one_way": one_way,
                "stay_days": stay_days,
                "dep_date": dep_date,
                "ret_date": ret_date,
                "price": int(round(price)),
                "transfers": transfers,
                "airline": airline,
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

    # ================= A. 上海出发 (outbound) =================
    # 1. 热门单程
    for idx, it in enumerate(fetch_prices_for_dates({
        "origin": HOME_CITY, "sorting": "route", "unique": "true",
        "one_way": "true", "direct": "false", "limit": 100
    }), start=1):
        upsert_deal(it, one_way=True, direction="outbound", default_origin=HOME_CITY, is_popular=True, pop_rank=idx)

    # 2. v1 热门方向
    for idx, (_, it) in enumerate(fetch_city_directions(HOME_CITY).items(), start=1):
        has_ret = bool(it.get("return_at"))
        upsert_deal(it, one_way=not has_ret, direction="outbound", default_origin=HOME_CITY, is_popular=True, pop_rank=idx)

    # 3. 全网低价单程 & 全网3-7天往返（往返不设 unique=true，以免只返回超长天数被过滤光）
    for page in (1, 2):
        for it in fetch_prices_for_dates({
            "origin": HOME_CITY, "sorting": "price", "unique": "true",
            "one_way": "true", "direct": "false", "limit": 100, "page": page
        }):
            upsert_deal(it, one_way=True, direction="outbound", default_origin=HOME_CITY)

    for it in fetch_prices_for_dates({
        "origin": HOME_CITY, "sorting": "price", "unique": "false",
        "one_way": "false", "direct": "false", "limit": 1000
    }):
        upsert_deal(it, one_way=False, direction="outbound", default_origin=HOME_CITY)

    # 4. 官方促销
    for it in fetch_special_offers(HOME_CITY):
        upsert_deal(it, one_way=not bool(it.get("return_at")), direction="outbound", default_origin=HOME_CITY, is_special=True, note=it.get("title", "官方促销"))

    # ================= B. 飞往上海 (inbound) =================
    # 1. 全网飞往上海热门 & 低价单程
    for idx, it in enumerate(fetch_prices_for_dates({
        "destination": HOME_CITY, "sorting": "route", "unique": "false",
        "one_way": "true", "direct": "false", "limit": 500
    }), start=1):
        upsert_deal(it, one_way=True, direction="inbound", default_dest=HOME_CITY, is_popular=idx <= 100, pop_rank=idx)

    for it in fetch_prices_for_dates({
        "destination": HOME_CITY, "sorting": "price", "unique": "false",
        "one_way": "true", "direct": "false", "limit": 500
    }):
        upsert_deal(it, one_way=True, direction="inbound", default_dest=HOME_CITY)

    # 2. 全网飞往上海 3-7天往返
    for it in fetch_prices_for_dates({
        "destination": HOME_CITY, "sorting": "price", "unique": "false",
        "one_way": "false", "direct": "false", "limit": 1000
    }):
        upsert_deal(it, one_way=False, direction="inbound", default_dest=HOME_CITY)

    # ================= C. 重点城市未来6个月双向深度扫描 =================
    months = get_next_months(DEEP_MONTHS)
    for city in DEEP_SCAN_CITIES:
        for month in months:
            for ow in (True, False):
                # 出发: SHA -> city
                for it in fetch_prices_for_dates({
                    "origin": HOME_CITY, "destination": city, "departure_at": month,
                    "one_way": "true" if ow else "false", "direct": "false", "sorting": "price", "limit": 100
                }):
                    upsert_deal(it, one_way=ow, direction="outbound", default_origin=HOME_CITY, default_dest=city, is_popular=True, pop_rank=50)

                # 回程/来沪: city -> SHA
                for it in fetch_prices_for_dates({
                    "origin": city, "destination": HOME_CITY, "departure_at": month,
                    "one_way": "true" if ow else "false", "direct": "false", "sorting": "price", "limit": 100
                }):
                    upsert_deal(it, one_way=ow, direction="inbound", default_origin=city, default_dest=HOME_CITY, is_popular=True, pop_rank=50)

    # ================= D. 计算各自方向的中位数基准与异常低价 =================
    grouped_prices = {}
    for rec in records.values():
        gkey = (rec["direction"], rec["city"], rec["one_way"])
        grouped_prices.setdefault(gkey, []).append(rec["price"])

    baselines = {
        gkey: int(round(statistics.median(plist)))
        for gkey, plist in grouped_prices.items() if len(plist) >= 5
    }

    all_deals = list(records.values())
    for rec in all_deals:
        base_p = baselines.get((rec["direction"], rec["city"], rec["one_way"]))
        if base_p and base_p > 0:
            rec["baseline_price"] = base_p
            if rec["price"] < base_p:
                save = base_p - rec["price"]
                pct = round(save / base_p * 100)
                rec["save_amount"] = save
                rec["discount_pct"] = pct
                if pct >= 25:
                    rec["is_anomaly"] = True
        if rec["is_special_offer"]:
            rec["is_anomaly"] = True

    all_deals.sort(key=lambda x: x["price"])

    now_bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M 北京时间")
    out_cnt = sum(1 for d in all_deals if d["direction"] == "outbound")
    in_cnt = sum(1 for d in all_deals if d["direction"] == "inbound")
    anom_cnt = sum(1 for d in all_deals if d["is_anomaly"])

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    payload = {
        "updated_at": now_bj,
        "home_city": HOME_CITY,
        "roundtrip_days": [ROUNDTRIP_MIN_DAYS, ROUNDTRIP_MAX_DAYS],
        "stats": {
            "total": len(all_deals),
            "outbound_total": out_cnt,
            "inbound_total": in_cnt,
            "anomalies": anom_cnt,
        },
        "deals": all_deals,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"已写入 {OUTPUT_FILE}：总计 {len(all_deals)} 条（出发 {out_cnt} / 飞来 {in_cnt}），异常低价 {anom_cnt} 条")

    # Telegram 精简日报（同时汇总上海出发 + 飞往上海）
    if TG_BOT_TOKEN and TG_CHAT_ID:
        out_top = sorted([d for d in all_deals if d["direction"] == "outbound" and d["is_anomaly"]], key=lambda x: (-x["discount_pct"], x["price"]))[:3]
        in_top = sorted([d for d in all_deals if d["direction"] == "inbound" and d["is_anomaly"]], key=lambda x: (-x["discount_pct"], x["price"]))[:3]
        lines = [f"📡 上海双向机票雷达（{now_bj}）", f"上海出发 {out_cnt} 条 | 飞往上海 {in_cnt} 条（往返限3-7天）\n"]
        if out_top:
            lines.append("🛫 上海出发异常低价 Top 3：")
            for d in out_top:
                t_txt = "单程" if d["one_way"] else f"往返{d['stay_days']}天"
                lines.append(f"• →{d['dest_name']} [{t_txt}] ¥{d['price']} ({d['dep_date']} · 省{d['discount_pct']}%)")
        if in_top:
            lines.append("\n🛬 飞往上海异常低价 Top 3：")
            for d in in_top:
                t_txt = "单程" if d["one_way"] else f"往返{d['stay_days']}天"
                lines.append(f"• {d['origin_name']}→ [{t_txt}] ¥{d['price']} ({d['dep_date']} · 省{d['discount_pct']}%)")
        if DASHBOARD_URL:
            lines.append(f"\n📊 双向筛选看板：{DASHBOARD_URL}")
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": "\n".join(lines), "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()

if __name__ == "__main__":
    main()
