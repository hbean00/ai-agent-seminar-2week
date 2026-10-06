"""Recurring jobs: "매일 아침 7시에 ~해줘".

Why this lives in the bot rather than in the OS scheduler: on the machine this
was built for, Task Scheduler could not launch *any* action -- an inline
PowerShell command, a -File script, cmd running a batch, and python.exe
invoked directly all failed without producing a line of output, while every
one of those command lines worked when run by hand. Registering a task also
needs an elevation prompt nobody is there to click. cron has none of those
problems, but then macOS and Windows would need separate implementations of
the same feature. A loop inside the bot works the same everywhere and needs no
privileges.

Catch-up, not skip: a schedule is due whenever its most recent slot has passed
and has not been run. A PC that was asleep at 07:00 therefore runs the morning
job when it wakes rather than silently dropping it, and the reply says the run
was late. ``last_run_at`` is what keeps that from re-firing -- once today's
07:00 slot is marked, the next one is tomorrow's, so a bot restarted five
times before noon still runs it once.
"""

import contextlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_PATH = Path("~/.claudecord/schedules.json").expanduser()

# Local time throughout, deliberately. "매일 아침 7시" means seven o'clock
# where the user is, and this bot runs on that user's own PC -- storing UTC
# would only add a conversion that a DST change could get wrong.
DAILY = "daily"
WEEKLY = "weekly"
KINDS = (DAILY, WEEKLY)

# A run this far past its slot is reported as late, so a schedule that fired
# at 09:40 because the PC was asleep does not read as a 07:00 run.
LATE_RUN_THRESHOLD = timedelta(minutes=5)

_store_cache: dict[Path, dict[str, Any]] = {}
_store_lock = threading.Lock()


@dataclass(frozen=True)
class Schedule:
    id: str
    channel_id: str
    prompt: str
    kind: str
    at: time
    # Only meaningful for WEEKLY: Monday is 0, matching datetime.weekday().
    weekdays: tuple[int, ...] = ()
    created_at: datetime | None = None
    last_run_at: datetime | None = None

    def describe(self) -> str:
        """The Korean one-liner the bot echoes back when confirming."""
        clock = self.at.strftime("%H:%M")
        if self.kind == DAILY:
            return f"매일 {clock}"
        names = "·".join(WEEKDAY_NAMES[d] for d in sorted(self.weekdays))
        return f"매주 {names} {clock}"


WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")


def _matches_day(schedule: Schedule, moment: datetime) -> bool:
    if schedule.kind == DAILY:
        return True
    return moment.weekday() in schedule.weekdays


def previous_slot(schedule: Schedule, now: datetime) -> datetime | None:
    """The most recent moment this schedule was supposed to fire, at or before ``now``.

    None when it has never been due yet -- a weekly Monday job registered on a
    Wednesday has no past slot within the look-back window.
    """
    # Eight days back covers a weekly schedule's single missed slot plus the
    # day boundary; looking further would resurrect jobs from a holiday the
    # machine spent switched off, which is noise rather than catch-up.
    for days_ago in range(8):
        candidate_day = (now - timedelta(days=days_ago)).date()
        candidate = datetime.combine(candidate_day, schedule.at)
        if candidate > now:
            continue
        if _matches_day(schedule, candidate):
            return candidate
    return None


def is_due(schedule: Schedule, now: datetime) -> bool:
    slot = previous_slot(schedule, now)
    if slot is None:
        return False
    # Never run before: only the slot that opened after registration counts,
    # otherwise registering at 09:00 would immediately fire this morning's
    # 07:00 job the user has not asked for yet.
    reference = schedule.last_run_at or schedule.created_at
    if reference is None:
        return True
    return slot > reference


def next_slot(schedule: Schedule, now: datetime) -> datetime:
    """The next moment this schedule will fire, strictly after ``now``."""
    for days_ahead in range(8):
        candidate_day = (now + timedelta(days=days_ahead)).date()
        candidate = datetime.combine(candidate_day, schedule.at)
        if candidate <= now:
            continue
        if _matches_day(schedule, candidate):
            return candidate
    # Unreachable for a valid schedule; returning a far-future time beats
    # raising inside the loop that drives every tick.
    return now + timedelta(days=8)


# --------------------------------------------------------------- the store

