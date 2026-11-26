"""The stream desk: YouTube's side of going live, kept by the server (#81).

OBS still sends the stream and the deck's LIVE key still starts and stops it
(`deck/live.py`). What is new is who creates the video. YouTube used to make
one itself when OBS connected, copying the title and visibility of the last
stream. Now this desk creates it just before OBS starts, so the title, the
visibility, the thumbnail and the watch link are right from the first second:

1. the deck's hold posts `/api/stream/prepare`, which returns at once;
2. the desk creates the broadcast, attaches it to the stream whose key OBS
   holds, sets its description and category and uploads its thumbnail, in a
   worker thread, and then reads `ready`;
3. the deck, polling `/api/state`, sees `ready` and sends OBS `StartStream`;
4. the deck reports what OBS says (`on_air`, `off_air`), because OBS is the
   only thing that knows. Nothing here polls YouTube.

**If YouTube refuses or does not answer, the desk reads `failed` and the deck
never starts OBS.** OBS alone would make YouTube create a video with the last
stream's settings, which might be Public.

The desk also holds what the control page's Stream section edits: the title
(the match's, until one is typed) and the visibility. Both live in memory
only, so every `run` starts Unlisted on the match's own title, and both go
back there when a stream ends. On air, an edit goes to the live video.

It is deliberately not part of the `Hub`: nothing here is a hub input, so a
session replay never calls YouTube. It journals what the archive pass needs
to write chapters afterwards (`youtube/chapters.py`): the video's id, when it
went on air, and a `stream_mark` each time the match changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from PIL import Image

from cueclock.youtube import thumbnail
from cueclock.youtube.api import Account, DeviceCode, SignedOutError, YouTube, YouTubeError
from cueclock.youtube.title import Naming, clean_title, naming, race

if TYPE_CHECKING:
    from cueclock.server.hub import Hub

_T = TypeVar("_T")

OBS_CONFIG = Path.home() / "Library" / "Application Support" / "obs-studio"
VISIBILITIES = ("unlisted", "public")
# A prepared video OBS never went on air for is removed after this long, in
# case the deck that asked for it never says what happened. The deck gives a
# start 20 s (`deck/live.py`), so by now it has failed or been forgotten.
READY_TTL_S = 60.0


def obs_stream_key(root: Path = OBS_CONFIG) -> str | None:
    """The stream key in OBS's current profile, or None if there is none.

    Read from the profile on disk (`service.json`, where OBS keeps it), so the
    desk needs no running OBS and the key never crosses the HTTP API.
    """
    profile: str | None = None
    for ini in ("user.ini", "global.ini"):  # OBS 31 moved it to user.ini
        with contextlib.suppress(OSError):
            for line in (root / ini).read_text().splitlines():
                if line.startswith("ProfileDir="):
                    profile = line.split("=", 1)[1].strip()
        if profile:
            break
    profiles = root / "basic" / "profiles"
    paths = [profiles / profile / "service.json"] if profile else sorted(profiles.glob("*/*.json"))
    for path in paths:
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            key = json.loads(path.read_text())["settings"]["key"]
            if isinstance(key, str) and key:
                return key
    return None


class StreamDesk:
    """One `run`'s stream: its words, its visibility, and its video on YouTube."""

    def __init__(
        self,
        hub: Hub,
        account: Account,
        *,
        still: Path | None = None,
        obs_key: Callable[[], str | None] = obs_stream_key,
        today: Callable[[], date] = date.today,
        now: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.hub = hub
        self.account = account
        self.youtube = YouTube(account)
        self.still = still
        self._obs_key = obs_key
        self._today, self._now, self._wall = today, now, wall
        # What the phone edits. Unlisted and the match's own title are where
        # every session and every stream start.
        self.visibility = "unlisted"
        self.typed: str | None = None
        # idle → preparing → ready → live → idle, or preparing → failed.
        self.phase = "idle"
        self.error: str | None = None  # why the last go-live was refused
        self.note: str | None = None  # something the stream goes ahead without
        self.on_air = False
        self.started: datetime | None = None
        self.video_id: str | None = None
        self.live_title: str | None = None
        self._description = ""
        self._account_state = account.state
        self._code: DeviceCode | None = None
        self._code_until = 0.0
        self._signing_in = False  # a code has been asked for and not yet used up
        self._ready_at = 0.0
        self._abandoned = False  # the deck gave up on a prepare still running
        self._stream_id: str | None = None
        self._last_mark: tuple[object, ...] | None = None
        self._lock = asyncio.Lock()  # one conversation with YouTube at a time
        self._tasks: set[asyncio.Task[None]] = set()

    # ---------- what every page and the deck are told ----------

    def _naming(self, typed: str | None = None) -> Naming:
        return naming(
            self.hub.score.as_dict(),
            self.hub.clock.settings.as_dict(),
            self.hub.bracket.name if self.hub.bracket is not None else None,
            self._today(),
            typed,
        )

    def payload(self) -> dict[str, object]:
        """The `stream` object on every broadcast and in `GET /api/state`."""
        auto = self._naming()
        live = self.on_air and self.video_id is not None
        minutes: int | None = None
        if self.on_air and self.started is not None:
            minutes = max(0, int((self._wall() - self.started).total_seconds() // 60))
        return {
            "account": self._account_state,  # ok | signed_out | unconfigured
            "signin": {"code": self._code.user_code, "url": self._code.url}
            if self._code is not None
            else None,
            "phase": self.phase,
            "on_air": self.on_air,
            "minutes": minutes,
            "error": self.error,
            "note": self.note,
            "title": self.live_title
            if live and self.live_title
            else self._naming(self.typed).title,
            # Where the title in the field came from: the phone words its hint on it.
            "source": "live" if live else "typed" if self.typed is not None else auto.kind,
            "auto_title": auto.title,
            "visibility": self.visibility,
            "url": f"https://youtu.be/{self.video_id}" if live else None,
        }

    def _journal(self, kind: str, **detail: object) -> None:
        self.hub.journal.log(kind, **detail)

    # ---------- the phone's edits ----------

    async def set_title(self, text: str | None) -> None:
        """Type a title, or (None or empty) go back to the match's own.

        Off air it is kept for the next stream. On air it goes to the live
        video; raises `YouTubeError` if YouTube will not take it.
        """
        cleaned = clean_title(text or "")
        auto = self._naming().title
        if self.on_air and self.video_id is not None:
            title = cleaned or auto
            if title != self.live_title:
                video_id, description = self.video_id, self._description
                await self._ask(lambda: self.youtube.describe(video_id, title, description))
                self.live_title = title
                self._journal("stream_edit", video_id=video_id, title=title)
            return
        self.typed = cleaned if cleaned and cleaned != auto else None

    async def set_visibility(self, value: str) -> None:
        """Unlisted or Public: for the next stream, or for the one on air."""
        if value not in VISIBILITIES:
            raise ValueError(value)
        if self.on_air and self.video_id is not None:
            video_id = self.video_id
            actual = await self._ask(lambda: self.youtube.set_privacy(video_id, value))
            self.visibility = actual
            self.note = (
                None
                if actual == value
                else f"YouTube kept this stream {actual.capitalize()}. Change it in YouTube Studio."
            )
            self._journal("stream_edit", video_id=video_id, visibility=actual)
            return
        self.visibility = value

    async def _ask(self, call: Callable[[], _T]) -> _T:
        """One YouTube call off the event loop, never two at once."""
        async with self._lock:
            try:
                return await asyncio.to_thread(call)
            except SignedOutError:
                self._account_state = self.account.state
                raise

    # ---------- going live ----------

    def prepare(self) -> str | None:
        """Start making the video; None, or why not, in words.

        Returns at once. The deck reads `phase` off its poll: `ready` means
        start OBS, `failed` means do not.
        """
        self._account_state = self.account.state
        if self._account_state == "unconfigured":
            return "YouTube is not set up on this machine"
        if self._account_state != "ok":
            return "Not signed in to YouTube"
        if self.on_air:
            return "Already on air"
        if self.phase != "preparing":
            self.phase, self.error, self.note = "preparing", None, None
            self._abandoned = False
            self._spawn(self._prepare())
        return None

    async def _prepare(self) -> None:
        words, privacy = self._naming(self.typed), self.visibility
        try:
            made = await self._ask(lambda: self._create(words, privacy))
        except YouTubeError as exc:
            self._fail(str(exc))
            return
        except Exception as exc:  # a bug must not leave STARTING… on the key
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        if self._abandoned or self.on_air:
            # The deck stopped waiting, so OBS will not start (or OBS was
            # started by hand meanwhile, onto a video YouTube made itself):
            # nothing may be left on YouTube that a stream could start by
            # accident.
            await self._discard(made["video_id"])
            if not self.on_air:
                self._fail("YouTube took too long to answer")
            return
        self.video_id = made["video_id"]
        self.live_title, self._description = words.title, words.description
        self.visibility, self.note = made["privacy"], made["note"]
        self.phase, self._ready_at = "ready", self._now()
        self._journal(
            "stream_prepared",
            video_id=self.video_id,
            title=words.title,
            visibility=self.visibility,
            note=self.note,
        )

    def _fail(self, why: str) -> None:
        self.phase, self.error = "failed", why
        self.video_id = self.live_title = None
        self._account_state = self.account.state
        self._journal("stream_failed", error=why)

    def _create(self, words: Naming, privacy: str) -> dict[str, Any]:
        """The YouTube half of a go-live, in order. Runs on a worker thread."""
        yt = self.youtube
        stream_id = self._find_stream()
        # An unstarted auto-start video on OBS's key would go on air with this
        # one, under its own title and visibility. Only this desk makes them,
        # so one found here is left over from a go-live that never finished.
        for old in yt.upcoming():
            details, status = old.get("contentDetails", {}), old.get("status", {})
            if (
                details.get("boundStreamId") == stream_id
                and details.get("enableAutoStart")
                and status.get("lifeCycleStatus") in ("created", "ready")
            ):
                with contextlib.suppress(YouTubeError):
                    yt.delete_broadcast(str(old["id"]))
        made = yt.create_broadcast(words.title, privacy, self._wall())
        video_id = str(made["id"])
        actual = str(made.get("status", {}).get("privacyStatus", privacy))
        try:
            if actual == "private":
                raise YouTubeError("YouTube would only make this stream Private")
            yt.bind(video_id, stream_id)
        except YouTubeError:
            with contextlib.suppress(YouTubeError):
                yt.delete_broadcast(video_id)
            raise
        # From here the stream is right where it matters (its title and who
        # can watch), so the rest is noted when it fails, not refused.
        notes = []
        if actual != privacy:
            notes.append(
                f"YouTube made this stream {actual.capitalize()}, not {privacy.capitalize()}."
            )
        try:
            yt.describe(video_id, words.title, words.description)
        except YouTubeError as exc:
            notes.append(f"Description not set ({exc}).")
        try:
            yt.set_thumbnail(video_id, thumbnail.jpeg(thumbnail.render(words, self._picture())))
        except YouTubeError as exc:
            notes.append(f"Thumbnail not set ({exc}).")
        return {"video_id": video_id, "privacy": actual, "note": " ".join(notes) or None}

    def _find_stream(self) -> str:
        """The id of the YouTube stream whose key OBS holds (asked once a run)."""
        if self._stream_id is None:
            key = self._obs_key()
            if key is None:
                raise YouTubeError("OBS has no stream key (Settings, Stream)")
            match = [s["id"] for s in self.youtube.streams() if s["key"] == key]
            if not match:
                raise YouTubeError("OBS's stream key is not one of this channel's keys")
            self._stream_id = match[0]
        return self._stream_id

    def _picture(self) -> Image.Image | None:
        if self.still is None:
            return None
        try:
            with Image.open(self.still) as picture:
                return picture.convert("RGB")
        except OSError:
            return None  # no picture chosen yet: the panel is drawn plain

    async def _discard(self, video_id: str) -> None:
        with contextlib.suppress(YouTubeError):
            await self._ask(lambda: self.youtube.delete_broadcast(video_id))

    def cancel(self) -> None:
        """The deck's start did not go on air: take the prepared video back."""
        if self.phase == "preparing":
            self._abandoned = True  # removed when the worker lands (`_prepare`)
        elif self.phase == "ready" and self.video_id is not None:
            self._spawn(self._discard(self.video_id))
            self._fail("OBS did not start the stream")

    # ---------- what the deck reports of OBS ----------

    def went_on_air(self, duration_ms: int = 0) -> None:
        """OBS is streaming, and has been for ``duration_ms``."""
        if self.on_air:
            return
        if self.phase != "ready":
            # Started some other way (OBS's own button): YouTube made that
            # video itself, and this desk does not know it.
            self.video_id = self.live_title = None
        self.on_air, self.phase = True, "live"
        self.started = self._wall() - timedelta(milliseconds=max(0, duration_ms))
        self._journal(
            "stream_on_air",
            video_id=self.video_id,
            start_wall=self.started.isoformat(timespec="milliseconds"),
            title=self.live_title,
            visibility=self.visibility,
        )
        self._last_mark = None
        self._mark()

    def went_off_air(self) -> None:
        """OBS stopped streaming: everything goes back to where a stream starts."""
        if not self.on_air:
            return
        self._journal("stream_off_air", video_id=self.video_id)
        self.on_air, self.started, self.phase = False, None, "idle"
        self.video_id = self.live_title = None
        self._description = ""
        self.error = self.note = None
        # Last week's Public, and last match's typed title, never carry over.
        self.visibility, self.typed = "unlisted", None

    def _mark(self) -> None:
        """Journal the match as it stands, if it changed: the chapters' source."""
        if not self.on_air or self.video_id is None:
            return
        score = self.hub.score
        targets = race(score.as_dict())
        bracket = self.hub.bracket
        mark = (
            score.p1_name,
            score.p2_name,
            score.p1_score,
            score.p2_score,
            targets is not None and (score.p1_score >= targets[0] or score.p2_score >= targets[1]),
            bracket.name if bracket is not None else None,
            bracket.loaded if bracket is not None else None,
        )
        if mark == self._last_mark:
            return
        self._last_mark = mark
        self._journal(
            "stream_mark",
            video_id=self.video_id,
            p1=mark[0],
            p2=mark[1],
            s1=mark[2],
            s2=mark[3],
            over=mark[4],
            tournament=mark[5],
            match=mark[6],
        )

    # ---------- the sign-in, from the phone ----------

    def sign_in(self) -> None:
        """Ask Google for a code to type at google.com/device, once.

        Every phone with the Setup tab open asks, so a sign-in already under
        way is left alone: there is one code at a time, and they all show it.
        """
        self._account_state = self.account.state
        if self._account_state == "signed_out" and not self._signing_in:
            self._signing_in = True
            self._spawn(self._sign_in())

    async def _sign_in(self) -> None:
        try:
            code = await self._ask(self.account.request_code)
            self._code, self._code_until = code, self._now() + code.expires_s
            self.error = None
            while self._now() < self._code_until:
                await asyncio.sleep(code.interval_s)
                try:
                    if await self._ask(lambda: self.account.poll_code(code)):
                        self._journal("stream_signed_in")
                        return
                except YouTubeError as exc:
                    if not exc.unreachable:  # refused at Google, or the code ran out
                        self.error = str(exc)
                        return
        except YouTubeError as exc:
            self.error = str(exc)
        finally:
            self._code, self._signing_in = None, False
            self._account_state = self.account.state

    # ---------- the desk's own clock ----------

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def tick(self) -> None:
        """Once a second: the cheap things nothing else triggers."""
        if not self._signing_in:
            self._account_state = self.account.state
        if self.phase == "ready" and self._now() - self._ready_at > READY_TTL_S:
            self.cancel()
        self._mark()

    async def run(self) -> None:
        """The desk's loop, for the life of the server."""
        self._spawn(self._check_sign_in())
        while True:
            await asyncio.sleep(1.0)
            self.tick()

    async def _check_sign_in(self) -> None:
        """Find out at session start whether the sign-in on file still works,
        so the LIVE key can say SIGN IN before anybody holds it."""
        if self._account_state != "ok":
            return
        with contextlib.suppress(YouTubeError):  # unreachable is not signed out
            await self._ask(self.account.token)
        self._account_state = self.account.state
