import base64, ctypes, json, os, secrets, shutil, subprocess, sys, threading, time, traceback, winreg
import webview
import pyaudiowpatch as pyaudio
from . import engine

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ["APPDATA"], "Record")
SETTINGS = os.path.join(DATA, "settings.json")
LOG = os.path.join(DATA, "log.txt")
ICON = os.path.join(HERE, "ui", "icon.ico")
DEFAULTS = {"base": os.path.join(os.path.expanduser("~"), "Music", "Record"), "format": "mp3", "theme": "system",
            "source": "", "library": [], "selected": None}


def log(text):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text.rstrip()}\n")
    except OSError:
        pass


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    settings = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    if settings["format"] not in engine.FORMATS:
        settings["format"] = "mp3"
    return settings


def audio_device():
    p = pyaudio.PyAudio()
    try:
        return engine.loopback_device(p)["name"].replace(" [Loopback]", "")
    except Exception:
        return None
    finally:
        p.terminate()


def disk_stats(folder):
    used = sum(os.path.getsize(p) for p in engine.saved_names(folder).values())
    existing = folder
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    return {"used": used, "free": shutil.disk_usage(existing).free}


def folder_cover(folder):
    # the cover art of the first saved track that has one, as a data URL the page can show
    for path in sorted(engine.saved_names(folder).values()):
        if found := engine.read_cover(path):
            data, mime = found
            return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return None


def system_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return True


def set_title_bar(window, dark):
    try:
        hwnd = window.native.Handle.ToInt32()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(int(dark))), 4)
    except Exception:
        pass


