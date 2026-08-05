"""
妙想选股 - 盘中累计观察池监控脚本（自包含 + 自带授权，可跨机器拷贝）
================================================================
依赖: 仅 httpx（可选 qrcode 用于生成授权二维码）
不依赖任何外部技能目录；首次运行会自动引导扫码授权，之后免授权。

家用电脑部署步骤（极简）:
  1) 把本文件拷到家里电脑任意目录
  2) 装依赖:  pip install httpx qrcode
  3) 运行一次:  python screener_monitor.py --force
       -> 首次会打印/生成授权二维码，手机扫码授权
  4) 再运行一次:  python screener_monitor.py --force   (此时已授权，开始工作)
  5) 建一个同样的定时任务（工作日每2分钟，脚本自动守卫交易时段）

用法:
  python screener_monitor.py            # 仅交易时段执行，命中即累计合并到观察池
  python screener_monitor.py --force    # 忽略交易时段守卫（用于手动建池/测试/首次授权）
"""
import argparse
import asyncio
import csv
import json
import os
import stat
import sys
import uuid
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx  # pip install httpx

# WORKSPACE 跟随脚本自身位置，clone 到任何目录/机器都能用
WORKSPACE = Path(__file__).resolve().parent
OUTPUT_DIR = WORKSPACE / "mx_stocks_screener"
TMP_DIR = OUTPUT_DIR / "_tmp"
MASTER_CSV = OUTPUT_DIR / "观察池_累计.csv"

MCP_URL = "https://ai-saas.eastmoney.com/proxy/b/mcp/tool/selectSecurity"
AUTH_BASE = os.environ.get("EM_AUTH_BASE", "https://ai-saas.eastmoney.com").rstrip("/")
CLIENT_ID = "mx-stocks-screener"
API_KEY_PAGE_URL = "https://ai.eastmoney.com/mxClaw"
DEFAULT_EXPIRES_IN = 30 * 24 * 60 * 60


# ---------------- 自带授权（无需外部技能） ----------------
def _mx_dir() -> Path:
    return Path.home() / ".mx-skills"


def _key_path() -> Path:
    return _mx_dir() / "em_api_key"


def _pending_path() -> Path:
    return _mx_dir() / "pending_auth.json"


def _has_valid_key() -> bool:
    if (os.environ.get("EM_API_KEY") or "").strip():
        return True
    p = _key_path()
    return p.exists() and bool(p.read_text(encoding="utf-8").strip())


def _load_api_key() -> str:
    v = (os.environ.get("EM_API_KEY") or "").strip()
    if v:
        return v
    p = _key_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _write_api_key(api_key: str) -> None:
    p = _key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(api_key.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass


def _read_pending():
    p = _pending_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _clear_pending()
        return None
    if not isinstance(data, dict) or not data.get("token"):
        _clear_pending()
        return None
    if int(data.get("expiresAt", 0)) <= int(__import__("time").time()):
        _clear_pending()
        return None
    return data


def _write_pending(token, auth_url, api_key_url, expires_in):
    p = _pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "token": token, "authUrl": auth_url, "apiKeyUrl": api_key_url,
        "expiresAt": int(__import__("time").time()) + int(expires_in) - 5,
    }, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass


def _clear_pending() -> None:
    p = _pending_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def _api_create():
    r = httpx.post(f"{AUTH_BASE}/api/auth/token/create", json={"clientId": CLIENT_ID}, timeout=30.0)
    r.raise_for_status()
    d = r.json()
    if d.get("code") not in (0, "0", 200, "200", None):
        raise RuntimeError(d.get("message") or d.get("msg") or "create 业务错误")
    data = d.get("data")
    if not data or not data.get("token") or not data.get("authUrl"):
        raise RuntimeError(f"create 未返回 token/authUrl: {data}")
    return data["token"], data["authUrl"], data.get("apiKeyUrl") or API_KEY_PAGE_URL, int(data.get("expiresIn", DEFAULT_EXPIRES_IN))


