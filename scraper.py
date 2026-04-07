"""
UPP Meeting Tracker — Weekly Scraper
Urban Phoenix Project / Valley Urban Action Alliance

Runs every Friday via GitHub Actions.
Sources:
  - City of Phoenix (Legistar REST API)
  - City of Phoenix Boards & Commissions (phoenix.gov notice page)
  - Valley Metro  (board-resources page; main site is JS-gated)
  - MAG           (individual committee pages; calendar is JS-gated)
  - Maricopa County (CivicEngage AgendaCenter search)

Outputs (written to docs/):
  - index.html     public GitHub Pages webpage
  - digest.html    Mailchimp-paste HTML block
  - digest.txt     plain text fallback
  - meetings.json  structured data
"""

import json
import re
import logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

import requests
from bs4 import BeautifulSoup

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False
    logging.warning("pdfplumber not installed — PDF agenda scanning disabled")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("upp-tracker")

# ── Config ────────────────────────────────────────────────────────────────────
LOOK_AHEAD_DAYS = 28
LOOK_BACK_DAYS  = 3
OUTPUT_DIR      = Path("docs")
REQUEST_TIMEOUT = 25
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

# ── Keywords ──────────────────────────────────────────────────────────────────
KEYWORDS = [
    "zoning", "rezoning", "rezone", "variance", "general plan", "land use",
    "housing", "affordable housing", "housing affordability", "middle housing",
    "density", "multifamily", "accessory dwelling", "adu", "infill",
    "building code", "building codes", "construction code", "single stair",
    "fire code", "code reform", "development standard", "code amendment",
    "transit", "light rail", "bus rapid transit", "brt", "bus route", "bus",
    "high-capacity transit", "high capacity transit",
    "transit oriented development", "transit oriented community",
    "transit oriented communities", "tod",
    "prop 400", "prop 479", "proposition 400", "proposition 479",
    "transportation", "mobility", "commuter", "fare",
    "walkability", "walkable", "pedestrian", "sidewalk", "sidewalks",
    "crosswalk", "crosswalks", "bike lane", "bike lanes", "bicycle",
    "vision zero", "street safety", "road diet", "road enhancement",
    "traffic calming", "speed limit", "speed camera", "speed cameras",
    "parking", "parking reform", "complete streets", "complete street",
    "cycle track", "cycle tracks", "ada", "accessibility", "americans with disabilities",
    "heat", "heat mitigation", "urban heat", "tree", "trees", "canopy",
    "tree protection", "tree ordinance", "shade", "green infrastructure",
    "climate", "sustainability", "environment", "environmental",
    "budget", "capital improvement", "cip", "appropriation", "fiscal",
    "public hearing",
]
KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Phoenix Legistar bodies
PHOENIX_LEGISTAR_BODIES = [
    "City Council Formal Meeting",
    "City Council Policy Session",
    "City Council Special Meeting",
    "Community Services and Education Subcommittee",
    "Transportation, Infrastructure, and Planning Subcommittee",
    "Public Safety and Justice Subcommittee",
    "Economic Development and the Arts Subcommittee",
    "Planning Commission",
    "Virtual Community Budget Hearing",
    "Community and Cultural Investment Subcommittee",
]

# Keywords matched against notice names on phoenix.gov boards page
PHOENIX_BOARDS_KEYWORDS = [
    "Development Advisory Board",
    "Environmental Quality and Sustainability",
    "Village Planning",
    "Citizen Transportation Commission",
    "Vision Zero",
    "Mayor's Commission on Disability",
    "Planning Commission",
    "Zoning",
]

# MAG committee pages (calendar is JS-gated; individual pages sometimes load)
# MAG is fully behind Cloudflare — no API accessible from GitHub Actions.
# We link directly to their calendar and known event URL pattern.
# When MAG posts agendas they use /Event/XXXXX URLs (e.g. azmag.gov/Event/52083).
# The calendar page lists all upcoming events with those IDs.
MAG_CALENDAR_URL = "https://azmag.gov/About-Us/Calendar"
MAG_COMMITTEE_PAGES = {
    "MAG Regional Council":
        "https://azmag.gov/About-Us/Calendar",
    "MAG Regional Council Executive Committee":
        "https://azmag.gov/About-Us/Calendar",
    "MAG Transportation Policy Committee":
        "https://azmag.gov/About-Us/Calendar",
    "MAG Environment & Sustainable Communities Committee":
        "https://azmag.gov/About-Us/Calendar",
    "MAG Active Transportation Committee":
        "https://azmag.gov/About-Us/Calendar",
    "MAG Human Services & Public Safety Committee":
        "https://azmag.gov/About-Us/Calendar",
}

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class AgendaItem:
    title: str
    matched_keywords: list[str] = field(default_factory=list)

