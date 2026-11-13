import json
from datetime import datetime
from email.message import Message
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mirrordash_now_playing_spotify.plugin import (
    NowPlayingSpotifyModule,
    auth_code,
    parse,
)


class Answer(tuple):
    """What the mirror's fetch/fetch_json give: (data, error), and .fetched_at, when the data was downloaded."""
    fetched_at = datetime(2026, 1, 1, 9, 30)
    headers = Message()  # what the mirror gives: no headers, read case-insensitively


PLAYING = {
    "is_playing": True, "progress_ms": 60000, "currently_playing_type": "track", "device": {"name": "Kitchen"},
    "item": {"name": "Song", "duration_ms": 180000, "artists": [{"name": "A"}, {"name": "B"}],
             "album": {"name": "Album", "images": [{"url": "big", "width": 640}, {"url": "mid", "width": 300},
                                                   {"url": "small", "width": 64}]}},
}
TOKEN = {"access_token": "access", "expires_in": 3600, "refresh_token": "refresh"}


def make_module(tmp_path, state=None, player=PLAYING, **settings):
    """The module as the mirror sets it up, with the mirror's helpers replaced by fakes."""
    if state is not None:
        (tmp_path / "token.json").write_text(json.dumps(state))
    module = NowPlayingSpotifyModule({"client_id": "id", "data_dir": str(tmp_path),
                                      "api_path": "/api/modules/spotify", **settings})
    module.render_template = MagicMock(side_effect=lambda name, **context: context)  # returns what the template gets
    module.translate = lambda key, default=None: default
    module.fetch_json = AsyncMock(return_value=Answer((TOKEN, None)))
    module.fetch = AsyncMock(return_value=Answer((json.dumps(player).encode() if player else b"", None)))
    return module


def saved(tmp_path):
    return json.loads((tmp_path / "token.json").read_text())


def test_the_code_comes_from_the_pasted_address_or_alone():
    assert auth_code(" http://127.0.0.1:8888/callback?code=abc123&state=x ") == "abc123"
    assert auth_code("abc123") == "abc123"
    assert auth_code("http://127.0.0.1:8888/callback?error=access_denied") == ""
    assert auth_code("") == ""


def test_parse_a_track_an_episode_and_nothing():
    track = parse(PLAYING)
    assert track["artist"] == "A, B" and track["album"] == "Album" and track["cover"] == "mid"
    assert track["device"] == "Kitchen" and track["progress_ms"] == 60000
    episode = parse({"is_playing": True, "currently_playing_type": "episode",
                     "item": {"name": "Ep 1", "images": [{"url": "ep", "width": 300}], "show": {"name": "The Show"}}})
    assert episode["artist"] == "The Show" and episode["album"] == "" and episode["cover"] == "ep"
    assert parse(None) is None
    assert parse({**PLAYING, "is_playing": False}) is None  # paused: the card hides
    assert parse({"is_playing": True, "item": None}) is None  # an ad, or a private session


@pytest.mark.asyncio
async def test_without_a_client_id_it_shows_example_data(tmp_path):
    module = make_module(tmp_path, client_id="")
    shown = await module.render()
    assert shown["track"]["title"] and "Client ID" in shown["message"]
    module.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_not_logged_in_shows_the_login_address_and_its_qr_code(tmp_path, monkeypatch):
    from mirrordash_now_playing_spotify import plugin
    monkeypatch.setattr(plugin, "mirror_address", lambda: "192.168.1.50")
    module = make_module(tmp_path)
    shown = await module.render()
    assert shown["track"] is None
    assert "http://192.168.1.50/api/modules/spotify/login" in shown["message"]
    assert shown["qr"].startswith("<svg") and 'fill="#fff"' in shown["qr"]  # dark on light, for any camera
    module.fetch_json.assert_not_called()


def test_the_mirrors_address_is_an_ip_or_its_name():
    from mirrordash_now_playing_spotify.plugin import mirror_address
    address = mirror_address()
    assert address == "mirrordash.local" or address.count(".") == 3


@pytest.mark.asyncio
async def test_the_admin_preview_draws_the_sample_track_not_the_login(tmp_path):
    module = make_module(tmp_path, preview=True)  # no login saved, as in the preview's empty data_dir
    shown = await module.render()
    assert shown["track"]["title"] == "Song" and shown["message"] is None and shown["qr"] is None
    module.fetch_json.assert_not_called()  # no token asked for


@pytest.mark.asyncio
async def test_login_redirects_to_spotify_with_a_pkce_challenge(tmp_path):
    module = make_module(tmp_path)
    reply = await module.login()
    assert reply.status_code == 302
    location = reply.headers["location"]
    assert location.startswith("https://accounts.spotify.com/authorize?") and "code_challenge=" in location
    assert "client_id=id" in location and saved(tmp_path)["verifier"] not in location  # only its hash leaves


