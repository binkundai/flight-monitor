import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import requests

LONGXIA_TOKEN = os.environ["LONGXIA_TOKEN"].strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

API_URL = "https://api.longxiachuxing.com/open/v1/flight/search"
HOME_CITY = "SHA"
DATA_FILE = "docs/data.json"
HISTORY_FILE = "history_longxia.json"

# 扫描天数与价格阈值（含税总价 lowest_price 严格小于该值才触发 Telegram 报警）
DOMESTIC_DAYS = 45
DOMESTIC_THRESHOLD = 550
INTL_DAYS = 120
INTL_THRESHOLD = 1200
EUROPE_THRESHOLD = 1800      # 欧洲线默认同为1000（若想放宽欧洲报警，可单独改如 1800）

PRICE_DROP_STEP = 30
DIRECT_ONLY_NOTIFY = False
MAX_WORKERS = 18
MIN_INTERVAL = 0.26          # 全局限速 ≈ 4.16 QPS（安全低于 5 QPS 上限）

# 国内 32 个城市
DOMESTIC_CITIES = {
    #"KMG": "昆明",
    "CAN": "广州", "SZX": "深圳", "CTU": "成都", "CKG": "重庆",
    "KMG": "昆明", "XIY": "西安", "SYX": "三亚", "XMN": "厦门", "TAO": "青岛",
    "NNG": "南宁", "KWL": "桂林", "HAK": "海口", "FOC": "福州", "LJG": "丽江",
    "KWE": "贵阳", "DLC": "大连", "SHE": "沈阳", "CGQ": "长春", 
    "LHW": "兰州", "TYN": "太原", "TSN": "天津", "NBS": "白山", "URC": "乌鲁木齐",
    "LXA": "拉萨", "SWA": "汕头", "KHN": "南昌",
    "LYA": "洛阳", "XNN": "西宁", "YZY": "张掖", "AAT": "阿勒泰",
}

# 亚洲：日本 / 东南亚 / 南亚（共 19 城，已去掉新加坡 SIN）
ASIA_INTL_CITIES = {
    "OSA": "大阪", "TYO": "东京", "FUK": "福冈", "SPK": "札幌", "OKA": "冲绳","NGO": "名古屋",
    "BKK": "曼谷", "CNX": "清迈", "HKT": "普吉岛",
    "KUL": "吉隆坡", "BKI": "亚庇", "TWU": "斗湖/仙本那", "PEN": "槟城", "JHB": "新山",
    "DPS": "巴厘岛", "JKT": "雅加达",
    "SGN": "胡志明", "DAD": "岘港", "CXR": "芽庄",
    "KTM": "加德满都",
}

# 欧洲 / 土耳其（共 8 城）
EUROPE_CITIES = {
    "BCN": "巴塞罗那", "MAD": "马德里", "ROM": "罗马", "MIL": "米兰",
    "ATH": "雅典", "IST": "伊斯坦布尔", "BUD": "布达佩斯", "BEG": "贝尔格莱德",
}

ALL_CITY_NAMES = {"SHA": "上海", **DOMESTIC_CITIES, **ASIA_INTL_CITIES, **EUROPE_CITIES}

_rate_lock = threading.Lock()
_next_req_ts = 0.0
_rate_limit_hits = 0
_thread_local = threading.local()

def get_session():
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LONGXIA_TOKEN}",
        })
        _thread_local.session = s
    return _thread_local.session

def throttle():
    """预约发包时间槽并在锁外休眠，严格保证发包速率 <= 3.84 QPS"""
    global _next_req_ts
    with _rate_lock:
        now = time.monotonic()
        if _next_req_ts < now:
            _next_req_ts = now
        wait = _next_req_ts - now
        _next_req_ts += MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)

