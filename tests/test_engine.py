"""Runs the real recorder against a fake player and fake sound card, on a simulated clock.

Two complete tracks, one skipped after 10 s and one cut off by Stop: only the complete ones may be saved.
"""
import array, math, os, time, types
import mutagen, pytest
from record import engine

RATE = 44100
FRAMES = int(RATE * engine.BLOCK)
LOUD = array.array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(FRAMES) for _ in range(2))).tobytes()
QUIET = bytes(len(LOUD))

# (title, artist, audio from, audio until, reported length); the player reports a new title 0.2 s after its audio starts
PLAYLIST = [
    ("Song A", "Artist A", 1.0, 37.0, 36.0),
    ("Song B", "Artist B", 37.4, 72.4, 35.0),
    ("Song C", "Artist C", 72.6, 82.6, 40.0),   # skipped by the listener after 10 s
    ("Song D", "Artist D", 82.8, 200.0, 40.0),  # still playing when Stop is pressed
]
STOP_AT = 95.0

pytestmark = pytest.mark.skipif(not engine.find_ffmpeg(), reason="needs ffmpeg")


class FakeNowPlaying:
    def __init__(self, clock):
        self.clock = clock

    def song(self):
        for title, artist, start, end, length in PLAYLIST:
            if start + 0.2 <= self.clock.t < end + 0.2:
                return {"name": f"{artist} - {title}", "title": title, "artist": artist, "album": "",
                        "app": "Fake.exe", "duration": length}
        return None

    def cover(self):
        return b"\x89PNG fake cover"

    def control(self, action):
        pass

    def close(self):
        pass


class FakeStream:
    def stop_stream(self):
        pass

    def close(self):
        pass


class FakePyAudio:
    callback = None

    def open(self, stream_callback, **_):
        FakePyAudio.callback = stream_callback
        return FakeStream()

    def terminate(self):
        pass


def test_saves_only_complete_tracks(tmp_path, monkeypatch):
    clock = types.SimpleNamespace(t=0.0)
    events = []

    def sleep(seconds):
        # advance the simulated clock and play whatever sounds at that moment
        for _ in range(round(seconds / engine.BLOCK)):
            playing = any(start <= clock.t < end for _, _, start, end, _ in PLAYLIST)
            FakePyAudio.callback(LOUD if playing else QUIET)
            clock.t += engine.BLOCK
        if clock.t >= STOP_AT:
            recorder.stop(now=True)

    monkeypatch.setattr(engine, "time", types.SimpleNamespace(sleep=sleep, time_ns=time.time_ns))
    monkeypatch.setattr(engine, "NowPlaying", lambda source: FakeNowPlaying(clock))
    monkeypatch.setattr(engine.pyaudio, "PyAudio", FakePyAudio)
    monkeypatch.setattr(engine, "loopback_device", lambda p: {"maxInputChannels": 2, "defaultSampleRate": RATE,
                                                              "index": 0, "name": "Fake speakers"})

    recorder = engine.Recorder(str(tmp_path), "Test collection", "mp3", emit=lambda kind, **data: events.append((kind, data)))
    recorder.start()
    recorder.thread.join(120)
    assert not recorder.thread.is_alive()

    final = {data["title"]: data["status"] for kind, data in events if kind == "status"}
    assert final == {"Song A": "saved", "Song B": "saved", "Song C": "partial", "Song D": "partial"}
    assert not [kind for kind, _ in events if kind == "error"]

    files = sorted(os.listdir(tmp_path))
    assert files == ["01 Artist A Song A.mp3", "02 Artist B Song B.mp3"]
    for name, length in zip(files, (36.0, 35.0)):
        audio = mutagen.File(tmp_path / name, easy=True)
        assert abs(audio.info.length - length) < 1
        assert audio["album"] == ["Test collection"]
    assert engine.read_cover(str(tmp_path / files[0]))[0] == b"\x89PNG fake cover"
    assert [t["title"] for t in engine.saved_tracks(str(tmp_path))] == ["Song A", "Song B"]


def test_app_name():
    assert engine.app_name("vlc.exe") == "vlc"
    assert engine.app_name("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == "Media Player"
    assert engine.app_name("308046B0AF4A39CB") == "Firefox"
    assert engine.app_name("Chrome") == "Chrome"


def test_song_name_without_artist():
    assert engine.song_name("", "Live at: the Park?") == "Live at the Park"
    assert engine.song_name("Band", "Song") == "Band Song"