@pytest.mark.asyncio
async def test_a_pasted_code_is_exchanged_once_and_the_refresh_token_kept(tmp_path):
    module = make_module(tmp_path, state={"verifier": "v"}, auth_response="http://127.0.0.1:8888/callback?code=c1")
    shown = await module.render()
    form = module.fetch_json.call_args.kwargs["data"]
    assert form["grant_type"] == "authorization_code" and form["code"] == "c1" and form["code_verifier"] == "v"
    assert saved(tmp_path)["refresh_token"] == "refresh" and saved(tmp_path)["code"] == "c1"
    assert module.fetch.call_args.kwargs["headers"] == {"Authorization": "Bearer access"}
    assert shown["track"]["title"] == "Song" and shown["message"] is None
    # A restart with the same code in the settings refreshes instead of sending the used code again
    module = make_module(tmp_path, auth_response="c1")
    await module.render()
    assert module.fetch_json.call_args.kwargs["data"]["grant_type"] == "refresh_token"


@pytest.mark.asyncio
async def test_the_access_token_is_reused_until_it_runs_out(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"})
    await module.render()
    await module.render()
    assert module.fetch_json.call_count == 1
    module.expires_at = 0
    await module.render()
    assert module.fetch_json.call_count == 2


@pytest.mark.asyncio
async def test_a_refused_refresh_asks_to_log_in_again(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"})
    module.fetch_json.return_value = Answer((TOKEN, "http 400"))  # invalid_grant; the cached answer is ignored
    shown = await module.render()
    assert "log in again" in shown["message"] and "refresh_token" not in saved(tmp_path)
    module.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_offline_while_refreshing_keeps_the_login(tmp_path):
    module = make_module(tmp_path, state={"verifier": "v"}, auth_response="c1")
    module.fetch_json.return_value = Answer((None, "offline"))
    shown = await module.render()
    assert "Can't reach Spotify" in shown["message"]
    assert "code" not in saved(tmp_path)  # the code is tried again when the network is back


@pytest.mark.asyncio
async def test_nothing_playing_hides_the_card(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"}, player=None)  # 204: an empty answer
    assert await module.render() == ""
    module.fetch.return_value = Answer((json.dumps({**PLAYING, "is_playing": False}).encode(), None))
    assert await module.render() == ""


@pytest.mark.asyncio
async def test_offline_shows_the_last_track_and_says_so(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"})
    module.fetch.return_value = Answer((json.dumps(PLAYING).encode(), "offline"))
    shown = await module.render()
    assert shown["track"]["title"] == "Song" and "Can't reach Spotify" in shown["message"]


@pytest.mark.asyncio
async def test_an_expired_access_token_is_dropped(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"})
    module.fetch.return_value = Answer((None, "rejected"))
    assert await module.render() == ""
    assert module.access_token is None


@pytest.mark.asyncio
async def test_the_start_time_stays_the_same_while_it_plays_on(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"})
    first = (await module.render())["track"]["started"]
    Answer.fetched_at = datetime(2026, 1, 1, 9, 30, 10)
    module.fetch.return_value = Answer((json.dumps({**PLAYING, "progress_ms": 70000}).encode(), None))
    try:
        assert (await module.render())["track"]["started"] == first  # same HTML: no redraw
    finally:
        Answer.fetched_at = datetime(2026, 1, 1, 9, 30)


@pytest.mark.asyncio
async def test_parts_can_be_turned_off(tmp_path):
    module = make_module(tmp_path, client_id="", show_cover=False, show_progress=False, show_album=False,
                         show_device=False, show_header=False, show_title=False, show_playlist=False,
                         show_artist=False, cover_style="color")
    shown = await module.render()
    assert not any(shown["show"].values()) and not shown["show_header"] and shown["cover_in_color"] is True



@pytest.mark.asyncio
async def test_the_playlist_it_plays_from_is_named(tmp_path):
    from_playlist = {**PLAYING, "context": {"type": "playlist", "uri": "spotify:playlist:abc"}}
    module = make_module(tmp_path, state={"refresh_token": "r"}, player=from_playlist)
    module.fetch_json.side_effect = [Answer((TOKEN, None)), Answer(({"name": "Winter"}, None))]
    shown = await module.render()
    assert shown["track"]["playlist"] == "Winter"
    assert module.fetch_json.call_args.args[0] == "https://api.spotify.com/v1/playlists/abc"


@pytest.mark.asyncio
async def test_liked_songs_an_album_and_spotifys_own_playlists(tmp_path):
    liked = {**PLAYING, "context": {"type": "collection", "uri": "spotify:user:x:collection"}}
    module = make_module(tmp_path, state={"refresh_token": "r"}, player=liked)
    assert (await module.render())["track"]["playlist"] == "Liked Songs"
    module.fetch.return_value = Answer((json.dumps({**PLAYING, "context": {"type": "album"}}).encode(), None))
    assert (await module.render())["track"]["playlist"] == ""
    module.fetch.return_value = Answer((json.dumps({**PLAYING, "context": {"type": "playlist", "uri": "spotify:playlist:37i9"}}).encode(), None))
    module.fetch_json.return_value = Answer((None, "http 404"))  # Discover Weekly and friends: not for apps
    assert (await module.render())["track"]["playlist"] == ""
    calls = module.fetch_json.call_count
    await module.render()
    assert module.fetch_json.call_count == calls  # asked once, not every round


@pytest.mark.asyncio
async def test_a_playlist_name_is_asked_again_after_a_network_error(tmp_path):
    from_playlist = {**PLAYING, "context": {"type": "playlist", "uri": "spotify:playlist:abc"}}
    module = make_module(tmp_path, state={"refresh_token": "r"}, player=from_playlist)
    module.fetch_json.side_effect = [Answer((TOKEN, None)), Answer((None, "offline")), Answer(({"name": "Winter"}, None))]
    assert (await module.render())["track"]["playlist"] == ""
    assert (await module.render())["track"]["playlist"] == "Winter"
    assert (await module.render())["track"]["playlist"] == "Winter" and module.fetch_json.call_count == 3


@pytest.mark.asyncio
async def test_a_429_stops_the_calls_for_as_long_as_retry_after_says(tmp_path):
    module = make_module(tmp_path, state={"refresh_token": "r"})
    shown = await module.render()
    busy = Answer((json.dumps(PLAYING).encode(), "http 429"))
    busy.headers = {"Retry-After": "120"}
    module.fetch.return_value = busy
    shown = await module.render()
    assert shown["track"]["title"] == "Song" and shown["message"]  # the last answer, and that it couldn't update
    calls = module.fetch.call_count
    assert await module.render() == shown and module.fetch.call_count == calls  # waiting: no call, same card
    module.busy_until = 0
    await module.render()
    assert module.fetch.call_count == calls + 1


def test_a_retry_after_is_capped_at_an_hour(tmp_path):
    import time
    module = make_module(tmp_path)
    busy = Answer((None, "http 429"))
    busy.headers = {"Retry-After": "999999999"}
    module.wait_if_busy(busy)
    assert module.busy_until - time.monotonic() <= 3600


@pytest.mark.asyncio
async def test_a_playlist_id_that_isnt_one_never_reaches_a_url(tmp_path):
    odd = {**PLAYING, "context": {"type": "playlist", "uri": "spotify:playlist:../../me?x=1"}}
    module = make_module(tmp_path, state={"refresh_token": "r"}, player=odd)
    assert (await module.render())["track"]["playlist"] == ""
    assert module.fetch_json.call_count == 1  # the token only, no playlist call


def test_a_retry_after_that_is_a_date_waits_30_seconds(tmp_path):
    import time
    module = make_module(tmp_path)
    busy = Answer((None, "http 429"))
    busy.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    module.wait_if_busy(busy)
    assert 25 < module.busy_until - time.monotonic() <= 30


# ── Messages ──

def playing(title, artist="A", is_playing=True):
    return {**PLAYING, "is_playing": is_playing, "item": {**PLAYING["item"], "name": title, "artists": [{"name": artist}]}}


async def play(module, player):
    module.fetch.return_value = Answer((json.dumps(player).encode() if player else b"", None))
    await module.render()


@pytest.mark.asyncio
async def test_a_new_track_sends_a_heads_up_but_not_the_one_playing_at_the_start_or_the_same_one_again(tmp_path):
    bus = MagicMock()
    module = make_module(tmp_path, state={"refresh_token": "r"}, heads_up=True, event_bus=bus)
    await module.render()  # "Song" is already playing at the start
    await module.render()
    bus.publish.assert_not_called()
    await play(module, playing("Next", "Band"))
    bus.publish.assert_called_once_with("mirrordash_now_playing_spotify.heads_up", {"text": "Next – Band"}, public=True)
    await play(module, playing("Next", "Band"))  # still playing
    await play(module, playing("Next", "Band", is_playing=False))  # paused...
    await play(module, None)  # ...or nothing plays
    await play(module, playing("Next", "Band"))  # ...and resumed
    assert bus.publish.call_count == 1
    await play(module, playing("Third", "Band"))
    assert bus.publish.call_count == 2


@pytest.mark.asyncio
async def test_a_track_after_nothing_played_at_the_start_is_new_and_it_is_off_by_default(tmp_path):
    bus = MagicMock()
    module = make_module(tmp_path, state={"refresh_token": "r"}, player=None, heads_up=True, event_bus=bus)
    await module.render()  # nothing plays
    await play(module, PLAYING)
    bus.publish.assert_called_once()
    off = make_module(tmp_path, state={"refresh_token": "r"}, player=None, event_bus=bus)  # setting not given: off
    await off.render()
    await play(off, PLAYING)
    assert bus.publish.call_count == 1
    await play(make_module(tmp_path, heads_up=True), PLAYING)  # no event bus (a test): no error
    schema = json.loads((Path(__file__).parent.parent / "mirrordash_now_playing_spotify" / "config_schema.json").read_text())
    assert schema["properties"]["heads_up"]["default"] is False
