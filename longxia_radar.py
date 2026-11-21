import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import requests

_bj_now = datetime.now(timezone.utc) + timedelta(hours=8)
_token_1 = os.environ["LONGXIA_TOKEN"].strip()
_token_2 = os.environ.get("LONGXIA_TOKEN_2", "").strip()
# 若配置了第 2 个 Token，北京时间 12 点以后自动切换用第 2 个；未配置则全天用第 1 个
LONGXIA_TOKEN = _token_2 if (_bj_now.hour >= 12 and _token_2) else _token_1

TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()
DASHBOARD_URL = (os.environ.get("DASHBOARD_URL") or "").strip()

# 运行模式：auto（12点前早班全量，12点后晚班临期）/ morning / evening
SCAN_MODE = os.environ.get("SCAN_MODE", "auto").strip().lower()
if SCAN_MODE not in ("morning", "evening"):
    SCAN_MODE = "morning" if _bj_now.hour < 12 else "evening"

API_URL = "https://api.longxiachuxing.com/open/v1/flight/search"
HOME_CITY = "SHA"
DATA_FILE = "docs/data.json"
HISTORY_FILE = "history_longxia.json"

# 价格阈值（含税总价 lowest_price 严格小于该值才触发 Telegram 报警）
DOMESTIC_THRESHOLD = 550
INTL_THRESHOLD = 1200
EUROPE_THRESHOLD = 1800      # 欧洲线默认同为 1000（若想放宽欧洲报警，可单独改如 1800）

# 早晚班扫描天数配置（早班 7,380 次 + 晚班 2,100 次 = 9,480 次/天 < 10,000）
SCHEDULE_DAYS = {
    "morning": {"domestic": 35, "asia": 110, "europe": 60},
    "evening": {"domestic": 15, "asia": 30, "europe": 0},
}

PRICE_DROP_STEP = 30
DIRECT_ONLY_NOTIFY = False
MAX_WORKERS = 18             # 18 线程并发扛住跨海网络延迟
MIN_INTERVAL = 0.26          # 严格发包间隔 >=0.26s（最大 3.84 QPS < 5 QPS 上限）

# 国内 32 个城市
DOMESTIC_CITIES = {
    "BJS": "北京", "CAN": "广州", "SZX": "深圳", "CTU": "成都", "CKG": "重庆",
    "KMG": "昆明", "XIY": "西安", "SYX": "三亚", "XMN": "厦门", "TAO": "青岛",
    "NNG": "南宁", "KWL": "桂林", "HAK": "海口", "FOC": "福州", "LJG": "丽江",
    "KWE": "贵阳", "DLC": "大连", "SHE": "沈阳", "CGQ": "长春", "HRB": "哈尔滨",
    "LHW": "兰州", "TYN": "太原", "TSN": "天津", "NBS": "白山", "URC": "乌鲁木齐",
    "LXA": "拉萨", "SWA": "汕头", "KHN": "南昌",
    "LYA": "洛阳", "XNN": "西宁", "YZY": "张掖", "AAT": "阿勒泰",
}

