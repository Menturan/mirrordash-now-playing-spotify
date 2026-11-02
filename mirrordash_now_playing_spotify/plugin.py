import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse

from fastapi import APIRouter  # on the mirror already: not in pyproject's dependencies
from fastapi.responses import RedirectResponse

logger = logging.getLogger("mirrordash.modules.mirrordash_now_playing_spotify")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
PLAYER_URL = "https://api.spotify.com/v1/me/player"
# Spotify allows http only for a loopback address, so the mirror can't take the answer itself: the owner's
# browser lands on this address (which doesn't load) and the owner pastes it into the settings.
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-currently-playing user-read-playback-state playlist-read-private playlist-read-collaborative"

# Shown, marked as example data, until a Client ID is set: an answer like Spotify's
EXAMPLE = {
    "is_playing": True, "progress_ms": 83000, "currently_playing_type": "track",
    "device": {"name": "Living Room"}, "context": {"type": "collection", "uri": "spotify:user:me:collection"},
    "item": {"name": "Northern Lights", "duration_ms": 225000, "artists": [{"name": "The Quiet Hours"}],
             "album": {"name": "Long Winter", "images": []}},
}


def auth_code(pasted: str) -> str:
    """The code in the address the browser landed on (…/callback?code=…), or the code itself."""
    pasted = pasted.strip()
    if "?" in pasted or "code=" in pasted:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query or pasted).get("code", [""])[0]
    return pasted


def parse(player: dict | None) -> dict | None:
    """What the card shows from GET /me/player, or None when nothing plays (stopped, paused, no device)."""
    if not player or not player.get("is_playing") or not player.get("item"):
        return None
    item = player["item"]
    episode = player.get("currently_playing_type") == "episode"
    album = item.get("show" if episode else "album") or {}
    images = album.get("images") or item.get("images") or []
    context = player.get("context") or {}  # album, playlist, collection (Liked Songs), artist, show, or none
    cover = min(images, key=lambda i: abs((i.get("width") or 0) - 300))["url"] if images else ""  # 640/300/64
    return {
        "title": item.get("name", ""),
        "artist": album.get("name", "") if episode else ", ".join(a.get("name", "") for a in item.get("artists", [])),
        "album": "" if episode else album.get("name", ""),
        "cover": cover,
        "progress_ms": player.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "device": (player.get("device") or {}).get("name", ""),
        "context": context.get("type", ""),
        "playlist_id": context.get("uri", "").removeprefix("spotify:playlist:") if context.get("type") == "playlist" else "",
    }


