import json
from datetime import datetime
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
async def test_not_logged_in_shows_the_login_address(tmp_path):
    module = make_module(tmp_path)
    shown = await module.render()
    assert shown["track"] is None
    assert "http://mirrordash.local/api/modules/spotify/login" in shown["message"]
    module.fetch_json.assert_not_called()


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
