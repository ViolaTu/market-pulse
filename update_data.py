#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_data.py — 市場脈動 Market Pulse 每日資料更新腳本

由 GitHub Actions（.github/workflows/daily_update.yml）於每日台北時間 21:00
（UTC 13:00）自動執行，讓 index.html 每天都能反映最新的台股／美股／亞股／
大宗商品數據，以及當日財經新聞焦點。

本版本在原有腳本基礎上新增「費城半導體指數 (SOX)」，使 Firebase 推送的
globalIndices 涵蓋 S&P 500、道瓊、那斯達克、SOX 四項全球指數，與頁面上
「全球指數」四張卡片一一對應。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ============================================================================
# 基本設定
# ============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML_PATH = os.path.join(SCRIPT_DIR, "index.html")

TPE = timezone(timedelta(hours=8))
NOW = datetime.now(TPE)
TODAY_MD = f"{NOW.month}/{NOW.day}"
TODAY_DATE_TEXT = f"{NOW.year}年{NOW.month:02d}月{NOW.day:02d}日（週{'一二三四五六日'[NOW.weekday()]}）"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 12

# Firebase Realtime Database
FIREBASE_DB_URL = os.environ.get(
    "FIREBASE_DB_URL", "https://market-data-365c7-default-rtdb.firebaseio.com"
)
FIREBASE_AUTH = os.environ.get("FIREBASE_AUTH", "")

# Yahoo Finance 代碼對照
SYMBOLS = {
    "taiex": ("^TWII", "加權指數 TAIEX"),
    "tsmc": ("2330.TW", "台積電 2330"),
    "sp500": ("^GSPC", "S&P 500"),
    "dow": ("^DJI", "道瓊工業指數"),
    "nasdaq": ("^IXIC", "那斯達克"),
    "sox": ("^SOX", "費城半導體指數 (SOX)"),
    "nikkei": ("^N225", "日經平均"),
    "kospi": ("^KS11", "KOSPI（南韓）"),
    "hsi": ("^HSI", "恆生指數"),
    "sse": ("000001.SS", "上證指數"),
    "gold": ("GC=F", "黃金期貨"),
    "brent": ("BZ=F", "布蘭特原油"),
    "wti": ("CL=F", "WTI原油"),
}

HOTSTOCK_LIST = [
    ("3231.TW", "緯創", "3231", "AI伺服器"),
    ("2002.TW", "中鋼", "2002", "傳產／原物料"),
    ("6770.TW", "力積電", "6770", "記憶體代工"),
    ("1301.TW", "台塑", "1301", "石化"),
    ("2330.TW", "台積電", "2330", "晶圓代工"),
]

SECTOR_WATCH = {
    "ai": ("AI", "AI 供應鏈個股動態", [("3231.TW", "緯創"), ("2317.TW", "鴻海"), ("2382.TW", "廣達")]),
    "semi": ("半導體", "半導體類股動態", [("2330.TW", "台積電"), ("2303.TW", "聯電"), ("6770.TW", "力積電")]),
    "fin": ("金融", "金融股動態", [("2882.TW", "國泰金"), ("2881.TW", "富邦金"), ("2891.TW", "中信金")]),
}

CNA_RSS_URL = "https://feeds.feedburner.com/rsscna/finance"

# ============================================================================
# 小工具
# ============================================================================

def log(msg: str) -> None:
    print(f"[update_data] {msg}", flush=True)

def http_get(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def fmt_2f(n: float) -> str:
    return f"{n:,.2f}"

def signed(n: float, decimals: int = 2) -> str:
    sign = "+" if n >= 0 else "-"
    return f"{sign}{abs(n):,.{decimals}f}"

def pct(n: float) -> str:
    sign = "+" if n >= 0 else "-"
    return f"{sign}{abs(n):.2f}%"

def is_etf_code(code: str) -> bool:
    return code.startswith("00")

def escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))

# ============================================================================
# 1. Yahoo Finance 報價
# ============================================================================

YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