def _api_result(token):
    r = httpx.post(f"{AUTH_BASE}/api/auth/token/result", json={"token": token}, timeout=30.0)
    r.raise_for_status()
    d = r.json()
    if d.get("code") not in (0, "0", 200, "200", None):
        raise RuntimeError(d.get("message") or d.get("msg") or "result 业务错误")
    data = d.get("data") or {}
    return data.get("state") or "invalid", data.get("apiKey")


def _print_auth(auth_url, api_key_url):
    print("=" * 56)
    print("首次使用需要授权（扫码一次即可，之后免授权）:")
    print(f"  扫码授权链接: {auth_url}")
    print(f"  或手动复制 apikey: {api_key_url}")
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(auth_url)
        qr.make(fit=True)
        out = Path("auth_qr.png")
        qr.make_image(fill_color="black", back_color="white").save(out)
        print(f"  二维码已生成: {out.resolve()}  (用手机扫码)")
    except Exception:
        print("  (未安装 qrcode，可直接打开上面的链接扫码)")
    print("扫码完成后，重新运行一次本脚本即可开始监控。")
    print("=" * 56)


def ensure_api_key() -> str:
    """返回可用 key；若无则引导授权并退出，下次运行自动落盘。"""
    if _has_valid_key():
        return _load_api_key()
    pending = _read_pending()
    if pending is not None:
        try:
            state, api_key = _api_result(pending["token"])
        except Exception:
            _clear_pending()
            pending = None
        if pending is not None:
            if state == "done" and api_key:
                _write_api_key(api_key)
                _clear_pending()
                return api_key
            if state == "pending":
                _print_auth(pending.get("authUrl") or pending.get("qrUrl"), pending.get("apiKeyUrl") or API_KEY_PAGE_URL)
                sys.exit(0)
            _clear_pending()
    token, auth_url, api_key_url, exp = _api_create()
    _write_pending(token, auth_url, api_key_url, exp)
    _print_auth(auth_url, api_key_url)
    sys.exit(0)


# ---------------- 选股与累计逻辑 ----------------
QUERY = (
    "A股，剔除北交所、科创板、创业板，剔除ST股和退市股，剔除有新规风险的股票，"
    "剔除近三个月有减持计划的股票，剔除未来三个月有解禁的股票，剔除近三个月收到监管函或监管工作函的股票，"
    "涨跌幅在3%到5%之间，量比大于1.5，换手率在5%到10%之间，总市值在50亿到200亿之间，近20日有涨停"
)

MAPPING = [
    ("代码", "代码"),
    ("名称", "名称"),
    ("上市板块", "上市板块"),
    ("最新价(元)", "最新价(元)"),
    ("涨跌幅(%)", "涨跌幅(%)"),
    ("量比", "量比"),
    ("换手率(%)", "换手率(%)"),
    ("总市值", "总市值(元)"),
    ("涨停次数(近20日)", "涨停次数(次)"),
    ("ST股票", "ST股票"),
    ("退市股", "退市股"),
    ("戴帽预期(新规)", "戴帽预期(新规)"),
]
T1_COLS = [
    "次日_跟踪状态", "次日_跟踪日期",
    "次日_开盘涨跌幅", "次日_午间涨跌幅", "次日_收盘涨跌幅",
    "次日_最高涨跌幅", "次日_最低涨跌幅", "次日_形态",
]
MASTER_COLS = [m[0] for m in MAPPING] + [
    "首次入选日期", "首次入选时间", "最近入选时间", "入选次数", "入选扫描时间点"
] + T1_COLS


def now_shanghai() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=8)).replace(tzinfo=None)