@dataclass
class Meeting:
    body: str
    date: str
    time: str
    location: str
    virtual_url: str
    agenda_url: str
    source_label: str
    relevant_items: list[AgendaItem] = field(default_factory=list)
    notes: str = ""

    @property
    def date_obj(self) -> date:
        try:
            return date.fromisoformat(self.date)
        except ValueError:
            return date(9999, 12, 31)  # sentinel sorts to end

    @property
    def is_placeholder(self) -> bool:
        return self.date == "0000-00-00"

    @property
    def display_date(self) -> str:
        if self.date == "0000-00-00":
            return "Date pending — verify at source"
        try:
            return self.date_obj.strftime("%A, %B %-d, %Y")
        except Exception:
            return self.date


# ── Helpers ───────────────────────────────────────────────────────────────────
def get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning(f"  GET failed: {url[:80]}  ({e})")
        return None

def in_window(dt: date) -> bool:
    today = date.today()
    return (today - timedelta(days=LOOK_BACK_DAYS)) <= dt <= (today + timedelta(days=LOOK_AHEAD_DAYS))

def keywords_in(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in KEYWORD_PATTERN.finditer(text)))

DATE_RE = re.compile(
    r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})"
    r"|((?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)

def parse_date(raw: str) -> Optional[date]:
    raw = raw.strip().replace(",", "")
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d",
                "%B %d %Y", "%b %d %Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None

def extract_virtual_url(text: str) -> str:
    for pat in [r"https?://\S*zoom\.us\S*", r"https?://\S*webex\.com\S*",
                r"https?://\S*gotomeeting\S*", r"https?://\S*teams\.microsoft\S*"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).rstrip(".,;)")
    return ""

def scan_pdf(url: str) -> list[AgendaItem]:
    if not PDF_SUPPORT or not url or not url.lower().endswith(".pdf"):
        return []
    r = get(url)
    if not r:
        return []
    try:
        import io
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            items, seen = [], set()
            for page in pdf.pages:
                for line in (page.extract_text() or "").split("\n"):
                    line = line.strip()
                    if len(line) < 10:
                        continue
                    kws = keywords_in(line)
                    key = line[:60]
                    if kws and key not in seen:
                        seen.add(key)
                        items.append(AgendaItem(title=line[:220], matched_keywords=kws))
                if len(items) >= 20:
                    break
            return items[:20]
    except Exception as e:
        log.warning(f"  PDF scan failed ({e})")
        return []


# ── Phoenix City Clerk PDF discovery ─────────────────────────────────────────
PHOENIX_PDF_BASE = (
    "https://www.phoenix.gov/content/dam/phoenix/"
    "cityclerksite/publicmeetings/notices/{year}/{month}/{yy}{mm}{dd}{seq:03d}.pdf"
)
MONTH_NAMES = {
    1:"january",2:"february",3:"march",4:"april",5:"may",6:"june",
    7:"july",8:"august",9:"september",10:"october",11:"november",12:"december"
}

def find_phoenix_pdfs(dt: date) -> list[tuple[str, str]]:
    """
    Probe the Phoenix City Clerk PDF directory for a given date.
    Returns list of (url, first_page_text) for each PDF found.
    Stops at the first 404. Caps at 15 per day.
    """
    results = []
    yy = str(dt.year)[2:]
    mm = f"{dt.month:02d}"
    dd = f"{dt.day:02d}"
    month_name = MONTH_NAMES[dt.month]
    year = str(dt.year)

    for seq in range(1, 16):
        url = PHOENIX_PDF_BASE.format(
            year=year, month=month_name,
            yy=yy, mm=mm, dd=dd, seq=seq
        )
        r = get(url, headers={"Accept": "application/pdf"})
        if not r or r.status_code == 404:
            break
        text = ""
        if PDF_SUPPORT:
            try:
                import io
                with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                    text = (pdf.pages[0].extract_text() or "")[:600]
            except Exception:
                pass
        results.append((url, text))
    return results

def match_legistar_to_pdf(body: str, dt: date) -> str:
    """
    Try to find the Phoenix City Clerk PDF that matches a given Legistar body name.
    Returns the PDF URL if found, otherwise returns empty string.
    """
    pdfs = find_phoenix_pdfs(dt)
    if not pdfs:
        return ""

    # Build match terms from body name
    body_lower = body.lower()
    match_terms = []
    if "city council formal" in body_lower:
        match_terms = ["city council", "formal"]
    elif "subcommittee" in body_lower or "policy session" in body_lower:
        # Extract key word from subcommittee name
        words = [w for w in body_lower.split() if len(w) > 4
                 and w not in ("subcommittee","committee","session","meeting")]
        match_terms = words[:2]
    elif "planning commission" in body_lower:
        match_terms = ["planning commission"]
    elif "budget" in body_lower:
        match_terms = ["budget"]
    else:
        match_terms = [w for w in body_lower.split() if len(w) > 4][:3]

    for url, text in pdfs:
        text_lower = text.lower()
        if all(term in text_lower for term in match_terms):
            log.info(f"    Matched PDF: {url.split('/')[-1]} for '{body}'")
            return url

    # No exact match — return the first PDF for Council meetings,
    # otherwise return empty so we fall back to City Clerk notices page
    if "city council" in body_lower and pdfs:
        return pdfs[0][0]
    return ""


