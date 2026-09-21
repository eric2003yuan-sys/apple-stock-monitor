#!/usr/bin/env python3
"""Apple Store 香港门店取货库存监控 —— GitHub Actions 版。

每次被定时任务唤起后，连续检查几分钟（每分钟一次）再退出，由下一次定时接力。
查的是 Apple 官网公开的取货端点；有货时推送 Bark(iPhone) 和微信(Server酱)。
只做查询和提醒，不会下单。

状态存在仓库里的 state.json（只在变化时提交）。密钥来自 GitHub Secrets，
代码和日志里都不出现。

环境变量:
    BARK_KEY / SERVERCHAN_KEY   推送密钥（Secrets）
    SILENT        "true" = Bark 只弹无声横幅；否则 critical 级别持续响铃
    LOOP_SECONDS  本次运行持续多久（默认 240）
    TEST_PART     测试用：换成一个确定有货的型号，只查一次、不写状态、不发微信
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PARTS = {
    "MJXY4ZA/A": {
        "name": "iPhone 18 Pro Max 1TB 银色",
        "short": "ProMax 1TB银",
        "buy_url": "https://www.apple.com/hk/shop/buy-iphone/iphone-18-pro/6.9-inch-display-1tb-silver",
    },
}
STORE_CN = {"R409": "铜锣湾", "R428": "ifc", "R499": "广东道", "R673": "apm", "R485": "又一城", "R610": "新城市广场"}
ENDPOINT = "https://www.apple.com/hk/shop/retail/pickup-message"
USER_AGENT = "iphone-stock-monitor/1.0 (personal single-user availability check)"

INTERVAL = 60
REALERT_EVERY = 300          # 持续有货时 Bark 再提醒间隔（秒）
WECHAT_REALERT_EVERY = 3600  # 微信再提醒间隔；本地 Mac 上的监控也会发，云端这边省着用
WECHAT_DAILY_CAP = 3
WECHAT_BURST = 1
FAIL_ALERT_AFTER = 5         # 连续失败几次后提醒"监控失明"

HKT = timezone(timedelta(hours=8))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
SILENT = os.environ.get("SILENT", "").strip().lower() == "true"
BARK_KEY = os.environ.get("BARK_KEY", "").strip()
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "").strip()


def now_hk():
    return datetime.now(HKT)


def log(msg):
    print(f"{now_hk():%H:%M:%S}  {msg}", flush=True)


def fetch(parts):
    q = [("pl", "true"), ("location", "Hong Kong")] + [(f"parts.{i}", p) for i, p in enumerate(parts)]
    url = ENDPOINT + "?" + urllib.parse.urlencode(q, safe="/")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    stores = data.get("body", {}).get("stores")
    if not stores:
        raise RuntimeError("响应里没有门店数据（接口可能变了）")
    result = {p: [] for p in parts}
    for s in stores:
        for p in parts:
            a = (s.get("partsAvailability") or {}).get(p)
            if a is not None:
                result[p].append({"store": s.get("storeName"), "number": s.get("storeNumber"),
                                  "available": a.get("pickupDisplay") == "available",
                                  "quote": a.get("pickupSearchQuote") or ""})
    if not any(result.values()):
        raise RuntimeError("响应里没有该型号的库存字段")
    return result


def push_bark(title, body, url=None, loud=False):
    if not BARK_KEY:
        return log(f"[Bark 未配置，跳过] {title}")
    payload = {"device_key": BARK_KEY, "title": title, "body": body, "group": "Apple库存"}
    if url:
        payload["url"] = url
    payload.update({"level": "critical", "volume": 10, "call": "1", "sound": "alarm"} if loud
                   else {"level": "timeSensitive", "sound": "silence"})
    req = urllib.request.Request("https://api.day.app/push", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).close()
        log(f"[Bark 已推送] {title}")
    except Exception as e:  # 只记录异常类型，避免任何带密钥的内容进公开日志
        log(f"[warn] Bark 推送失败: {type(e).__name__}")


def push_wechat(title, body):
    if not SERVERCHAN_KEY:
        return log(f"[微信未配置，跳过] {title}")
    m = re.match(r"^sctp(\d+)t", SERVERCHAN_KEY)
    url = (f"https://{m.group(1)}.push.ft07.com/send/{SERVERCHAN_KEY}.send" if m
           else f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send")
    data = urllib.parse.urlencode({"title": title[:32], "desp": body}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=15).close()
        log(f"[微信已推送] {title}")
    except Exception as e:
        log(f"[warn] 微信推送失败: {type(e).__name__}")


def wechat_take(state, n):
    today = f"{now_hk():%Y-%m-%d}"
    if state.get("wechat_date") != today:
        state["wechat_date"], state["wechat_used"] = today, 0
    allowed = max(0, min(n, WECHAT_DAILY_CAP - state["wechat_used"]))
    state["wechat_used"] += allowed
    return allowed


def check_once(parts, state, test=False):
    """查一次并按需提醒。返回 state 是否有变化。"""
    now = time.time()
    dirty = False
    result = fetch(parts)
    if state.get("blind"):
        push_bark("库存监控已恢复", "云端接口恢复正常，继续监控。")
    if state.get("fails") or state.get("blind"):
        state.update(fails=0, blind=False, last_error=None)
        dirty = True

    for p, rows in result.items():
        avail = [r for r in rows if r["available"]]
        now_set = sorted(r["number"] for r in avail)
        before = state["in_stock"].get(p, [])
        is_new = any(n not in before for n in now_set)
        gone = [n for n in before if n not in now_set]
        if gone:
            log(f"[售罄] {parts[p]['name']}: {'、'.join(STORE_CN.get(n, n) for n in gone)}")
        log(f"{parts[p]['name']}: " + ("有货 → " + "、".join(STORE_CN.get(n, n) for n in now_set) if now_set else "6 店均无货"))

        if is_new or (now_set and now - state["last_alert"].get(p, 0) >= REALERT_EVERY):
            names = [STORE_CN.get(r["number"], r["store"]) for r in avail]
            where = "、".join(names[:2]) + (f"等{len(names)}店" if len(names) > 2 else "")  # Server酱标题上限 32 字
            title = f"🔴有货｜{parts[p]['short']}｜{where}"
            push_bark(title, "；".join(f"{STORE_CN.get(r['number'], r['store'])}（{r['quote']}）" for r in avail),
                      url=parts[p]["buy_url"], loud=not SILENT)
            want = 0 if test else (WECHAT_BURST if is_new else
                                   (1 if now - state["last_wechat"].get(p, 0) >= WECHAT_REALERT_EVERY else 0))
            if want and wechat_take(state, want):
                md = "\n\n".join(f"- **{STORE_CN.get(r['number'], r['store'])}**（{r['store']}）：{r['quote']}" for r in avail)
                push_wechat(title, f"{md}\n\n### [👉 点这里去下单]({parts[p]['buy_url']})\n\n{now_hk():%H:%M:%S} 发现（云端监控）")
                state["last_wechat"][p] = now
            state["last_alert"][p] = now
            dirty = True
        if before != now_set:
            state["in_stock"][p] = now_set
            dirty = True
    return dirty


def main():
    test_part = os.environ.get("TEST_PART", "").strip()
    parts = ({test_part: {"name": "测试配件", "short": "测试配件", "buy_url": "https://www.apple.com/hk/"}}
             if test_part else PARTS)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    if test_part:
        state = {}  # 测试不读也不写真实状态
    for k, v in {"in_stock": {}, "last_alert": {}, "last_wechat": {}, "fails": 0, "blind": False,
                 "wechat_date": "", "wechat_used": 0}.items():
        state.setdefault(k, v)

    loop_seconds = 0 if test_part else int(os.environ.get("LOOP_SECONDS", "240"))
    log(f"开始 | 静音={'开' if SILENT else '关'} | Bark={'有' if BARK_KEY else '无'} | 微信={'有' if SERVERCHAN_KEY else '无'}"
        + (f" | 测试型号 {test_part}" if test_part else ""))
    started, dirty, ok = time.time(), False, True
    while True:
        try:
            dirty |= check_once(parts, state, test=bool(test_part))
            ok = True
        except Exception as e:
            ok = False
            log(f"[错误] {type(e).__name__}: {e}")
            # 失明后不再累加：万一长期被拦，也不会每次运行都往仓库提交一次
            if not state["blind"]:
                state["fails"] += 1
                state["last_error"] = f"{now_hk():%m-%d %H:%M} {type(e).__name__}: {e}"
                if state["fails"] >= FAIL_ALERT_AFTER:
                    state["blind"] = True
                    push_bark("库存监控失明", f"云端已连续 {state['fails']} 次查询失败：{type(e).__name__}")
                dirty = True
        if time.time() - started + INTERVAL > loop_seconds:
            break
        time.sleep(INTERVAL)

    if dirty and not test_part:
        state["updated"] = f"{now_hk():%Y-%m-%d %H:%M:%S} HKT"
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log("state.json 有变化，已写入")
    if not ok:
        log("本次运行以查询失败结束（不标红，避免 GitHub 失败邮件刷屏；连续失败会走 Bark 提醒）")


if __name__ == "__main__":
    main()