def in_trading_hours(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    morning = datetime.strptime("09:30", "%H:%M").time() <= t <= datetime.strptime("11:30", "%H:%M").time()
    afternoon = datetime.strptime("13:00", "%H:%M").time() <= t <= datetime.strptime("15:00", "%H:%M").time()
    return morning or afternoon


async def mcp_call(query: str, select_type: str, api_key: str) -> dict:
    meta = {
        "query": query,
        "selectType": select_type,
        "toolContext": {
            "callId": f"call_{uuid.uuid4().hex[:8]}",
            "userInfo": {"userId": f"user_{uuid.uuid4().hex[:8]}"},
        },
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            MCP_URL, json=meta,
            headers={
                "Content-Type": "application/json",
                "em_api_key": api_key,
                "x-open-id-vendor": "tencent",
                "x-open-id-app": "workbuddy",
            },
        )
        if r.status_code == 401:
            raise RuntimeError("EM_API_KEY 失效 (HTTP 401)，需重新授权")
        try:
            payload = r.json()
        except Exception as e:
            raise RuntimeError(f"响应 JSON 解析失败: {e}") from e
        if isinstance(payload, dict):
            code = payload.get("code")
            status = payload.get("status")
            if code in (401, "401") or status in (401, "401"):
                raise RuntimeError("EM_API_KEY 失效 (业务码 401)，需重新授权")
            data = payload.get("data") or {}
            if isinstance(data, dict) and data:
                return data
        raise RuntimeError("MCP 返回为空或非预期结构")


def parse_rows(data: dict):
    result_node = (data.get("allResults") or {}).get("result") or {}
    data_list = result_node.get("dataList") or []
    columns = result_node.get("columns") or []
    if not data_list:
        return []
    col_map, order = {}, []
    for c in columns:
        if not isinstance(c, dict):
            continue
        en = c.get("field") or c.get("name") or c.get("key")
        cn = c.get("displayName") or c.get("title") or c.get("label") or en
        if c.get("dateMsg"):
            cn = f"{cn} {c['dateMsg']}"
        if en is not None and cn is not None:
            col_map[str(en)] = str(cn)
            order.append(str(en))
    rows = []
    for row in data_list:
        if not isinstance(row, dict):
            continue
        cn_row = {}
        for en in order:
            if en in row:
                v = row[en]
                cn_row[col_map[en]] = "" if v is None else (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v))
        rows.append(cn_row)
    return rows


def find_col(headers, key):
    for h in headers:
        if h == key or h.startswith(key):
            return h
    return None


def run_screener() -> list:
    api_key = ensure_api_key()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    data = asyncio.run(mcp_call(QUERY, "A股", api_key))
    rows = parse_rows(data)
    if not rows:
        return []
    headers = list(rows[0].keys())
    src_cols = {m[1]: find_col(headers, m[1]) for m in MAPPING}
    out = []
    for r in rows:
        code = (r.get(src_cols["代码"]) or "").strip()
        if not code:
            continue
        m = {}
        for mcol, key in MAPPING:
            col = src_cols[key]
            m[mcol] = (r.get(col) or "").strip() if col else ""
        out.append(m)
    return out


def load_master() -> dict:
    pool = {}
    if MASTER_CSV.exists():
        with MASTER_CSV.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = (r.get("代码") or "").strip()
                if code:
                    pool[code] = r
    return pool


def _git(args):
    """best-effort git 调用，失败仅提示，不中断监控。"""
    try:
        import subprocess
        r = subprocess.run(["git"] + args, cwd=str(WORKSPACE),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip().replace("\n", " ")
            print(f"[git] 跳过: {' '.join(args)} -> {msg[:160]}")
        return r.returncode == 0
    except Exception as e:
        print(f"[git] 跳过: {e}")
        return False


def git_sync_before():
    _git(["pull", "--no-edit"])


def git_sync_after():
    _git(["add", str(MASTER_CSV)])
    if _git(["commit", "-m", f"local: 更新观察池 {now_shanghai():%Y-%m-%d %H:%M}"]):
        _git(["pull", "--no-edit"])
        _git(["push"])


def _merge_rows(pool, rows, now):
    """把本次命中合并进 pool（in-memory），返回 (added, updated) 代码列表。"""
    ts = now.strftime("%Y-%m-%d %H:%M")
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H:%M")
    added, updated = [], []
    for m in rows:
        code = m["代码"]
        if code not in pool:
            new = dict(m)
            new.update({
                "首次入选日期": date, "首次入选时间": time,
                "最近入选时间": ts, "入选次数": "1", "入选扫描时间点": ts,
            })
            pool[code] = new
            added.append(code)
        else:
            ex = pool[code]
            ex.update(m)
            ex["入选次数"] = str(int(ex.get("入选次数", "0") or 0) + 1)
            ex["最近入选时间"] = ts
            pts = [p for p in (ex.get("入选扫描时间点") or "").split(";") if p]
            pts.append(ts)
            ex["入选扫描时间点"] = ";".join(pts)
            updated.append(code)
    return added, updated


def _write_pool(pool):
    MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    with MASTER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_COLS, extrasaction="ignore")
        w.writeheader()
        for r in pool.values():
            w.writerow(r)


