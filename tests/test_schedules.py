import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock

from src import schedules
from src.schedules import DAILY, WEEKLY, Schedule


def _schedule(**overrides) -> Schedule:
    base = {
        "id": "s1",
        "channel_id": "C01ALLOW",
        "prompt": "뉴스 요약해줘",
        "kind": DAILY,
        "at": time(7, 0),
        "weekdays": (),
        "created_at": datetime(2026, 3, 2, 9, 0),
        "last_run_at": None,
    }
    base.update(overrides)
    return Schedule(**base)


class _StoreTestCase(unittest.TestCase):
    """Repoints the store at a temp file so no test touches ~/.claudecord."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "schedules.json"
        patcher = mock.patch.object(schedules, "_STORE_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        schedules._store_cache.clear()
        self.addCleanup(schedules._store_cache.clear)


class SlotArithmeticTests(unittest.TestCase):
    def test_previous_slot_is_todays_time_once_it_has_passed(self):
        now = datetime(2026, 3, 2, 10, 0)  # Monday
        self.assertEqual(
            schedules.previous_slot(_schedule(), now), datetime(2026, 3, 2, 7, 0)
        )

    def test_previous_slot_is_yesterdays_before_todays_time_arrives(self):
        now = datetime(2026, 3, 2, 6, 30)
        self.assertEqual(
            schedules.previous_slot(_schedule(), now), datetime(2026, 3, 1, 7, 0)
        )

    def test_a_weekly_schedule_only_lands_on_its_own_weekday(self):
        # Monday-only, asked about on Wednesday: the last slot is Monday's.
        weekly = _schedule(kind=WEEKLY, weekdays=(0,))
        now = datetime(2026, 3, 4, 10, 0)  # Wednesday
        self.assertEqual(schedules.previous_slot(weekly, now), datetime(2026, 3, 2, 7, 0))

    def test_next_slot_is_strictly_in_the_future(self):
        now = datetime(2026, 3, 2, 7, 0)  # exactly on the slot
        self.assertEqual(schedules.next_slot(_schedule(), now), datetime(2026, 3, 3, 7, 0))


class DueTests(unittest.TestCase):
    def test_a_schedule_registered_after_todays_slot_does_not_fire_today(self):
        # Registering at 09:00 must not immediately run this morning's 07:00
        # job the user has not asked for yet.
        created = _schedule(created_at=datetime(2026, 3, 2, 9, 0))
        self.assertFalse(schedules.is_due(created, datetime(2026, 3, 2, 9, 1)))

    def test_it_fires_at_the_next_slot_after_registration(self):
        created = _schedule(created_at=datetime(2026, 3, 2, 9, 0))
        self.assertTrue(schedules.is_due(created, datetime(2026, 3, 3, 7, 0)))

    def test_a_slot_missed_while_the_pc_slept_still_fires_on_wake(self):
        # The whole point of catch-up: 07:00 passed with the machine asleep,
        # and the job runs at 09:40 rather than being dropped.
        ran_yesterday = _schedule(last_run_at=datetime(2026, 3, 1, 7, 0))
        self.assertTrue(schedules.is_due(ran_yesterday, datetime(2026, 3, 2, 9, 40)))

    def test_it_does_not_fire_twice_in_one_slot(self):
        # A bot restarted five times before noon must still run it once.
        just_ran = _schedule(last_run_at=datetime(2026, 3, 2, 7, 0))
        for hour in (7, 8, 11, 23):
            with self.subTest(hour=hour):
                self.assertFalse(schedules.is_due(just_ran, datetime(2026, 3, 2, hour, 30)))

    def test_it_fires_again_the_following_day(self):
        just_ran = _schedule(last_run_at=datetime(2026, 3, 2, 7, 0))
        self.assertTrue(schedules.is_due(just_ran, datetime(2026, 3, 3, 7, 0)))

    def test_a_weekly_schedule_stays_quiet_on_other_days(self):
        weekly = _schedule(
            kind=WEEKLY, weekdays=(0,), created_at=datetime(2026, 3, 2, 8, 0)
        )
        self.assertFalse(schedules.is_due(weekly, datetime(2026, 3, 4, 9, 0)))  # Wednesday
        self.assertTrue(schedules.is_due(weekly, datetime(2026, 3, 9, 7, 0)))  # next Monday

    def test_a_week_long_outage_replays_only_the_latest_slot(self):
        # Coming back after a holiday must not fire seven mornings at once.
        stale = _schedule(last_run_at=datetime(2026, 2, 20, 7, 0))
        now = datetime(2026, 3, 2, 9, 0)
        self.assertTrue(schedules.is_due(stale, now))
        self.assertEqual(schedules.previous_slot(stale, now), datetime(2026, 3, 2, 7, 0))


class StoreTests(_StoreTestCase):
    def test_add_then_list_round_trips_every_field(self):
        added = schedules.add_schedule(
            "C01ALLOW", "뉴스 요약해줘", WEEKLY, time(7, 30), (0, 4),
            now=datetime(2026, 3, 2, 9, 0),
        )
        schedules._store_cache.clear()  # force a real re-read from disk

        [loaded] = schedules.list_schedules()
        self.assertEqual(loaded.id, added.id)
        self.assertEqual(loaded.channel_id, "C01ALLOW")
        self.assertEqual(loaded.prompt, "뉴스 요약해줘")
        self.assertEqual(loaded.kind, WEEKLY)
        self.assertEqual(loaded.at, time(7, 30))
        self.assertEqual(loaded.weekdays, (0, 4))
        self.assertEqual(loaded.created_at, datetime(2026, 3, 2, 9, 0))

    def test_list_can_be_scoped_to_one_channel(self):
        schedules.add_schedule("C01", "a", DAILY, time(7, 0))
        schedules.add_schedule("C02", "b", DAILY, time(8, 0))
        self.assertEqual([s.channel_id for s in schedules.list_schedules("C01")], ["C01"])

    def test_remove_reports_whether_it_removed_anything(self):
        added = schedules.add_schedule("C01", "a", DAILY, time(7, 0))
        self.assertTrue(schedules.remove_schedule(added.id))
        self.assertFalse(schedules.remove_schedule(added.id))
        self.assertEqual(schedules.list_schedules(), [])

    def test_mark_ran_closes_the_current_slot(self):
        added = schedules.add_schedule(
            "C01", "a", DAILY, time(7, 0), now=datetime(2026, 3, 1, 6, 0)
        )
        due_at = datetime(2026, 3, 1, 7, 0)
        self.assertTrue(schedules.is_due(schedules.list_schedules()[0], due_at))

        schedules.mark_ran(added.id, now=due_at)

        self.assertFalse(schedules.is_due(schedules.list_schedules()[0], due_at + timedelta(hours=2)))

    def test_mark_ran_on_an_unknown_id_is_a_no_op(self):
        schedules.mark_ran("nope")  # must not raise
        self.assertEqual(schedules.list_schedules(), [])

    def test_due_schedules_returns_only_the_ones_that_are_due(self):
        schedules.add_schedule(
            "C01", "morning", DAILY, time(7, 0), now=datetime(2026, 3, 1, 6, 0)
        )
        schedules.add_schedule(
            "C01", "evening", DAILY, time(22, 0), now=datetime(2026, 3, 1, 6, 0)
        )
        due = schedules.due_schedules(now=datetime(2026, 3, 1, 8, 0))
        self.assertEqual([s.prompt for s in due], ["morning"])


class MalformedStoreTests(_StoreTestCase):
    def test_one_bad_record_does_not_hide_the_good_ones(self):
        # The scheduler loop reads this on every tick: a store that raised
        # would stop every other schedule too.
        good = schedules.add_schedule("C01", "good", DAILY, time(7, 0))
        store = dict(schedules._load())
        store["broken"] = {"kind": "daily"}  # no at/channel_id/prompt
        store["wrong-type"] = "not a dict"
        store["unknown-kind"] = {**store[good.id], "kind": "fortnightly"}
        store["weekly-without-days"] = {**store[good.id], "kind": "weekly", "weekdays": []}
        schedules._save(store)
        schedules._store_cache.clear()

        self.assertEqual([s.prompt for s in schedules.list_schedules()], ["good"])

    def test_a_corrupt_store_file_is_quarantined_rather_than_crashing(self):
        schedules._STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        schedules._STORE_PATH.write_text("{ not json", encoding="utf-8")
        schedules._store_cache.clear()

        self.assertEqual(schedules.list_schedules(), [])
        self.assertTrue(schedules._STORE_PATH.with_name("schedules.json.corrupt").exists())


class DescribeTests(unittest.TestCase):
    def test_daily_reads_back_in_korean(self):
        self.assertEqual(_schedule(at=time(7, 0)).describe(), "매일 07:00")

    def test_weekly_names_its_days(self):
        weekly = _schedule(kind=WEEKLY, weekdays=(4, 0), at=time(21, 30))
        self.assertEqual(weekly.describe(), "매주 월·금 21:30")


if __name__ == "__main__":
    unittest.main()


class DetectorTests(unittest.TestCase):
    def test_it_spots_the_common_korean_recurring_phrasings(self):
        for text in (
            "매일 아침 7시에 뉴스 요약해줘",
            "매주 월요일에 주간보고 써줘",
            "평일 아침마다 일정 정리해줘",
            "주말 저녁마다 백업 돌려줘",
            "날마다 로그 확인해줘",
            "이거 예약해줘",
        ):
            with self.subTest(text=text):
                self.assertTrue(schedules.looks_like_schedule_request(text))

    def test_ordinary_requests_cost_nothing(self):
        # A false negative here loses the feature; a false positive only
        # costs one CLI call, which is why the filter errs loose. These are
        # the shapes that must never pay for extraction.
        for text in (
            "뉴스 요약해줘",
            "이 파일 고쳐줘",
            "어제 만든 스크립트 다시 돌려줘",
            "종료",
        ):
            with self.subTest(text=text):
                self.assertFalse(schedules.looks_like_schedule_request(text))


class CommandParsingTests(unittest.TestCase):
    def test_list_command_variants(self):
        for text in ("예약 목록", "예약목록", "  예약 리스트  "):
            with self.subTest(text=text):
                self.assertTrue(schedules.parse_list_command(text))
        self.assertFalse(schedules.parse_list_command("예약 목록 보여줘"))

    def test_delete_command_returns_the_id(self):
        self.assertEqual(schedules.parse_delete_command("예약 삭제 a1b2c3d4"), "a1b2c3d4")
        self.assertEqual(schedules.parse_delete_command("예약 취소 a1b2c3d4"), "a1b2c3d4")
        self.assertIsNone(schedules.parse_delete_command("예약 삭제"))
        self.assertIsNone(schedules.parse_delete_command("예약 삭제 전부"))


class ExtractionParsingTests(unittest.TestCase):
    def test_a_daily_extraction_round_trips(self):
        spec = schedules.parse_extraction(
            '{"is_schedule": true, "kind": "daily", "at": "07:00", "prompt": "뉴스 요약"}'
        )
        self.assertEqual(spec.kind, DAILY)
        self.assertEqual(spec.at, time(7, 0))
        self.assertEqual(spec.weekdays, ())
        self.assertEqual(spec.prompt, "뉴스 요약")

    def test_a_weekly_extraction_sorts_and_dedupes_its_days(self):
        spec = schedules.parse_extraction(
            '{"is_schedule": true, "kind": "weekly", "at": "09:30",'
            ' "weekdays": [4, 0, 0], "prompt": "주간보고"}'
        )
        self.assertEqual(spec.weekdays, (0, 4))

    def test_json_wrapped_in_prose_or_a_fence_is_still_read(self):
        # The model is told to emit bare JSON and usually does; a stray
        # sentence must not lose the whole registration.
        wrapped = (
            "네, 아래와 같이 해석했습니다.\n```json\n"
            '{"is_schedule": true, "kind": "daily", "at": "22:00", "prompt": "백업"}\n'
            "```\n도움이 되었길 바랍니다."
        )
        spec = schedules.parse_extraction(wrapped)
        self.assertEqual(spec.at, time(22, 0))

    def test_braces_inside_a_string_do_not_end_the_object_early(self):
        spec = schedules.parse_extraction(
            '{"is_schedule": true, "kind": "daily", "at": "07:00",'
            ' "prompt": "리포트에 {총계} 넣어줘"}'
        )
        self.assertEqual(spec.prompt, "리포트에 {총계} 넣어줘")

    def test_every_unusable_reply_falls_back_to_none(self):
        # The caller's fallback is to run the message as an ordinary job,
        # which is what the user would have got anyway -- so a bad parse must
        # never raise and kill the turn.
        for raw in (
            "",
            "그건 일정이 아닙니다",
            '{"is_schedule": false}',
            "{not json}",
            '{"is_schedule": true, "kind": "monthly", "at": "07:00", "prompt": "x"}',
            '{"is_schedule": true, "kind": "daily", "at": "99:99", "prompt": "x"}',
            '{"is_schedule": true, "kind": "daily", "at": "07:00", "prompt": "  "}',
            '{"is_schedule": true, "kind": "daily", "prompt": "x"}',
            '{"is_schedule": true, "kind": "weekly", "at": "07:00", "prompt": "x"}',
            '{"is_schedule": true, "kind": "weekly", "at": "07:00", "weekdays": [],'
            ' "prompt": "x"}',
            '{"is_schedule": true, "kind": "weekly", "at": "07:00", "weekdays": [9],'
            ' "prompt": "x"}',
        ):
            with self.subTest(raw=raw[:40]):
                self.assertIsNone(schedules.parse_extraction(raw))
