#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股开盘前 · 美股核心板块晨报生成器
====================================
每个A股交易日开盘前运行(建议北京时间 08:00-08:45),自动:
  1. 抓取隔夜美股核心指数 / 板块 / 关键个股 / 宏观资产数据(yfinance)
  2. 通过规则引擎推导对 A股相关板块与代表个股的传导方向
  3. (可选)调用 Claude API 生成一段综合点评
  4. 输出自包含的 HTML 晨报到 reports/ 目录

依赖:  pip install yfinance
可选:  export ANTHROPIC_API_KEY=sk-ant-...   (启用 AI 综合点评)
用法:
  python3 us_premarket_report.py            # 正式抓取
  python3 us_premarket_report.py --demo     # 用内置示例数据演示版式
  python3 us_premarket_report.py --out DIR  # 指定输出目录

⚠️ 免责声明:本工具仅做公开行情数据整理与逻辑映射,不构成任何投资建议。
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BJT = ZoneInfo("Asia/Shanghai")

# ---------------------------------------------------------------------------
# 1. 监控清单:美股核心标的
# ---------------------------------------------------------------------------
WATCHLIST = {
    "核心指数": [
        ("^GSPC", "标普500"),
        ("^IXIC", "纳斯达克"),
        ("^DJI", "道琼斯"),
        ("^SOX", "费城半导体"),
    ],
    "宏观资产": [
        ("DX-Y.NYB", "美元指数"),
        ("^TNX", "10Y美债收益率"),
        ("CNH=X", "离岸人民币"),
        ("GC=F", "COMEX黄金"),
        ("CL=F", "WTI原油"),
    ],
    "AI/算力链": [
        ("NVDA", "英伟达"),
        ("AMD", "超威半导体"),
        ("AVGO", "博通"),
        ("TSM", "台积电ADR"),
        ("MU", "美光"),
        ("VRT", "维谛技术"),
        ("SMCI", "超微电脑"),
        ("ANET", "Arista"),
    ],
    "科技巨头": [
        ("MSFT", "微软"),
        ("META", "Meta"),
        ("GOOGL", "谷歌"),
        ("AMZN", "亚马逊"),
        ("AAPL", "苹果"),
        ("TSLA", "特斯拉"),
        ("ORCL", "甲骨文"),
    ],
    "中概股": [
        ("BABA", "阿里巴巴"),
        ("PDD", "拼多多"),
        ("JD", "京东"),
        ("BIDU", "百度"),
        ("KWEB", "中概互联ETF"),
    ],
}