# ── Scraper 1: City of Phoenix — Legistar REST API ────────────────────────────
def scrape_phoenix_legistar() -> list[Meeting]:
    log.info("Scraping City of Phoenix — Legistar API…")
    meetings = []
    today = date.today()
    start = (today - timedelta(days=LOOK_BACK_DAYS)).strftime("%Y-%m-%d")
    end   = (today + timedelta(days=LOOK_AHEAD_DAYS)).strftime("%Y-%m-%d")

    _filter = (
        f"EventDate ge datetime'{start}T00:00:00'"
        f" and EventDate le datetime'{end}T23:59:59'"
    )
    _qs = (
        f"?$filter={requests.utils.quote(_filter)}"
        f"&$orderby=EventDate+asc&$top=200"
    )
    r = get(
        "https://webapi.legistar.com/v1/phoenix/events" + _qs,
        headers={"Accept": "application/json"},
    )
    if not r:
        return meetings
    try:
        events = r.json()
    except Exception:
        log.warning("  Could not parse Legistar JSON")
        return meetings

    for ev in events:
        body = ev.get("EventBodyName", "")
        if not any(b.lower() in body.lower() for b in PHOENIX_LEGISTAR_BODIES):
            continue

        iso_date = (ev.get("EventDate") or "")[:10]
        time_str = ev.get("EventTime") or "See agenda"
        if not iso_date:
            continue
        try:
            if not in_window(date.fromisoformat(iso_date)):
                continue
        except ValueError:
            continue

        location    = ev.get("EventLocation") or "Phoenix City Hall, 200 W. Washington St."
        virtual_url = extract_virtual_url(location + " " + (ev.get("EventInSiteURL") or ""))

        # Try to find the actual PDF on the City Clerk's server
        try:
            pdf_url = match_legistar_to_pdf(body, date.fromisoformat(iso_date))
        except Exception:
            pdf_url = ""

        agenda_file = pdf_url or ev.get("EventAgendaFile") or ""
        clerk_notices = (
            "https://www.phoenix.gov/administration/departments/cityclerk"
            "/programs-services/other-public-meetings/notices.html"
        )
        agenda_url = agenda_file or clerk_notices

        items = scan_pdf(agenda_file)
        if not items:
            kws = keywords_in(body)
            if kws:
                items = [AgendaItem(
                    title=f"(Full agenda — body matched: {', '.join(kws)})",
                    matched_keywords=kws,
                )]
        meetings.append(Meeting(
            body=body, date=iso_date, time=time_str,
            location=location, virtual_url=virtual_url, agenda_url=agenda_url,
            source_label="City of Phoenix", relevant_items=items,
        ))
        log.info(f"  + {body}  {iso_date}  {time_str}")
    return meetings


# ── Scraper 2: City of Phoenix — Boards & Commissions (JSON API) ─────────────
PHOENIX_NOTICES_API = (
    "https://www.phoenix.gov/administration/departments/cityclerk/programs-services"
    "/other-public-meetings/notices/_jcr_content/root/container/container-nav"
    "/container-full-width/container-content/public_meeting_table.results.json"
)
PHOENIX_NOTICES_PAGE = (
    "https://www.phoenix.gov/administration/departments/cityclerk"
    "/programs-services/other-public-meetings/notices.html"
)

def scrape_phoenix_boards() -> list[Meeting]:
    """
    Uses the City Clerk's JSON API that powers the public meeting notices table.
    Returns all upcoming meetings matching our board/commission keyword list,
    with direct links to agenda PDFs.
    """
    log.info("Scraping City of Phoenix — Boards & Commissions (JSON API)…")
    meetings = []

    r = get(
        PHOENIX_NOTICES_API,
        params={
            "offset": "0",
            "limit": "200",
            "orderby": "@jcr:content/metadata/meetingTime",
            "sortorder": "asc",
        },
        headers={"Accept": "application/json"},
    )
    if not r:
        log.warning("  Phoenix notices JSON API unavailable")
        return meetings

    try:
        data = r.json()
    except Exception:
        log.warning("  Could not parse Phoenix notices JSON")
        return meetings

    results = data.get("results", [])
    log.info(f"  Phoenix notices API returned {len(results)} items")

    for item in results:
        title = item.get("title", "").strip()
        if not title:
            continue

        # Skip cancelled meetings
        if any(w in title.lower() for w in ["cancel", "cancelled", "canceled"]):
            log.info(f"  Skipping cancelled: {title[:60]}")
            continue

        # Filter to boards/commissions we care about
        if not any(kw.lower() in title.lower() for kw in PHOENIX_BOARDS_KEYWORDS):
            continue

        # Parse date/time from ISO timestamp
        raw_time = (item.get("properties") or {}).get("metadata/meetingTime", "")
        if not raw_time:
            continue
        try:
            dt_obj = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            # Convert UTC to Arizona time (UTC-7, no DST)
            from datetime import timezone
            az_offset = timezone(timedelta(hours=-7))
            dt_az = dt_obj.astimezone(az_offset)
            iso_date = dt_az.date().isoformat()
            time_str = dt_az.strftime("%-I:%M %p")
        except Exception:
            continue

        if not in_window(date.fromisoformat(iso_date)):
            continue

        # Build PDF URL from the path field
        path = item.get("url") or item.get("path") or ""
        if path.startswith("/content/dam/"):
            pdf_url = "https://www.phoenix.gov" + path
        elif path.startswith("http"):
            pdf_url = path
        else:
            pdf_url = PHOENIX_NOTICES_PAGE

        items = scan_pdf(pdf_url)

        meetings.append(Meeting(
            body=title,
            date=iso_date,
            time=time_str,
            location="City of Phoenix (see notice PDF for location/virtual link)",
            virtual_url="",
            agenda_url=pdf_url,
            source_label="City of Phoenix",
            relevant_items=items,
        ))
        log.info(f"  + {title[:60]}  {iso_date}  {time_str}")

    return meetings


