from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml
from bs4 import BeautifulSoup, Tag
from urllib3.util import connection as urllib3_connection


# The site is sometimes unreachable over IPv6 from cloud runners.
# For a local Windows launch IPv4 is also a safe choice.
def force_ipv4_for_requests() -> None:
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


force_ipv4_for_requests()

WEEK_RANGE_RE = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{4})\s*[-–—]\s*(\d{2})\.(\d{2})\.(\d{4})"
)
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
INTEGER_RE = re.compile(r"(\d{1,3})")


@dataclass(frozen=True)
class Event:
    date: dt.date
    hour: int
    minute: int
    duration_minutes: int
    title: str
    location: str
    trainer: str
    adult: bool
    paid: bool
    cancelled: bool

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.date.isoformat(),
            f"{self.hour:02d}:{self.minute:02d}",
            title_match_key(self.title),
        )


def clean_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalized_text(value: str | None) -> str:
    value = clean_text(value)
    value = value.replace("ё", "е").replace("Ё", "Е")
    value = (
        value.replace("«", '"')
        .replace("»", '"')
        .replace("“", '"')
        .replace("”", '"')
    )
    return value.upper()


def title_match_key(value: str | None) -> str:
    """Case-insensitive title key that ignores quotes and punctuation.

    Examples:
      DANCE MIX. == Dance mix
      OUTDOOR «RUNNING» == Outdoor "Running"
    """
    value = normalized_text(value)
    value = re.sub(r"[^0-9A-ZА-Я ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def allowed_title_keys(allowed_titles: Iterable[str]) -> set[str]:
    return {title_match_key(title) for title in allowed_titles if title_match_key(title)}


def parse_date_range(value: str) -> tuple[dt.date, dt.date] | None:
    match = WEEK_RANGE_RE.search(value)
    if not match:
        return None
    start = dt.date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    end = dt.date(int(match.group(6)), int(match.group(5)), int(match.group(4)))
    return start, end


def selected_week_range(soup: BeautifulSoup) -> tuple[dt.date, dt.date] | None:
    selected = soup.select_one("#schedule-date option[selected]")
    if selected is None:
        selected = soup.select_one("#schedule-date option:checked")
    if selected is not None:
        parsed = parse_date_range(selected.get_text(" ", strip=True))
        if parsed:
            return parsed

    for option in soup.select("#schedule-date option"):
        parsed = parse_date_range(option.get_text(" ", strip=True))
        if parsed:
            return parsed
    return None


def direct_child_with_class(parent: Tag, tag_name: str, class_name: str) -> Tag | None:
    for child in parent.children:
        if isinstance(child, Tag) and child.name == tag_name and class_name in child.get("class", []):
            return child
    return None


def first_text(parent: Tag, selector: str) -> str:
    node = parent.select_one(selector)
    return clean_text(node.get_text(" ", strip=True)) if node else ""


def parse_duration(value: str, default_minutes: int) -> int:
    match = INTEGER_RE.search(value)
    if not match:
        return default_minutes
    minutes = int(match.group(1))
    return minutes if 1 <= minutes <= 240 else default_minutes


def parse_schedule_html(
    html: str,
    allowed_titles: list[str],
    expected_week_start: dt.date | None = None,
    default_duration_minutes: int = 50,
) -> tuple[list[Event], dt.date | None]:
    """Parse the real FITNESS 24 HTML structure.

    The page contains seven containers:
      #schedule-table > .schedule > .days#day_0 ... #day_6

    Each lesson is stored in:
      .day > .lesson > .lesson-card

    The visible compact card contains span.time, span.duration, span.type,
    span.hall, span.old and span.trainer-name.
    """
    soup = BeautifulSoup(html, "html.parser")
    week_range = selected_week_range(soup)
    page_week_start = week_range[0] if week_range else expected_week_start

    if expected_week_start is not None and page_week_start != expected_week_start:
        return [], page_week_start
    if page_week_start is None:
        return [], None

    wanted = allowed_title_keys(allowed_titles)
    events: dict[tuple[str, str, str], Event] = {}

    day_containers = soup.select("#schedule-table > .schedule > .days[id]")
    for day_container in day_containers:
        day_id = day_container.get("id", "")
        match = re.fullmatch(r"day_(\d)", day_id)
        if not match:
            continue
        day_index = int(match.group(1))
        if not 0 <= day_index <= 6:
            continue
        event_date = page_week_start + dt.timedelta(days=day_index)

        # Each direct .day child represents one scheduled lesson.
        for day_block in day_container.find_all("div", class_="day", recursive=False):
            lesson = direct_child_with_class(day_block, "div", "lesson")
            if lesson is None:
                continue
            card = direct_child_with_class(lesson, "div", "lesson-card")
            if card is None:
                continue

            site_title = first_text(card, "span.type")
            if title_match_key(site_title) not in wanted:
                continue

            time_text = first_text(card, "span.time")
            time_match = TIME_RE.fullmatch(time_text)
            if not time_match:
                continue

            duration_text = first_text(card, "span.duration")
            duration_minutes = parse_duration(duration_text, default_duration_minutes)
            location = first_text(card, "span.hall")
            trainer = first_text(card, "span.trainer-name")

            age_values = {
                normalized_text(node.get_text(" ", strip=True))
                for node in card.select("span.old")
                if clean_text(node.get_text(" ", strip=True))
            }
            adult = any(age == "ВЗРОСЛЫЕ" for age in age_values)

            header = card.select_one(".lesson-header")
            header_classes = set(header.get("class", [])) if header else set()
            lesson_text = normalized_text(lesson.get_text(" ", strip=True))

            paid = (
                "lesson-money" in header_classes
                or card.select_one(".money") is not None
                or "$$" in site_title
                or "ПЛАТНЫЙ УРОК" in lesson_text
            )
            cancelled = (
                lesson.select_one(".icon-otmena, .otmena") is not None
                or "УРОК ВРЕМЕННО ОТМЕНЕН" in lesson_text
                or "ОТМЕНЕНО" in lesson_text
            )

            event = Event(
                date=event_date,
                hour=int(time_match.group(1)),
                minute=int(time_match.group(2)),
                duration_minutes=duration_minutes,
                # Preserve the site's spelling/case and avoid adding punctuation from config.yaml.
                title=site_title,
                location=location,
                trainer=trainer,
                adult=adult,
                paid=paid,
                cancelled=cancelled,
            )
            events[event.key] = event

    return sorted(events.values(), key=lambda e: (e.date, e.hour, e.minute, e.title)), page_week_start


def week_start_for(value: dt.date) -> dt.date:
    return value - dt.timedelta(days=value.weekday())


def candidate_urls(base_url: str, week_start: dt.date) -> list[str]:
    iso = week_start.isocalendar()
    week_code = f"{iso.year}-{iso.week:02d}"

    # The normal ?date=YYYY-WW form is sufficient on the current site.
    # A timestamp + week code is retained as a fallback because the site also emits such URLs.
    moscow = timezone_for("Europe/Moscow")
    timestamp = int(
        dt.datetime.combine(week_start, dt.time(12, 0), tzinfo=moscow).timestamp()
    )
    return [
        base_url + "?" + urlencode({"date": week_code}),
        base_url + f"?date={timestamp}&date={week_code}",
    ]


def fetch(url: str, timeout_seconds: int = 25, retries: int = 2) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding or "utf-8"
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)

    assert last_error is not None
    raise last_error