# ---------------------------------------------------------------------------
# 2. 传导规则:美股信号 → A股板块映射
#    每条规则: 名称 / 信号源ticker列表 / 权重方式 / 映射的A股板块与代表个股
#    direction: same=同向传导  inverse=反向传导
# ---------------------------------------------------------------------------
RULES = [
    {
        "signal_name": "费城半导体 + 核心芯片股",
        "tickers": ["^SOX", "NVDA", "AMD", "AVGO", "TSM", "MU"],
        "direction": "same",
        "sectors": [
            {"sector": "半导体设计/制造", "stocks": "中芯国际(688981)、海光信息(688041)、寒武纪(688256)、北方华创(002371)"},
            {"sector": "存储芯片", "stocks": "兆易创新(603986)、江波龙(301308)、佰维存储(688525)"},
        ],
        "logic": "美股半导体是A股芯片板块最直接的隔夜锚,SOX与核心芯片股的合力方向通常在A股开盘竞价即有映射。",
    },
    {
        "signal_name": "英伟达 + AI基建资本开支链",
        "tickers": ["NVDA", "AVGO", "VRT", "SMCI", "ANET"],
        "direction": "same",
        "sectors": [
            {"sector": "CPO/光模块", "stocks": "中际旭创(300308)、新易盛(300502)、天孚通信(300394)"},
            {"sector": "IDC/算力租赁", "stocks": "数据港(603881)、润泽科技(300442)、光环新网(300383)、润建股份(002929)"},
            {"sector": "服务器/液冷电源", "stocks": "浪潮信息(000977)、工业富联(601138)、英维克(002837)、麦格米特(002851)"},
        ],
        "logic": "NVDA代表AI算力需求预期;VRT/SMCI/ANET反映数据中心电源、整机与网络设备景气度,直接映射A股算力基建链。",
    },
    {
        "signal_name": "云巨头(资本开支主力)",
        "tickers": ["MSFT", "META", "GOOGL", "AMZN", "ORCL"],
        "direction": "same",
        "sectors": [
            {"sector": "AI应用/云计算", "stocks": "金山办公(688111)、科大讯飞(002230)、深桑达A(000032)"},
            {"sector": "算力基建(资本开支预期)", "stocks": "数据港(603881)、润泽科技(300442)、中际旭创(300308)"},
        ],
        "logic": "四大云厂+甲骨文是全球AI资本开支的资金来源,其股价隐含市场对Capex持续性的定价,影响A股算力链中期预期。",
    },
    {
        "signal_name": "特斯拉",
        "tickers": ["TSLA"],
        "direction": "same",
        "sectors": [
            {"sector": "汽车零部件/机器人", "stocks": "拓普集团(601689)、三花智控(002050)、鸣志电器(603728)"},
            {"sector": "锂电池链", "stocks": "宁德时代(300750)、亿纬锂能(300014)"},
        ],
        "logic": "特斯拉波动传导至A股T链与人形机器人概念,大涨大跌时映射明显。",
    },
    {
        "signal_name": "苹果",
        "tickers": ["AAPL"],
        "direction": "same",
        "sectors": [
            {"sector": "消费电子果链", "stocks": "立讯精密(002475)、歌尔股份(002241)、蓝思科技(300433)"},
        ],
        "logic": "苹果隔夜表现影响果链情绪,财报与出货数据节点尤甚。",
    },
    {
        "signal_name": "中概股/中国资产风险偏好",
        "tickers": ["BABA", "PDD", "JD", "BIDU", "KWEB"],
        "direction": "same",
        "sectors": [
            {"sector": "互联网/消费(经港股映射)", "stocks": "恒生科技相关ETF、白酒消费(贵州茅台600519等)情绪面"},
            {"sector": "AI应用(百度链)", "stocks": "汉得信息(300170)、每日互动(300766)等情绪联动"},
        ],
        "logic": "中概隔夜表现反映海外资金对中国资产的风险偏好,先传导港股,再影响A股北向情绪。",
    },
    {
        "signal_name": "美元指数 + 10Y美债收益率",
        "tickers": ["DX-Y.NYB", "^TNX"],
        "direction": "inverse",
        "sectors": [
            {"sector": "A股整体流动性/成长股估值", "stocks": "北向资金敏感型权重与高估值成长(算力、创新药)"},
        ],
        "logic": "美元与美债收益率走强通常压制新兴市场流动性与成长股估值,为反向传导。",
    },
    {
        "signal_name": "离岸人民币(CNH)",
        "tickers": ["CNH=X"],
        "direction": "inverse",
        "sectors": [
            {"sector": "外资流入敏感板块", "stocks": "沪深300权重、消费白马"},
        ],
        "logic": "USDCNH上行=人民币贬值,通常抑制外资流入;下行=升值,利好核心资产。",
    },
    {
        "signal_name": "COMEX黄金",
        "tickers": ["GC=F"],
        "direction": "same",
        "sectors": [
            {"sector": "黄金/有色", "stocks": "紫金矿业(601899)、山东黄金(600547)、赤峰黄金(600988)"},
        ],
        "logic": "金价与A股黄金股同向性强,隔夜大波动次日开盘映射直接。",
    },
    {
        "signal_name": "WTI原油",
        "tickers": ["CL=F"],
        "direction": "same",
        "sectors": [
            {"sector": "石油石化/油服", "stocks": "中国海油(600938)、中海油服(601808)、广汇能源(600256)"},
        ],
        "logic": "油价传导至A股油气开采与油服板块,同时反向影响航空、物流成本预期。",
    },
]

# 信号强度阈值(信号源合力涨跌幅,%)
TH_STRONG, TH_MILD = 1.5, 0.5