def _quarantine_corrupt_store(path: Path) -> None:
    backup = path.with_name(f"{path.name}.corrupt")
    try:
        os.replace(path, backup)
    except OSError:
        logger.warning("Schedule store %s was unreadable and could not be moved", path, exc_info=True)
        return
    logger.warning(
        "Schedule store %s was unreadable; moved it to %s and started empty", path, backup
    )


def _load() -> dict[str, Any]:
    path = _STORE_PATH
    with _store_lock:
        cached = _store_cache.get(path)
        if cached is not None:
            return cached

        store: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _quarantine_corrupt_store(path)
            else:
                if isinstance(loaded, dict):
                    store = loaded
                else:
                    _quarantine_corrupt_store(path)
        _store_cache[path] = store
        return store


def _save(store: dict[str, Any]) -> bool:
    """Persist ``store`` atomically. Never raises -- see sessions._save."""
    path = _STORE_PATH
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(store, ensure_ascii=False, indent=2) + "\n"
    with _store_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("Failed to persist schedule store at %s", path, exc_info=True)
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            return False
        _store_cache[path] = store
        return True


def _to_record(schedule: Schedule) -> dict[str, Any]:
    return {
        "channel_id": schedule.channel_id,
        "prompt": schedule.prompt,
        "kind": schedule.kind,
        "at": schedule.at.strftime("%H:%M"),
        "weekdays": list(schedule.weekdays),
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
    }