def search_day(trip_mode, from_code, to_code, depart_date):
    global _rate_limit_hits
    payload = {
        "trip_mode": trip_mode,
        "trip_type": "oneway",
        "from_code": from_code,
        "to_code": to_code,
        "cabin_class": "economy",
        "depart_date": depart_date,
        "passengers": {"adult": 1, "child": 0, "infant": 0},
        "page_size": 3,
        "sort_by": "price",
    }
    session = get_session()
    for attempt in range(3):
        throttle()
        try:
            resp = session.post(API_URL, json=payload, timeout=(5, 12))
            if resp.status_code == 429:
                with _rate_lock:
                    _rate_limit_hits += 1
                time.sleep(2.0 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return []
            body = resp.json()
            if body.get("code") != 0:
                return []
            return (body.get("data") or {}).get("flights") or []
        except Exception:
            if attempt >= 1:  # 超时/网络错误最多重试1次，防止死等拖慢整个池子
                return []
            time.sleep(0.5)
    return []

def extract_flight_record(flight, task):
    cabins = flight.get("cabins") or []
    if not cabins:
        return None

    # 选含税最低价舱位
    best_cabin = None
    best_price = None
    for c in cabins:
        lp = c.get("lowest_price")
        if not isinstance(lp, (int, float)) or lp <= 0:
            lp = (c.get("adult_price") or 0) + (c.get("airport_tax") or 0) + (c.get("fuel_tax") or 0)
        if lp > 0 and (best_price is None or lp < best_price):
            best_price = int(round(lp))
            best_cabin = c

    if best_price is None:
        return None

    airline_code = flight.get("airline_code") or ""
    raw_fno = str(flight.get("flight_no") or "")
    fno = raw_fno if raw_fno.startswith(airline_code) else f"{airline_code}{raw_fno}"
    airline_name = flight.get("airline_name") or airline_code
    dep_hm = (flight.get("dep_time") or "")[11:16]
    arr_hm = (flight.get("arr_time") or "")[11:16]
    stop_count = flight.get("stop_count", 0)

    origin_ap = flight.get("dep_airport_code") or task["from_code"]
    dest_ap = flight.get("arr_airport_code") or task["to_code"]
    dep_date = task["dep_date"]
    origin_code = task["from_code"]
    dest_code = task["to_code"]
    direction = task["direction"]
    other_city = dest_code if direction == "outbound" else origin_code

    ctrip_link = f"https://flights.ctrip.com/online/list/oneway-{origin_code.lower()}-{dest_code.lower()}?depdate={dep_date}"
    adult_p = best_cabin.get("adult_price") or 0
    tax_p = (best_cabin.get("airport_tax") or 0) + (best_cabin.get("fuel_tax") or 0)

    return {
        "id": f"{direction}|{origin_code}|{dest_code}|{dep_date}|{stop_count}",
        "direction": direction,
        "trip_mode": task["trip_mode"],
        "threshold": task["threshold"],
        "origin": origin_code,
        "destination": dest_code,
        "origin_name": ALL_CITY_NAMES[origin_code],
        "dest_name": ALL_CITY_NAMES[dest_code],
        "city": other_city,
        "city_name": ALL_CITY_NAMES[other_city],
        "origin_airport": origin_ap,
        "dest_airport": dest_ap,
        "one_way": True,
        "stay_days": None,
        "dep_date": dep_date,
        "ret_date": "",
        "price": best_price,
        "adult_price": adult_p,
        "tax_price": tax_p,
        "transfers": stop_count,
        "airline": f"{airline_name} {fno} ({dep_hm}-{arr_hm})".strip(),
        "link": ctrip_link,
        "is_popular": True,
        "popular_rank": 1 if task["trip_mode"] == "domestic" else 2,
        "is_special_offer": best_price < task["threshold"],
        "is_anomaly": best_price < task["threshold"],
        "baseline_price": None,
        "discount_pct": 0,
        "save_amount": 0,
    }

def run_task(task):
    flights = search_day(task["trip_mode"], task["from_code"], task["to_code"], task["dep_date"])
    if not flights:
        return []
    picked = {}
    # 分别保留当天“最低总价”和“最低直飞价”（如果第一班就是直飞则只存1条）
    for fl in flights:
        rec = extract_flight_record(fl, task)
        if not rec:
            continue
        sc = rec["transfers"]
        key = "direct" if sc == 0 else "any_stop"
        if key not in picked or rec["price"] < picked[key]["price"]:
            picked[key] = rec
    if "direct" in picked and "any_stop" in picked and picked["direct"]["price"] <= picked["any_stop"]["price"]:
        del picked["any_stop"]
    return list(picked.values())

def main():
    today = date.today()
    today_str = today.isoformat()

    tasks = []
    # 1. 国内各大城市 未来 30 天（双向）
    for city in DOMESTIC_CITIES:
        for d in range(1, DOMESTIC_DAYS + 1):
            dt = (today + timedelta(days=d)).isoformat()
            tasks.append({"trip_mode": "domestic", "threshold": DOMESTIC_THRESHOLD, "direction": "outbound", "from_code": HOME_CITY, "to_code": city, "dep_date": dt})
            tasks.append({"trip_mode": "domestic", "threshold": DOMESTIC_THRESHOLD, "direction": "inbound", "from_code": city, "to_code": HOME_CITY, "dep_date": dt})

    # 2. 亚洲国际城市 未来 90 天（双向）
    for city in ASIA_INTL_CITIES:
        for d in range(1, INTL_DAYS + 1):
            dt = (today + timedelta(days=d)).isoformat()
            tasks.append({"trip_mode": "international", "threshold": INTL_THRESHOLD, "direction": "outbound", "from_code": HOME_CITY, "to_code": city, "dep_date": dt})
            tasks.append({"trip_mode": "international", "threshold": INTL_THRESHOLD, "direction": "inbound", "from_code": city, "to_code": HOME_CITY, "dep_date": dt})

    # 3. 欧洲/土耳其城市 未来 90 天（双向）
    for city in EUROPE_CITIES:
        for d in range(1, INTL_DAYS + 1):
            dt = (today + timedelta(days=d)).isoformat()
            tasks.append({"trip_mode": "international", "threshold": EUROPE_THRESHOLD, "direction": "outbound", "from_code": HOME_CITY, "to_code": city, "dep_date": dt})
            tasks.append({"trip_mode": "international", "threshold": EUROPE_THRESHOLD, "direction": "inbound", "from_code": city, "to_code": HOME_CITY, "dep_date": dt})

    print(f"准备执行龙虾全量实时扫描：共 {len(tasks)} 个航段日期任务（预计耗时约 {int(len(tasks) * MIN_INTERVAL / 60)} 分钟）")

    all_deals = []
    t0 = time.monotonic()
    total_tasks = len(tasks)
    done_cnt = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(run_task, t) for t in tasks]
        for fut in as_completed(futures):
            all_deals.extend(fut.result())
            done_cnt += 1
            if done_cnt % 200 == 0 or done_cnt == total_tasks:
                elapsed = max(time.monotonic() - t0, 0.1)
                print(f"[进度] {done_cnt}/{total_tasks} | 耗时 {elapsed:.0f}s | 速率 {done_cnt/elapsed:.2f} 次/秒 | 429限流 {_rate_limit_hits} 次", flush=True)

    # 计算每条航线中位数基准价
    grouped = {}
    for rec in all_deals:
        grouped.setdefault((rec["direction"], rec["city"]), []).append(rec["price"])
    baselines = {k: int(round(statistics.median(v))) for k, v in grouped.items() if len(v) >= 5}

    for rec in all_deals:
        bp = baselines.get((rec["direction"], rec["city"]))
        if bp and bp > 0:
            rec["baseline_price"] = bp
            if rec["price"] < bp:
                rec["save_amount"] = bp - rec["price"]
                rec["discount_pct"] = round(rec["save_amount"] / bp * 100)

    all_deals.sort(key=lambda x: x["price"])

    now_bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M 北京时间")
    out_cnt = sum(1 for d in all_deals if d["direction"] == "outbound")
    in_cnt = sum(1 for d in all_deals if d["direction"] == "inbound")
    hit_deals = [d for d in all_deals if d["price"] < d["threshold"]]

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    payload = {
        "updated_at": f"{now_bj} (龙虾实时)",
        "home_city": HOME_CITY,
        "stats": {
            "total": len(all_deals),
            "outbound_total": out_cnt,
            "inbound_total": in_cnt,
            "anomalies": len(hit_deals),
        },
        "deals": all_deals,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"扫描完成！共获取 {len(all_deals)} 条每日实时最低价，其中达标特价 {len(hit_deals)} 条")