# ---------------------------------------------------------------------------
# 3. 数据采集
# ---------------------------------------------------------------------------
def fetch_quotes():
    """用 yfinance 抓取全部监控标的的最新收盘价与涨跌幅。返回 {ticker: dict}"""
    import yfinance as yf

    tickers = [t for group in WATCHLIST.values() for t, _ in group]
    data = yf.download(
        tickers, period="10d", interval="1d",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )
    quotes = {}
    for t in tickers:
        try:
            df = data[t].dropna(subset=["Close"])
            if len(df) < 2:
                raise ValueError("insufficient rows")
            last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
            quotes[t] = {
                "price": float(last),
                "chg_pct": (float(last) / float(prev) - 1) * 100,
                "date": str(df.index[-1].date()),
                "ok": True,
            }
        except Exception as e:  # 单标的失败不影响整体
            quotes[t] = {"price": None, "chg_pct": None, "date": None,
                         "ok": False, "err": str(e)[:80]}
    return quotes


def demo_quotes():
    """演示模式:内置一组示例数据,用于本机无网络时预览版式。"""
    sample = {
        "^GSPC": 0.42, "^IXIC": 0.95, "^DJI": -0.12, "^SOX": 2.31,
        "DX-Y.NYB": -0.35, "^TNX": -1.20, "CNH=X": -0.15,
        "GC=F": 0.88, "CL=F": -1.65,
        "NVDA": 3.42, "AMD": 2.10, "AVGO": 1.85, "TSM": 2.66, "MU": 4.05,
        "VRT": 3.10, "SMCI": 1.42, "ANET": 2.20,
        "MSFT": 0.77, "META": 1.95, "GOOGL": 0.55, "AMZN": 0.30,
        "AAPL": -0.85, "TSLA": -2.40, "ORCL": 1.10,
        "BABA": 1.25, "PDD": -0.60, "JD": 0.40, "BIDU": 0.95, "KWEB": 0.72,
    }
    base_price = {"^GSPC": 6852.3, "^IXIC": 24310.5, "^DJI": 47120.8, "^SOX": 6890.2,
                  "DX-Y.NYB": 102.35, "^TNX": 4.18, "CNH=X": 7.128,
                  "GC=F": 3480.5, "CL=F": 71.2}
    quotes = {}
    for group in WATCHLIST.values():
        for t, _ in group:
            quotes[t] = {
                "price": base_price.get(t, round(100 + hash(t) % 400 + 0.5, 2)),
                "chg_pct": sample.get(t, 0.0),
                "date": datetime.now(BJT).strftime("%Y-%m-%d"),
                "ok": True,
            }
    return quotes


# ---------------------------------------------------------------------------
# 4. 规则引擎:信号 → A股传导判断
# ---------------------------------------------------------------------------
def grade(pct):
    a = abs(pct)
    if a >= TH_STRONG:
        return "强"
    if a >= TH_MILD:
        return "中"
    return "弱"