# ── Scraper 3: Valley Metro ────────────────────────────────────────────────────
def _next_nth_weekday(from_date: date, weekday: int, n: int) -> date:
    """Return the Nth occurrence of a weekday in the next 60 days."""
    d = from_date + timedelta(days=1)
    count = 0
    for _ in range(90):
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    return from_date + timedelta(days=30)

def scrape_valley_metro() -> list[Meeting]:
    """
    Valley Metro's main site is behind Cloudflare. Try the SDG subdomain
    and the board-resources page. Fall back to an estimated placeholder.
    """
    log.info("Scraping Valley Metro…")
    meetings = []

    meeting_kws = ["board of directors", "board meeting", "rpta", "vmr",
                   "transit management committee", "tmc", "rmc"]

    for src in [
        "https://sdg.valleymetro.org/news-events",
        "https://www.valleymetro.org/about/boards-directors/board-resources",
        "https://www.valleymetro.org/about/boards-directors/board-book",
    ]:
        r = get(src)
        if not r or "just a moment" in r.text.lower():
            continue
        s = BeautifulSoup(r.text, "html.parser")
        for link in s.find_all("a", href=True):
            text = link.get_text(" ", strip=True)
            href = link["href"]
            if not any(kw in text.lower() for kw in meeting_kws):
                continue
            parent = link.find_parent("tr") or link.find_parent("li") or link.parent
            ctx = (parent.get_text(" ", strip=True) if parent else "") + " " + text
            dm  = DATE_RE.search(ctx)
            if not dm:
                continue
            dt = parse_date(dm.group(0))
            if not dt or not in_window(dt):
                continue
            full = href if href.startswith("http") else "https://www.valleymetro.org" + href
            meetings.append(Meeting(
                body=text[:100], date=dt.isoformat(), time="See agenda",
                location="101 N. 1st Ave, Suite 1400, Phoenix AZ 85003",
                virtual_url="https://www.valleymetro.org/event/valley-metro-board-meetings",
                agenda_url=full, source_label="Valley Metro",
                relevant_items=scan_pdf(full),
            ))
            log.info(f"  + {text[:60]}  {dt.isoformat()}")
        if meetings:
            break

    if not meetings:
        log.info("  Valley Metro JS-gated — inserting estimated placeholder")
        est = _next_nth_weekday(date.today(), weekday=3, n=3)  # 3rd Thursday
        meetings.append(Meeting(
            body="Valley Metro Board of Directors (RPTA & VMR)",
            date=est.isoformat(),
            time="~11:15 AM (estimate — verify)",
            location="101 N. 1st Ave, Suite 1400, Phoenix AZ 85003",
            virtual_url="https://www.valleymetro.org/event/valley-metro-board-meetings",
            agenda_url="https://www.valleymetro.org/about/boards-directors/board-book",
            source_label="Valley Metro",
            notes=(
                "Valley Metro's website requires a manual check for confirmed dates and agenda. "
                "Visit valleymetro.org/about/boards-directors/board-book — boards typically meet "
                "on the third Thursday of each month."
            ),
        ))
    return meetings


