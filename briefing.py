import anthropic
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

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

SOURCES = [
    ("Yahoo Finance Gainers", "https://finance.yahoo.com/markets/stocks/gainers/"),
    ("Yahoo Finance Losers", "https://finance.yahoo.com/markets/stocks/losers/"),
    ("Yahoo Finance Most Active", "https://finance.yahoo.com/markets/stocks/most-active/"),
    ("SemiEngineering", "https://semiengineering.com"),
    ("NextPlatform", "https://www.nextplatform.com"),
    ("GazettaByte", "https://www.gazettabyte.com"),
    ("World Nuclear News", "https://www.world-nuclear-news.org"),
    ("Utility Dive", "https://www.utilitydive.com"),
    ("Data Center Knowledge", "https://www.datacenterknowledge.com"),
    ("Construction Dive", "https://www.constructiondive.com"),
    ("EE Times", "https://www.eetimes.com"),
    ("Reuters Technology", "https://www.reuters.com/technology/"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_text(url: str, max_chars: int = 3000) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if len(l.strip()) > 25]
        return "\n".join(lines)[:max_chars]
    except Exception as e:
        return f"[Could not fetch: {e}]"


def gather_content() -> str:
    sections = []
    for name, url in SOURCES:
        print(f"  Fetching {name}...")
        text = fetch_text(url)
        sections.append(f"=== {name} ===\n{text}")
    return "\n\n".join(sections)


def build_prompt(today: str, day_of_week: str, content: str) -> str:
    return f"""Today is {day_of_week}, {today}. You are a market research assistant for an investor focused on AI infrastructure, optical/photonics, semiconductors, data center construction, power equipment, nuclear energy, and utilities.

## STOCK WATCHLIST
{WATCHLIST}

## FETCHED CONTENT FROM NEWS SOURCES AND YAHOO FINANCE
{content}

## YOUR TASK
Analyze the fetched content above and produce a morning market briefing.

1. PRICE MOVERS: From the Yahoo Finance data, identify any watchlist stocks with 5%+ moves. Note ticker, % change, price, and reason if available.

2. NEWS ANALYSIS: For each relevant story, identify:
   - Direct impact: which watchlist stocks are explicitly named?
   - Indirect impact: which benefit/suffer even if not named?
     (e.g. TSMC capacity news → NVMI, CAMT, ICHR | Hyperscaler capex cut → COHR, LITE, MRVL bearish | Nuclear PPA → CEG, CCJ, LEU bullish | Data center contract → PWR, EME, IESC bullish)
   - Urgency: actionable Today / This Week / Background context
   - Sentiment: Bullish / Bearish / Neutral per stock

## OUTPUT FORMAT

---
MORNING MARKET BRIEFING — {today} | 10 AM ET
AI Infrastructure | Optical | Semiconductors | Nuclear | Power | Data Centers

PRICE ALERTS — 5%+ MOVERS
[List watchlist stocks with 5%+ move, or "No watchlist stocks with 5%+ move this morning."]
- TICKER: +X% | $XX.XX | Reason: [headline]

---

HIGH IMPACT — Act or Monitor Closely
[For each story: 2-3 sentence summary, then Stock | Bullish/Bearish/Neutral | Why, then Urgency]

MEDIUM IMPACT — Worth Knowing

LOW IMPACT / BACKGROUND

SECTORS WITH NO NEWS TODAY

---

Quality rules: be honest if nothing meaningful — do not pad. Only use content from the fetched data above. Make the analyst connections a human would make, not just keyword matches."""


def get_briefing_text() -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = datetime.now().strftime("%B %d, %Y")
    day_of_week = datetime.now().strftime("%A")

    print("Fetching news sources...")
    content = gather_content()

    prompt = build_prompt(today, day_of_week, content)

    print("Sending to Claude for analysis...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )

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
