from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
import socket
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from urllib3.util import connection as urllib3_connection
import yaml
from bs4 import BeautifulSoup


def force_ipv4_for_requests() -> None:
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


force_ipv4_for_requests()

RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

DAY_RE = re.compile(
    r"^(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье),\s*(\d{1,2})\s+([А-Яа-яёЁ]+)",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*\|\s*(?:(\d{1,3})\s*(?:мин\.?|минут)?)?", re.IGNORECASE)
WEEK_RANGE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2})\.(\d{2})\.(\d{4})")

KNOWN_HALLS = [
    'Зал программ "Разумное тело"',
    "Зал основных фитнес-направлений",
    "Парк 300-летия СПб",
    "Тренажерный зал",
    "Зона FT",
    "Бассейн",
]
CHILD_MARKERS = ["ЮНИОР", "МОЛОДЕЖНЫЙ", " ДЕТ", "ДЕТИ", "ДЕТЕЙ", "14-18", "10-13", "7-9"]
CANCEL_MARKERS = ["ОТМЕНЕНО", "ОТМЕНЕН", "ОТМЕНЁН"]
PAID_MARKERS = ["$$", "ПЛАТНЫЙ УРОК", "ВЗИМАЕТСЯ ДОП", "ДОП.ПЛАТ", "ДОП. ПЛАТ"]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = value.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
    value = re.sub(r"\s+", " ", value.upper()).strip()
    return value


def target_variants(title: str) -> set[str]:
    n = normalize(title)
    variants = {n}
    if n.endswith("."):
        variants.add(n[:-1])
    variants.add(n.replace('"', ""))
    variants.add(n.replace('"', "").replace("  ", " "))
    return variants


@dataclass
class Event:
    date: dt.date
    hour: int
    minute: int
    duration_minutes: int
    title: str
    raw_title_line: str
    location: str = ""
    trainer: str = ""
    cancelled: bool = False
    paid: bool = False
    child_or_youth: bool = False
    adult: bool = False
    blocks: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.date.isoformat(), f"{self.hour:02d}:{self.minute:02d}", normalize(self.title))

    def merge(self, other: "Event") -> None:
        self.cancelled = self.cancelled or other.cancelled
        self.paid = self.paid or other.paid
        self.child_or_youth = self.child_or_youth or other.child_or_youth
        self.adult = self.adult or other.adult
        if not self.location and other.location:
            self.location = other.location
        if not self.trainer and other.trainer:
            self.trainer = other.trainer
        self.blocks.extend(other.blocks)


def extract_title(line: str, allowed_titles: list[str]) -> str | None:
    nline = normalize(line)
    for title in sorted(allowed_titles, key=lambda x: len(normalize(x)), reverse=True):
        for variant in sorted(target_variants(title), key=len, reverse=True):
            if nline == variant or nline.startswith(variant + " "):
                return title
    return None


def extract_location(line: str) -> str:
    nline = normalize(line)
    for hall in KNOWN_HALLS:
        if normalize(hall) in nline:
            return hall
    return ""


def extract_trainer(block_lines: list[str]) -> str:
    for line in block_lines:
        if "Инструктор" in line:
            m = re.match(r"(.+?)\s+Инструктор\b", line)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def closest_date(day: int, month: int, week_start: dt.date | None, fallback_year: int) -> dt.date:
    years = [fallback_year - 1, fallback_year, fallback_year + 1]
    candidates = []
    for year in years:
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            continue
        if week_start is None:
            return candidate
        candidates.append((abs((candidate - week_start).days), candidate))
    if not candidates:
        raise ValueError(f"Cannot build date for {day}.{month}")
    return min(candidates, key=lambda item: item[0])[1]


def clean_text_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = []
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def parse_week_ranges(lines: list[str]) -> list[tuple[dt.date, dt.date]]:
    ranges = []
    joined = "\n".join(lines[:200])
    for m in WEEK_RANGE_RE.finditer(joined):
        start = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        end = dt.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
        ranges.append((start, end))
    return ranges