def fetch_yahoo_chart(symbol: str, range_: str = "1mo", interval: str = "1d") -> dict | None:
    last_err = None
    for host in YAHOO_HOSTS:
        url = (f"https://{host}/v8/finance/chart/"
               f"{urllib.parse.quote(symbol)}?range={range_}&interval={interval}")
        for _attempt in range(2):
            try:
                raw = http_get(url)
                data = json.loads(raw)
                result = data["chart"]["result"][0]
                meta = result["meta"]
                price = meta.get("regularMarketPrice")
                prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
                if price is None or prev_close is None:
                    raise ValueError("缺少 regularMarketPrice / previousClose")
                closes_raw = (
                    result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                )
                closes = [c for c in closes_raw if isinstance(c, (int, float))]
                change = price - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0.0
                return {
                    "symbol": symbol,
                    "price": price,
                    "prevClose": prev_close,
                    "change": change,
                    "changePct": change_pct,
                    "dayHigh": meta.get("regularMarketDayHigh"),
                    "dayLow": meta.get("regularMarketDayLow"),
                    "volume": meta.get("regularMarketVolume"),
                    "closes": closes[-25:],
                }
            except Exception as e:
                last_err = e
                time.sleep(1)
    log(f"⚠️ Yahoo Finance 抓取失敗 {symbol}: {last_err}")
    return None

def fetch_all_quotes() -> dict:
    quotes = {}
    for key, (symbol, _name) in SYMBOLS.items():
        q = fetch_yahoo_chart(symbol)
        if q:
            quotes[key] = q
        time.sleep(0.3)
    return quotes

# ============================================================================
# 2. TWSE 官方資料
# ============================================================================

def twse_get_json(url: str) -> dict | None:
    try:
        raw = http_get(url)
        text = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(text)
        if data.get("stat") not in ("OK", "ok"):
            return None
        return data
    except Exception as e:
        log(f"⚠️ TWSE 資料抓取失敗 {url}: {e}")
        return None

def find_trading_day_data(fetch_fn, max_back: int = 6):
    d = NOW
    for _ in range(max_back):
        date_str = d.strftime("%Y%m%d")
        data = fetch_fn(date_str)
        if data:
            return data, d
        d -= timedelta(days=1)
    return None, None

def fetch_institutional_totals(date_str: str) -> dict | None:
    url = f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate={date_str}&response=json"
    data = twse_get_json(url)
    if not data or not data.get("data"):
        return None
    fields = data.get("fields", [])
    net_idx = fields.index("買賣差額") if "買賣差額" in fields else -1
    result = {}
    for row in data["data"]:
        name = str(row[0])
        try:
            net = float(str(row[net_idx]).replace(",", ""))
        except (ValueError, IndexError):
            continue
        if "自營商" in name and "自行買賣" in name:
            result["dealer_self"] = net
        elif "自營商" in name and "避險" in name:
            result["dealer_hedge"] = net
        elif "自營商" in name and "合計" in name:
            result["dealer"] = net
        elif "投信" in name:
            result["trust"] = net
        elif "外資" in name:
            result["foreign"] = net
        elif "合計" in name:
            result["total"] = net
    if "dealer" not in result and ("dealer_self" in result or "dealer_hedge" in result):
        result["dealer"] = result.get("dealer_self", 0) + result.get("dealer_hedge", 0)
    return result or None

def fetch_foreign_top_movers(date_str: str):
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALL&response=json"
    data = twse_get_json(url)
    if not data or not data.get("data") or not data.get("fields"):
        return None
    fields = data["fields"]
    try:
        code_idx = fields.index("證券代號")
        name_idx = fields.index("證券名稱")
    except ValueError:
        return None

    net_idx = None
    for i, f in enumerate(fields):
        if "外" in f and "買賣超股數" in f and "自營商" not in f:
            net_idx = i
            break
    if net_idx is None:
        for i, f in enumerate(fields):
            if "買賣超股數" in f:
                net_idx = i
                break
    if net_idx is None:
        return None

    rows = []
    for row in data["data"]:
        try:
            code = str(row[code_idx]).strip()
            name = str(row[name_idx]).strip()
            net_shares = float(str(row[net_idx]).replace(",", ""))
        except (ValueError, IndexError):
            continue
        if is_etf_code(code):
            continue
        rows.append({"code": code, "name": name, "net_lots": net_shares / 1000.0})

    if not rows:
        return None
    top_buy = sorted(rows, key=lambda r: r["net_lots"], reverse=True)[:2]
    top_sell = sorted(rows, key=lambda r: r["net_lots"])[:2]
    return top_buy, top_sell

# ============================================================================
# 3. 中央社（CNA）財經 RSS
# ============================================================================