# Telegram 报警去重推送
    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    history = {k: v for k, v in history.items() if v.get("dep_date", "9999-99-99") >= today_str}

    new_hits = []
    for d in hit_deals:
        if DIRECT_ONLY_NOTIFY and d["transfers"] > 0:
            continue
        hkey = f"{d['direction']}|{d['origin']}|{d['destination']}|{d['dep_date']}"
        last_p = history.get(hkey, {}).get("price")
        if last_p is None or d["price"] <= last_p - PRICE_DROP_STEP:
            new_hits.append((hkey, d))

    if new_hits and TG_BOT_TOKEN and TG_CHAT_ID:
        dom_hits = [(k, d) for k, d in new_hits if d["trip_mode"] == "domestic"]
        intl_hits = [(k, d) for k, d in new_hits if d["trip_mode"] == "international"]

        lines = [f"🦞 龙虾实时特价快报（{now_bj}）", f"新发现达标特价：国内 {len(dom_hits)} 条 | 日韩东南亚 {len(intl_hits)} 条\n"]
        if dom_hits:
            lines.append(f"🇨🇳 国内 30 天含税 < ¥{DOMESTIC_THRESHOLD}（Top 8）：")
            for _, d in dom_hits[:8]:
                stop_t = "直飞" if d["transfers"] == 0 else f"{d['transfers']}转"
                lines.append(f"• {d['origin_name']}→{d['dest_name']} | ¥{d['price']} | {d['dep_date']} ({d['airline']} {stop_t})")
        if intl_hits:
            lines.append(f"\n🌏 日本/东南亚 3 个月含税 < ¥{INTL_THRESHOLD}（Top 8）：")
            for _, d in intl_hits[:8]:
                stop_t = "直飞" if d["transfers"] == 0 else f"{d['transfers']}转"
                lines.append(f"• {d['origin_name']}→{d['dest_name']} | ¥{d['price']} | {d['dep_date']} ({d['airline']} {stop_t})")
        if DASHBOARD_URL:
            lines.append(f"\n📊 全部日期日历看板：{DASHBOARD_URL}")

        try:
            tg_resp = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN.strip()}/sendMessage",
                json={"chat_id": TG_CHAT_ID.strip(), "text": "\n".join(lines), "disable_web_page_preview": True},
                timeout=15,
            )
            if tg_resp.status_code != 200:
                print(f"[Telegram 推送失败] HTTP {tg_resp.status_code}: {tg_resp.text}")
            else:
                print("[Telegram 推送成功]")
                for k, d in new_hits:
                    history[k] = {"price": d["price"], "dep_date": d["dep_date"]}
        except Exception as e:
            print(f"[Telegram 推送异常] {e}")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