# ── Scraper 4: MAG ────────────────────────────────────────────────────────────
def scrape_mag() -> list[Meeting]:
    """
    MAG calendar is JS-gated. Check individual committee pages and
    insert placeholders with direct committee links for any that block.
    """
    log.info("Scraping MAG…")
    meetings = []
    today = date.today()

    for body_name, page_url in MAG_COMMITTEE_PAGES.items():
        r = get(page_url)
        if not r or "just a moment" in r.text.lower():
            log.info(f"  MAG blocked (Cloudflare) — placeholder for {body_name}")
            meetings.append(Meeting(
                body=body_name, date="0000-00-00", time="Varies",
                location="302 N. 1st Ave, Suite 300, Phoenix AZ 85003",
                virtual_url=MAG_CALENDAR_URL,
                agenda_url=MAG_CALENDAR_URL, source_label="MAG",
                notes=(
                    "MAG meetings require manual check. Visit azmag.gov/About-Us/Calendar "
                    "for the full schedule. Each meeting links to an agenda packet and "
                    "virtual attendance info. MAG committees relevant to UPP include: "
                    "Regional Council (policy), Transportation Policy Committee, "
                    "Active Transportation Committee, and Environment & Sustainable "
                    "Communities Committee."
                ),
            ))
            continue

        s    = BeautifulSoup(r.text, "html.parser")
        text = s.get_text(" ")
        found_dates = []
        for dm in DATE_RE.finditer(text):
            dt = parse_date(dm.group(0))
            if dt and in_window(dt):
                found_dates.append(dt)

        agenda_links = []
        for a in s.find_all("a", href=True):
            h, t = a["href"], a.get_text(strip=True)
            if any(w in (h + t).lower() for w in ["agenda", "pdf", "packet"]):
                full = h if h.startswith("http") else "https://azmag.gov" + h
                agenda_links.append(full)

        if found_dates:
            for dt in found_dates[:2]:
                au = agenda_links[0] if agenda_links else page_url
                meetings.append(Meeting(
                    body=body_name, date=dt.isoformat(), time="See agenda",
                    location="302 N. 1st Ave, Suite 300, Phoenix AZ 85003",
                    virtual_url="https://azmag.gov/About-Us/Calendar",
                    agenda_url=au, source_label="MAG",
                    relevant_items=scan_pdf(au) if agenda_links else [],
                ))
                log.info(f"  + {body_name}  {dt.isoformat()}")
        else:
            meetings.append(Meeting(
                body=body_name, date="0000-00-00", time="Varies",
                location="302 N. 1st Ave, Suite 300, Phoenix AZ 85003",
                virtual_url="https://azmag.gov/About-Us/Calendar",
                agenda_url=page_url, source_label="MAG",
                notes=f"No upcoming dates detected automatically. Check {page_url}.",
            ))
    return meetings


# ── Scraper 5: Maricopa County — CivicEngage AgendaCenter ────────────────────
def scrape_maricopa_county() -> list[Meeting]:
    """
    Maricopa County uses CivicEngage. The AgendaCenter search endpoint
    returns parseable HTML with meeting titles, dates, and agenda PDF links.
    """
    log.info("Scraping Maricopa County — AgendaCenter…")
    meetings = []
    today = date.today()
    s_str = (today - timedelta(days=LOOK_BACK_DAYS)).strftime("%m%%2F%d%%2F%Y")
    e_str = (today + timedelta(days=LOOK_AHEAD_DAYS)).strftime("%m%%2F%d%%2F%Y")

    r = get(
        f"https://www.maricopa.gov/AgendaCenter/Search/"
        f"?term=&CIDs=all&startDate={s_str}&endDate={e_str}"
        f"&dateRange=custom&dateSelector=startDate"
    )
    if not r:
        log.warning("  Maricopa County AgendaCenter unavailable")
        return meetings

    relevant_bodies = [
        "board of supervisors", "planning and zoning", "planning commission",
        "zoning", "board of adjustment", "development", "transportation",
        "environment", "air quality", "flood control",
    ]

    for link in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        href = link["href"]
        if not text or len(text) < 8:
            continue
        if not any(kw in text.lower() for kw in relevant_bodies):
            continue
        if "/AgendaCenter/ViewFile/Agenda/" not in href:
            continue

        # Parse date from filename pattern _MMDDYYYY-XXXX
        m = re.search(r"_(\d{2})(\d{2})(\d{4})", href)
        if m:
            try:
                dt = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
        else:
            dm = DATE_RE.search(text)
            if not dm:
                continue
            dt = parse_date(dm.group(0))
            if not dt:
                continue

        if not in_window(dt):
            continue

        # Skip cancelled meetings
        if "cancel" in text.lower():
            log.info(f"  Skipping cancelled: {text[:60]}")
            continue

        full_url = "https://www.maricopa.gov" + href if href.startswith("/") else href
        # Strip date prefix from body label
        body = re.sub(
            r"^(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},?\s+\d{4}\s*[-–]?\s*",
            "", text, flags=re.IGNORECASE,
        ).strip() or "Maricopa County Board of Supervisors"

        virtual_url = "https://www.maricopa.gov/324"
        if any(w in text.lower() for w in ["webinar", "goto", "virtual", "online"]):
            virtual_url = "https://www.maricopa.gov/324 (see agenda for GoToWebinar access code)"

        pdf_url  = full_url.replace("?html=true", "")
        meetings.append(Meeting(
            body=body[:120], date=dt.isoformat(), time="9:00 AM",
            location="301 W. Jefferson St., Phoenix AZ 85003",
            virtual_url=virtual_url, agenda_url=full_url,
            source_label="Maricopa County", relevant_items=scan_pdf(pdf_url),
        ))
        log.info(f"  + {body[:60]}  {dt.isoformat()}")
    return meetings


