from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup
from urllib3.util import connection as urllib3_connection


# GitHub Actions sometimes reaches Russian sites through a route that breaks on IPv6.
# For this small script it is safer to force requests/urllib3 to use IPv4.
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
    value = value.replace("\u00a0", " ")
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
            variant_no_quotes = variant.replace('"', "")
            nline_no_quotes = nline.replace('"', "")
            if nline == variant or nline.startswith(variant + " "):
                return title
            if nline_no_quotes == variant_no_quotes or nline_no_quotes.startswith(variant_no_quotes + " "):
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
        if "Инструктор" not in line:
            continue
        cleaned = re.sub(r"\s+", " ", line).strip()
        # Common format: "Дарья Середа Инструктор групповых программ".
        m = re.match(r"(.+?)\s+Инструктор\b", cleaned)
        if m:
            name = m.group(1).strip()
            # Remove common image/accessibility leftovers if they appear in extracted text.
            name = re.sub(r"^(Image:?\s*)+", "", name, flags=re.IGNORECASE).strip()
            return name
    return ""


def closest_date(day: int, month: int, week_start: dt.date | None, fallback_year: int) -> dt.date:
    years = [fallback_year - 1, fallback_year, fallback_year + 1]
    candidates: list[tuple[int, dt.date]] = []
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
    lines: list[str] = []
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line.replace("\u00a0", " ")).strip()
        if line:
            lines.append(line)
    return lines


def parse_week_ranges(lines: list[str]) -> list[tuple[dt.date, dt.date]]:
    ranges: list[tuple[dt.date, dt.date]] = []
    joined = "\n".join(lines[:300])
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
        # The site usually places the title immediately after time, but the detailed card may
        # add icon text. Checking the first few lines makes the parser tolerant to that.
        for candidate in block[1:10]:
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
    week_code = f"{iso.year}-{iso.week:02d}"

    tz = ZoneInfo("Europe/Moscow")
    timestamp_points = [
        dt.datetime.combine(week_start, dt.time(12, 0), tzinfo=tz),
        dt.datetime.combine(week_start + dt.timedelta(days=2), dt.time(12, 0), tzinfo=tz),
        dt.datetime.combine(week_start + dt.timedelta(days=2), dt.time(23, 26, 17), tzinfo=tz),
        dt.datetime.combine(week_start + dt.timedelta(days=6), dt.time(12, 0), tzinfo=tz),
    ]
    timestamps = [int(point.timestamp()) for point in timestamp_points]

    urls = [
        base_url,
        base_url + "?" + urlencode({"date": week_code}),
    ]
    for timestamp in timestamps:
        urls.append(base_url + f"?date={timestamp}&date={week_code}")
    return urls


