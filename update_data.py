#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_data.py — 市場脈動 Market Pulse 每日資料更新腳本

由 GitHub Actions（.github/workflows/daily_update.yml）於每日台北時間 21:00
（UTC 13:00）自動執行，讓 index.html 每天都能反映最新的台股／美股／亞股／
大宗商品數據，以及當日財經新聞焦點。

腳本做兩件事：

1. 即時資料 → 寫入 Firebase Realtime Database
   index.html 內建的前端 JS（<script type="module"> 區塊）會自動監聽
   Firebase 的 /market-data 節點並即時渲染：頁首日期／資料時間、跑馬燈、
   全球三大指數卡片、即時熱門股表格。本腳本負責把最新數字寫進這個節點。
   　→ 若 Firebase 規則需要驗證才能寫入，請在 GitHub Secrets 新增
      FIREBASE_AUTH（Realtime Database Secret 或具寫入權限的 ID token），
      腳本會自動帶上；若資料庫規則本來就允許公開寫入，則不需要設定。

2. 靜態內容 → 直接改寫 index.html
   台股焦點大數字／三大法人與台積電速覽／亞股 strip／大宗商品卡片／
   今日四大焦點／兩組觀察名單，這些區塊是純靜態 HTML，本腳本利用
   「<!-- AUTO:XXX:START/END -->」註解錨點，安全地找到對應區塊並整段覆寫，
   其餘手動撰寫的版面、CSS、既有敘述文字完全不受影響。

資料來源：
   - Yahoo Finance v8 chart API（台股加權指數、台積電、美股三大指數、
     四個亞股指數、黃金／布蘭特／WTI 期貨、觀察名單代表個股）
   - 台灣證券交易所（TWSE）官方 JSON：
     三大法人買賣金額統計表（BFI82U）、外資及陸資買賣超彙總表（T86）
   - 中央社（CNA）財經 RSS：作為「今日四大焦點」的新聞標題與連結來源

設計原則：
   - 只用 Python 標準函式庫（urllib / json / re / datetime / xml.etree），
     不需要額外 pip install，可在 GitHub Actions 的 ubuntu-latest +
     actions/setup-python 環境中直接執行。
   - 每個資料來源都包在 try/except 中，單一來源失敗不會讓整支腳本中斷；
     失敗時會保留 index.html 原本內容並印出警告，絕不寫入捏造的假資料。
   - 「今日四大焦點」的標題與連結一律直接取自中央社 RSS 原始資料，
     腳本不會自行編造新聞內容或分析。
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

# Firebase Realtime Database（與 index.html 內 firebaseConfig 相同）
FIREBASE_DB_URL = os.environ.get(
    "FIREBASE_DB_URL", "https://market-data-365c7-default-rtdb.firebaseio.com"
)
FIREBASE_AUTH = os.environ.get("FIREBASE_AUTH", "")  # 選填，見上方說明

# Yahoo Finance 代碼對照：key → (symbol, 顯示名稱)
SYMBOLS = {
    "taiex":  ("^TWII",     "加權指數 TAIEX"),
    "tsmc":   ("2330.TW",   "台積電 2330"),
    "sp500":  ("^GSPC",     "S&P 500"),
    "dow":    ("^DJI",      "道瓊工業指數"),
    "nasdaq": ("^IXIC",     "那斯達克"),
    "nikkei": ("^N225",     "日經平均"),
    "kospi":  ("^KS11",     "KOSPI（南韓）"),
    "hsi":    ("^HSI",      "恆生指數"),
    "sse":    ("000001.SS", "上證指數"),
    "gold":   ("GC=F",      "黃金期貨"),
    "brent":  ("BZ=F",      "布蘭特原油"),
    "wti":    ("CL=F",      "WTI原油"),
}

# 「即時熱門股動態」表格用的個股清單（寫入 Firebase /market-data/hotStocks）
HOTSTOCK_LIST = [
    ("3231.TW", "緯創", "3231", "AI伺服器"),
    ("2002.TW", "中鋼", "2002", "傳產／原物料"),
    ("6770.TW", "力積電", "6770", "記憶體代工"),
    ("1301.TW", "台塑", "1301", "石化"),
    ("2330.TW", "台積電", "2330", "晶圓代工"),
]

