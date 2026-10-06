import os
from functools import lru_cache

from src.chat import IncomingMessage

# Slack IDs are opaque strings, not the integers Discord used. The prefixes are
# checked because the mistake this catches is pasting a display name or an
# @handle instead of the member ID -- without a check the bot starts fine and
# then silently ignores every message, which is exactly the failure #9 made
# this module fail fast about.
#
# U is a workspace member, W a member on Enterprise Grid. Channels are C for a
# public channel, G for a private one, D for a DM conversation.
OWNER_ID_PREFIXES = ("U", "W")
CHANNEL_ID_PREFIXES = ("C", "G", "D")


def _looks_like_id(value: str, prefixes: tuple[str, ...]) -> bool:
    return value[:1] in prefixes and value[1:].isalnum()


def _owner_ids(name: str) -> frozenset[str]:
    # #28: same comma-separated shape as ALLOWED_CHANNEL_IDS, but a single
    # bare value (the previous format) still works unchanged.
    raw = os.environ.get(name)
    if raw is None:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    owners = set()
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        if not _looks_like_id(value, OWNER_ID_PREFIXES):
            raise RuntimeError(
                f"환경변수 {name}은 쉼표로 구분한 Slack 사용자 ID여야 합니다 (U 또는 W로 시작). 받은 값: {value!r}"
            )
        owners.add(value)
    if not owners:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    return frozenset(owners)


def _allowed_channels() -> frozenset[str]:
    raw = os.environ.get("ALLOWED_CHANNEL_IDS", "")
    channels = set()
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        if not _looks_like_id(value, CHANNEL_ID_PREFIXES):
            raise RuntimeError(
                f"ALLOWED_CHANNEL_IDS는 쉼표로 구분한 Slack 채널 ID여야 합니다 (C, G 또는 D로 시작). 받은 값: {value!r}"
            )
        channels.add(value)
    return frozenset(channels)


# #9: reading env vars at import time made every test that merely imports
# src.main (and therefore src.auth) require chat config. Deferring the
# read behind a cached function lets unrelated tests import this module
# freely, while ensure_configured() still gives the bot an explicit,
# fail-fast check at startup. Tests that need to vary the env vars should
# call _config.cache_clear() between runs.
@lru_cache(maxsize=1)
def _config() -> tuple[frozenset[str], frozenset[str]]:
    return _owner_ids("OWNER_SLACK_ID"), _allowed_channels()


def ensure_configured() -> None:
    """Validate auth-related env vars now, so bad config fails at startup, not on the first message."""
    _config()


def is_authorized(msg: IncomingMessage) -> bool:
    owner_ids, allowed_channels = _config()
    if msg.is_bot or msg.author_id not in owner_ids:
        return False
    if msg.is_dm:
        return True
    return msg.channel.id in allowed_channels