def parse_events(html: str, allowed_titles: list[str], week_start: dt.date | None = None) -> list[Event]:
    lines = clean_text_lines(html)
    ranges = parse_week_ranges(lines)
    fallback_year = week_start.year if week_start else (ranges[0][0].year if ranges else dt.date.today().year)

    events_by_key: dict[tuple[str, str, str], Event] = {}
    current_date: dt.date | None = None
    i = 0
    while i < len(lines):
        day_match = DAY_RE.match(lines[i])
        if day_match:
            day = int(day_match.group(2))
            month_name = day_match.group(3).lower().replace("ё", "е")
            month = RU_MONTHS.get(month_name)
            if month:
                current_date = closest_date(day, month, week_start, fallback_year)
            i += 1
            continue

        time_match = TIME_RE.match(lines[i])
        if not (time_match and current_date):
            i += 1
            continue

        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        duration = int(time_match.group(3) or 50)

        block = [lines[i]]
        j = i + 1
        while j < len(lines):
            if TIME_RE.match(lines[j]) or DAY_RE.match(lines[j]):
                break
            block.append(lines[j])
            j += 1

        title_line = ""
        title = None
        for candidate in block[1:6]:
            title = extract_title(candidate, allowed_titles)
            if title:
                title_line = candidate
                break

        if title:
            block_text = "\n".join(block)
            nblock = normalize(block_text)
            event = Event(
                date=current_date,
                hour=hour,
                minute=minute,
                duration_minutes=duration,
                title=title,
                raw_title_line=title_line,
                location=extract_location(title_line),
                trainer=extract_trainer(block),
                cancelled=any(marker in nblock for marker in CANCEL_MARKERS),
                paid=any(marker in nblock for marker in PAID_MARKERS),
                child_or_youth=any(marker in nblock for marker in CHILD_MARKERS),
                adult="ВЗРОСЛЫЕ" in nblock,
                blocks=[block_text],
            )
            if week_start is None or week_start <= event.date < week_start + dt.timedelta(days=7):
                existing = events_by_key.get(event.key)
                if existing:
                    existing.merge(event)
                else:
                    events_by_key[event.key] = event

        i = j

    return sorted(events_by_key.values(), key=lambda e: (e.date, e.hour, e.minute, e.title))


def week_start_for(date_value: dt.date) -> dt.date:
    return date_value - dt.timedelta(days=date_value.weekday())


def candidate_urls(base_url: str, week_start: dt.date) -> list[str]:
    iso = week_start.isocalendar()
    # The site has been observed using date=YYYY-WW in the query string.
    week_code = f"{iso.year}-{iso.week:02d}"
    midday = dt.datetime.combine(week_start + dt.timedelta(days=2), dt.time(12, 0), tzinfo=ZoneInfo("Europe/Moscow"))
    timestamp = int(midday.timestamp())
    return [
        base_url,
        base_url + "?" + urlencode({"date": week_code}),
        base_url + f"?date={timestamp}&date={week_code}",
    ]


def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; fitness24-calendar/1.0; +https://github.com/)",
        "Accept-Language": "ru,en;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def get_candidate_events(config: dict) -> list[Event]:
    base_url = config["schedule_url"]
    allowed_titles = config["allowed_titles"]
    today = dt.date.today()
    current_week = week_start_for(today)
    lookahead = int(config.get("lookahead_weeks", 2))
    desired_weeks = [current_week + dt.timedelta(days=7 * offset) for offset in range(lookahead)]

    all_events: dict[tuple[str, str, str], Event] = {}
    seen_urls: set[str] = set()

    for week_start in desired_weeks:
        for url in candidate_urls(base_url, week_start):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                html = fetch(url)
            except Exception as exc:
                print(f"WARN: could not fetch {url}: {exc}", file=sys.stderr)
                continue
            events = parse_events(html, allowed_titles, week_start=week_start)
            for event in events:
                all_events.setdefault(event.key, event).merge(event)

    return sorted(all_events.values(), key=lambda e: (e.date, e.hour, e.minute, e.title))


