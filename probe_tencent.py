"""CI 连通性探针：验证腾讯自选股行情接口从当前环境可达且字段正常。

退出码非 0 表示失败（GitHub Actions 会标记为红色运行）。
覆盖三个接口：
  - qt.gtimg.cn           实时 OHLC+昨收（T+1 主数据源）
  - web.ifzq.gtimg.cn     分时（取 11:30 午间价）
  - web.ifzq.gtimg.cn     日K（用于"已过期"补抓，可回溯历史某天 OHLC）
"""
import sys
import re
import httpx

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
CODE = "sh600519"  # 贵州茅台，做连通性样本

EP_GTIMG = f"https://qt.gtimg.cn/q={CODE}"
EP_MINUTE = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={CODE}"
# 回溯源：day/query 返回最近 ~5 个交易日的分时（含 date + 分时序列），
# 可用于补抓"已过期"股票的历史 T+1 当天 OHLC 与 11:30 午间价。
EP_DAY_QUERY = f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={CODE}"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_gtimg():
    r = httpx.get(EP_GTIMG, timeout=15, headers=HEADERS)
    r.raise_for_status()
    m = re.search(r'"([^"]*)"', r.text)
    assert m, "gtimg 响应无引号内容"
    f = m.group(1).split("~")
    assert len(f) >= 35, f"gtimg 字段数不足: {len(f)}"
    price, prev = _num(f[3]), _num(f[4])
    assert price and prev and 0 < price < 100000, f"gtimg 价格异常 price={price} prev={prev}"
    print(f"[OK] gtimg 实时: 现价={price} 昨收={prev}")
    return True


def check_minute():
    r = httpx.get(EP_MINUTE, timeout=15, headers=HEADERS)
    r.raise_for_status()
    d = r.json()
    node = d.get("data", {}).get(CODE, {}).get("data")
    assert isinstance(node, dict) and node.get("date"), f"ifzq minute 无 date: {node}"
    rows = node.get("data") or []
    assert rows, "ifzq minute 无数据行"
    print(f"[OK] ifzq 分时: date={node['date']} 行数={len(rows)} 首行={rows[0]}")
    return True


def check_day_query():
    r = httpx.get(EP_DAY_QUERY, timeout=15, headers=HEADERS)
    r.raise_for_status()
    d = r.json()
    days = d.get("data", {}).get(CODE, {}).get("data") or []
    assert isinstance(days, list) and days, f"ifzq day/query 无数据: {days}"
    dates = [x.get("date") for x in days if isinstance(x, dict)]
    assert dates, "day/query 无带 date 的日"
    # 至少要包含一个"过去的交易日"（验证可回溯），且当天分时序列非空
    past = [dt for dt in dates if dt != dates[0]]
    assert past, f"day/query 仅含当天，无法验证回溯: {dates}"
    sample = next((x for x in days if isinstance(x, dict) and x.get("date") == past[0]), None)
    assert sample and sample.get("data"), f"过去日 {past[0]} 分时序列为空"
    print(f"[OK] ifzq day/query: 返回 {len(days)} 天，日期={dates}，可回溯至 {past[0]}")
    return True


def main():
    checks = [
        ("gtimg_realtime", check_gtimg),
        ("ifzq_minute", check_minute),
        ("ifzq_day_query", check_day_query),
    ]
    ok = True
    for name, fn in checks:
        try:
            fn()
        except Exception as e:
            ok = False
            print(f"[FAIL] {name}: {e}")
    if ok:
        print("==== 探针全部通过：腾讯接口从本环境可达 ====")
        sys.exit(0)
    else:
        print("==== 探针失败：腾讯接口不可达或字段异常 ====", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
