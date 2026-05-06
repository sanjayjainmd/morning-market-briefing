import anthropic
import os
import smtplib
import ssl
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from tavily import TavilyClient

WATCHLIST = """
AI Chips & Foundry: NVDA, AVGO, AMD, ARM, TSM, ASML, INTC
Semiconductor Metrology & Equipment: NVMI, CAMT, ONTO, FORM, ICHR, AEHR, AEIS, AMBA
Optical / Photonics / Interconnect: COHR, LITE, MTSI, CIEN, POET, AXTI, AAOI, GLW
Networking & Connectivity Silicon: MRVL, ANET, CRDO, ALAB, SITM
Power Semiconductors (GaN/SiC): POWI, NVTS
AI Cloud, Software & Hyperscalers: NBIS, CRWV, APP, PLTR, SNOW, MSFT, GOOGL, AMZN, META, ORCL
AI Servers & Contract Manufacturing: SMCI, CLS
Data Center Construction & Electrical: PWR, EME, DY, IESC, PRIM, STRL, MYRG, ORN, ROAD, TPC, GVA, AMRC, AGX, MTRX, LMB, NVEE, FER, ECG
Data Center Cooling & HVAC: MOD, VRT, TT, NVT, AAON, SPXC
Power Equipment & Grid: GEV, POWL, AZZ, AMSC, PSIX, ELWS, LYTS, QXO
Nuclear Power Generation: CEG, VST, TLN, NNE
Nuclear Equipment & Services: SMR, OKLO, BWXT
Uranium & Nuclear Fuel: CCJ, LEU, UUUU, UROY, UEC, ASPI
Nuclear / Uranium ETFs: URNM, URA, NLR
Utilities & Broader Power: NEE, DUK, SO, AEP, EIX, D, LNG, EXE, SEI
Quantum Computing: IONQ, QUBT, QBTS, RGTI
Aerospace & Specialty Alloys: TDG, HWM, ATI, MTUAY
Steel & Metals: STLD, NUE, MTUS
Water & Pipe Infrastructure: MWA, IIIN, NWPX, WMS, ROCK
"""

SEARCH_QUERIES = [
    "stock market premarket movers biggest gainers losers today {today}",
    "earnings report beat miss guidance data center AI infrastructure {today}",
    "NVDA AVGO AMD ARM TSM ASML INTC earnings news analyst {today}",
    "COHR LITE MRVL ANET CRDO optical interconnect semiconductor news {today}",
    "PWR EME IESC STRL PRIM data center construction contractor earnings {today}",
    "AI data center hyperscaler capex spending Microsoft Google Amazon Meta {today}",
    "nuclear energy SMR small modular reactor power purchase agreement CEG VST {today}",
    "uranium CCJ LEU nuclear fuel supply contract news {today}",
    "power grid GEV POWL AZZ utility electricity demand AI {today}",
    "semiconductor metrology equipment NVMI CAMT ONTO FORM AEIS news {today}",
]

YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_yahoo_movers() -> str:
    screens = [
        ("Gainers", "day_gainers"),
        ("Losers", "day_losers"),
        ("Most Active", "most_actives"),
    ]
    api_headers = {
        "User-Agent": YAHOO_HEADERS["User-Agent"],
        "Accept": "application/json",
    }
    parts = []
    for label, scr_id in screens:
        try:
            url = (
                f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
                f"?scrIds={scr_id}&count=25&formatted=false"
            )
            resp = requests.get(url, headers=api_headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            quotes = (
                data.get("finance", {})
                    .get("result", [{}])[0]
                    .get("quotes", [])
            )
            lines = []
            for q in quotes:
                ticker = q.get("symbol", "")
                price = q.get("regularMarketPrice", "")
                pct = q.get("regularMarketChangePercent", 0)
                name = q.get("shortName", "")
                volume = q.get("regularMarketVolume", "")
                lines.append(f"{ticker} ({name}): {pct:+.1f}% | ${price} | Vol: {volume:,}")
            parts.append(f"=== Yahoo Finance {label} ===\n" + "\n".join(lines))
        except Exception as e:
            parts.append(f"=== Yahoo Finance {label} ===\n[Could not fetch: {e}]")
    return "\n\n".join(parts)


def run_searches(today: str) -> str:
    tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    sections = []

    for query_template in SEARCH_QUERIES:
        query = query_template.format(today=today)
        print(f"  Searching: {query[:60]}...")
        try:
            results = tavily.search(
                query=query,
                max_results=3,
                search_depth="basic",
            )
            snippets = []
            for r in results.get("results", []):
                snippets.append(f"- [{r['title']}]({r['url']})\n  {r.get('content', '')[:500]}")
            sections.append(f"=== Search: {query} ===\n" + "\n".join(snippets))
        except Exception as e:
            sections.append(f"=== Search: {query} ===\n[Search failed: {e}]")

    return "\n\n".join(sections)


def build_prompt(today: str, day_of_week: str, yahoo_content: str, search_content: str) -> str:
    return f"""Today is {day_of_week}, {today}. You are a market research assistant for an investor focused on AI infrastructure, optical/photonics, semiconductors, data center construction, power equipment, nuclear energy, and utilities.

## STOCK WATCHLIST
{WATCHLIST}

## YAHOO FINANCE — PRICE MOVERS
{yahoo_content}

## WEB SEARCH RESULTS
{search_content}

## YOUR TASK
Analyze the content above and produce a morning market briefing.

1. PRICE MOVERS: From Yahoo Finance, identify any watchlist stocks with 5%+ moves. For each, find the reason from the search results (earnings beat/miss, guidance change, deal announcement, etc.).

2. EARNINGS ANALYSIS: For any watchlist company with recent earnings, extract:
   - Exact revenue, EPS vs consensus
   - Guidance raised/lowered/maintained
   - Key segment performance (e.g. data center revenue, AI revenue)
   - Whether the stock reaction seems justified or overdone

3. NEWS ANALYSIS: For each relevant story:
   - Direct impact: which watchlist stocks are explicitly named?
   - Indirect impact: which benefit/suffer even if not named?
     TSMC/Intel foundry news → NVMI, CAMT, ICHR bullish
     Hyperscaler capex increase → COHR, LITE, MRVL, SMCI, VRT bullish
     Hyperscaler capex cut → COHR, LITE, MRVL bearish
     Data center construction win → PWR, EME, IESC, STRL bullish (check peers for sympathy moves)
     Nuclear PPA signed → CEG, VST, CCJ, LEU bullish
     SMR order/deal → BWXT, SMR, OKLO bullish
     Power equipment shortage → POWL, AZZ, GEV bullish
   - Thesis check: does this strengthen or weaken the investment thesis?
   - Urgency: Today (act now) / This Week (monitor) / Background (context)
   - Sentiment: Bullish / Bearish / Neutral per stock

## OUTPUT FORMAT

---
MORNING MARKET BRIEFING — {today} | 10 AM ET
AI Infrastructure | Optical | Semiconductors | Nuclear | Power | Data Centers

PRICE ALERTS — 5%+ MOVERS
[List watchlist stocks with 5%+ move, or "No watchlist stocks with 5%+ move this morning."]
- TICKER: +X% | $XX.XX | Reason: [one line]

---

HIGH IMPACT — Act or Monitor Closely
For each story:
- Headline and 2-3 sentence summary with specific numbers (revenue, EPS, % beats)
- Stock | Bullish/Bearish/Neutral | Why (one sentence each)
- Urgency: Today / This Week / Background

MEDIUM IMPACT — Worth Knowing
[Same format, less urgent]

LOW IMPACT / BACKGROUND
[Thematic context only]

SECTORS WITH NO NEWS TODAY
[List sectors checked with no relevant news]

---

Quality rules:
- Include specific numbers — revenue figures, EPS beats, guidance ranges
- For earnings beats: note if peer stocks should see sympathy moves (check if they haven't caught up yet)
- For sell-offs on good earnings: explicitly assess if the reaction is overdone
- Be honest if nothing meaningful — do not pad
- Only today's and this week's news — do not summarize old articles"""


def get_briefing_text() -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = datetime.now().strftime("%B %d, %Y")
    day_of_week = datetime.now().strftime("%A")

    print("Fetching Yahoo Finance price movers...")
    yahoo_content = fetch_yahoo_movers()

    print("Running targeted news searches...")
    search_content = run_searches(today)

    print("Sending to Claude for analysis...")
    prompt = build_prompt(today, day_of_week, yahoo_content, search_content)
    estimated_tokens = len(prompt) // 4
    print(f"  Prompt size: {len(prompt):,} chars (~{estimated_tokens:,} tokens)")
    if estimated_tokens > 25000:
        # Truncate search content to fit within safe limit
        max_content = 25000 * 4 - len(prompt) + len(search_content)
        search_content = search_content[:max_content]
        prompt = build_prompt(today, day_of_week, yahoo_content, search_content)
        print(f"  Trimmed to: {len(prompt):,} chars (~{len(prompt)//4:,} tokens)")

    for attempt in range(3):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            break
        except anthropic.RateLimitError:
            if attempt == 2:
                raise
            print(f"Rate limited. Waiting 61s...")
            time.sleep(61)

    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    if not text:
        raise ValueError("Response contained no text content")

    return text


def send_email(body: str, date: str) -> None:
    sender = os.environ["GMAIL_SENDER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = "Sanjayja@gmail.com"

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = f"Morning Market Briefing — {date}"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    print(f"Email sent to {recipient}")


def save_briefing(body: str, date_slug: str) -> None:
    os.makedirs("DailyBriefings", exist_ok=True)
    path = f"DailyBriefings/briefing-{date_slug}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Saved to {path}")


def main() -> None:
    today = datetime.now().strftime("%B %d, %Y")
    date_slug = datetime.now().strftime("%Y-%m-%d")

    print(f"Running morning market briefing for {today}...")
    briefing = get_briefing_text()

    try:
        send_email(briefing, today)
    except Exception as e:
        print(f"Email failed: {e}")
        print("Falling back to file save only.")

    save_briefing(briefing, date_slug)


if __name__ == "__main__":
    main()