def run_rules(quotes):
    results = []
    for rule in RULES:
        vals = [quotes[t]["chg_pct"] for t in rule["tickers"]
                if quotes.get(t, {}).get("ok")]
        if not vals:
            continue
        signal = sum(vals) / len(vals)                      # 信号源合力
        effect = signal if rule["direction"] == "same" else -signal
        results.append({
            "signal_name": rule["signal_name"],
            "signal_pct": signal,
            "direction": rule["direction"],
            "effect": effect,                               # 对A股的推导方向
            "tone": "偏多" if effect > TH_MILD else ("偏空" if effect < -TH_MILD else "中性"),
            "strength": grade(effect),
            "sectors": rule["sectors"],
            "logic": rule["logic"],
            "members": [(t, quotes[t]["chg_pct"]) for t in rule["tickers"]
                        if quotes.get(t, {}).get("ok")],
        })
    # 按影响强度排序,强信号在前
    results.sort(key=lambda r: abs(r["effect"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# 5. (可选)Claude API 综合点评
# ---------------------------------------------------------------------------
def ai_commentary(quotes, rule_results):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    brief = {
        "指数": {name: round(quotes[t]["chg_pct"], 2)
               for t, name in WATCHLIST["核心指数"] if quotes[t]["ok"]},
        "宏观": {name: round(quotes[t]["chg_pct"], 2)
               for t, name in WATCHLIST["宏观资产"] if quotes[t]["ok"]},
        "信号": [{"名称": r["signal_name"], "合力%": round(r["signal_pct"], 2),
                "对A股": r["tone"]} for r in rule_results[:6]],
    }
    prompt = (
        "你是一名A股盘前策略助理。以下是隔夜美股与宏观数据摘要(JSON):\n"
        + json.dumps(brief, ensure_ascii=False)
        + "\n请用中文写4-6句盘前综合点评:1)隔夜美股主线;2)对A股开盘情绪的整体判断;"
          "3)最值得关注的1-2个板块传导逻辑;4)一条风险提示。"
          "只做信息梳理与情景推演,不给出买卖建议,不使用夸张措辞。直接输出正文。"
    )
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception as e:
        return f"(AI点评生成失败:{e})"


# ---------------------------------------------------------------------------
# 6. HTML 渲染
# ---------------------------------------------------------------------------
def _fmt_price(t, p):
    if p is None:
        return "—"
    if t == "CNH=X":
        return f"{p:,.4f}"
    return f"{p:,.2f}"


def _chg_html(pct):
    """A股配色习惯:红涨绿跌"""
    if pct is None:
        return '<span class="chg flat">—</span>'
    cls = "up" if pct > 0.005 else ("down" if pct < -0.005 else "flat")
    sign = "+" if pct > 0 else ""
    return f'<span class="chg {cls}">{sign}{pct:.2f}%</span>'


def render_html(quotes, rule_results, commentary, generated_at):
    date_str = generated_at.strftime("%Y年%m月%d日")
    time_str = generated_at.strftime("%H:%M")
    weekday = "一二三四五六日"[generated_at.weekday()]

    # --- 行情表格 ---
    tables = []
    for group, items in WATCHLIST.items():
        rows = []
        for t, name in items:
            q = quotes.get(t, {})
            rows.append(
                f'<tr><td class="tname">{name}<span class="tk">{t}</span></td>'
                f'<td class="num">{_fmt_price(t, q.get("price"))}</td>'
                f'<td class="num">{_chg_html(q.get("chg_pct"))}</td></tr>'
            )
        tables.append(
            f'<div class="qcard"><div class="qhead">{group}</div>'
            f'<table><thead><tr><th>标的</th><th class="num">收盘</th>'
            f'<th class="num">涨跌</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )

    # --- 传导链卡片(签名区块) ---
    chains = []
    for r in rule_results:
        tone_cls = {"偏多": "bull", "偏空": "bear", "中性": "flat2"}[r["tone"]]
        dir_note = "同向传导" if r["direction"] == "same" else "反向传导"
        members = " · ".join(
            f'{t} {"+" if c > 0 else ""}{c:.1f}%' for t, c in r["members"][:6]
        )
        sector_rows = "".join(
            f'<div class="sec-row"><span class="sec-name">{s["sector"]}</span>'
            f'<span class="sec-stocks">{s["stocks"]}</span></div>'
            for s in r["sectors"]
        )
        chains.append(f'''
        <div class="chain {tone_cls}">
          <div class="chain-left">
            <div class="sig-name">{r["signal_name"]}</div>
            <div class="sig-val">{"+" if r["signal_pct"] > 0 else ""}{r["signal_pct"]:.2f}%<span class="sig-note">信号源合力</span></div>
            <div class="sig-members">{members}</div>
          </div>
          <div class="chain-arrow"><span class="dirlabel">{dir_note}</span><svg viewBox="0 0 64 24" aria-hidden="true"><path d="M2 12 H52 M52 12 L42 4 M52 12 L42 20" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg><span class="tone-chip">{r["tone"]} · {r["strength"]}</span></div>
          <div class="chain-right">
            {sector_rows}
            <div class="logic">{r["logic"]}</div>
          </div>
        </div>''')

    commentary_html = ""
    if commentary:
        commentary_html = (
            '<section class="block"><h2><span class="hz">综合点评</span>'
            '<span class="en">AI COMMENTARY</span></h2>'
            f'<div class="commentary">{commentary}</div></section>'
        )

    up_cnt = sum(1 for q in quotes.values() if q.get("ok") and q["chg_pct"] > 0)
    dn_cnt = sum(1 for q in quotes.values() if q.get("ok") and q["chg_pct"] < 0)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>盘前晨报 · {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@600;900&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --paper:#F4F5F2; --ink:#191C1F; --ink2:#5A6069; --line:#D8DAD4;
  --navy:#1E3A5F; --up:#C9302B; --down:#0E8A63; --card:#FFFFFF;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--paper); color:var(--ink);
  font-family:"IBM Plex Sans","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:15px; line-height:1.65; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:40px 28px 64px; }}

/* ── 报头 ── */
header {{ border-bottom:3px double var(--ink); padding-bottom:20px; margin-bottom:8px; }}
.masthead {{ font-family:"Noto Serif SC",serif; font-weight:900; font-size:clamp(30px,5vw,46px);
  letter-spacing:.12em; }}
.masthead .accent {{ color:var(--navy); }}
.dateline {{ display:flex; flex-wrap:wrap; gap:8px 24px; margin-top:10px;
  font-size:13px; color:var(--ink2); }}
.dateline b {{ color:var(--ink); font-weight:600; }}
.breadth {{ margin-left:auto; }}
.breadth .u {{ color:var(--up); font-weight:600; }}
.breadth .d {{ color:var(--down); font-weight:600; }}

/* ── 区块标题 ── */
.block {{ margin-top:44px; }}
h2 {{ display:flex; align-items:baseline; gap:12px; border-left:5px solid var(--navy);
  padding-left:12px; margin-bottom:18px; }}
h2 .hz {{ font-family:"Noto Serif SC",serif; font-weight:900; font-size:22px; }}
h2 .en {{ font-size:11px; letter-spacing:.22em; color:var(--ink2); }}

/* ── 行情卡片 ── */
.qgrid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; }}
.qcard {{ background:var(--card); border:1px solid var(--line); border-radius:6px; overflow:hidden; }}
.qhead {{ background:var(--navy); color:#fff; font-weight:600; font-size:13.5px;
  letter-spacing:.08em; padding:8px 14px; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ font-size:11.5px; font-weight:500; color:var(--ink2); text-align:left;
  padding:8px 14px 4px; }}
td {{ padding:7px 14px; border-top:1px solid #EEEFEA; font-size:14px; }}
.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.tname {{ font-weight:500; }}
.tk {{ color:#9AA0A8; font-size:11px; margin-left:7px; }}
.chg {{ font-weight:600; }}
.chg.up {{ color:var(--up); }}
.chg.down {{ color:var(--down); }}
.chg.flat {{ color:var(--ink2); }}

/* ── 传导链(签名区块) ── */
.chain {{ display:grid; grid-template-columns:minmax(220px,1fr) 130px minmax(320px,1.6fr);
  gap:0 18px; background:var(--card); border:1px solid var(--line); border-radius:6px;
  padding:18px 20px; margin-bottom:14px; align-items:center; position:relative; }}
.chain::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:5px;
  border-radius:6px 0 0 6px; background:var(--ink2); }}
.chain.bull::before {{ background:var(--up); }}
.chain.bear::before {{ background:var(--down); }}
.sig-name {{ font-weight:600; font-size:15px; }}
.sig-val {{ font-family:"Noto Serif SC",serif; font-weight:900; font-size:28px;
  font-variant-numeric:tabular-nums; margin-top:2px; }}
.bull .sig-val {{ color:var(--up); }}
.bear .sig-val {{ color:var(--down); }}
.sig-note {{ font-size:11px; color:var(--ink2); font-family:"IBM Plex Sans",sans-serif;
  font-weight:400; margin-left:8px; }}
.sig-members {{ font-size:11.5px; color:var(--ink2); margin-top:6px;
  font-variant-numeric:tabular-nums; }}
.chain-arrow {{ text-align:center; color:var(--ink2); }}
.chain-arrow svg {{ width:56px; height:22px; display:block; margin:4px auto; }}
.bull .chain-arrow svg {{ color:var(--up); }}
.bear .chain-arrow svg {{ color:var(--down); }}
.dirlabel {{ font-size:11px; letter-spacing:.1em; }}
.tone-chip {{ display:inline-block; font-size:12px; font-weight:600; padding:2px 10px;
  border-radius:99px; border:1.5px solid currentColor; }}
.bull .tone-chip {{ color:var(--up); }}
.bear .tone-chip {{ color:var(--down); }}
.flat2 .tone-chip {{ color:var(--ink2); }}
.sec-row {{ padding:5px 0; border-bottom:1px dashed #E4E5E0; }}
.sec-row:last-of-type {{ border-bottom:none; }}
.sec-name {{ display:inline-block; min-width:150px; font-weight:600; font-size:13.5px; }}
.sec-stocks {{ font-size:13px; color:var(--ink2); }}
.logic {{ margin-top:10px; font-size:12.5px; color:var(--ink2); background:#F7F8F4;
  border-radius:4px; padding:8px 12px; }}

.commentary {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--navy);
  border-radius:6px; padding:20px 24px; font-size:15px; white-space:pre-wrap; }}

footer {{ margin-top:56px; border-top:1px solid var(--line); padding-top:16px;
  font-size:12px; color:var(--ink2); }}

@media (max-width:820px) {{
  .chain {{ grid-template-columns:1fr; gap:12px; }}
  .chain-arrow svg {{ transform:rotate(90deg); }}
}}
@media (prefers-reduced-motion:no-preference) {{
  .chain {{ animation:rise .4s ease both; }}
  @keyframes rise {{ from {{ opacity:0; transform:translateY(6px); }} to {{ opacity:1; transform:none; }} }}
}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="masthead">盘前<span class="accent">晨报</span></div>
    <div class="dateline">
      <span><b>{date_str}</b> 星期{weekday}</span>
      <span>生成于北京时间 <b>{time_str}</b></span>
      <span>隔夜美股 → A股开盘前传导速览</span>
      <span class="breadth">监控标的 <span class="u">涨 {up_cnt}</span> / <span class="d">跌 {dn_cnt}</span></span>
    </div>
  </header>

  <section class="block">
    <h2><span class="hz">隔夜行情</span><span class="en">OVERNIGHT MARKETS</span></h2>
    <div class="qgrid">{"".join(tables)}</div>
  </section>

  <section class="block">
    <h2><span class="hz">A股传导链</span><span class="en">US → A-SHARE TRANSMISSION</span></h2>
    {"".join(chains)}
  </section>

  {commentary_html}

  <footer>
    数据来源:Yahoo Finance(收盘价口径,盘后波动未计入)。板块映射为固定规则推导,仅供盘前信息参考,
    <b>不构成任何投资建议</b>;开盘表现受A股自身消息面、资金面影响,可能与隔夜信号背离。
  </footer>
</div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="A股盘前·美股晨报生成器")
    ap.add_argument("--demo", action="store_true", help="使用内置示例数据")
    ap.add_argument("--out", default="reports", help="输出目录(默认 reports/)")
    args = ap.parse_args()

    now = datetime.now(BJT)
    print(f"[{now:%F %T}] 开始生成盘前晨报 ...")

    if args.demo:
        quotes = demo_quotes()
        print("· 演示模式:使用内置示例数据")
    else:
        quotes = fetch_quotes()
        ok = sum(1 for q in quotes.values() if q["ok"])
        print(f"· 行情抓取完成:{ok}/{len(quotes)} 个标的成功")

    rule_results = run_rules(quotes)
    print(f"· 规则引擎:生成 {len(rule_results)} 条传导判断")

    commentary = None if args.demo else ai_commentary(quotes, rule_results)
    if commentary:
        print("· AI综合点评:已生成")

    html = render_html(quotes, rule_results, commentary, now)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"premarket_{now:%Y%m%d}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    # 同时维护一份 latest.html 方便固定书签访问
    latest = os.path.join(args.out, "latest.html")
    with open(latest, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ 报告已输出:{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
