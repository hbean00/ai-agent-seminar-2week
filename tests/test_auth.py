"""Tests for src/auth.py: lazy config (#9) and multi-owner support (#28)."""

import sys

import pytest

from src import auth
from src.chat import IncomingMessage


@pytest.fixture(autouse=True)
def _clear_config_cache():
    # Each test sets its own env vars, so the cached config from a previous
    # test (or from module import) must not leak in.
    auth._config.cache_clear()
    yield
    auth._config.cache_clear()


def _make_message(author_id: str, *, is_bot: bool = False, channel: str | None = None):
    """A message event shaped the way Slack delivers one.

    ``channel=None`` means a DM, which is what the Discord version expressed
    by handing over a DMChannel instance.
    """
    event = {
        "type": "message",
        "text": "안녕",
        "user": author_id,
        "channel": channel or "D0DIRECT",
        "channel_type": "channel" if channel else "im",
        "ts": "1700000000.000001",
    }
    if is_bot:
        event["bot_id"] = "B0BOT"
    return IncomingMessage(client=None, event=event)


def test_import_succeeds_without_env_vars(monkeypatch):
    # #9: importing src.auth must not require OWNER_SLACK_ID / ALLOWED_CHANNEL_IDS.
    monkeypatch.delenv("OWNER_SLACK_ID", raising=False)
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    for mod in ("src.auth",):
        sys.modules.pop(mod, None)
    import src.auth as reimported  # noqa: F401 -- just verifying it doesn't raise


def test_ensure_configured_raises_without_owner_env(monkeypatch):
    monkeypatch.delenv("OWNER_SLACK_ID", raising=False)
    with pytest.raises(RuntimeError, match="OWNER_SLACK_ID"):
        auth.ensure_configured()


def test_ensure_configured_raises_on_blank_owner_env(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "   ")
    with pytest.raises(RuntimeError, match="OWNER_SLACK_ID"):
        auth.ensure_configured()


def test_ensure_configured_raises_on_invalid_owner_id(monkeypatch):
    # The mistake worth catching early is pasting a display name or @handle
    # instead of the member ID: the bot would start and then ignore everything.
    monkeypatch.setenv("OWNER_SLACK_ID", "@username")
    with pytest.raises(RuntimeError, match="Slack 사용자 ID"):
        auth.ensure_configured()


def test_enterprise_grid_owner_ids_are_accepted(monkeypatch):
    # Enterprise Grid members are W-prefixed rather than U-prefixed.
    # ALLOWED_CHANNEL_IDS is cleared because importing src.main elsewhere in
    # the suite runs load_dotenv over the developer's own .env, and a test
    # about owner ids must not depend on what that file happens to hold.
    monkeypatch.setenv("OWNER_SLACK_ID", "W01ABCDEF")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    auth.ensure_configured()  # should not raise


def test_ensure_configured_ok_with_valid_env(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    auth.ensure_configured()  # should not raise


def test_single_owner_id_backward_compatible(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)

    dm_msg = _make_message("U01ABCDEF")
    assert auth.is_authorized(dm_msg) is True

    other_msg = _make_message("U02OTHER")
    assert auth.is_authorized(other_msg) is False


def test_multiple_owner_ids_comma_separated(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01AAA, U02BBB,U03CCC")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)

    assert auth.is_authorized(_make_message("U01AAA")) is True
    assert auth.is_authorized(_make_message("U02BBB")) is True
    assert auth.is_authorized(_make_message("U03CCC")) is True
    assert auth.is_authorized(_make_message("U04DDD")) is False


def test_rejects_unauthorized_user(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    assert auth.is_authorized(_make_message("U09NOBODY")) is False


def test_rejects_bots_even_if_owner_id_matches(monkeypatch):
    # Without this the bot answers its own posts, which in a thread is a loop.
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    bot_msg = _make_message("U01ABCDEF", is_bot=True)
    assert auth.is_authorized(bot_msg) is False


def test_allows_dm_regardless_of_channel_whitelist(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "C05ALLOWED")
    dm_msg = _make_message("U01ABCDEF")  # channel_type im
    assert auth.is_authorized(dm_msg) is True


def test_channel_whitelist_enforced_for_non_dm(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "C05ALLOWED,C06ALSOOK")

    allowed_msg = _make_message("U01ABCDEF", channel="C05ALLOWED")
    assert auth.is_authorized(allowed_msg) is True

    other_msg = _make_message("U01ABCDEF", channel="C07DENIED")
    assert auth.is_authorized(other_msg) is False


def test_private_channel_ids_are_accepted(monkeypatch):
    # A private channel is G-prefixed; rejecting it would lock the bot out of
    # exactly the channel someone is most likely to run it in.
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "G05PRIVATE")
    assert auth.is_authorized(_make_message("U01ABCDEF", channel="G05PRIVATE")) is True


def test_invalid_channel_id_raises(monkeypatch):
    monkeypatch.setenv("OWNER_SLACK_ID", "U01ABCDEF")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "#general")
    with pytest.raises(RuntimeError, match="ALLOWED_CHANNEL_IDS"):
        auth.ensure_configured()