def filter_events(events: Iterable[Event], config: dict) -> list[Event]:
    exclude = config.get("exclude", {})
    filtered = []
    for event in events:
        if exclude.get("cancelled", True) and event.cancelled:
            continue
        if exclude.get("paid", True) and event.paid:
            continue
        if exclude.get("children", True) and (event.child_or_youth or not event.adult):
            continue
        filtered.append(event)
    return filtered


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str) -> str:
    # RFC 5545 recommends 75 octets; this character-based fold is enough for common clients.
    if len(line) <= 73:
        return line
    chunks = [line[:73]]
    rest = line[73:]
    while rest:
        chunks.append(" " + rest[:72])
        rest = rest[72:]
    return "\r\n".join(chunks)


def fmt_utc(dt_value: dt.datetime) -> str:
    return dt_value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(event: Event, club_name: str) -> str:
    seed = f"{club_name}|{event.date.isoformat()}|{event.hour:02d}:{event.minute:02d}|{normalize(event.title)}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"fitness24-{digest}@fitness24-calendar.local"


def build_ics(events: list[Event], config: dict) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tz = ZoneInfo(config.get("club_timezone", "Europe/Moscow"))
    floating = bool(config.get("floating_times", False))
    reminder_hours = int(config.get("reminder_hours_before", 3))
    club_name = config.get("club_name", "FITNESS 24")
    source_url = config.get("source_url", config.get("schedule_url", ""))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//fitness24-calendar//fitness24 selected classes//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(config.get('calendar_name', club_name))}",
        "X-WR-TIMEZONE:Europe/Moscow",
    ]

    for event in events:
        start_naive = dt.datetime.combine(event.date, dt.time(event.hour, event.minute))
        end_naive = start_naive + dt.timedelta(minutes=event.duration_minutes)
        start_local = start_naive.replace(tzinfo=tz)
        end_local = end_naive.replace(tzinfo=tz)

        description_parts = [f"{club_name}"]
        if event.trainer:
            description_parts.append(f"Тренер: {event.trainer}")
        if event.location:
            description_parts.append(f"Зал/место: {event.location}")
        description_parts.append(f"Источник: {source_url}")
        description = "\n".join(description_parts)

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{event_uid(event, club_name)}",
            f"DTSTAMP:{now}",
            f"SUMMARY:{ics_escape(event.title)}",
            f"LOCATION:{ics_escape(event.location or club_name)}",
            f"DESCRIPTION:{ics_escape(description)}",
            f"URL:{ics_escape(source_url)}",
        ])
        if floating:
            lines.append(f"DTSTART:{start_naive.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND:{end_naive.strftime('%Y%m%dT%H%M%S')}")
        else:
            lines.append(f"DTSTART:{fmt_utc(start_local)}")
            lines.append(f"DTEND:{fmt_utc(end_local)}")
        if reminder_hours > 0:
            lines.extend([
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{ics_escape(event.title)}",
                f"TRIGGER:-PT{reminder_hours}H",
                "END:VALARM",
            ])
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an .ics calendar from FITNESS 24 schedule.")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw_events = get_candidate_events(config)
    events = filter_events(raw_events, config)

    if not raw_events:
        print("ERROR: no target-title events were fetched. Calendar was not updated.", file=sys.stderr)
        return 1

    output = Path(config["output_file"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_ics(events, config), encoding="utf-8")

    print(f"Found {len(raw_events)} target-title events; wrote {len(events)} filtered events to {output}")
    for event in events:
        print(f"- {event.date} {event.hour:02d}:{event.minute:02d} {event.title} ({event.location or 'no location'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