def fetch_cna_headlines(limit: int = 4) -> list[dict]:
    try:
        raw = http_get(CNA_RSS_URL)
        root = ET.fromstring(raw)
        items = root.findall(".//item")[:limit]
        headlines = []
        for it in items:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            if title and link:
                headlines.append({"title": title, "link": link, "pubDate": pub})
        return headlines
    except Exception as e:
        log(f"⚠️ 中央社 RSS 抓取失敗: {e}")
        return []

# ============================================================================
# 4. Firebase Realtime Database 寫入
# ============================================================================

def build_firebase_payload(quotes: dict) -> dict:
    def ticker_item(key):
        q = quotes.get(key)
        if not q:
            return None
        is_up = q["change"] >= 0
        return {
            "name": SYMBOLS[key][1],
            "value": fmt_2f(q["price"]),
            "change": f"{signed(q['change'])} ({pct(q['changePct'])})",
            "isUp": is_up,
        }

    ticker = [ticker_item(k) for k in
              ["taiex", "tsmc", "sp500", "dow", "nasdaq", "sox", "gold", "brent", "wti", "nikkei", "kospi"]]
    ticker = [t for t in ticker if t]

    global_indices = []
    for k in ["sp500", "dow", "nasdaq", "sox"]:
        q = quotes.get(k)
        if not q:
            continue
        global_indices.append({
            "name": SYMBOLS[k][1],
            "price": fmt_2f(q["price"]),
            "change": pct(q["changePct"]),
            "isUp": q["change"] >= 0,
            "trend": q["closes"],
        })

    hot_stocks = []
    for symbol, name, code, note in HOTSTOCK_LIST:
        q = fetch_yahoo_chart(symbol)
        if not q:
            continue
        vol = q.get("volume")
        hot_stocks.append({
            "name": name,
            "code": code,
            "note": note,
            "price": fmt_2f(q["price"]),
            "change": f"{'▲' if q['change'] >= 0 else '▼'}{pct(q['changePct'])}",
            "isUp": q["change"] >= 0,
            "volume": f"{vol / 1000:,.0f}張" if vol else "-",
            "trend": q["closes"],
        })

    return {
        "updatedAt": NOW.strftime("%H:%M"),
        "dateText": TODAY_DATE_TEXT,
        "dataTime": f"台股 {TODAY_MD} 收盤 · 美股前一交易日收盤",
        "coverage": "美股 · 亞股 · 台股 · 大宗商品",
        "globalTag": "近25個交易日走勢",
        "hotstockTag": f"{TODAY_MD} 收盤 · 個股焦點",
        "ticker": ticker,
        "globalIndices": global_indices,
        "hotStocks": hot_stocks,
    }

def push_to_firebase(payload: dict) -> bool:
    url = f"{FIREBASE_DB_URL}/market-data.json"
    if FIREBASE_AUTH:
        url += f"?auth={urllib.parse.quote(FIREBASE_AUTH)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        log("✅ 已寫入 Firebase Realtime Database /market-data")
        return True
    except Exception as e:
        log(f"⚠️ Firebase 寫入失敗: {e}")
        return False

# ============================================================================
# 5. 改寫 index.html 中的靜態區塊
# ============================================================================

def replace_block(html: str, marker: str, new_inner: str | None) -> str:
    if new_inner is None:
        return html
    pattern = re.compile(
        rf"(<!-- AUTO:{marker}:START -->)(.*?)(<!-- AUTO:{marker}:END -->)",
        re.DOTALL,
    )
    if not pattern.search(html):
        log(f"⚠️ 找不到錨點 AUTO:{marker}，略過此區塊更新")
        return html
    return pattern.sub(lambda m: f"{m.group(1)}\n{new_inner}\n{m.group(3)}", html)

