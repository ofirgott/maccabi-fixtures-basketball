#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import sys
import json
from datetime import datetime
from urllib.parse import urljoin

import pytz
import requests
from bs4 import BeautifulSoup

TZ = pytz.timezone("Asia/Jerusalem")
BASE = "https://www.maccabi.co.il/"
# EuroLeague and Winner League pages are distinguished by cType (1=EuroLeague, 2=Winner League)
# cYear is the *second* year of the season (e.g., 2026 for season 2025–26)

HEADERS = {"User-Agent": "Mozilla/5.0 (CalendarBot; +https://github.com)"}

SEASONS = [1, 2]  # 1 = EuroLeague, 2 = Winner League


def current_season_cyear(now: datetime) -> int:
    """Return the cYear param used by the site for the *current* season.
    For seasons spanning two years, use the second year (e.g., 2026 for 2025–26).
    Heuristic: if month >= 7 (Jul–Dec), cYear = year+1; else = year.
    """
    return now.year + 1 if now.month >= 7 else now.year


def fetch_fixture_page(season_type: int, cyear: int, lang="en"):
    url = (
        f"{BASE}season.asp?cMode=0&cType={season_type}&cYear={cyear}&lang={lang}"
    )
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text, url


def parse_events(html: str):
    """Parse the fixture page into a list of game dicts.
    The site’s markup can change; we use a few tolerant patterns.
    Returns: list of {start: datetime, title: str, location: str, comp: str}
    Only future (now-) games are kept.
    """
    soup = BeautifulSoup(html, "html.parser")
    events = []

    # Strategy: look for date/time blocks near team names.
    # Many entries are structured in cards; dates often appear like '28/10/2025' and times like '21:15'.
    # We’ll scan text nodes and capture neighbors.

    text = soup.get_text("\n", strip=True)

    # Capture lines with a date pattern (both dd/mm/yyyy and 'October 3, 2025', etc.)
    # We'll support Hebrew and English months by also scanning siblings later.
    date_patterns = [
        re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),  # 28/10/2025
        re.compile(r"\b([A-Za-z]+)\s(\d{1,2}),\s(\d{4})\b"),  # October 3, 2025
        re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"),  # 28.10.2025 just in case
    ]
    time_patterns = [
        re.compile(r"\b(\d{1,2}):(\d{2})\b"),  # 21:15
    ]

    # Helper to find near tokens in soup around a given string occurrence
    def neighbors_for(substr, limit=200):
        idx = html.find(substr)
        if idx == -1:
            return ""
        start = max(0, idx - limit)
        end = min(len(html), idx + len(substr) + limit)
        snippet = BeautifulSoup(html[start:end], "html.parser").get_text(" ", strip=True)
        return snippet

    # Collect candidate date strings from the HTML
    candidates = set()
    for pat in date_patterns:
        for m in pat.finditer(text):
            candidates.add(m.group(0))

    now = datetime.now(TZ)

    for cand in sorted(candidates):
        neigh = neighbors_for(cand)
        # time
        hh, mm = 0, 0
        for tp in time_patterns:
            tm = tp.search(neigh)
            if tm:
                hh, mm = int(tm.group(1)), int(tm.group(2))
                break

        # Parse date
        dt = None
        m1 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", cand)
        m2 = re.match(r"^([A-Za-z]+)\s(\d{1,2}),\s(\d{4})$", cand)
        m3 = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", cand)
        try:
            if m1:
                d, m, y = map(int, m1.groups())
                dt = TZ.localize(datetime(y, m, d, hh, mm))
            elif m2:
                mon, d, y = m2.groups()
                dt = TZ.localize(datetime.strptime(f"{mon} {d} {y} {hh}:{mm}", "%B %d %Y %H:%M"))
            elif m3:
                d, m, y = map(int, m3.groups())
                dt = TZ.localize(datetime(y, m, d, hh, mm))
        except Exception:
            dt = None

        if not dt or dt < now:
            continue

        # Build title from nearby text: look for opponent/team and competition keywords
        title = "Maccabi TLV – Game"
        comp = None
        location = None

        # Competition hints
        if re.search(r"Euro ?League|יורוליג|EUROLEAGUE", neigh, re.I):
            comp = "EuroLeague"
        elif re.search(r"Winner|ליגת העל|Isra(el)? League|Ligat|League Cup|State Cup", neigh, re.I):
            comp = "Winner League"

        # Opponent detection (very heuristic): words around Vs or images alt text sometimes include team names
        opp = None
        m_vs = re.search(r"Vs\s+([A-Za-z \-’'\.]+)", neigh, re.I)
        if m_vs:
            opp = m_vs.group(1).strip()
        else:
            # try ‘Maccabi’ sibling pattern like "Maccabi Rapyd TA  vs  Panathinaikos"
            m_alt = re.search(r"Maccabi[ A-Za-z]*\s*(?:vs|VS|V)\s*([A-Za-z \-’'\.]+)", neigh)
            if m_alt:
                opp = m_alt.group(1).strip()

        if opp:
            title = f"Maccabi TLV vs {opp}" if "Maccabi" not in opp else f"{opp} vs Maccabi TLV"

        # Location hints
        if re.search(r"Menora|היכל מנורה|Yad Eliyahu|Tel Aviv", neigh, re.I):
            location = "Menora Mivtachim Arena, Tel Aviv"
        elif re.search(r"Belgrade|Stark|Pionir|Aleksandar Nikolic|ניקוליץ|בלגרד", neigh, re.I):
            location = "Aleksandar Nikolic Hall, Belgrade"

        events.append({
            "start": dt,
            "title": title if comp is None else f"{title} ({comp})",
            "location": location or "",
        })

    # Deduplicate by start time + title
    uniq = {}
    for e in events:
        key = (e["start"].isoformat(), e["title"])
        if key not in uniq:
            uniq[key] = e

    return sorted(uniq.values(), key=lambda x: x["start"])[:200]


def build_ics(events, prodid="-//Ofir//MaccabiTLV Fixtures//EN"):
    def ics_escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Maccabi Tel Aviv BC (Auto)",
        "X-WR-TIMEZONE:Asia/Jerusalem",
    ]

    for ev in events:
        dtstart = ev["start"].strftime("%Y%m%dT%H%M%S")
        uid = f"maccabi-{dtstart}-{abs(hash(ev['title']))}@ofir"
        summary = ics_escape(ev["title"])
        location = ics_escape(ev.get("location", ""))
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;TZID=Asia/Jerusalem:{dtstart}",
            f"SUMMARY:{summary}",
            *( [f"LOCATION:{location}"] if location else [] ),
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    now = datetime.now(TZ)
    cyear = current_season_cyear(now)

    all_events = []
    for ctype in SEASONS:
        try:
            html, url = fetch_fixture_page(ctype, cyear)
            parsed = parse_events(html)
            all_events.extend(parsed)
            print(f"Parsed {len(parsed)} events from {url}")
        except Exception as e:
            print(f"WARN: failed {ctype}: {e}")

    # sort and write
    all_events = sorted({(e['start'], e['title']): e for e in all_events}.values(), key=lambda x: x['start'])
    os.makedirs("docs", exist_ok=True)
    ics = build_ics(all_events)
    with open("docs/maccabi.ics", "w", encoding="utf-8") as f:
        f.write(ics)
    print(f"Wrote docs/maccabi.ics with {len(all_events)} events")


if __name__ == "__main__":
    sys.exit(main())