# 觀察名單「產業主題」用的代表個股：僅回報當日真實漲跌幅，不做主觀新聞判斷
SECTOR_WATCH = {
    "ai":   ("AI", "AI 供應鏈個股動態", [("3231.TW", "緯創"), ("2317.TW", "鴻海"), ("2382.TW", "廣達")]),
    "semi": ("半導體", "半導體類股動態", [("2330.TW", "台積電"), ("2303.TW", "聯電"), ("6770.TW", "力積電")]),
    "fin":  ("金融", "金融股動態", [("2882.TW", "國泰金"), ("2881.TW", "富邦金"), ("2891.TW", "中信金")]),
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
    # 台股 ETF 代碼慣例以 00 開頭（0050、0056、00981A...），一般個股不會以此開頭
    return code.startswith("00")


def escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


# ============================================================================
# 1. Yahoo Finance 報價（美股／亞股／大宗商品／台股個股皆適用）
# ============================================================================

YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]


def fetch_yahoo_chart(symbol: str, range_: str = "1mo", interval: str = "1d") -> dict | None:
    """回傳 dict：price/prevClose/change/changePct/dayHigh/dayLow/volume/closes(list)。
    失敗（含逾時、格式異常）回傳 None，呼叫端須自行處理缺值，不可假設一定成功。
    """
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
            except Exception as e:  # noqa: BLE001 - 單一標的失敗不可讓整支腳本掛掉
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
        time.sleep(0.3)  # 放慢節奏，降低被 Yahoo 判定為濫用流量的機率
    return quotes


# ============================================================================
# 2. TWSE 官方資料：三大法人買賣金額統計表（BFI82U）／外資買賣超日報（T86）
#    以「欄位名稱比對」取值而非寫死欄位順序，較能適應 TWSE 未來調整欄位。
# ============================================================================

def twse_get_json(url: str) -> dict | None:
    try:
        raw = http_get(url)
        text = raw.decode("utf-8-sig", errors="replace")
        data = json.loads(text)
        if data.get("stat") not in ("OK", "ok"):
            return None  # 非交易日或尚未公布時，TWSE 會回傳非 OK 的 stat
        return data
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ TWSE 資料抓取失敗 {url}: {e}")
        return None


def find_trading_day_data(fetch_fn, max_back: int = 6):
    """由今天往前最多找 max_back 天，直到抓到有效資料（跳過假日／尚未公布）。"""
    d = NOW
    for _ in range(max_back):
        date_str = d.strftime("%Y%m%d")
        data = fetch_fn(date_str)
        if data:
            return data, d
        d -= timedelta(days=1)
    return None, None


def fetch_institutional_totals(date_str: str) -> dict | None:
    """三大法人買賣金額統計表 → 外資/投信/自營商/合計 買賣超金額（元，正負皆有意義）"""
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
    """個股外資及陸資買賣超彙總表(T86) → 回傳 (top_buy[], top_sell[])，皆排除ETF。
    找不到當日資料（假日／尚未公布）時回傳 None。
    """
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
# 3. 中央社（CNA）財經 RSS → 今日四大焦點的新聞標題／連結
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
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ 中央社 RSS 抓取失敗（若持續失敗請確認 CNA_RSS_URL 是否已變更）: {e}")
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
              ["taiex", "tsmc", "sp500", "dow", "nasdaq", "gold", "brent", "wti", "nikkei", "kospi"]]
    ticker = [t for t in ticker if t]

    global_indices = []
    for k in ["sp500", "dow", "nasdaq"]:
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
    except urllib.error.HTTPError as e:
        log(f"⚠️ Firebase 寫入失敗 HTTP {e.code}: {e.read()[:300]!r}"
            f"（若資料庫規則需要驗證，請在 GitHub Secrets 設定 FIREBASE_AUTH）")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ Firebase 寫入失敗: {e}")
        return False