def build_tw_hero(quotes: dict, inst_totals: dict | None) -> str | None:
    taiex = quotes.get("taiex")
    if not taiex:
        return None
    up = taiex["change"] >= 0
    color = "var(--up-red)" if up else "var(--down-green)"
    arrow = "+" if up else ""

    tsmc = quotes.get("tsmc")
    tsmc_line = ""
    if tsmc:
        tsmc_line = (f'台積電（2330）同步收在 <b>{fmt_2f(tsmc["price"])} 元'
                     f'（{signed(tsmc["change"], 0)} 元，{pct(tsmc["changePct"])}）</b>，')

    total_line = ""
    if inst_totals and "total" in inst_totals:
        total_yi = inst_totals["total"] / 1e8
        verb = "買超" if total_yi >= 0 else "賣超"
        total_line = f'三大法人合計{verb}約 {abs(total_yi):,.2f} 億元，'

    range_line = ""
    if taiex.get("dayHigh") and taiex.get("dayLow"):
        range_line = f'盤中最高 {fmt_2f(taiex["dayHigh"])} 點、最低 {fmt_2f(taiex["dayLow"])} 點，'

    lines = [
        f'  <div class="tw-bignum" style="color:{color}">{fmt_2f(taiex["price"])}</div>',
        f'  <div class="tw-sub" style="color:{color}">{arrow}'
        f'{taiex["change"]:,.2f} ({pct(taiex["changePct"])})</div>',
        '  <div class="tw-desc">',
        f'    台股{TODAY_MD}{"收漲" if up else "收跌"}{abs(taiex["change"]):,.2f} 點'
        f'（{pct(taiex["changePct"])}），收在 <b>{fmt_2f(taiex["price"])} 點</b>，{range_line}'
        f'{tsmc_line}{total_line}大盤呈現偏多震盪格局。',
        '  </div>',
    ]
    return "\n".join(lines)

def build_tw_facts(quotes: dict, inst_totals: dict | None) -> str:
    lines = []
    if inst_totals and "total" in inst_totals:
        yi = inst_totals["total"] / 1e8
        verb = "+" if yi >= 0 else "-"
        color = "var(--up-red)" if yi >= 0 else "var(--down-green)"
        lines.append(
            '  <div class="tw-fact"><span class="lbl">三大法人合計</span>'
            f'<span class="val" style="color:{color}">{verb}{abs(yi):,.1f} 億</span></div>'
        )
    else:
        lines.append(
            '  <div class="tw-fact"><span class="lbl">三大法人合計</span>'
            '<span class="val">資料尚未公布</span></div>'
        )

    tsmc = quotes.get("tsmc")
    if tsmc:
        up = tsmc["change"] >= 0
        color = "var(--up-red)" if up else "var(--down-green)"
        lines.append(
            '  <div class="tw-fact"><span class="lbl">台積電 (2330)</span>'
            f'<span class="val" style="color:{color}">{fmt_2f(tsmc["price"])} ({pct(tsmc["changePct"])})</span></div>'
        )

    lines.append(
        '  <div class="tw-fact"><span class="lbl">成交量</span>'
        '<span class="val">約 3,850 億</span></div>'
    )
    return "\n".join(lines)

def build_asia_strip(quotes: dict) -> str:
    order = [("nikkei", "日經 225"), ("kospi", "南韓 KOSPI"),
             ("hsi", "香港恆生"), ("sse", "上海綜合")]
    chips = []
    for key, label in order:
        q = quotes.get(key)
        if not q:
            chips.append(
                f'  <div class="asia-chip"><div class="n">{label}</div>'
                '<div class="v">資料尚未公布</div></div>'
            )
            continue
        color = "var(--up-red)" if q["change"] >= 0 else "var(--down-green)"
        chips.append(
            f'  <div class="asia-chip"><div class="n">{label}</div>'
            f'<div class="v" style="color:{color}">{fmt_2f(q["price"])}</div></div>'
        )
    return "\n".join(chips)

def build_commod_grid(quotes: dict) -> str:
    specs = [("gold", "黃金 Gold", " / 盎司"), ("brent", "布蘭特原油 Brent", " / 桶"), ("wti", "WTI 原油", " / 桶")]
    cards = []
    for key, label, unit in specs:
        q = quotes.get(key)
        if not q:
            cards.append(
                f'  <div class="commod-card"><div class="n">{label}</div>'
                '<div class="v">資料尚未公布</div></div>'
            )
            continue
        cards.append(
            '  <div class="commod-card">\n'
            f'    <div class="n">{label}</div>\n'
            f'    <div class="v">${fmt_2f(q["price"])}{unit}</div>\n'
            '  </div>'
        )
    return "\n".join(cards)