class NowPlayingSpotifyModule:
    def __init__(self, config):
        self.config = config
        self.name = "mirrordash_now_playing_spotify"
        self.interval = config.get("interval", 10)
        self.client_id = config.get("client_id", "").strip()
        self.code = auth_code(config.get("auth_response", ""))
        self.show_header = config.get("show_header", True)
        self.show = {part: config.get(f"show_{part}", True) for part in ("cover", "title", "artist", "album", "playlist", "device", "progress")}
        self.cover_in_color = config.get("cover_style", "gray") == "color"
        self.api_path = config.get("api_path", "")
        self.state_file = os.path.join(config["data_dir"], "token.json") if config.get("data_dir") else None
        self.state = self.load_state()  # refresh_token, the code it came from, the PKCE verifier of the last login
        self.access_token, self.expires_at = None, 0.0
        self.router = APIRouter()
        self.router.get("/login")(self.login)

    # ── Logging in (Authorization Code with PKCE: no client secret) ──
    async def login(self):
        """GET <api_path>/login: on to Spotify's login page. Open to the home network, but it only redirects."""
        if not self.client_id:
            return RedirectResponse("/admin", status_code=302)
        self.state["verifier"] = secrets.token_urlsafe(64)
        self.save_state()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.state["verifier"].encode()).digest()).rstrip(b"=").decode()
        return RedirectResponse(AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPE,
            "code_challenge_method": "S256", "code_challenge": challenge}), status_code=302)

    async def token(self) -> str | None:
        """A valid access token, or None with self.problem saying what the owner should do."""
        if self.access_token and time.monotonic() < self.expires_at:
            return self.access_token
        new_code = self.code and self.code != self.state.get("code") and self.state.get("verifier")
        if new_code:
            form = {"grant_type": "authorization_code", "code": self.code, "redirect_uri": REDIRECT_URI,
                    "client_id": self.client_id, "code_verifier": self.state["verifier"]}
        elif self.state.get("refresh_token"):
            form = {"grant_type": "refresh_token", "refresh_token": self.state["refresh_token"],
                    "client_id": self.client_id}
        else:
            self.problem = ("log_in", "Log in to Spotify: open {url} and paste the address you land on into the module's settings.")
            return None
        answer, error = await self.fetch_json(TOKEN_URL, method="POST", data=form, max_age=0)
        if error == "offline" or (error or "").startswith("http 5"):
            self.problem = ("offline", "Can't reach Spotify.")
            return None
        if new_code:  # Spotify has seen it: a code works once, so a restart doesn't send it again
            self.state["code"] = self.code
        if error or not answer.get("access_token"):  # 400 invalid_grant: the code was old, or the login revoked
            self.state.pop("refresh_token", None)
            self.save_state()
            self.problem = ("log_in_again", "Spotify wants you to log in again: open {url} and paste the address you land on into the module's settings.")
            return None
        self.access_token = answer["access_token"]
        self.expires_at = time.monotonic() + answer.get("expires_in", 3600) - 60
        self.state["refresh_token"] = answer.get("refresh_token") or self.state.get("refresh_token")  # may rotate
        self.save_state()
        return self.access_token

    # ── What the card shows ──
    async def render(self) -> str:
        if not self.client_id:
            track = parse(EXAMPLE)
            return self.card(track, self.translate("missing_client_id", "Example data. Add your Spotify app's Client ID in the module's settings."), "key-round")
        if not await self.token():
            key, text = self.problem
            url = f"http://mirrordash.local{self.api_path}/login"
            return self.card(None, self.translate(key, text).format(url=url), "log-in" if key != "offline" else "cloud-off")
        # fetch, not fetch_json: "nothing plays" is an empty 204 answer, which fetch_json would call invalid
        reply = await self.fetch(PLAYER_URL, headers={"Authorization": f"Bearer {self.access_token}"},
                                 params={"additional_types": "episode"})
        raw, error = reply
        if error == "rejected":  # the access token ran out early: a new one next time
            self.access_token = None
        try:
            track = parse(json.loads(raw)) if raw else None
        except ValueError:
            track = None
        if track and reply.fetched_at:
            # When the track started, from the data's own time: the same HTML while it plays on, so no redraw
            # ponytail: rounded to 2 s, so the bar can be off by a second; network jitter rarely crosses a step
            track["started"] = round((reply.fetched_at.timestamp() * 1000 - track["progress_ms"]) / 2000) * 2000
        if track and self.show["playlist"]:
            track["playlist"] = await self.playlist_name(track)
        message = None
        if error and error != "rejected":
            message = self.translate("offline" if error == "offline" else "fetch_failed",
                                     "Can't reach Spotify." if error == "offline" else "Couldn't update.")
        if not track and not message:
            return ""  # nothing plays: no card
        return self.card(track, message, "cloud-off")

    async def playlist_name(self, track: dict) -> str:
        """The playlist it plays from: GET /me/player gives only its id. Spotify's own playlists (Discover
        Weekly, …) answer 404 to apps like this one: then no line."""
        if track["context"] == "collection":
            return self.translate("liked_songs", "Liked Songs")
        if not track["playlist_id"]:
            return ""
        answer, _ = await self.fetch_json(f"https://api.spotify.com/v1/playlists/{track['playlist_id']}",
                                              headers={"Authorization": f"Bearer {self.access_token}"},
                                              params={"fields": "name"}, max_age=3600)
        return (answer or {}).get("name", "")

    def card(self, track, message, icon) -> str:
        return self.render_template("widget.html", track=track, message=message, icon=icon, show=self.show,
                                    show_header=self.show_header,
                                    cover_in_color=self.cover_in_color)

    # ── Saving the login ── data_dir is kept and backed up
    def load_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (TypeError, OSError, ValueError):
            return {}  # no data_dir (a test), not logged in yet, or a broken file

    def save_state(self) -> None:
        if self.state_file:
            with open(self.state_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(self.state_file + ".tmp", self.state_file)