def _scan_once(pool, force):
    """单次扫描并合并；返回是否有命中。"""
    now = now_shanghai()
    if not force and not in_trading_hours(now):
        print(f"[跳过] {now:%Y-%m-%d %H:%M} 非交易时段")
        return False
    print(f"[执行] {now:%Y-%m-%d %H:%M} 拉取妙想选股 ...")
    rows = run_screener()
    print(f"[结果] 本次命中 {len(rows)} 只")
    if not rows:
        return False
    added, updated = _merge_rows(pool, rows, now)
    print(f"[累计] 本次新增 {len(added)}: {added} | 刷新 {len(updated)}: {updated}")
    return True


# ---------------- T+1 次日表现跟踪 ----------------
# 思路：在股票「首次入选日的下一个交易日（T+1）」收盘后，抓取当天 OHLC + 11:30 午间价，
# 以昨收为基准换算涨跌幅，覆盖"上午涨下午跌"的日内路径（不只看收盘）。
# 数据源：腾讯自选股公开行情接口（qt.gtimg.cn 实时 + web.ifzq.gtimg.cn 分时），
# 全部以「元」为单位，无东方财富 push2 的 ×100 单位错位问题，且沙箱连通性更稳。

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _http_get_json(url, params=None, timeout=15.0, retries=3):
    import time as _t
    last = None
    for attempt in range(retries):
        try:
            r = httpx.get(url, params=params, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
    print(f"[跟踪] 请求失败 {url}: {last}")
    return None


def _http_get_text(url, timeout=15.0, retries=3):
    """腾讯 qt.gtimg.cn 返回纯文本（v_xxx="1~..."），需按文本解析。"""
    import time as _t
    last = None
    for attempt in range(retries):
        try:
            r = httpx.get(url, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            if attempt < retries - 1:
                _t.sleep(1.5 * (attempt + 1))
    print(f"[跟踪] 请求失败 {url}: {last}")
    return None


def _tx_prefix(code, plate):
    """腾讯行情代码前缀：上交所/科创板用 sh，深交所/北交所用 sz。"""
    p = plate or ""
    if ("上交所" in p) or ("沪" in p) or ("科创" in p):
        return "sh"
    return "sz"  # 深交所、北交所及默认


def _next_trading_day(d):
    """返回 d 之后的第一个交易日（排除周末与法定节假日）。"""
    try:
        import chinese_calendar as cn
        nd = d
        while True:
            nd = nd + timedelta(days=1)
            if cn.is_workday(nd):
                return nd
    except Exception:
        pass
    nd = d
    while True:
        nd = nd + timedelta(days=1)
        if nd.weekday() < 5:
            return nd


def _fetch_live(code, plate):
    """腾讯实时行情（qt.gtimg.cn）：现价/最高/最低/今开/昨收，单位均为元。
    返回字段：price 现价, high 最高, low 最低, open 今开, prev_close 昨收。"""
    pre = _tx_prefix(code, plate)
    text = _http_get_text(f"https://qt.gtimg.cn/q={pre}{code}")
    if not text:
        return None
    m = re.search(r'"([^"]*)"', text)
    if not m:
        return None
    f = m.group(1).split("~")
    if len(f) < 35:
        return None
    return {
        "price": _num(f[3]),       # 现价
        "prev_close": _num(f[4]),  # 昨收（作为 T+1 涨跌幅基准）
        "open": _num(f[5]),        # 今开
        "high": _num(f[33]),       # 最高
        "low": _num(f[34]),        # 最低
    }


def _fetch_mid_price(code, plate, target):
    """取目标日 11:30 的「原始价格（元）」，涨跌幅在调用处用同一 prev_close 计算，
    从源头避免单位错位。返回价格或 None。
    腾讯分时接口（web.ifzq.gtimg.cn）只返回最近一个交易日的数据；
    本函数仅用于 target == today 的收盘后跟踪，故天然命中目标日。"""
    pre = _tx_prefix(code, plate)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={pre}{code}"
    d = _http_get_json(url)
    if not d:
        return None
    node = d.get("data", {}).get(f"{pre}{code}", {}).get("data")
    if not isinstance(node, dict):
        return None
    # 交易日校验：仅当接口返回日 == 目标日才采信（防止跨日脏数据）
    if str(node.get("date")) != target.strftime("%Y%m%d"):
        return None
    rows = node.get("data") or []
    best = None
    for row in rows:
        parts = str(row).split()
        if len(parts) < 2:
            continue
        t = parts[0]              # "HHMM"（无冒号）
        if t <= "1130":
            best = _num(parts[1])
    if best is None:
        # 兜底：取 11:30 之后第一条（部分标的午间无独立点）
        for row in rows:
            parts = str(row).split()
            if len(parts) >= 2 and parts[0] >= "1130":
                return _num(parts[1])
        return None
    return best


def _classify_shape(open_p, mid_p, close_p, high_p, low_p):
    try:
        o = float(open_p); c = float(close_p)
        h = float(high_p) if high_p not in (None, "") else None
        l = float(low_p) if low_p not in (None, "") else None
    except (TypeError, ValueError):
        return "数据不足"
    m = None
    try:
        if mid_p not in (None, ""):
            m = float(mid_p)
    except (TypeError, ValueError):
        m = None
    # 上午涨、下午跌（用户核心场景），需要午间数据
    if m is not None and m > 0 and c < m - 0.5:
        return "冲高回落" if (h is not None and h > max(m, c) + 1) else "上午涨下午跌"
    if o > 0.5 and c > o:
        return "高开高走"
    if o > 0.5 and c < o:
        return "高开低走"
    if o < -0.5 and c > o:
        return "低开高走"
    if o < -0.5 and c < o:
        return "低开低走"
    return "震荡收涨" if c >= 0 else "震荡收跌"


def _fetch_day_minutes(code, plate, target):
    """用 day/query 回溯目标日（最近 ~5 交易日）的分时，返回原始价(元)字典：
    open/high/low/close/mid + prev_close(目标日昨收，取前一交易日收盘)。
    分时接口只能取最近几天，故仅对近期错过的 T+1 可补抓；返回 None 表示超窗口/无数据。"""
    pre = _tx_prefix(code, plate)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={pre}{code}"
    d = _http_get_json(url)
    if not d:
        return None
    days = d.get("data", {}).get(f"{pre}{code}", {}).get("data") or []
    if not isinstance(days, list):
        return None
    tgt_s = target.strftime("%Y%m%d")
    for i, day in enumerate(days):
        if not isinstance(day, dict) or day.get("date") != tgt_s:
            continue
        rows = day.get("data") or []
        prices = []
        for row in rows:
            parts = str(row).split()
            if len(parts) < 2:
                continue
            p = _num(parts[1])
            if p is not None:
                prices.append((parts[0], p))
        if not prices:
            return None
        open_p = prices[0][1]
        close_p = prices[-1][1]
        high_p = max(p for _, p in prices)
        low_p = min(p for _, p in prices)
        mid = None
        for t, p in prices:
            if t <= "1130":
                mid = p
        prev_close = None
        if i + 1 < len(days):           # 列表按日期降序，下一项即前一交易日
            nxt = days[i + 1]
            if isinstance(nxt, dict):
                nrows = nxt.get("data") or []
                if nrows:
                    last = str(nrows[-1]).split()
                    if len(last) >= 2:
                        prev_close = _num(last[1])
        return {"open": open_p, "high": high_p, "low": low_p, "close": close_p,
                "mid": mid, "prev_close": prev_close}
    return None


def _apply_t1(ex, target, prev, o, h, l, c, m, status):
    """用统一 prev_close 把原始价换算为涨跌幅并写入 T1 字段，返回形态。"""
    def _pct(v):
        return round((float(v) - prev) / prev * 100, 2) if v is not None else ""
    open_p = _pct(o)
    high_p = _pct(h)
    low_p = _pct(l)
    close_p = _pct(c)
    mid_p = _pct(m)
    shape = _classify_shape(open_p, mid_p, close_p, high_p, low_p)
    ex["次日_跟踪状态"] = status
    ex["次日_跟踪日期"] = target.strftime("%Y-%m-%d")
    ex["次日_开盘涨跌幅"] = open_p
    ex["次日_午间涨跌幅"] = mid_p if mid_p is not None else ""
    ex["次日_收盘涨跌幅"] = close_p
    ex["次日_最高涨跌幅"] = high_p
    ex["次日_最低涨跌幅"] = low_p
    ex["次日_形态"] = shape
    return shape


# 本次运行内 T+1 当天应抓但取数失败的股票（用于 CI ::error:: 告警，避免静默丢失）
T1_FETCH_FAILURES = []

def track_followups(pool, force):
    """对池中每只股票，在其 T+1 日收盘后记录次日表现；返回是否有变更。
    - target==today：优先 gtimg 实时+分时；gtimg 失败则 day/query 同源兜底。
    - target<today（错过窗口）：用 day/query 回溯补抓（最近~5交易日有效），
      成功标"已补抓"，超窗口/无数据才标"已过期"（仍可后续重试）。
    取数失败记入 T1_FETCH_FAILURES 并打印 ::error::，使 CI 运行变红可见。"""
    global T1_FETCH_FAILURES
    T1_FETCH_FAILURES = []
    now = now_shanghai()
    today = now.date()
    after_close = now.time() >= datetime.strptime("15:00", "%H:%M").time()
    changed = False
    for code, ex in pool.items():
        if (ex.get("次日_跟踪状态") or "") in ("已跟踪", "已补抓"):
            continue
        first_s = (ex.get("首次入选日期") or "").strip()
        if not first_s:
            continue
        try:
            first_d = datetime.strptime(first_s, "%Y-%m-%d").date()
        except Exception:
            continue
        plate = ex.get("上市板块") or ""
        target = _next_trading_day(first_d)
        if target > today:
            continue                      # 还没到 T+1
        if target < today:               # 错过窗口：尝试用 day/query 回溯补抓
            kb = _fetch_day_minutes(code, plate, target)
            if kb and kb.get("prev_close"):
                shape = _apply_t1(ex, target, kb["prev_close"], kb["open"],
                                  kb["high"], kb["low"], kb["close"], kb["mid"], "已补抓")
                changed = True
                print(f"[补抓] {code} T+1 回溯成功: 开{ex['次日_开盘涨跌幅']} 午{ex['次日_午间涨跌幅']} 收{ex['次日_收盘涨跌幅']} 高{ex['次日_最高涨跌幅']} 低{ex['次日_最低涨跌幅']} [{shape}]")
            else:
                if (ex.get("次日_跟踪状态") or "") != "已过期":
                    ex["次日_跟踪状态"] = "已过期"
                    ex["次日_跟踪日期"] = target.strftime("%Y-%m-%d")
                    changed = True
                print(f"::warning::T+1 回溯失败(超回溯窗口或无数据): {code}")
            continue
        # target == today：仅收盘后（或 --force）抓取，确保完整 OHLC
        if not (after_close or force):
            continue
        live = _fetch_live(code, plate)
        if not live or not live.get("prev_close"):
            kb = _fetch_day_minutes(code, plate, target)   # gtimg 失败，同源兜底
            if kb and kb.get("prev_close"):
                shape = _apply_t1(ex, target, kb["prev_close"], kb["open"],
                                  kb["high"], kb["low"], kb["close"], kb["mid"], "已跟踪")
                changed = True
                print(f"[跟踪] {code} T+1 已记录(day/query兜底): {ex['次日_开盘涨跌幅']}/{ex['次日_午间涨跌幅']}/{ex['次日_收盘涨跌幅']} [{shape}]")
                continue
            print(f"::error::T+1 取数失败(当日应抓): {code} 取不到昨收/实时行情")
            T1_FETCH_FAILURES.append(code)
            continue
        prev = live["prev_close"]
        mid_price = _fetch_mid_price(code, plate, target)  # 11:30 原始价（元）
        shape = _apply_t1(ex, target, prev, live.get("open"), live.get("high"),
                          live.get("low"), live.get("price"), mid_price, "已跟踪")
        if ex["次日_午间涨跌幅"] == "":
            print(f"::warning::T+1 午间价缺失(其余字段正常): {code}")
        changed = True
        print(f"[跟踪] {code} T+1 已记录: 开{ex['次日_开盘涨跌幅']} 午{ex['次日_午间涨跌幅']} 收{ex['次日_收盘涨跌幅']} 高{ex['次日_最高涨跌幅']} 低{ex['次日_最低涨跌幅']} [{shape}]")
    return changed


def _report_t1_failures():
    """T+1 取数失败的收尾告警：让 CI 运行变红，但不影响已完成的 git 同步。"""
    if T1_FETCH_FAILURES:
        codes = ",".join(T1_FETCH_FAILURES)
        print(f"::error::T+1 跟踪存在 {len(T1_FETCH_FAILURES)} 只当日应抓却取数失败: {codes}")
        sys.exit(1)


def _install_log_tee():
    """把 stdout/stderr 同时写到控制台（若有）与 monitor.log，保证静默运行时仍可查日志。"""
    log_path = WORKSPACE / "monitor.log"
    try:
        _log_f = log_path.open("a", encoding="utf-8")
    except Exception:
        return
    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, s):
            for st in self._streams:
                try:
                    st.write(s)
                except Exception:
                    pass
        def flush(self):
            for st in self._streams:
                try:
                    st.flush()
                except Exception:
                    pass
    sys.stdout = _Tee(sys.stdout, _log_f)
    sys.stderr = _Tee(sys.stderr, _log_f)


def main():
    _install_log_tee()
    print(f"\n--- run {now_shanghai():%Y-%m-%d %H:%M:%S} ---", flush=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略交易时段守卫")
    ap.add_argument("--loop", action="store_true",
                    help="单次运行内循环扫描（云端密集模式，约每2分钟一次）")
    ap.add_argument("--loop-interval", type=int, default=120,
                    help="循环间隔秒（默认120）")
    ap.add_argument("--loop-max-seconds", type=int, default=240,
                    help="单次运行最长持续秒（默认240，须<300避免与下次触发重叠）")
    args = ap.parse_args()

    git_sync_before()  # 拉取云端最新观察池（单次 pull；循环内只在内存累计，结束再统一写回）
    pool = load_master()

    if args.loop:
        import time as _time
        interval = max(10, int(args.loop_interval))
        max_sec = min(int(args.loop_max_seconds), 295)
        deadline = _time.monotonic() + max_sec
        iterations = 0
        while _time.monotonic() < deadline:
            try:
                _scan_once(pool, args.force)
                track_followups(pool, args.force)  # 顺带处理 T+1 跟踪（收盘后抓取）
            except Exception as e:
                print(f"[错误] {now_shanghai():%Y-%m-%d %H:%M} 单次迭代失败，跳过: {e}")
            iterations += 1
            if _time.monotonic() + interval < deadline:
                _time.sleep(interval)
            else:
                break
        _write_pool(pool)
        print(f"[循环结束] 共 {iterations} 次扫描尝试，观察池共 {len(pool)} 只")
        git_sync_after()  # 统一 commit+push 一次，与云端互不冲突
        _report_t1_failures()
        return

    # 单次模式：先处理 T+1 跟踪（不受交易时段限制，收盘后也可跑），再视情况选股
    track_followups(pool, args.force)
    now = now_shanghai()
    if not args.force and not in_trading_hours(now):
        print(f"[跳过] 当前 {now:%Y-%m-%d %H:%M} 非交易时段，仅处理 T+1 跟踪，不执行选股扫描。")
    else:
        try:
            rows = run_screener()
            print(f"[结果] 本次命中 {len(rows)} 只")
            if rows:
                _merge_rows(pool, rows, now)
        except Exception as e:
            print(f"[错误] 扫描失败: {e}")
    _write_pool(pool)
    print(f"[累计] 观察池共 {len(pool)} 只")
    git_sync_after()  # 把本地本次扫描合并回仓库，与云端互为补充
    _report_t1_failures()


if __name__ == "__main__":
    main()