def get_candidate_events(config: dict) -> list[Event]:
    base_url = str(config["schedule_url"])
    allowed_titles = list(config["allowed_titles"])
    lookahead_weeks = int(config.get("lookahead_weeks", 2))
    default_duration = int(config.get("default_duration_minutes", 50))

    current_week = week_start_for(dt.date.today())
    desired_weeks = [
        current_week + dt.timedelta(days=7 * offset)
        for offset in range(lookahead_weeks)
    ]

    all_events: dict[tuple[str, str, str], Event] = {}
    fetched_matching_weeks = 0

    for requested_week in desired_weeks:
        week_loaded = False
        for url in candidate_urls(base_url, requested_week):
            try:
                html = fetch(
                    url,
                    timeout_seconds=int(config.get("request_timeout_seconds", 25)),
                    retries=int(config.get("request_retries", 2)),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"WARN: could not fetch {url}: {exc}", file=sys.stderr)
                continue

            parsed, page_week = parse_schedule_html(
                html,
                allowed_titles,
                expected_week_start=requested_week,
                default_duration_minutes=default_duration,
            )

            if page_week != requested_week:
                shown = page_week.isoformat() if page_week else "unknown"
                print(
                    f"WARN: {url} returned week {shown}, expected {requested_week.isoformat()}"
                )
                continue

            fetched_matching_weeks += 1
            week_loaded = True
            print(
                f"Loaded week {requested_week.isoformat()} from {url}; "
                f"target-title events before filters: {len(parsed)}"
            )
            for event in parsed:
                flags = []
                if not event.adult:
                    flags.append("non-adult")
                if event.paid:
                    flags.append("paid")
                if event.cancelled:
                    flags.append("cancelled")
                print(
                    f"  {event.date} {event.hour:02d}:{event.minute:02d} "
                    f"{event.title} ({event.location or 'без зала'})"
                    + (f" [{', '.join(flags)}]" if flags else "")
                )
                all_events[event.key] = event
            break

        if not week_loaded:
            print(
                f"WARN: no matching schedule page was obtained for week "
                f"{requested_week.isoformat()}",
                file=sys.stderr,
            )

    if fetched_matching_weeks == 0:
        raise RuntimeError("No requested schedule week could be fetched and parsed")

    return sorted(all_events.values(), key=lambda e: (e.date, e.hour, e.minute, e.title))