# 亚洲：日本 / 东南亚 / 南亚（共 19 城）
ASIA_INTL_CITIES = {
    "OSA": "大阪", "TYO": "东京", "FUK": "福冈", "SPK": "札幌", "OKA": "冲绳",
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
    """返回 (flights_list, request_ok)"""
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
                return [], False
            body = resp.json()
            if body.get("code") != 0:
                return [], False
            return (body.get("data") or {}).get("flights") or [], True
        except Exception:
            if attempt >= 1:
                return [], False
            time.sleep(0.5)
    return [], False

def extract_flight_record(flight, task):
    cabins = flight.get("cabins") or []
    if not cabins:
        return None

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
    flights, ok = search_day(task["trip_mode"], task["from_code"], task["to_code"], task["dep_date"])
    if not ok:
        return task, [], False
    if not flights:
        return task, [], True

    picked = {}
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
    return task, list(picked.values()), True

def main():
    today = date.today()
    today_str = today.isoformat()
    cfg = SCHEDULE_DAYS[SCAN_MODE]

    tasks = []
    # 1. 国内城市
    for city in DOMESTIC_CITIES:
        for d in range(1, cfg["domestic"] + 1):
            dt = (today + timedelta(days=d)).isoformat()
            tasks.append({"trip_mode": "domestic", "threshold": DOMESTIC_THRESHOLD, "direction": "outbound", "from_code": HOME_CITY, "to_code": city, "dep_date": dt})
            tasks.append({"trip_mode": "domestic", "threshold": DOMESTIC_THRESHOLD, "direction": "inbound", "from_code": city, "to_code": HOME_CITY, "dep_date": dt})

    # 2. 亚洲国际城市
    for city in ASIA_INTL_CITIES:
        for d in range(1, cfg["asia"] + 1):
            dt = (today + timedelta(days=d)).isoformat()
            tasks.append({"trip_mode": "international", "threshold": INTL_THRESHOLD, "direction": "outbound", "from_code": HOME_CITY, "to_code": city, "dep_date": dt})
            tasks.append({"trip_mode": "international", "threshold": INTL_THRESHOLD, "direction": "inbound", "from_code": city, "to_code": HOME_CITY, "dep_date": dt})

    # 3. 欧洲/土耳其城市
    for city in EUROPE_CITIES:
        for d in range(1, cfg["europe"] + 1):
            dt = (today + timedelta(days=d)).isoformat()
            tasks.append({"trip_mode": "international", "threshold": EUROPE_THRESHOLD, "direction": "outbound", "from_code": HOME_CITY, "to_code": city, "dep_date": dt})
            tasks.append({"trip_mode": "international", "threshold": EUROPE_THRESHOLD, "direction": "inbound", "from_code": city, "to_code": HOME_CITY, "dep_date": dt})

    mode_label = "早班全量" if SCAN_MODE == "morning" else "晚班临期"
    print(f"[{mode_label}] 准备执行 {len(tasks)} 次查询 (国内{cfg['domestic']}天 / 亚洲{cfg['asia']}天 / 欧洲{cfg['europe']}天)", flush=True)

    t0 = time.monotonic()
    total_tasks = len(tasks)
    done_cnt = 0
    fresh_deals = []
    succeeded_slots = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(run_task, t) for t in tasks]
        for fut in as_completed(futures):
            task, recs, ok = fut.result()
            if ok:
                succeeded_slots.add((task["direction"], task["from_code"], task["to_code"], task["dep_date"]))
                fresh_deals.extend(recs)
            done_cnt += 1
            if done_cnt % 200 == 0 or done_cnt == total_tasks:
                elapsed = max(time.monotonic() - t0, 0.1)
                print(f"[进度] {done_cnt}/{total_tasks} | 耗时 {elapsed:.0f}s | 速率 {done_cnt/elapsed:.2f} 次/秒 | 429限流 {_rate_limit_hits} 次", flush=True)

    # 读取旧 docs/data.json 并增量合并：保留未过期的且本次未重新查询（或本次超时失败）的数据
    existing_deals = []
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                existing_deals = (json.load(f).get("deals") or [])
        except Exception as e:
            print(f"读取旧 {DATA_FILE} 失败，将重新生成: {e}")

    valid_cities = set(ALL_CITY_NAMES.keys())
    kept_deals = [
        d for d in existing_deals
        if d.get("dep_date", "") >= today_str
        and d.get("origin") in valid_cities
        and d.get("destination") in valid_cities
        and (d.get("direction"), d.get("origin"), d.get("destination"), d.get("dep_date")) not in succeeded_slots
    ]

    all_deals = kept_deals + fresh_deals

    # 重新计算每条航线中位数基准价
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
            else:
                rec["save_amount"] = 0
                rec["discount_pct"] = 0

    all_deals.sort(key=lambda x: x["price"])

    now_bj = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M 北京时间")
    out_cnt = sum(1 for d in all_deals if d["direction"] == "outbound")
    in_cnt = sum(1 for d in all_deals if d["direction"] == "inbound")
    hit_all = [d for d in all_deals if d["price"] < d["threshold"]]

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    payload = {
        "updated_at": f"{now_bj} (龙虾{mode_label})",
        "home_city": HOME_CITY,
        "stats": {
            "total": len(all_deals),
            "outbound_total": out_cnt,
            "inbound_total": in_cnt,
            "anomalies": len(hit_all),
        },
        "deals": all_deals,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"写入完成！本次刷新 {len(fresh_deals)} 条 + 保留 {len(kept_deals)} 条 = 总计 {len(all_deals)} 条（达标特价 {len(hit_all)} 条）")

    # Telegram 仅对本次新扫出来的达标特价做去重推送（支持逗号填多个 ID：个人 + 频道）
    history = {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    history = {k: v for k, v in history.items() if v.get("dep_date", "9999-99-99") >= today_str}

    fresh_hits = [d for d in fresh_deals if d["price"] < d["threshold"]]
    fresh_hits.sort(key=lambda x: x["price"])

    new_hits = []
    for d in fresh_hits:
        if DIRECT_ONLY_NOTIFY and d["transfers"] > 0:
            continue
        hkey = f"{d['direction']}|{d['origin']}|{d['destination']}|{d['dep_date']}"
        last_p = history.get(hkey, {}).get("price")
        if last_p is None or d["price"] <= last_p - PRICE_DROP_STEP:
            new_hits.append((hkey, d))

    if new_hits and TG_BOT_TOKEN and TG_CHAT_ID:
        dom_hits = [(k, d) for k, d in new_hits if d["trip_mode"] == "domestic"]
        intl_hits = [(k, d) for k, d in new_hits if d["trip_mode"] == "international"]

        lines = [f"🦞 龙虾{mode_label}快报（{now_bj}）", f"新发现达标特价：国内 {len(dom_hits)} 条 | 国际 {len(intl_hits)} 条\n"]
        if dom_hits:
            lines.append(f"🇨🇳 国内含税 < ¥{DOMESTIC_THRESHOLD}（Top 8）：")
            for _, d in dom_hits[:8]:
                stop_t = "直飞" if d["transfers"] == 0 else f"{d['transfers']}转"
                lines.append(f"• {d['origin_name']}→{d['dest_name']} | ¥{d['price']} | {d['dep_date']} ({d['airline']} {stop_t})")
        if intl_hits:
            lines.append(f"\n🌏 国际含税 < ¥{INTL_THRESHOLD}（Top 8）：")
            for _, d in intl_hits[:8]:
                stop_t = "直飞" if d["transfers"] == 0 else f"{d['transfers']}转"
                lines.append(f"• {d['origin_name']}→{d['dest_name']} | ¥{d['price']} | {d['dep_date']} ({d['airline']} {stop_t})")
        if DASHBOARD_URL:
            lines.append(f"\n📊 全部日期日历看板：{DASHBOARD_URL}")

        try:
            any_ok = False
            for cid in [c.strip() for c in TG_CHAT_ID.split(",") if c.strip()]:
                tg_resp = requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": "\n".join(lines), "disable_web_page_preview": True},
                    timeout=15,
                )
                if tg_resp.status_code != 200:
                    print(f"[Telegram 推送失败 {cid}] HTTP {tg_resp.status_code}: {tg_resp.text}")
                else:
                    print(f"[Telegram 推送成功 {cid}]")
                    any_ok = True
            if any_ok:
                for k, d in new_hits:
                    history[k] = {"price": d["price"], "dep_date": d["dep_date"]}
        except Exception as e:
            print(f"[Telegram 推送异常] {e}")
    elif not new_hits:
        print("本次暂无新增达标特价")

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