def _from_record(schedule_id: str, record: Any) -> Schedule | None:
    """Parse one stored record, or None if it is unusable.

    A single malformed entry must not take the whole store down with it: the
    scheduler loop reads this on every tick, and a store that raises would
    stop every *other* schedule too.
    """
    if not isinstance(record, dict):
        return None
    try:
        hour, minute = (int(part) for part in str(record["at"]).split(":", 1))
        at = time(hour=hour, minute=minute)
        kind = str(record["kind"])
        if kind not in KINDS:
            return None
        weekdays = tuple(int(d) for d in record.get("weekdays") or ())
        if kind == WEEKLY and not weekdays:
            return None
        return Schedule(
            id=schedule_id,
            channel_id=str(record["channel_id"]),
            prompt=str(record["prompt"]),
            kind=kind,
            at=at,
            weekdays=weekdays,
            created_at=_parse_dt(record.get("created_at")),
            last_run_at=_parse_dt(record.get("last_run_at")),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Ignoring malformed schedule %s", schedule_id, exc_info=True)
        return None


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def add_schedule(
    channel_id: str,
    prompt: str,
    kind: str,
    at: time,
    weekdays: tuple[int, ...] = (),
    *,
    now: datetime | None = None,
) -> Schedule:
    now = now or datetime.now()
    schedule = Schedule(
        id=uuid.uuid4().hex[:8],
        channel_id=channel_id,
        prompt=prompt,
        kind=kind,
        at=at,
        weekdays=weekdays,
        created_at=now,
    )
    store = dict(_load())
    store[schedule.id] = _to_record(schedule)
    _save(store)
    return schedule


def list_schedules(channel_id: str | None = None) -> list[Schedule]:
    schedules = []
    for schedule_id, record in _load().items():
        schedule = _from_record(schedule_id, record)
        if schedule is None:
            continue
        if channel_id is None or schedule.channel_id == channel_id:
            schedules.append(schedule)
    return sorted(schedules, key=lambda s: (s.at, s.id))


def remove_schedule(schedule_id: str) -> bool:
    store = dict(_load())
    if schedule_id not in store:
        return False
    del store[schedule_id]
    _save(store)
    return True


def due_schedules(now: datetime | None = None) -> list[Schedule]:
    now = now or datetime.now()
    return [s for s in list_schedules() if is_due(s, now)]


def mark_ran(schedule_id: str, *, now: datetime | None = None) -> None:
    """Record that this schedule's current slot has fired.

    Written *before* the job runs, not after: a job that crashes or takes
    twenty minutes must not leave the slot open for the next tick to pick up
    again, which would retry a failing schedule every minute.
    """
    now = now or datetime.now()
    store = dict(_load())
    record = store.get(schedule_id)
    if not isinstance(record, dict):
        return
    updated = dict(record)
    updated["last_run_at"] = now.isoformat()
    store[schedule_id] = updated
    _save(store)


# ------------------------------------------------- recognising the request

# A cheap pre-filter, not a parser. Every inbound message is tested against
# it, so it must not cost a CLI call; only messages that match pay for the
# extraction below. It is deliberately loose -- "매일 쓰는 스크립트 고쳐줘"
# matches and is then rejected by the model, which is the right way round:
# a false positive costs one call, a false negative loses the feature.
_SCHEDULE_HINT_RE = re.compile(
    r"매일|매주|매 ?주|평일|주말|날마다|아침마다|저녁마다|밤마다|정기적으로|예약해|예약 ?등록"
)

LIST_COMMANDS = ("예약 목록", "예약목록", "예약 리스트")
_DELETE_RE = re.compile(r"^예약\s*(?:삭제|취소|해제)\s+([0-9a-f]{4,32})$")

EXTRACTION_SYSTEM_HINT = (
    "너는 한국어 요청에서 반복 일정을 추출하는 파서다. "
    "설명·인사·코드펜스 없이 JSON 객체 하나만 출력한다. "
    "도구를 쓰지 말고 파일을 만들지 마라."
)

EXTRACTION_PROMPT = """다음 메시지가 '반복 일정 등록' 요청인지 판단하고 JSON 하나만 출력해라.

메시지:
{text}

반복 일정 요청이 아니면: {{"is_schedule": false}}

반복 일정 요청이면:
{{"is_schedule": true, "kind": "daily" 또는 "weekly",
  "at": "HH:MM" (24시간제),
  "weekdays": [0-6 정수 배열, 월요일이 0. kind가 weekly일 때만],
  "prompt": "그 시각에 실제로 수행할 작업 지시문"}}

규칙:
- "아침"은 07:00, "점심"은 12:00, "저녁"은 19:00, "밤"은 22:00으로 본다. 시각이 명시되면 그쪽을 따른다.
- "평일"은 weekly에 weekdays [0,1,2,3,4], "주말"은 [5,6]이다.
- prompt에는 시각 표현을 빼고 수행할 일만 남긴다.
- 매달·매년처럼 일/주 단위가 아니면 {{"is_schedule": false}}로 답한다."""


@dataclass(frozen=True)
class ScheduleSpec:
    """What the model extracted, before it becomes a stored Schedule."""

    kind: str
    at: time
    weekdays: tuple[int, ...]
    prompt: str


def looks_like_schedule_request(text: str) -> bool:
    return bool(_SCHEDULE_HINT_RE.search(text))


def parse_list_command(text: str) -> bool:
    return text.strip() in LIST_COMMANDS


def parse_delete_command(text: str) -> str | None:
    match = _DELETE_RE.match(text.strip())
    return match.group(1) if match else None


def _first_json_object(raw: str) -> str | None:
    """The first balanced {...} in ``raw``.

    The model is told to emit bare JSON and usually does, but a stray
    sentence or a ```json fence around it must not lose the answer -- and
    scanning for braces is cheaper to get right than prompting harder.
    """
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def parse_extraction(raw: str) -> ScheduleSpec | None:
    """Turn the model's reply into a spec, or None if it is not a schedule.

    None covers every failure the same way on purpose: not a schedule, no
    JSON at all, JSON missing a field, an hour of 99. The caller's fallback
    is to treat the message as an ordinary job, which is what the user would
    have got anyway -- raising here would turn a bad parse into a dead turn.
    """
    blob = _first_json_object(raw or "")
    if blob is None:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("is_schedule"):
        return None

    kind = str(data.get("kind") or "")
    if kind not in KINDS:
        return None

    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return None

    try:
        hour_raw, minute_raw = str(data.get("at") or "").split(":", 1)
        at = time(hour=int(hour_raw), minute=int(minute_raw))
    except (TypeError, ValueError):
        return None

    weekdays: tuple[int, ...] = ()
    if kind == WEEKLY:
        raw_days = data.get("weekdays")
        if not isinstance(raw_days, list):
            return None
        try:
            weekdays = tuple(sorted({int(d) for d in raw_days}))
        except (TypeError, ValueError):
            return None
        if not weekdays or any(d < 0 or d > 6 for d in weekdays):
            return None

    return ScheduleSpec(kind=kind, at=at, weekdays=weekdays, prompt=prompt)