def filter_events(events: Iterable[Event], config: dict) -> list[Event]:
    exclude = config.get("exclude", {})
    kept: list[Event] = []

    for event in events:
        reasons: list[str] = []
        if bool(exclude.get("cancelled", True)) and event.cancelled:
            reasons.append("отменено")
        if bool(exclude.get("paid", True)) and event.paid:
            reasons.append("платное")
        if bool(exclude.get("children", True)) and not event.adult:
            reasons.append("не взрослая группа")

        if reasons:
            print(
                f"Skipped: {event.date} {event.hour:02d}:{event.minute:02d} "
                f"{event.title} — {', '.join(reasons)}"
            )
        else:
            kept.append(event)

    return kept


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str) -> str:
    """Fold an iCalendar line at 75 UTF-8 octets without breaking characters."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    chunks: list[str] = []
    current = ""
    current_bytes = 0
    limit = 75

    for char in line:
        char_bytes = len(char.encode("utf-8"))
        if current and current_bytes + char_bytes > limit:
            chunks.append(current)
            current = " " + char
            current_bytes = 1 + char_bytes
            limit = 75
        else:
            current += char
            current_bytes += char_bytes
    if current:
        chunks.append(current)
    return "\r\n".join(chunks)


def timezone_for(name: str) -> dt.tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return dt.timezone(dt.timedelta(hours=3), name="Europe/Moscow")
        raise


def fmt_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(event: Event, club_name: str) -> str:
    seed = (
        f"{club_name}|{event.date.isoformat()}|"
        f"{event.hour:02d}:{event.minute:02d}|{title_match_key(event.title)}"
    )
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:18]
    return f"fitness24-{digest}@fitness24-calendar.local"


def build_ics(events: list[Event], config: dict) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    timezone_name = str(config.get("club_timezone", "Europe/Moscow"))
    tz = timezone_for(timezone_name)
    reminder_hours = int(config.get("reminder_hours_before", 3))
    club_name = str(config.get("club_name", "FITNESS 24"))
    source_url = str(config.get("source_url", config.get("schedule_url", "")))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//fitness24-calendar//selected classes//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(str(config.get('calendar_name', club_name)))}",
        f"X-WR-TIMEZONE:{ics_escape(timezone_name)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for event in events:
        start_local = dt.datetime(
            event.date.year,
            event.date.month,
            event.date.day,
            event.hour,
            event.minute,
            tzinfo=tz,
        )
        end_local = start_local + dt.timedelta(minutes=event.duration_minutes)

        description_parts = [club_name]
        if event.trainer:
            description_parts.append(f"Тренер: {event.trainer}")
        if event.location:
            description_parts.append(f"Зал/место: {event.location}")
        description_parts.append(f"Источник: {source_url}")

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event_uid(event, club_name)}",
                f"DTSTAMP:{now}",
                f"SUMMARY:{ics_escape(event.title)}",
                f"DTSTART:{fmt_utc(start_local)}",
                f"DTEND:{fmt_utc(end_local)}",
                f"LOCATION:{ics_escape(event.location or club_name)}",
                f"DESCRIPTION:{ics_escape(chr(10).join(description_parts))}",
                f"URL:{ics_escape(source_url)}",
            ]
        )
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
    parser = argparse.ArgumentParser(
        description="Generate an iCalendar file from the FITNESS 24 schedule."
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--html-file",
        help="Optional local HTML file for parser testing instead of downloading the site",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    try:
        if args.html_file:
            html = Path(args.html_file).read_text(encoding="utf-8")
            parsed, page_week = parse_schedule_html(
                html,
                list(config["allowed_titles"]),
                expected_week_start=None,
                default_duration_minutes=int(config.get("default_duration_minutes", 50)),
            )
            if page_week is None:
                print("ERROR: could not determine the selected week in the HTML", file=sys.stderr)
                return 1
            raw_events = parsed
            print(
                f"Parsed local HTML for week {page_week.isoformat()}; "
                f"target-title events before filters: {len(raw_events)}"
            )
        else:
            raw_events = get_candidate_events(config)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    filtered_events = filter_events(raw_events, config)

    if not raw_events:
        print(
            "ERROR: no target-title events were found. Calendar was not updated.",
            file=sys.stderr,
        )
        return 1
    if not filtered_events:
        print(
            "ERROR: all target-title events were removed by filters. "
            "Calendar was not updated.",
            file=sys.stderr,
        )
        return 1

    output = Path(str(config["output_file"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_ics(filtered_events, config), encoding="utf-8", newline="")

    print(
        f"Found {len(raw_events)} target-title events; "
        f"wrote {len(filtered_events)} events to {output}"
    )
    for event in filtered_events:
        print(
            f"- {event.date} {event.hour:02d}:{event.minute:02d} "
            f"{event.title} ({event.location or 'без зала'})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
