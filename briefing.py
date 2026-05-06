import anthropic
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

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


def build_prompt(today: str, day_of_week: str) -> str:
    return f"""Today is {day_of_week}, {today}. You are an intelligent market research assistant for an investor focused on AI infrastructure, optical/photonics, semiconductors, data center construction, power equipment, nuclear energy, and utilities.

## YOUR OBJECTIVE
Produce a morning market briefing covering:
1. Any 5%+ price moves on watchlist stocks today
2. Relevant news from key sources with intelligent stock impact analysis

## COMPLETE STOCK WATCHLIST
{WATCHLIST}

## STEP 1 — SCAN FOR PRICE MOVERS
Use web search to find:
- "stock market premarket movers today {today}"
- "biggest stock gainers losers today {today}"
- Fetch https://finance.yahoo.com/markets/stocks/gainers/ and https://finance.yahoo.com/markets/stocks/losers/

Cross-reference every stock against the watchlist above. For any watchlist stock with a 5%+ move: note ticker, price, % change, and reason.

## STEP 2 — FETCH NEWS FROM THESE SOURCES
Search for recent articles from each of these:
- semiengineering.com — AI chips, packaging, metrology
- nextplatform.com — HPC, data center, AI infrastructure
- gazettabyte.com — optical, photonics, interconnect
- world-nuclear-news.org — nuclear, SMRs, uranium
- utilitydive.com — power grid, utilities, PPAs
- datacenterknowledge.com — data center deals and construction
- constructiondive.com — data center construction, contractors
- eetimes.com — semiconductors, chip industry
- Reuters technology, CNBC technology

Also run these searches:
- "AI data center infrastructure news today {today}"
- "nuclear energy SMR deal news today {today}"
- "semiconductor optical interconnect earnings {today}"
- "data center construction power grid news {today}"

## STEP 3 — INTELLIGENT ANALYSIS
For each relevant development:
1. Direct impact — which watchlist stocks are explicitly named?
2. Indirect impact — which benefit/suffer even if not named?
   Examples: TSMC capacity → NVMI, CAMT, ICHR | Hyperscaler capex cut → COHR, LITE, MRVL bearish |
   Nuclear PPA → CEG, CCJ, LEU bullish | SMR order → BWXT, SMR, OKLO bullish |
   Data center contract → PWR, EME, IESC bullish | Equipment shortage → POWL, AZZ, GEV bullish
3. Thesis check — does this strengthen or weaken the investment thesis?
4. Urgency — actionable today, this week, or background context?
5. Sentiment — Bullish / Bearish / Neutral per stock

## STEP 4 — FORMAT THE REPORT

Use this exact format:

---
MORNING MARKET BRIEFING — {today} | 10 AM ET
AI Infrastructure | Optical | Semiconductors | Nuclear | Power | Data Centers

PRICE ALERTS — 5%+ MOVERS
[List watchlist stocks with 5%+ move, or "No watchlist stocks with 5%+ move this morning."]
- TICKER: +X% | $XX.XX | Reason: [headline]

---

HIGH IMPACT — Act or Monitor Closely
[Direct, immediate stock impact]

For each story:
- 2-3 sentence summary of what happened
- Stock | Impact (Bullish / Bearish / Neutral) | Why (one sentence)
- Urgency: Today / This Week / Background

MEDIUM IMPACT — Worth Knowing
[Relevant to thesis but not immediately actionable]

LOW IMPACT / BACKGROUND
[Thematic context, no immediate action needed]

SECTORS WITH NO NEWS TODAY
[Confirm which sectors had no relevant news]

---

Quality rules:
- Be honest if nothing meaningful — do not pad
- 3 real insights beat 10 irrelevant headlines
- Only today's news — do not summarize old articles
- Make the connections a human analyst would make, not just keyword matches"""


def get_briefing_text() -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = datetime.now().strftime("%B %d, %Y")
    day_of_week = datetime.now().strftime("%A")

    prompt = build_prompt(today, day_of_week)
    messages = [{"role": "user", "content": prompt}]

    # Loop handles pause_turn (fires when server-side tool hits its 10-iteration limit)
    max_continuations = 5
    response = None

    for _ in range(max_continuations):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=messages,
        )

        if response.stop_reason != "pause_turn":
            break

        # Re-send with assistant response appended so the server resumes
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response.content},
        ]

    if response is None:
        raise RuntimeError("No response received")

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