# ── Collect & deduplicate ─────────────────────────────────────────────────────
def collect_all() -> list[Meeting]:
    all_meetings: list[Meeting] = []
    for fn in [scrape_phoenix_legistar, scrape_phoenix_boards,
               scrape_valley_metro, scrape_mag, scrape_maricopa_county]:
        try:
            all_meetings.extend(fn())
        except Exception as e:
            log.error(f"Scraper {fn.__name__} crashed: {e}", exc_info=True)

    seen: set[tuple] = set()
    unique: list[Meeting] = []
    for m in all_meetings:
        key = (m.body.strip().lower()[:50], m.date)
        if key not in seen:
            seen.add(key)
            unique.append(m)

    unique.sort(key=lambda m: ("9999-99-99" if m.date == "0000-00-00" else m.date, m.body.lower()))
    log.info(f"\nTotal unique meetings in window: {len(unique)}")
    return unique


# ── Colors ────────────────────────────────────────────────────────────────────
SOURCE_COLORS = {
    "City of Phoenix":  "#b04a1e",
    "Valley Metro":     "#0072bc",
    "MAG":              "#2e7d32",
    "Maricopa County":  "#6a1b9a",
}

def _badge(label: str) -> str:
    c = SOURCE_COLORS.get(label, "#555")
    return (
        f'<span style="background:{c};color:#fff;font-size:11px;padding:2px 8px;'
        f'border-radius:3px;font-family:Arial,sans-serif;font-weight:bold;'
        f'margin-right:6px;white-space:nowrap;">{label}</span>'
    )


# ── Output: full webpage ──────────────────────────────────────────────────────
def write_html_page(meetings: list[Meeting]) -> None:
    generated = datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p")

    confirmed    = [m for m in meetings if not m.is_placeholder]
    placeholders = [m for m in meetings if m.is_placeholder]

    by_week: dict[str, list[Meeting]] = {}
    for m in confirmed:
        monday = m.date_obj - timedelta(days=m.date_obj.weekday())
        by_week.setdefault(monday.isoformat(), []).append(m)

    cards_html = ""
    for wk in sorted(by_week.keys()):
        monday  = date.fromisoformat(wk)
        sunday  = monday + timedelta(days=6)
        cards_html += (
            f'<h2 class="wh">Week of {monday.strftime("%B %-d")} '
            f'&ndash; {sunday.strftime("%B %-d, %Y")}</h2>\n'
        )
        for m in confirmed:
            if (m.date_obj - timedelta(days=m.date_obj.weekday())).isoformat() != wk:
                continue
            if m.relevant_items:
                ai = "<ul class='ai'>" + "".join(
                    f"<li>{it.title[:220]}"
                    + "".join(f'<span class="kw">{k}</span>' for k in it.matched_keywords[:4])
                    + "</li>"
                    for it in m.relevant_items[:12]
                ) + "</ul>"
            elif m.notes:
                ai = f'<p class="note">{m.notes}</p>'
            else:
                ai = '<p class="pending">Agenda not yet posted — check back closer to the meeting.</p>'

            vrow = ""
            if m.virtual_url:
                vrow = (
                    f'<div class="vrow"><span class="vbadge">Virtual option</span> '
                    f'<a href="{m.virtual_url}" target="_blank" rel="noopener">'
                    f'{m.virtual_url[:70]}</a></div>'
                )

            cards_html += f"""<div class="card">
  <div class="ch">{_badge(m.source_label)}<span class="cn">{m.body}</span></div>
  <div class="cm">&#128197; {m.display_date} &nbsp;|&nbsp; &#128336; {m.time} &nbsp;|&nbsp; &#128205; {m.location}</div>
  {vrow}
  <div class="as"><div class="al">Relevant agenda items</div>{ai}</div>
  <div class="cf"><a href="{m.agenda_url}" target="_blank" rel="noopener" class="alink">View full agenda &rarr;</a></div>
</div>
"""

    if placeholders:
        cards_html += '<h2 class="wh" style="margin-top:2.5rem;border-bottom-color:#f9a825;color:#854f0b;">Manual check required</h2>\n'
        cards_html += (
            '<p style="font-size:13px;color:#777;margin-bottom:1rem;">'
            'These agencies\' calendars could not be auto-scraped this week. '
            'Dates are unconfirmed — visit each link to verify the current schedule.</p>\n'
        )
        for m in placeholders:
            cards_html += f"""<div class="card" style="border-left:3px solid #f9a825;background:#fffdf5;">
  <div class="ch">{_badge(m.source_label)}<span class="cn">{m.body}</span></div>
  <div class="cm">&#128205; {m.location}</div>
  <div class="note">{m.notes}</div>
  <div class="cf"><a href="{m.agenda_url}" target="_blank" rel="noopener" class="alink">Visit source &rarr;</a></div>
</div>
"""

    if not confirmed and not placeholders:
        cards_html = '<div class="empty"><p>No relevant meetings found in the next 4 weeks. Check back next Friday.</p></div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phoenix-Area Policy Meetings | Urban Phoenix Project</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#f5f4f0;color:#222;font-size:15px;line-height:1.6}}