def set_icon(window):
    # the red record dot instead of Python's icon, in the title bar and the taskbar
    try:
        hwnd, user32 = window.native.Handle.ToInt32(), ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
        for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
            icon = user32.LoadImageW(None, ICON, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            user32.SendMessageW(hwnd, 0x80, which, icon)  # WM_SETICON
    except Exception:
        pass


# Every public method is callable from the page as `pywebview.api.<name>()`.
class Api:
    def __init__(self):
        self._window, self._rec, self._rec_id = None, None, None
        self._settings = load_settings()
        self._extras = {}
        self._device = audio_device()

    def _save(self):
        os.makedirs(DATA, exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(self._settings, f, indent=2)

    def _entry(self, cid):
        return next((e for e in self._settings["library"] if e["id"] == cid), None)

    def _running(self):
        return bool(self._rec and self._rec.thread.is_alive())

    def _library(self):
        return [{**e, "saved": len(engine.saved_names(e["folder"]))} for e in self._settings["library"]]

    def _public_settings(self):
        return {k: v for k, v in self._settings.items() if k != "library"}

    def _view(self, cid):
        entry = self._entry(cid)
        return {"entry": {**entry, "cover": folder_cover(entry["folder"])}, "tracks": engine.saved_tracks(entry["folder"]),
                "extras": list(self._extras.get(cid, {}).values()), "disk": disk_stats(entry["folder"]),
                "recording": self._rec_id if self._running() else None}

    def _emit(self, kind, **data):
        if kind in ("error", "warning") or kind == "status" and data["status"] in ("partial", "failed", "saved"):
            log(f"{kind}: {data}")
        if kind == "status":
            self._extras.setdefault(self._rec_id, {})[data["key"]] = data
        if self._window:
            self._window.run_js(f"window.onEvent({json.dumps({'kind': kind, 'cid': self._rec_id, **data})})")

    def _on_closing(self):
        if not self._running():
            return True
        if not self._window.create_confirmation_dialog(
                "Quit while recording?", "The track playing now will be discarded. Finished tracks are kept."):
            return False
        self._rec.stop(now=True)
        self._rec.thread.join(120)
        return True

    def init(self):
        return {"settings": self._public_settings(), "library": self._library(), "checks": self.checks(),
                "formats": [{"id": k, "label": v[0]} for k, v in engine.FORMATS.items()]}

    def checks(self):
        return {"ffmpeg": bool(engine.find_ffmpeg()), "device": self._device}

    def sources(self):
        found = {s["id"]: s for s in engine.media_sources()}
        chosen = self._settings["source"]
        if chosen and chosen not in found:  # keep the chosen app listed while it's closed
            found[chosen] = {"id": chosen, "name": engine.app_name(chosen)}
        return list(found.values())

    def add(self, name):
        name = name.strip()
        if not engine.clean_name(name):
            return {"error": "Give it a name"}
        return self._add(name, os.path.join(self._settings["base"], engine.clean_name(name)))

    def add_folder(self):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=self._settings["base"])
        if not result:
            return None
        folder = os.path.normpath(result[0])
        return self._add(os.path.basename(folder) or folder, folder)

    def _add(self, name, folder):
        existing = next((e for e in self._settings["library"] if os.path.normcase(e["folder"]) == os.path.normcase(folder)), None)
        entry = existing or {"id": secrets.token_hex(4), "name": name, "folder": folder}
        if not existing:
            self._settings["library"].append(entry)
        self._settings["selected"] = entry["id"]
        self._save()
        return {"id": entry["id"], "library": self._library()}

    def open(self, cid):
        if not self._entry(cid):
            return {"error": "Collection not found"}
        self._settings["selected"] = cid
        self._save()
        return self._view(cid)

    def library(self):
        return self._library()

    def remove(self, cid):
        if self._running() and self._rec_id == cid:
            return {"error": "Stop the recording first"}
        self._settings["library"] = [e for e in self._settings["library"] if e["id"] != cid]
        self._extras.pop(cid, None)
        self._settings["selected"] = None
        self._save()
        return {"library": self._library()}

    def change_folder(self, cid):
        entry = self._entry(cid)
        start = entry["folder"] if os.path.isdir(entry["folder"]) else self._settings["base"]
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not result:
            return None
        entry["folder"] = os.path.normpath(result[0])
        self._save()
        return {"view": self._view(cid), "library": self._library()}

    def update(self, changes):
        if self._running() and set(changes) - {"theme"}:
            return {"error": "Locked while recording"}
        self._settings.update({k: v for k, v in changes.items() if k in DEFAULTS and k != "library"})
        self._save()
        return {"settings": self._public_settings()}

    def choose_base(self):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=self._settings["base"])
        return self.update({"base": os.path.normpath(result[0])}) if result else None

    def start(self, cid):
        if self._running():
            return {"error": "Already recording"}
        if not engine.find_ffmpeg():
            return {"error": "ffmpeg missing: winget install Gyan.FFmpeg"}
        entry, s = self._entry(cid), self._settings
        self._extras[cid], self._rec_id = {}, cid
        self._rec = engine.Recorder(entry["folder"], entry["name"], s["format"], s["source"], emit=self._emit)
        self._rec.start()
        return {"ok": True}

    def stop(self, now=False):
        if self._rec:
            self._rec.stop(now)

    def control(self, action):
        engine.media_control(action, self._settings["source"])

    def open_folder(self, cid):
        folder = self._entry(cid)["folder"]
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def reveal(self, cid, key):
        folder = self._entry(cid)["folder"]
        detail = self._extras.get(cid, {}).get(key, {}).get("detail")
        path = os.path.join(folder, detail or key)
        if engine.SAVED.fullmatch(os.path.basename(path)) and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", path])

    def set_dark(self, dark):
        set_title_bar(self._window, dark)


def main():
    # pythonw has no console, so anything that crashes is written to the log instead of vanishing
    sys.excepthook = lambda kind, value, tb: log("".join(traceback.format_exception(kind, value, tb)))
    threading.excepthook = lambda a: log("".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    log("app started")
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Record")  # own taskbar entry, not Python's
    api = Api()
    theme = api._settings["theme"]
    dark = theme == "dark" or theme == "system" and system_dark()
    window = webview.create_window("Record", os.path.join(HERE, "ui", "index.html"), js_api=api,
                                   width=1240, height=820, min_size=(720, 540),
                                   background_color="#0e0e0d" if dark else "#f6f6f1")
    api._window = window
    window.events.closing += api._on_closing
    window.events.shown += lambda: (set_title_bar(window, dark), set_icon(window))
    webview.start(http_server=True)


if __name__ == "__main__":
    main()