# ============================================================================
# 5. 改寫 index.html 中的靜態區塊（以 <!-- AUTO:XXX:START/END --> 為錨點）
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
    color = "var(--gain)" if up else "var(--loss)"
    arrow = "▲" if up else "▼"

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
        f'          <div class="tw-bignum" style="color:{color}">{fmt_2f(taiex["price"])}</div>',
        f'          <div class="tw-sub" style="color:{color}">{arrow} '
        f'{abs(taiex["change"]):,.2f} 點（{pct(taiex["changePct"])}）</div>',
        '          <div class="tw-desc">',
        f'            台股{TODAY_MD}{"收漲" if up else "收跌"}{abs(taiex["change"]):,.2f} 點'
        f'（{pct(taiex["changePct"])}），收在 <b>{fmt_2f(taiex["price"])} 點</b>，{range_line}'
        f'{tsmc_line}{total_line}以上數據由腳本於台北時間 {NOW.strftime("%H:%M")} 自動擷取自 '
        'Yahoo Finance 與台灣證券交易所公開資料，完整新聞脈絡請見下方「今日四大焦點」。',
        '          </div>',
    ]
    return "\n".join(lines)


def build_tw_facts(quotes: dict, inst_totals: dict | None) -> str:
    lines = []
    if inst_totals and "total" in inst_totals:
        yi = inst_totals["total"] / 1e8
        verb = "買超" if yi >= 0 else "賣超"
        color = "var(--gain)" if yi >= 0 else "var(--loss)"
        lines.append(
            '          <div class="tw-fact"><span class="lbl">三大法人合計</span>'
            f'<span class="val" style="color:{color}">{verb}約{abs(yi):,.2f}億元</span></div>'
        )
    else:
        lines.append(
            '          <div class="tw-fact"><span class="lbl">三大法人合計</span>'
            '<span class="val">資料尚未公布</span></div>'
        )

    tsmc = quotes.get("tsmc")
    if tsmc:
        up = tsmc["change"] >= 0
        color = "var(--gain)" if up else "var(--loss)"
        lines.append(
            '          <div class="tw-fact"><span class="lbl">台積電 (2330)</span>'
            f'<span class="val" style="color:{color}">{TODAY_MD} 收{fmt_2f(tsmc["price"])}元'
            f'（{signed(tsmc["change"], 0)}元，{pct(tsmc["changePct"])}）</span></div>'
        )

    lines.append(
        '          <div class="tw-fact"><span class="lbl">資料時間</span>'
        f'<span class="val">腳本於台北時間 {NOW.strftime("%H:%M")} 自動更新</span></div>'
    )
    lines.append(
        '          <div class="tw-fact"><span class="lbl">市場話題</span>'
        '<span class="val">詳見下方「今日四大焦點」</span></div>'
    )
    return "\n".join(lines)


def build_asia_strip(quotes: dict) -> str:
    order = [("nikkei", "日經平均"), ("kospi", "KOSPI（南韓）"),
             ("hsi", "恆生指數"), ("sse", "上證指數")]
    chips = []
    for key, label in order:
        q = quotes.get(key)
        if not q:
            chips.append(
                f'        <div class="asia-chip"><div class="n">{label}</div>'
                '<div class="v">資料尚未公布</div></div>'
            )
            continue
        up = q["change"] >= 0
        color = "var(--gain)" if up else "var(--loss)"
        arrow = "▲" if up else "▼"
        chips.append(
            f'        <div class="asia-chip"><div class="n">{label}</div>'
            f'<div class="v" style="color:{color}">{fmt_2f(q["price"])}・{arrow}{pct(q["changePct"])}'
            '</div></div>'
        )
    return "\n".join(chips)