.hdr{{background:#b04a1e;color:#fff;padding:1.5rem 2rem}}
.hdr h1{{font-size:1.35rem;font-weight:700;margin-bottom:3px}}
.hdr p{{font-size:.875rem;opacity:.9}}
.wrap{{max-width:900px;margin:0 auto;padding:2rem 1rem}}
.meta{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:.75rem 1rem;margin-bottom:1.5rem;font-size:13px;color:#555;display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}}
.meta strong{{color:#222}}.meta a{{color:#b04a1e;font-weight:600}}
.legend{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:1.5rem}}
.wh{{font-size:.875rem;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.05em;margin:2rem 0 .75rem;padding-bottom:4px;border-bottom:2px solid #ddd}}
.card{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1.1rem 1.25rem;margin-bottom:.9rem;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
.ch{{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:6px}}
.cn{{font-weight:600;font-size:1rem}}
.cm{{font-size:13px;color:#555;margin-bottom:7px}}
.vrow{{font-size:13px;color:#0072bc;margin-bottom:7px;word-break:break-all}}
.vbadge{{background:#e3f2fd;color:#0072bc;font-size:11px;padding:1px 6px;border-radius:3px;font-weight:700;margin-right:4px}}
.as{{margin-top:6px}}
.al{{font-size:11px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}}
.ai{{padding-left:1.2rem}}
.ai li{{font-size:13px;color:#333;margin-bottom:3px}}
.kw{{display:inline-block;background:#fff3e0;color:#e65100;font-size:10px;padding:0 5px;border-radius:3px;margin-left:4px;font-weight:700;vertical-align:middle}}
.pending{{font-size:13px;color:#999;font-style:italic}}
.note{{font-size:13px;color:#555;background:#fffde7;padding:7px 10px;border-radius:4px;border-left:3px solid #f9a825}}
.cf{{margin-top:9px}}
.alink{{font-size:13px;color:#b04a1e;font-weight:700;text-decoration:none}}
.alink:hover{{text-decoration:underline}}
.empty{{text-align:center;padding:3rem;color:#888}}
.footer{{margin-top:3rem;font-size:12px;color:#999;text-align:center;padding-top:1rem;border-top:1px solid #ddd;line-height:1.9}}
.footer a{{color:#b04a1e}}
@media(max-width:600px){{.hdr{{padding:1rem}}.wrap{{padding:1rem .5rem}}}}
</style>
</head>
<body>
<header class="hdr">
  <h1>Phoenix-Area Policy Meetings</h1>
  <p>Housing &middot; Zoning &middot; Transit &middot; Walkability &middot; Trees &amp; Heat &middot; Building Code &middot; Vision Zero &middot; Parking Reform</p>
</header>
<div class="wrap">
  <div class="meta">
    <span>Updated: <strong>{generated}</strong></span>
    <span>Next <strong>{LOOK_AHEAD_DAYS} days</strong></span>
    <span><strong>{len(meetings)}</strong> meetings identified</span>
    <span>Published by <a href="https://urbanphoenixproject.org">Urban Phoenix Project</a> &amp; <a href="https://valleyurban.org">VUAA</a></span>
  </div>
  <div class="legend">{''.join(_badge(s) for s in SOURCE_COLORS)}</div>
  {cards_html}
  <div class="footer">
    Auto-updated every Friday by
    <a href="https://urbanphoenixproject.org">Urban Phoenix Project</a> and
    <a href="https://valleyurban.org">Valley Urban Action Alliance</a>.<br>
    Sourced from official City of Phoenix, Valley Metro, MAG, and Maricopa County publications.
    Always verify details directly with the hosting agency.<br>
    <a href="meetings.json">meetings.json</a> &nbsp;&middot;&nbsp;
    <a href="digest.html">Mailchimp digest block</a> &nbsp;&middot;&nbsp;
    <a href="digest.txt">Plain text digest</a>
  </div>
</div>
</body>
</html>"""

    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")
    log.info(f"Wrote docs/index.html  ({len(html):,} bytes)")


# ── Output: Mailchimp digest ──────────────────────────────────────────────────
def write_digest(meetings: list[Meeting]) -> None:
    today_str = date.today().strftime("%B %-d, %Y")
    rows = ""
    for m in meetings:
        bc = SOURCE_COLORS.get(m.source_label, "#555")
        if m.relevant_items:
            body_content = "<br>".join(
                f"&bull;&nbsp;{it.title[:180]}" for it in m.relevant_items[:8]
            )
        elif m.notes:
            body_content = f'<em style="color:#888;">{m.notes}</em>'
        else:
            body_content = '<em style="color:#aaa;">Agenda not yet posted.</em>'

        vline = ""
        if m.virtual_url:
            vline = (
                f'<br><span style="color:#0072bc;font-size:11px;">'
                f'Virtual: <a href="{m.virtual_url}" style="color:#0072bc;">'
                f'{m.virtual_url[:65]}</a></span>'
            )

        rows += f"""<tr>
<td style="padding:14px 20px;border-bottom:1px solid #eee;vertical-align:top;font-family:Arial,sans-serif;">
  <div style="margin-bottom:5px;">
    <span style="background:{bc};color:#fff;font-size:10px;padding:2px 7px;border-radius:3px;font-weight:bold;">{m.source_label}</span>&nbsp;
    <strong style="font-size:14px;">{m.body}</strong>
  </div>
  <div style="font-size:12px;color:#666;margin-bottom:5px;">
    {m.display_date} &nbsp;|&nbsp; {m.time} &nbsp;|&nbsp; {m.location}{vline}
  </div>
  <div style="font-size:12px;color:#333;line-height:1.8;">{body_content}</div>
  <div style="margin-top:6px;">
    <a href="{m.agenda_url}" style="font-size:12px;color:#b04a1e;font-weight:bold;text-decoration:none;">View agenda &rarr;</a>
  </div>
</td></tr>"""

    if not rows:
        rows = '<tr><td style="padding:20px;color:#888;font-family:Arial,sans-serif;font-style:italic;">No relevant meetings found this week.</td></tr>'

    digest = f"""<!-- UPP MEETING DIGEST — paste into Mailchimp HTML content block
     Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | {len(meetings)} meetings -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;margin:0 auto;">
<tr><td style="background:#b04a1e;padding:18px 20px;">
  <h2 style="color:#fff;font-size:18px;margin:0 0 4px;font-family:Arial,sans-serif;">Phoenix-Area Policy Meetings</h2>
  <p style="color:#f5d0c0;font-size:12px;margin:0;font-family:Arial,sans-serif;">
    Housing &middot; Zoning &middot; Transit &middot; Vision Zero &middot; Trees &amp; Heat &middot; Building Code &nbsp;|&nbsp; Week of {today_str}
  </p>
</td></tr>
<tr><td style="padding:8px 20px;background:#fdf6f3;">
  <p style="font-size:12px;color:#777;margin:0;font-family:Arial,sans-serif;">
    {len(meetings)} meetings tracked &nbsp;&middot;&nbsp;
    <a href="https://urbanphoenixproject.github.io/meetings" style="color:#b04a1e;font-weight:bold;">View full tracker &rarr;</a>
  </p>
</td></tr>
<tr><td>
  <table width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>
</td></tr>
<tr><td style="padding:12px 20px;background:#f5f4f0;text-align:center;font-size:11px;color:#999;font-family:Arial,sans-serif;">
  Published by <a href="https://urbanphoenixproject.org" style="color:#b04a1e;">Urban Phoenix Project</a> &amp;
  <a href="https://valleyurban.org" style="color:#b04a1e;">Valley Urban Action Alliance</a>.
  Verify all meeting details with the hosting agency.
</td></tr>
</table><!-- end UPP Meeting Digest -->"""

    (OUTPUT_DIR / "digest.html").write_text(digest, encoding="utf-8")
    log.info("Wrote docs/digest.html")

    lines = [
        f"PHOENIX-AREA POLICY MEETINGS — Week of {today_str}",
        "Urban Phoenix Project & Valley Urban Action Alliance",
        "Full tracker: https://urbanphoenixproject.github.io/meetings",
        "=" * 62, "",
    ]
    for m in meetings:
        lines += [
            f"[{m.source_label.upper()}]  {m.body}",
            f"  {m.display_date}  |  {m.time}",
            f"  {m.location}",
        ]
        if m.virtual_url:
            lines.append(f"  Virtual: {m.virtual_url}")
        lines.append(f"  Agenda: {m.agenda_url}")
        for it in (m.relevant_items or [])[:6]:
            lines.append(f"  * {it.title[:160]}")
        if not m.relevant_items:
            lines.append(f"  Note: {m.notes}" if m.notes else "  Agenda items: pending")
        lines.append("")

    (OUTPUT_DIR / "digest.txt").write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote docs/digest.txt")


# ── Output: JSON ──────────────────────────────────────────────────────────────
def write_json(meetings: list[Meeting]) -> None:
    data = {
        "generated": datetime.now().isoformat(),
        "window_days": LOOK_AHEAD_DAYS,
        "count": len(meetings),
        "meetings": [asdict(m) for m in meetings],
    }
    (OUTPUT_DIR / "meetings.json").write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )
    log.info("Wrote docs/meetings.json")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 62)
    log.info("UPP Meeting Tracker — starting run")
    log.info(f"Date: {date.today()}  Window: -{LOOK_BACK_DAYS}/+{LOOK_AHEAD_DAYS} days")
    log.info("=" * 62)
    OUTPUT_DIR.mkdir(exist_ok=True)
    meetings = collect_all()
    write_html_page(meetings)
    write_digest(meetings)
    write_json(meetings)
    log.info("=" * 62)
    log.info(f"Done. {len(meetings)} meetings written to {OUTPUT_DIR}/")
    log.info("=" * 62)

if __name__ == "__main__":
    main()