def fetch(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001 - keep diagnostics readable in Actions logs.
            last_exc = exc
            if attempt < 3:
                time.sleep(2 * attempt)
    assert last_exc is not None
    raise last_exc


def compact_preview(text: str, limit: int = 900) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def print_fetch_diagnostics(url: str, html: str, allowed_titles: list[str], week_start: dt.date) -> None:
    lines = clean_text_lines(html)
    norm_html = normalize(html)
    ranges = parse_week_ranges(lines)
    title_hits = [title for title in allowed_titles if normalize(title).replace('"', "") in norm_html.replace('"', "")]
    day_count = sum(1 for line in lines if DAY_RE.match(line))
    time_count = sum(1 for line in lines if TIME_RE.match(line))

    print(f"FETCHED: {url}")
    print(f"Week requested: {week_start.isoformat()} - {(week_start + dt.timedelta(days=6)).isoformat()}")
    print(f"HTML length: {len(html)}; text lines: {len(lines)}; day headings: {day_count}; time rows: {time_count}")
    print("Week ranges on page: " + (", ".join(f"{a.isoformat()}..{b.isoformat()}" for a, b in ranges[:6]) or "none"))
    print("Target title words found in raw page: " + (", ".join(title_hits) or "none"))

    interesting: list[str] = []
    allowed_norms = [normalize(title).replace('"', "") for title in allowed_titles]
    for index, line in enumerate(lines):
        nline = normalize(line).replace('"', "")
        if any(title_norm in nline for title_norm in allowed_norms):
            start = max(index - 2, 0)
            end = min(index + 4, len(lines))
            interesting.append(" | ".join(lines[start:end]))
            if len(interesting) >= 5:
                break
    if interesting:
        print("Target context preview:")
        for item in interesting:
            print(f"  {compact_preview(item, 500)}")
    else:
        print("Page text preview: " + compact_preview(" | ".join(lines[:80]), 1200))


def get_candidate_events(config: dict) -> list[Event]:
    base_url = config["schedule_url"]
    allowed_titles = config["allowed_titles"]
    today = dt.date.today()
    current_week = week_start_for(today)
    lookahead = int(config.get("lookahead_weeks", 2))
    desired_weeks = [current_week + dt.timedelta(days=7 * offset) for offset in range(lookahead)]
    debug = bool(config.get("debug", True))

    all_events: dict[tuple[str, str, str], Event] = {}
    seen_urls: set[str] = set()
    successful_fetches = 0

    for week_start in desired_weeks:
        for url in candidate_urls(base_url, week_start):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                html = fetch(url)
            except Exception as exc:  # noqa: BLE001 - readable Actions log is more useful here.
                print(f"WARN: could not fetch {url}: {exc}", file=sys.stderr)
                continue

            successful_fetches += 1
            events = parse_events(html, allowed_titles, week_start=week_start)

            if debug:
                print_fetch_diagnostics(url, html, allowed_titles, week_start)
                print(f"Parsed target-title events from this URL: {len(events)}")
                for event in events[:20]:
                    flags = []
                    if event.cancelled:
                        flags.append("cancelled")
                    if event.paid:
                        flags.append("paid")
                    if event.child_or_youth:
                        flags.append("child/youth")
                    if event.adult:
                        flags.append("adult")
                    print(
                        f"  parsed: {event.date} {event.hour:02d}:{event.minute:02d} "
                        f"{event.title} [{', '.join(flags) or 'no flags'}]"
                    )

            for event in events:
                existing = all_events.get(event.key)
                if existing:
                    existing.merge(event)
                else:
                    all_events[event.key] = event

    print(f"Successful fetches: {successful_fetches}; total unique target-title events before filters: {len(all_events)}")
    return sorted(all_events.values(), key=lambda e: (e.date, e.hour, e.minute, e.title))


def filter_events(events: Iterable[Event], config: dict) -> list[Event]:
    exclude = config.get("exclude", {})
    filtered: list[Event] = []
    skipped: list[tuple[Event, str]] = []

    for event in events:
        reasons: list[str] = []
        if exclude.get("cancelled", True) and event.cancelled:
            reasons.append("cancelled")
        if exclude.get("paid", True) and event.paid:
            reasons.append("paid")
        if exclude.get("children", True) and (event.child_or_youth or not event.adult):
            reasons.append("children/non-adult")

        if reasons:
            skipped.append((event, ", ".join(reasons)))
        else:
            filtered.append(event)

    if skipped:
        print("Skipped by filters:")
        for event, reason in skipped[:50]:
            print(f"- {event.date} {event.hour:02d}:{event.minute:02d} {event.title}: {reason}")

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
        f"X-WR-TIMEZONE:{ics_escape(config.get('club_timezone', 'Europe/Moscow'))}",
    ]

    for event in events:
        start_naive = dt.datetime.combine(event.date, dt.time(event.hour, event.minute))
        end_naive = start_naive + dt.timedelta(minutes=event.duration_minutes)
        start_local = start_naive.replace(tzinfo=tz)
        end_local = end_naive.replace(tzinfo=tz)

        description_parts = [club_name]
        if event.trainer:
            description_parts.append(f"Тренер: {event.trainer}")
        if event.location:
            description_parts.append(f"Зал/место: {event.location}")
        description_parts.append(f"Источник: {source_url}")
        description = "\n".join(description_parts)

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event_uid(event, club_name)}",
                f"DTSTAMP:{now}",
                f"SUMMARY:{ics_escape(event.title)}",
                f"LOCATION:{ics_escape(event.location or club_name)}",
                f"DESCRIPTION:{ics_escape(description)}",
                f"URL:{ics_escape(source_url)}",
            ]
        )
        if floating:
            lines.append(f"DTSTART:{start_naive.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND:{end_naive.strftime('%Y%m%dT%H%M%S')}")
        else:
            lines.append(f"DTSTART:{fmt_utc(start_local)}")
            lines.append(f"DTEND:{fmt_utc(end_local)}")
        if reminder_hours > 0:
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{ics_escape(event.title)}",
                    f"TRIGGER:-PT{reminder_hours}H",
                    "END:VALARM",
                ]
            )
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

    if not events:
        print("ERROR: target-title events were found, but all were removed by filters. Calendar was not updated.", file=sys.stderr)
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