def build_commod_grid(quotes: dict) -> str:
    specs = [("gold", "黃金 Gold（期貨，GC=F）"), ("brent", "布蘭特原油 Brent"), ("wti", "WTI 原油")]
    cards = []
    for key, label in specs:
        q = quotes.get(key)
        if not q:
            cards.append(
                f'        <div class="commod-card"><div class="n">{label}</div>'
                '<div class="v">資料尚未公布</div></div>'
            )
            continue
        up = q["change"] >= 0
        cls = "up" if up else "down"
        arrow = "▲" if up else "▼"
        cards.append(
            '        <div class="commod-card">\n'
            f'          <div class="n">{label}</div>\n'
            f'          <div class="v">${fmt_2f(q["price"])}</div>\n'
            f'          <div class="c {cls}">{arrow} {pct(q["changePct"])}</div>\n'
            f'          <p>資料取自 Yahoo Finance，台北時間 {NOW.strftime("%H:%M")} 自動更新，'
            f'前一交易日收盤價 ${fmt_2f(q["prevClose"])}。</p>\n'
            '        </div>'
        )
    return "\n".join(cards)


def build_focus_stories(headlines: list[dict]) -> str | None:
    if not headlines:
        return None  # 沒抓到新聞就保留原本內容，不覆蓋成空白
    cards = []
    for i, h in enumerate(headlines[:4], start=1):
        cards.append(
            '        <div class="story-card">\n'
            f'          <span class="idx">{i:02d} · 中央社焦點</span>\n'
            f'          <h3><a class="src-link" href="{h["link"]}" target="_blank" '
            f'rel="noopener">{escape_html(h["title"])}</a></h3>\n'
            f'          <p>中央社財經新聞{(" · " + escape_html(h["pubDate"])) if h["pubDate"] else ""}，'
            '點擊標題可閱讀原文完整報導。</p>\n'
            '          <span class="kicker gold">中央社 CNA</span>\n'
            '        </div>'
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
        '        <div class="watch-card">\n'
        '          <span class="watch-tag">買超</span>\n'
        '          <div class="watch-body">\n'
        f'            <h4>{buy_names}</h4>\n'
        f'            <p>依 <a class="src-link" href="{TWSE_T86_URL}" target="_blank" '
        f'rel="noopener">台灣證券交易所外資及陸資買賣超彙總表</a>，{TODAY_MD} 外資買超個股'
        f'（排除ETF）前二大為 {buy_detail}。</p>\n'
        '          </div>\n'
        '        </div>'
    )

    def sell_card(tag: str, stock):
        if not stock:
            return (
                f'        <div class="watch-card">\n          <span class="watch-tag">{tag}</span>\n'
                '          <div class="watch-body">\n            <h4>資料尚未公布</h4>\n'
                '            <p>TWSE 外資買賣超資料尚未公布或非交易日。</p>\n          </div>\n        </div>'
            )
        return (
            '        <div class="watch-card">\n'
            f'          <span class="watch-tag">{tag}</span>\n'
            '          <div class="watch-body">\n'
            f'            <h4>{stock["name"]} ({stock["code"]})</h4>\n'
            f'            <p>依 <a class="src-link" href="{TWSE_T86_URL}" target="_blank" '
            f'rel="noopener">TWSE 外資及陸資買賣超彙總表</a>，{TODAY_MD} 外資賣超約'
            f'{abs(stock["net_lots"]):,.0f} 張。</p>\n'
            '          </div>\n'
            '        </div>'
        )

    s1 = top_sell[0] if top_sell else None
    s2 = top_sell[1] if top_sell and len(top_sell) > 1 else None
    card_sell1 = sell_card("賣超冠軍", s1)
    card_sell2 = sell_card("賣超居次", s2)

    if inst_totals and "total" in inst_totals:
        foreign_yi = inst_totals.get("foreign", 0) / 1e8
        trust_yi = inst_totals.get("trust", 0) / 1e8
        dealer_yi = inst_totals.get("dealer", 0) / 1e8
        total_yi = inst_totals["total"] / 1e8

        def verb(v):
            return "買超" if v >= 0 else "賣超"

        inst_p = (f'{TODAY_MD}三大法人合計{verb(total_yi)}約{abs(total_yi):,.2f}億元：'
                  f'外資{verb(foreign_yi)}{abs(foreign_yi):,.2f}億元、'
                  f'投信{verb(trust_yi)}{abs(trust_yi):,.2f}億元、'
                  f'自營商{verb(dealer_yi)}{abs(dealer_yi):,.2f}億元。')
    else:
        inst_p = "TWSE 三大法人買賣金額統計表資料尚未公布或非交易日。"

    card_inst = (
        '        <div class="watch-card">\n'
        '          <span class="watch-tag">三大法人</span>\n'
        '          <div class="watch-body">\n'
        '            <h4>整體籌碼動能</h4>\n'
        f'            <p>依 <a class="src-link" href="{TWSE_BFI82U_URL}" target="_blank" '
        f'rel="noopener">TWSE 三大法人買賣金額統計表</a>，{inst_p}</p>\n'
        '          </div>\n'
        '        </div>'
    )

    return "\n".join([card_buy, card_sell1, card_sell2, card_inst])


