import array, asyncio, ctypes, collections, glob, math, os, queue, re, shutil, subprocess, threading, time, wave
import mutagen
import pyaudiowpatch as pyaudio
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus
from winrt.windows.storage.streams import Buffer, InputStreamOptions

POLL = 0.2        # seconds between now-playing checks
BLOCK = 0.02      # seconds per audio block
HOLD = 1.5        # seconds of audio held back so a cut can land in the gap between tracks
TOLERANCE = 2.5   # a track counts as complete if at most this many seconds are missing
CROSSFADE_MAX = 13  # crossfades go up to about 12 s, so a natural ending short by more than this was a skip
LISTED_SLACK = 6  # a player's listed length can run a few seconds past the audio; fine if the track ended in silence
SILENT = 0.25     # level() below this (-45 dBFS) at the cut means a real gap between tracks, not a crossfade
MIN_SECONDS = 30
TRIM = "silenceremove=start_periods=1:start_threshold=-50dB"
FORMATS = {
    "mp3": ("MP3 320 kbps", "mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
    "flac": ("FLAC (lossless, large)", "flac", ["-c:a", "flac"]),
}
SAVED = re.compile(r"(\d+) (.+)\.(mp3|flac)", re.I)
CONTROLS = {"play_pause": "try_toggle_play_pause_async", "next": "try_skip_next_async",
            "previous": "try_skip_previous_async", "pause": "try_pause_async"}
APP_NAMES = {"308046B0AF4A39CB": "Firefox", "MSEdge": "Edge", "Microsoft.ZuneMusic": "Media Player"}
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def find_ffmpeg():
    if path := shutil.which("ffmpeg"):
        return path
    hits = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"))
    return hits[0] if hits else None


def clean_name(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)[:150].rstrip(" .")


def song_name(artist, title):
    return clean_name(f"{artist} {title}" if artist else title)


def saved_names(folder):
    if not os.path.isdir(folder):
        return {}
    return {m[2].lower(): os.path.join(folder, f) for f in os.listdir(folder) if (m := SAVED.fullmatch(f))}


def next_number(folder):
    numbers = [int(m[1]) for f in os.listdir(folder) if (m := SAVED.fullmatch(f))]
    return max(numbers, default=0) + 1


def saved_tracks(folder):
    if not os.path.isdir(folder):
        return []
    tracks = []
    for f in os.listdir(folder):
        if not (m := SAVED.fullmatch(f)):
            continue
        title, artist, length = m[2], "", 0
        try:
            audio = mutagen.File(os.path.join(folder, f), easy=True)
            title, artist = (audio.get("title") or [title])[0], (audio.get("artist") or [""])[0]
            length = audio.info.length
        except Exception:
            pass
        tracks.append({"key": f, "num": int(m[1]), "title": title, "artist": artist, "duration": length, "status": "saved"})
    return sorted(tracks, key=lambda t: (t["num"], t["key"]))


def image_mime(data):
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def add_cover(path, image):
    if path.endswith(".flac"):
        audio, picture = FLAC(path), Picture()
        picture.type, picture.mime, picture.data = 3, image_mime(image), image
        audio.add_picture(picture)
        audio.save()
    else:
        tags = ID3(path)
        tags.add(APIC(encoding=3, mime=image_mime(image), type=3, desc="Cover", data=image))
        tags.save()


def read_cover(path):
    try:
        if path.lower().endswith(".flac"):
            pictures = FLAC(path).pictures
            return (pictures[0].data, pictures[0].mime) if pictures else None
        frames = ID3(path).getall("APIC")
        return (frames[0].data, frames[0].mime) if frames else None
    except Exception:
        return None


def app_name(app_id):
    # "foobar2000.exe", "Chrome", "Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic" -> a short readable name
    name = app_id.split("!")[-1].removesuffix(".exe")
    name = APP_NAMES.get(name, name)
    return APP_NAMES.get(name.split("_")[0], name.split(".")[-1])


async def read_stream(ref):
    stream = await ref.open_read_async()
    buffer = Buffer(stream.size)
    await stream.read_async(buffer, buffer.capacity, InputStreamOptions.READ_AHEAD)
    return bytes(buffer)


class NowPlaying:
    """What Windows shows in its media controls. Works with every player that reports to them."""

    def __init__(self, source=""):
        self.source = source.lower()
        self.loop = asyncio.new_event_loop()
        self.manager = self.run(MediaManager.request_async())

    def run(self, coroutine):
        return self.loop.run_until_complete(coroutine)

    def close(self):
        self.loop.close()

    def sessions(self):
        return list(self.manager.get_sessions())

    def session(self):
        if self.source:
            return next((s for s in self.sessions() if s.source_app_user_model_id.lower() == self.source), None)
        return self.manager.get_current_session()

    def song(self):
        # {"name", "title", "artist", "album", "app", "duration"} while something plays, else None
        try:
            session = self.session()
            if not session or session.get_playback_info().playback_status != PlaybackStatus.PLAYING:
                return None
            props = self.run(session.try_get_media_properties_async())
            timeline = session.get_timeline_properties()
        except Exception:
            return None
        title, artist = (props.title or "").strip(), (props.artist or "").strip()
        if not title:
            return None
        duration = (timeline.end_time - timeline.start_time).total_seconds()
        return {"name": f"{artist} - {title}" if artist else title, "title": title, "artist": artist,
                "album": props.album_title or "", "app": session.source_app_user_model_id, "duration": duration if duration > 0 else None}

    def cover(self):
        try:
            props = self.run(self.session().try_get_media_properties_async())
            return self.run(read_stream(props.thumbnail)) if props.thumbnail else None
        except Exception:
            return None

    def control(self, action):
        try:
            if session := self.session():
                self.run(getattr(session, CONTROLS[action])())
        except Exception:
            pass


def media_sources():
    now = NowPlaying()
    try:
        return [{"id": s.source_app_user_model_id, "name": app_name(s.source_app_user_model_id)} for s in now.sessions()]
    except Exception:
        return []
    finally:
        now.close()


def media_control(action, source=""):
    now = NowPlaying(source)
    try:
        now.control(action)
    finally:
        now.close()


def loudness(block):
    a = array.array("h", block)
    return sum(x * x for x in a) / max(len(a), 1)


def level(block):
    return max(0.0, 1 + 10 * math.log10(loudness(block) / 32768 ** 2 + 1e-9) / 60)  # 0 at -60 dBFS, 1 at 0 dBFS


def loopback_device(p):
    out = p.get_device_info_by_index(p.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultOutputDevice"])
    for d in p.get_loopback_device_info_generator():
        if out["name"] in d["name"]:
            return d
    raise RuntimeError("No loopback device found for " + out["name"])


class Track:
    count = 0

    def __init__(self, song, folder, channels, rate):
        Track.count += 1
        self.name, self.title, self.artist, self.duration = song["name"], song["title"], song["artist"], song["duration"]
        self.channels, self.rate = channels, rate
        self.key = f"r{Track.count}"
        self.frames, self.skip, self.silent_end, self.cover, self.checked = 0, False, False, None, False
        self.path = os.path.join(folder, f"~recording-{time.time_ns()}.wav")
        self.wav = wave.open(self.path, "wb")
        self.wav.setnchannels(channels)
        self.wav.setsampwidth(2)
        self.wav.setframerate(rate)

    def write(self, blocks):
        data = b"".join(blocks)
        self.wav.writeframes(data)
        self.frames += len(data) // (2 * self.channels)

    def seconds(self):
        return self.frames / self.rate

    def complete(self):
        return bool(self.duration) and self.seconds() >= self.duration - TOLERANCE


class Recorder:
    def __init__(self, folder, album, fmt="mp3", source="", skip_existing=True, cover_art=True, emit=None):
        self.folder, self.album, self.fmt, self.source = folder, album, fmt, source
        self.skip_existing, self.cover_art = skip_existing, cover_art
        self.emit = emit or (lambda kind, **data: None)
        self.ffmpeg = find_ffmpeg()
        self.jobs, self.stops = queue.Queue(), 0
        self.near_misses, self.crossfade_warned = 0, False
        self.thread = threading.Thread(target=self.run)

    def start(self):
        self.thread.start()

    def stop(self, now=False):
        self.stops = 2 if now else max(self.stops, 1)

    def set_status(self, track, status, detail=""):
        self.emit("status", key=track.key, artist=track.artist, title=track.title, duration=track.duration,
                  status=status, detail=detail)

    def new_track(self, song):
        track = Track(song, self.folder, self.channels, self.rate)
        track.skip = self.skip_existing and song_name(track.artist, track.title).lower() in saved_names(self.folder)
        self.set_status(track, "already saved" if track.skip else "recording")
        self.emit("now", key=track.key, artist=track.artist, title=track.title, duration=track.duration,
                  app=app_name(song["app"]))
        return track

    def finish(self, track, stopped=False, natural=False):
        track.wav.close()
        got = track.seconds()
        allowed = LISTED_SLACK if natural and track.silent_end else TOLERANCE
        if track.skip:
            os.remove(track.path)
        elif stopped or track.duration and got < track.duration - allowed:
            os.remove(track.path)
            self.set_status(track, "partial", f"only {got:.0f} of {track.duration or 0:.0f} s recorded")
            # tracks that end on their own but a few seconds short, twice in a row: the player is crossfading,
            # so every track will be cut short; stop instead of recording a whole album for nothing
            short = (track.duration or 0) - got
            self.near_misses = self.near_misses + 1 if natural and short <= CROSSFADE_MAX else 0
            if self.near_misses >= 2 and not self.crossfade_warned:
                self.crossfade_warned = True
                self.emit("warning", reason="crossfade", text="Tracks overlap. Crossfade looks like it's on in your player.")
                self.now.control("pause")
                self.stop()
        else:
            self.near_misses = 0
            self.set_status(track, "converting")
            self.jobs.put(track)

    def run(self):
        # keep Windows from sleeping mid-album; the flag lives as long as this thread
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        try:
            self.now = NowPlaying(self.source)
            try:
                self.record()
            finally:
                self.now.close()
        except Exception as e:
            self.emit("error", text=str(e))
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.emit("done")

    def record(self):
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg not found. Install it with: winget install Gyan.FFmpeg")
        os.makedirs(self.folder, exist_ok=True)
        self.number = next_number(self.folder)
        worker = threading.Thread(target=self.convert)
        worker.start()

        pending = collections.deque()
        def on_audio(data, *_):
            pending.append(data)
            return None, pyaudio.paContinue

        p = pyaudio.PyAudio()
        dev = loopback_device(p)
        self.channels, self.rate = dev["maxInputChannels"], int(dev["defaultSampleRate"])
        stream = p.open(format=pyaudio.paInt16, channels=self.channels, rate=self.rate, input=True,
                        input_device_index=dev["index"], frames_per_buffer=int(self.rate * BLOCK),
                        stream_callback=on_audio)
        self.emit("log", text=f"Listening on {dev['name']}, saving to {self.folder}")
        self.emit("state", text="Waiting for music")

        hold = int(HOLD / BLOCK)
        track, playing = None, False
        song = self.now.song()
        ignored = song and song["name"]  # a track already playing when recording starts can't be recorded from the start
        while self.stops < 2 and not (self.stops and not playing):
            time.sleep(POLL)
            song = self.now.song()
            title = song and song["name"]
            if ignored and title != ignored:
                ignored = None
            if ignored:
                while len(pending) > hold:
                    pending.popleft()
                continue
            if title and track and title == track.name:
                if not playing:
                    playing = True
                    self.emit("state", text="Recording")
                while len(pending) > hold:
                    track.write([pending.popleft()])
                if not track.checked and track.seconds() > 3:
                    # players fill in the length and the cover art a moment after a track starts
                    track.checked = True
                    track.duration = track.duration or song["duration"]
                    track.cover = self.now.cover() if self.cover_art else None
                self.emit("tick", seconds=track.seconds(), duration=track.duration,
                          level=level(pending[-1]) if pending else 0)
            elif title:
                blocks = [pending.popleft() for _ in range(len(pending))]
                if track:
                    if playing and blocks:
                        cut = min(range(len(blocks)), key=lambda i: loudness(blocks[i]))
                        track.write(blocks[:cut])
                        track.silent_end = level(blocks[cut]) < SILENT
                        blocks = blocks[cut:]
                    self.finish(track, natural=playing)
                    track = None
                    if self.stops:
                        break
                elif blocks:  # first track: start at the quiet moment, not in the tail of whatever played before
                    blocks = blocks[min(range(len(blocks)), key=lambda i: loudness(blocks[i])):]
                track = self.new_track(song)
                playing = True
                track.write(blocks)
                self.emit("state", text="Recording")
            else:
                if track and playing:
                    track.write([pending.popleft() for _ in range(len(pending))])
                    playing = False
                    if track.complete():  # the track ended and the player stopped, e.g. at the end of an album
                        self.finish(track)
                        track = None
                    self.emit("state", text="Paused" if track else "Waiting for music")
                    self.emit("tick", seconds=track.seconds() if track else 0,
                              duration=track.duration if track else None, level=0)
                while len(pending) > hold:
                    pending.popleft()

        stream.stop_stream()
        stream.close()
        p.terminate()
        if track:
            self.finish(track, stopped=playing)
        self.jobs.put(None)
        self.emit("state", text="Finishing conversions")
        worker.join()
        self.emit("state", text="Stopped")

    def convert(self):
        _, ext, codec = FORMATS[self.fmt]
        while (track := self.jobs.get()) is not None:
            out = track.path[:-4] + "." + ext
            result = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", track.path,
                                     "-af", f"{TRIM},areverse,{TRIM},areverse", "-ac", "2", *codec, out],
                                    capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                self.set_status(track, "failed", f"ffmpeg error, WAV kept: {result.stderr.strip()[-200:]}")
                continue
            os.remove(track.path)
            audio = mutagen.File(out, easy=True)
            if audio.info.length < MIN_SECONDS:
                os.remove(out)
                self.set_status(track, "too short", f"under {MIN_SECONDS} s")
                continue
            number, self.number = self.number, self.number + 1
            if audio.tags is None:
                audio.add_tags()
            audio["title"], audio["album"], audio["tracknumber"] = track.title, self.album, str(number)
            if track.artist:
                audio["artist"] = track.artist
            audio.save()
            if track.cover:
                add_cover(out, track.cover)
            name = f"{number:02d} {song_name(track.artist, track.title)}.{ext}"
            os.replace(out, os.path.join(self.folder, name))
            self.set_status(track, "saved", name)