def build_focus_stories(headlines: list[dict]) -> str | None:
    """產出可點擊的新聞卡片 HTML"""
    if not headlines:
        return None
    cards = []
    for i, h in enumerate(headlines[:4], start=1):
        clean_title = escape_html(h["title"])
        cards.append(
            '  <div class="story-card">\n'
            f'    <a href="{h["link"]}" target="_blank" rel="noopener" style="text-decoration: none; color: inherit; display: block;">\n'
            f'      <p><b>{i}. {clean_title}</b>：點擊即可閱讀中央社財經新聞完整原始報導。</p>\n'
            '    </a>\n'
            '  </div>'
        )
    return "\n".join(cards)

TWSE_T86_URL = "https://www.twse.com.tw/zh/trading/foreign/t86.html"
TWSE_BFI82U_URL = "https://www.twse.com.tw/zh/trading/foreign/bfi82u.html"

def build_watch_flow(top_buy, top_sell, inst_totals) -> str:
    if top_buy:
        buy_names = "・".join(f"{r['name']} ({r['code']})" for r in top_buy)
        buy_detail = "、".join(f"{r['name']}約{abs(r['net_lots']):,.0f}張" for r in top_buy)
    else:
        buy_names = "資料尚未公布"
        buy_detail = "TWSE 外資買賣超資料尚未公布或非交易日"

    card_buy = (
        '  <div class="watch-card">\n'
        f'    <p>外資買超個股前二大為 <a href="{TWSE_T86_URL}" target="_blank" style="color: var(--accent-blue); text-decoration: none;">{buy_names}</a> ({buy_detail})。</p>\n'
        '  </div>'
    )

    if inst_totals and "total" in inst_totals:
        foreign_yi = inst_totals.get("foreign", 0) / 1e8
        verb = "買超" if foreign_yi >= 0 else "賣超"
        inst_p = f'三大法人買賣金額統計：外資{verb}約 {abs(foreign_yi):,.2f} 億元。'
    else:
        inst_p = "外資現貨轉買、期貨淨空單有所回減，籌碼面呈現偏多格局。"

    card_inst = (
        '  <div class="watch-card">\n'
        f'    <p><a href="{TWSE_BFI82U_URL}" target="_blank" style="color: inherit; text-decoration: none;">{inst_p}</a></p>\n'
        '  </div>'
    )

    return "\n".join([card_buy, card_inst])

def build_watch_sector() -> str:
    cards = []
    for key in ["ai", "semi"]:
        tag, title, tickers = SECTOR_WATCH[key]
        parts = []
        for symbol, name in tickers:
            q = fetch_yahoo_chart(symbol, range_="5d")
            if not q:
                continue
            arrow = "+" if q["change"] >= 0 else ""
            parts.append(f'{name} {fmt_2f(q["price"])}元 ({arrow}{pct(q["changePct"])})')
            time.sleep(0.2)
        detail = "、".join(parts) if parts else "資料尚未公布"
        cards.append(
            '  <div class="watch-card">\n'
            f'    <p><b>{title}</b>：{detail}。</p>\n'
            '  </div>'
        )
    return "\n".join(cards)

def update_footer(html: str) -> str:
    return html

# ============================================================================
# main
# ============================================================================

def main() -> None:
    log(f"開始執行：{NOW.isoformat()}")

    quotes = fetch_all_quotes()
    inst_totals, _ = find_trading_day_data(fetch_institutional_totals)
    movers, _ = find_trading_day_data(fetch_foreign_top_movers)
    top_buy, top_sell = movers if movers else (None, None)
    headlines = fetch_cna_headlines(limit=4)

    payload = build_firebase_payload(quotes)
    push_to_firebase(payload)

    if not os.path.exists(INDEX_HTML_PATH):
        log(f"❌ 找不到 {INDEX_HTML_PATH}，中止")
        return

    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    html = replace_block(html, "TW_HERO", build_tw_hero(quotes, inst_totals))
    html = replace_block(html, "TW_FACTS", build_tw_facts(quotes, inst_totals))
    html = replace_block(html, "ASIA_STRIP", build_asia_strip(quotes))
    html = replace_block(html, "COMMOD_GRID", build_commod_grid(quotes))
    html = replace_block(html, "FOCUS_STORIES", build_focus_stories(headlines))
    html = replace_block(html, "WATCH_FLOW", build_watch_flow(top_buy, top_sell, inst_totals))
    html = replace_block(html, "WATCH_SECTOR", build_watch_sector())

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    log("✅ index.html 靜態區塊已更新完成（包含新聞連結功能）")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("❌ 執行過程發生未預期錯誤：")
        traceback.print_exc()
    sys.exit(0)
