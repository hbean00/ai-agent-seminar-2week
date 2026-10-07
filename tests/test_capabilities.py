import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import capabilities


class _CapabilitiesTestCase(unittest.TestCase):
    """Points the registry at a temp directory so no test reads the real one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(capabilities, "DEFAULT_CAPABILITIES_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(capabilities.CAPABILITIES_DIR_ENV_VAR, None)
        capabilities._cache.clear()
        self.addCleanup(capabilities._cache.clear)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path


class RegistryTests(_CapabilitiesTestCase):
    def test_an_empty_registry_has_no_capabilities(self):
        self.assertEqual(capabilities.capabilities(), {})

    def test_a_missing_directory_is_not_an_error(self):
        # A fresh checkout has no capabilities/ folder.
        with mock.patch.object(capabilities, "DEFAULT_CAPABILITIES_DIR", self.dir / "nope"):
            capabilities._cache.clear()
            self.assertEqual(capabilities.capabilities(), {})

    def test_the_filename_becomes_the_tag(self):
        self.write("lms.md", "LMS 사용법")
        self.assertEqual(capabilities.find("lms").tag, "@lms")

    def test_non_markdown_files_are_ignored(self):
        self.write("notes.txt", "기능 아님")
        self.assertIsNone(capabilities.find("notes"))

    def test_an_empty_file_registers_nothing(self):
        self.write("blank.md", "   \n\n")
        self.assertIsNone(capabilities.find("blank"))

    def test_one_unreadable_file_does_not_hide_the_others(self):
        # A capability folder is edited by hand; one bad file must not take
        # every other capability down with it.
        self.write("good.md", "쓸 수 있는 기능")
        self.write("bad.md", "x").write_bytes(b"\xff\xfe\x00 invalid utf-8 \xff")

        with self.assertLogs("src.capabilities", level="WARNING"):
            found = capabilities.capabilities()

        self.assertIn("good", found)


class ResolveTests(_CapabilitiesTestCase):
    def test_a_tagged_message_yields_the_directive_and_the_rest(self):
        self.write("lms.md", "공지 조회: python canvas_api.py")

        directive, rest = capabilities.resolve("@lms 최근 공지 있어?")

        self.assertIn("python canvas_api.py", directive)
        self.assertEqual(rest, "최근 공지 있어?")

    def test_the_directive_tells_the_model_to_run_it(self):
        # The whole reason this moved out of the system prompt: the model read
        # the capability as background and answered "그런 기능이 없습니다"
        # without calling Bash once. The directive has to forbid that.
        self.write("lms.md", "공지 조회: python canvas_api.py")
        directive, _ = capabilities.resolve("@lms 공지?")
        self.assertIn("Bash", directive)
        self.assertIn("실행", directive)

    def test_an_untagged_message_is_returned_untouched(self):
        self.write("lms.md", "LMS")
        self.assertEqual(capabilities.resolve("최근 공지 있어?"), (None, "최근 공지 있어?"))

    def test_an_unknown_tag_is_left_alone_rather_than_swallowed(self):
        # A typo must not silently eat part of the message.
        self.write("lms.md", "LMS")
        self.assertEqual(capabilities.resolve("@lmss 공지?"), (None, "@lmss 공지?"))

    def test_a_tag_with_no_message_still_resolves(self):
        self.write("lms.md", "LMS 사용법")
        directive, rest = capabilities.resolve("@lms")
        self.assertIsNotNone(directive)
        self.assertEqual(rest, "")

    def test_a_tag_in_the_middle_does_not_fire(self):
        # Only a leading tag counts, same as the project tags in parser.py.
        self.write("lms.md", "LMS")
        self.assertEqual(capabilities.resolve("이거 @lms 관련이야"), (None, "이거 @lms 관련이야"))

    def test_an_oversized_capability_is_truncated_and_warned_about(self):
        self.write("huge.md", "가" * (capabilities.MAX_BLOCK_CHARS + 500))
        with self.assertLogs("src.capabilities", level="WARNING"):
            directive, _ = capabilities.resolve("@huge 해줘")
        self.assertLessEqual(len(directive), capabilities.MAX_BLOCK_CHARS + len("\n(이하 생략됨)"))


class CacheTests(_CapabilitiesTestCase):
    def test_an_edited_file_is_picked_up_without_a_restart(self):
        # The bot runs for days under Task Scheduler; requiring a restart to
        # register a capability would defeat the point.
        path = self.write("lms.md", "처음 내용")
        self.assertIn("처음 내용", capabilities.find("lms").text)

        path.write_text("바뀐 내용", encoding="utf-8")
        os.utime(path, (1, 1))  # force a different mtime

        self.assertIn("바뀐 내용", capabilities.find("lms").text)

    def test_a_new_file_is_picked_up_without_a_restart(self):
        self.assertIsNone(capabilities.find("lms"))
        self.write("lms.md", "나중에 추가된 기능")
        self.assertIsNotNone(capabilities.find("lms"))

    def test_an_unchanged_directory_is_not_reread(self):
        self.write("lms.md", "기능")
        capabilities.capabilities()

        with mock.patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
            capabilities.capabilities()


class EnvOverrideTests(_CapabilitiesTestCase):
    def test_the_directory_can_be_pointed_elsewhere(self):
        other = Path(self._tmp.name) / "elsewhere"
        other.mkdir()
        (other / "x.md").write_text("다른 폴더의 기능", encoding="utf-8")

        with mock.patch.dict(os.environ, {capabilities.CAPABILITIES_DIR_ENV_VAR: str(other)}):
            capabilities._cache.clear()
            self.assertIsNotNone(capabilities.find("x"))


if __name__ == "__main__":
    unittest.main()
