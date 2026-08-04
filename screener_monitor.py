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
MASTER_COLS = [m[0] for m in MAPPING] + [
    "首次入选日期", "首次入选时间", "最近入选时间", "入选次数", "入选扫描时间点"
]


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
                           capture_output=True, text=True, timeout=60)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略交易时段守卫")
    args = ap.parse_args()

    now = now_shanghai()
    git_sync_before()  # 拉取云端最新观察池，保证本地与仓库一致
    if not args.force and not in_trading_hours(now):
        print(f"[跳过] 当前 {now:%Y-%m-%d %H:%M} 非交易时段，本次不执行。")
        return

    print(f"[执行] {now:%Y-%m-%d %H:%M} 拉取妙想选股 ...")
    rows = run_screener()
    print(f"[结果] 本次命中 {len(rows)} 只")
    if not rows:
        print("[结束] 本次无新增命中，观察池不变。")
        return

    ts = now.strftime("%Y-%m-%d %H:%M")
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H:%M")
    pool = load_master()
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

    MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    with MASTER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_COLS, extrasaction="ignore")
        w.writeheader()
        for r in pool.values():
            w.writerow(r)

    print(f"[累计] 观察池共 {len(pool)} 只 | 本次新增 {len(added)}: {added} | 刷新 {len(updated)}: {updated}")
    print(f"[文件] {MASTER_CSV}")
    git_sync_after()  # 把本地本次扫描合并回仓库，与云端互为补充


if __name__ == "__main__":
    main()