def build_watch_sector() -> str:
    cards = []
    for key in ["ai", "semi", "fin"]:
        tag, title, tickers = SECTOR_WATCH[key]
        parts = []
        for symbol, name in tickers:
            q = fetch_yahoo_chart(symbol, range_="5d")
            if not q:
                continue
            arrow = "▲" if q["change"] >= 0 else "▼"
            parts.append(f'{name} {fmt_2f(q["price"])}元（{arrow}{pct(q["changePct"])}）')
            time.sleep(0.2)
        detail = "、".join(parts) if parts else "資料尚未公布"
        cards.append(
            '        <div class="watch-card">\n'
            f'          <span class="watch-tag">{tag}</span>\n'
            '          <div class="watch-body">\n'
            f'            <h4>{title}</h4>\n'
            f'            <p>{TODAY_MD} 代表個股收盤：{detail}。（依 Yahoo Finance 即時報價自動'
            '彙整，僅反映當日價格變動，非新聞事件解讀）</p>\n'
            '          </div>\n'
            '        </div>'
        )
    return "\n".join(cards)


def update_section_tags(html: str) -> str:
    html = re.sub(
        r'(<h2>台股焦點</h2>\s*<span class="tag">)[^<]+(</span>)',
        rf'\g<1>{TODAY_MD} 收盤快訊\g<2>', html,
    )
    html = re.sub(
        r'(<h2>大宗商品與利率</h2>\s*<span class="tag">)[^<]+(</span>)',
        rf'\g<1>{TODAY_MD} 最新\g<2>', html,
    )
    return html


def update_footer(html: str) -> str:
    today_zh = f"{NOW.year} 年 {NOW.month} 月 {NOW.day} 日"
    html = re.sub(
        r'(數據擷取自公開財經媒體與台灣證券交易所於 )[^，]+(之報導)',
        lambda m: f"{m.group(1)}{today_zh}{m.group(2)}",
        html,
    )
    return html


# ============================================================================
# main
# ============================================================================

def main() -> None:
    log(f"開始執行：{NOW.isoformat()}")

    quotes = fetch_all_quotes()
    if "taiex" not in quotes:
        log("⚠️ 無法取得台股加權指數資料，台股相關靜態區塊將維持原內容")

    inst_totals, inst_date = find_trading_day_data(fetch_institutional_totals)
    if inst_date:
        log(f"三大法人資料日期：{inst_date.strftime('%Y-%m-%d')}")

    movers, movers_date = find_trading_day_data(fetch_foreign_top_movers)
    top_buy, top_sell = movers if movers else (None, None)
    if movers_date:
        log(f"外資買賣超資料日期：{movers_date.strftime('%Y-%m-%d')}")

    headlines = fetch_cna_headlines(limit=4)
    log(f"取得 {len(headlines)} 則中央社財經新聞標題")

    # ---- 1) 寫入 Firebase（供跑馬燈／全球指數卡／熱門股表格即時渲染） ----
    payload = build_firebase_payload(quotes)
    push_to_firebase(payload)

    # ---- 2) 改寫 index.html 靜態區塊 ----
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
    html = update_section_tags(html)
    html = update_footer(html)

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    log("✅ index.html 靜態區塊已更新完成")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - 確保任何未預期錯誤都不會讓 workflow 直接爆掉
        log("❌ 執行過程發生未預期錯誤：")
        traceback.print_exc()
        sys.exit(0)  # 讓 workflow 的後續步驟仍可正常判斷「無變更」而略過 commit
