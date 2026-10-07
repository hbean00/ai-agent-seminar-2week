"""기능 레지스트리: `capabilities/` 폴더에 파일 하나 = `@기능명` 태그 하나.

`capabilities/lms.md`를 두면 `@lms 최근 공지 있어?`로 부를 수 있다. 태그가
붙은 턴에만 그 파일 내용이 **사용자 메시지 앞의 지시문으로** 들어가고, 모델은
거기 적힌 명령을 Bash로 실행한다.

## 왜 시스템 프롬프트가 아니라 사용자 턴인가

처음에는 모든 기능 설명을 매 턴 `--append-system-prompt`에 실었다. 전달 자체는
확실히 됐다 -- 같은 경로로 "모든 답변을 ZZZ로 시작하라"를 넣으면 그대로 따랐다.
그런데 **모델이 기능 블록은 배경 정보로 읽고 쓰지 않았다**: "최근 LMS 공지
있어?"에 Bash를 한 번도 부르지 않고 "그런 기능이 없습니다"라고 답했다. 같은
블록을 그대로 두고 사용자 턴에서 "거기 적힌 명령을 실행해라"라고만 하면 즉시
실행해 실제 공지를 가져왔다. 차이는 전달 여부가 아니라 **지시가 어느 턴에
있느냐**였다.

## 왜 키워드 자동 감지가 아니라 `@`인가

"LMS"가 들어간 메시지를 자동으로 잡는 방법도 있었지만, 오탐("LMS 과제 코드를
고쳐줘"가 조회를 실행)과 미탐이 둘 다 생긴다. 이 저장소는 이미 `@sess-`와
`@프로젝트` 태그를 쓰므로(`parser.py`), 같은 자리에 `@기능`을 얹는 쪽이
일관되고 사용자가 언제 실행되는지 정확히 안다. 태그를 안 쓴 턴은 토큰이 한
글자도 늘지 않는다.

## 한계

도구 스키마가 아니라 설명문이므로 모델이 인자를 틀릴 수 있다. 기능이 늘어 이
방식이 흔들리면 그때가 MCP(`--mcp-config`)를 재볼 시점이다 -- 다만 그 전에
`--tools` 허용목록이 MCP 도구를 걸러내는지부터 재야 한다(runner.py 상단 NOTE).
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

CAPABILITIES_DIR_ENV_VAR = "CAPABILITIES_DIR"
DEFAULT_CAPABILITIES_DIR = Path(__file__).resolve().parents[1] / "capabilities"

# 주입 상한. 태그를 쓴 턴에만 붙지만, 설명문 하나가 프롬프트를 삼키면 정작
# 사용자의 요청이 묻힌다.
MAX_BLOCK_CHARS = 4000

_HEADER = (
    "아래 기능을 이 PC에서 **실제로 실행할 수 있다**. 사용자가 이 기능을 지목했으므로,\n"
    "반드시 해당 명령을 Bash로 먼저 실행하고 그 결과로 답하라.\n"
    '실행해 보지도 않고 "그런 기능이 없다"거나 "직접 확인해 달라"고 답하지 마라.'
)

# parser.py의 projects.toml 캐시와 같은 이유로 stat 기반이다: 이 조회는 메시지
# 마다 일어나므로 매번 폴더를 읽으면 핫 패스에 디스크 I/O가 돌아오고, 그렇다고
# 영구 캐시로 두면 기능 파일을 고친 뒤 봇을 재시작해야 한다.
_Signature = tuple[tuple[str, int, int], ...]
_cache: dict[Path, tuple[_Signature, dict[str, "Capability"]]] = {}


@dataclass(frozen=True)
class Capability:
    name: str
    text: str

    @property
    def tag(self) -> str:
        return "@" + self.name

    def directive(self) -> str:
        """모델에게 보낼 지시 블록."""
        block = _HEADER + "\n\n" + self.text
        if len(block) > MAX_BLOCK_CHARS:
            logger.warning(
                "기능 '%s' 설명이 %s자로 상한(%s자)을 넘어 잘렸습니다.",
                self.name,
                len(block),
                MAX_BLOCK_CHARS,
            )
            block = block[:MAX_BLOCK_CHARS] + "\n(이하 생략됨)"
        return block


def capabilities_dir() -> Path:
    configured = os.environ.get(CAPABILITIES_DIR_ENV_VAR)
    if configured and configured.strip():
        return Path(configured).expanduser()
    return DEFAULT_CAPABILITIES_DIR


def _signature(directory: Path) -> _Signature:
    """(이름, mtime, 크기) 목록. 폴더가 없으면 빈 튜플."""
    try:
        entries = sorted(directory.glob("*.md"))
    except OSError:
        return ()
    signature = []
    for path in entries:
        try:
            st = path.stat()
        except OSError:
            continue
        signature.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(signature)


def _read(directory: Path) -> dict[str, "Capability"]:
    found: dict[str, Capability] = {}
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            # 기능 파일 하나가 깨졌다고 나머지 기능이 사라지면 안 된다.
            logger.warning("기능 파일을 읽지 못했습니다: %s", path.name, exc_info=True)
            continue
        if text:
            found[path.stem] = Capability(name=path.stem, text=text)
    return found


def capabilities() -> dict[str, Capability]:
    """등록된 기능 전체 (이름 → Capability). 폴더가 없거나 비면 빈 dict."""
    directory = capabilities_dir()
    signature = _signature(directory)

    cached = _cache.get(directory)
    if cached is not None and cached[0] == signature:
        return cached[1]

    found = _read(directory) if signature else {}
    _cache[directory] = (signature, found)
    return found


def find(name: str) -> Capability | None:
    return capabilities().get(name)


def resolve(text: str) -> tuple[str | None, str]:
    """맨 앞의 `@기능명` 태그를 떼어낸다.

    ``(지시 블록, 남은 메시지)``를 돌려준다. 태그가 없거나 등록되지 않은
    이름이면 ``(None, 원문)``이다 -- 모르는 태그를 삼키면 사용자가 오타를
    냈을 때 메시지 일부가 조용히 사라진다.
    """
    stripped = text.strip()
    if not stripped.startswith("@"):
        return None, text

    head, _, rest = stripped.partition(" ")
    capability = find(head[1:])
    if capability is None:
        return None, text
    return capability.directive(), rest.strip()
