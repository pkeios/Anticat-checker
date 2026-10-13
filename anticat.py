#!/usr/bin/env python3
"""Detect cats approaching the camera and play a deterrent sound."""

import argparse
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch
from ultralytics import YOLO

from notifier import EmailNotifier

COCO_CAT_CLASS = 15
DEFAULT_EMAIL_CONFIG = Path(__file__).resolve().parent / "email.ini"
WINDOW_NAME = "anticat"
IS_WINDOWS = sys.platform == "win32"
# Name fragments of built-in speaker devices (English / Spanish Windows)
SPEAKER_KEYWORDS = ("speaker", "altavoc", "parlante", "bocina")
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SOUND = BASE_DIR / "sounds" / "default.wav"


def generate_default_sound(path: Path, seconds: float = 2.0, rate: int = 44100) -> None:
    """Write a loud high-pitched sweep mixed with a hiss, which most cats dislike."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    # Repeated sweep 6 kHz -> 14 kHz, four times per clip
    period = seconds / 4
    phase_t = t % period
    freq = 6000 + (14000 - 6000) * (phase_t / period)
    sweep = np.sin(2 * np.pi * np.cumsum(freq) / rate)
    hiss = np.random.default_rng(0).uniform(-1, 1, t.size)
    signal = 0.7 * sweep + 0.3 * hiss
    # Short fades to avoid clicks
    fade = int(0.02 * rate)
    signal[:fade] *= np.linspace(0, 1, fade)
    signal[-fade:] *= np.linspace(1, 0, fade)
    pcm = (signal / np.abs(signal).max() * 32767 * 0.9).astype(np.int16)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


class SpeakerRouter:
    """Temporarily forces the built-in speakers on via ALSA mixer while the alarm plays.

    On laptops the headphone jack and speakers share one output, and the speakers are
    muted while headphones are plugged in. This unmutes them (and optionally mutes the
    headphones) for the duration of the alarm, then restores the previous state.
    """

    def __init__(self, card: str, volume: int, mute_headphones: bool):
        if not shutil.which("amixer"):
            raise RuntimeError("amixer not found (install alsa-utils) — needed for --output speakers")
        self.card = card
        self.volume = volume
        self.mute_headphones = mute_headphones
        self.saved: dict[str, tuple[str, str]] = {}

    def _get(self, control: str) -> tuple[str, str] | None:
        out = subprocess.run(["amixer", "-c", self.card, "sget", control],
                             capture_output=True, text=True)
        if out.returncode != 0:
            return None
        m = re.search(r"Playback (\d+) \[\d+%\].*\[(on|off)\]", out.stdout)
        return (m.group(1), "unmute" if m.group(2) == "on" else "mute") if m else None

    def _set(self, control: str, level: str, switch: str) -> None:
        subprocess.run(["amixer", "-c", self.card, "-q", "sset", control, level, switch],
                       check=False)

    def enable(self) -> None:
        self.saved = {c: s for c in ("Speaker", "Headphone") if (s := self._get(c))}
        self._set("Speaker", f"{self.volume}%", "unmute")
        if self.mute_headphones and "Headphone" in self.saved:
            self._set("Headphone", self.saved["Headphone"][0], "mute")

    def restore(self) -> None:
        for control, (level, switch) in self.saved.items():
            self._set(control, level, switch)
        self.saved = {}


def load_sounddevice():
    """Return the sounddevice module, or None if it (or the PortAudio library) is missing."""
    try:
        import sounddevice
        return sounddevice
    except (ImportError, OSError):
        return None


def list_output_devices(sd) -> list[tuple[int, str, str]]:
    apis = sd.query_hostapis()
    return [(i, d["name"], apis[d["hostapi"]]["name"])
            for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]


def find_output_device(sd, query: str) -> int | None:
    """Resolve an output device by index or case-insensitive name substring."""
    if query.isdigit():
        return int(query)
    query = query.lower()
    for idx, name, _ in list_output_devices(sd):
        if query in name.lower():
            return idx
    return None


class SoundPlayer:
    """Plays an audio file in the background without blocking detection.

    Uses sounddevice + soundfile when available (Windows/macOS, or Linux with PortAudio),
    which also allows picking a specific output device. Otherwise falls back to an
    external player (ffplay, mpv or aplay).
    """

    def __init__(self, sound: Path, volume: int, device: int | None = None,
                 router: SpeakerRouter | None = None):
        self.router = router
        self.device = device
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.sd = load_sounddevice()
        self.cmd: list[str] | None = None

        if self.sd:
            import soundfile
            data, self.rate = soundfile.read(str(sound), dtype="float32", always_2d=True)
            self.data = data * (volume / 100)
        elif shutil.which("ffplay"):
            self.cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                        "-volume", str(volume), str(sound)]
        elif shutil.which("mpv"):
            self.cmd = ["mpv", "--no-video", "--really-quiet",
                        f"--volume={volume}", str(sound)]
        elif shutil.which("aplay") and sound.suffix.lower() == ".wav":
            self.cmd = ["aplay", "-q", str(sound)]
        else:
            raise RuntimeError("No audio backend found: run 'pip install sounddevice soundfile' "
                               "(or install ffmpeg / mpv)")

    @property
    def backend(self) -> str:
        return "sounddevice" if self.sd else Path(self.cmd[0]).name

    def play(self) -> None:
        if self.thread and self.thread.is_alive():
            return  # still playing
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        if self.router:
            self.router.enable()
        try:
            if self.sd:
                self.sd.play(self.data, self.rate, device=self.device)
                self.sd.wait()
            else:
                self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
                self.proc.wait()
        except Exception as exc:
            print(f"Audio playback failed: {exc}")
        finally:
            if self.router:
                self.router.restore()

    def stop(self) -> None:
        if self.sd:
            self.sd.stop()
        elif self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.thread:
            self.thread.join(timeout=2)


class StopButton:
    """Clickable 'STOP' button drawn in the bottom-right corner of the preview window."""

    LABEL = "DETENER"
    WIDTH, HEIGHT, MARGIN = 150, 46, 12

    def __init__(self):
        self.rect = (0, 0, 0, 0)
        self.hover = False
        self.pressed = False

    def draw(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        x2, y2 = w - self.MARGIN, h - self.MARGIN
        x1, y1 = x2 - self.WIDTH, y2 - self.HEIGHT
        self.rect = (x1, y1, x2, y2)
        color = (40, 40, 230) if self.hover else (30, 30, 180)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, cv2.FILLED)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 255), 2)
        (tw, th), _ = cv2.getTextSize(self.LABEL, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cv2.putText(frame, self.LABEL, (x1 + (self.WIDTH - tw) // 2, y1 + (self.HEIGHT + th) // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    def on_mouse(self, event, x, y, flags, param) -> None:
        x1, y1, x2, y2 = self.rect
        inside = x1 <= x <= x2 and y1 <= y <= y2
        if event == cv2.EVENT_MOUSEMOVE:
            self.hover = inside
        elif event == cv2.EVENT_LBUTTONUP and inside:
            self.pressed = True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scare cats away with a sound when they approach the camera.")
    p.add_argument("--camera", default="0",
                   help="Camera index (0, 1, ...) or video/RTSP URL (default: 0)")
    p.add_argument("--sound", type=Path, default=None,
                   help="Audio file to play (wav/mp3/ogg...). Default: generated sweep")
    p.add_argument("--volume", type=int, default=100, help="Playback volume 0-100 (default: 100)")
    p.add_argument("--output", choices=["speakers", "default"], default="speakers",
                   help="'speakers' plays the alarm on the built-in speakers even with headphones "
                        "plugged in; 'default' uses the system's current output (default: speakers)")
    p.add_argument("--audio-device", default=None,
                   help="Output device index or name fragment (e.g. 'Altavoces'); overrides "
                        "--output. See --list-audio-devices")
    p.add_argument("--list-audio-devices", action="store_true",
                   help="List audio output devices and exit")
    p.add_argument("--speaker-volume", type=int, default=100,
                   help="[Linux] Built-in speaker level 0-100 while the alarm plays (default: 100)")
    p.add_argument("--keep-headphones", action="store_true",
                   help="[Linux] Don't mute the headphones while the alarm plays on the speakers")
    p.add_argument("--alsa-card", default="0",
                   help="[Linux] ALSA card for --output speakers (default: 0)")
    p.add_argument("--confidence", type=float, default=0.60,
                   help="Minimum detection confidence 0-1 (default: 0.60)")
    p.add_argument("--min-area", type=float, default=0.02,
                   help="Min fraction of the frame the cat must cover to count as 'close' "
                        "(0-1, default: 0.02). Raise it to trigger only when the cat is nearer.")
    p.add_argument("--confirm-seconds", type=float, default=1.5,
                   help="The cat must be seen continuously for this long before triggering "
                        "(default: 1.5)")
    p.add_argument("--confirm-frames", type=int, default=5,
                   help="...and in at least this many consecutive frames (default: 5)")
    p.add_argument("--cooldown", type=float, default=10.0,
                   help="Seconds between sound triggers (default: 10)")
    p.add_argument("--model", default="yolov8s.pt",
                   help="YOLO model (yolov8s.pt default; yolov8n.pt faster, yolov8m.pt stricter "
                        "but slower)")
    p.add_argument("--threads", type=int, default=4,
                   help="CPU threads for inference (default: 4)")
    p.add_argument("--imgsz", type=int, default=640, help="Inference image size (default: 640)")
    p.add_argument("--snapshots", type=Path, default=None,
                   help="Folder to save a photo each time the alarm fires")
    p.add_argument("--no-window", action="store_true", help="Run headless (no preview window)")
    p.add_argument("--email-config", type=Path, default=DEFAULT_EMAIL_CONFIG,
                   help="INI file with SMTP settings; email is enabled when it exists "
                        "(default: email.ini next to this script, see email.example.ini)")
    p.add_argument("--email-to", default=None,
                   help="Recipients (comma separated), overrides 'to' in the config")
    p.add_argument("--email-cooldown", type=float, default=None,
                   help="Minimum seconds between emails (default: config value, or 60)")
    p.add_argument("--no-email", action="store_true", help="Disable email notifications")
    p.add_argument("--test-email", action="store_true",
                   help="Send a test email with a camera snapshot and exit")
    return p.parse_args()


def open_camera(source: str) -> cv2.VideoCapture:
    if source.isdigit():
        # DirectShow opens webcams much faster than the default MSMF backend on Windows
        backend = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_ANY
        cap = cv2.VideoCapture(int(source), backend)
    else:
        cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"Could not open camera '{source}'")
    return cap


def build_player(args: argparse.Namespace, sound: Path) -> SoundPlayer:
    """Pick the output device / speaker routing for the current platform."""
    sd = load_sounddevice()
    device = None
    router = None

    if args.audio_device:
        if not sd:
            raise SystemExit("--audio-device needs sounddevice: pip install sounddevice soundfile")
        device = find_output_device(sd, args.audio_device)
        if device is None:
            raise SystemExit(f"No output device matches '{args.audio_device}' "
                             "(see --list-audio-devices)")
    elif args.output == "speakers":
        if sys.platform.startswith("linux") and shutil.which("amixer"):
            # Speakers and headphone jack share one output: toggle the mixer instead
            router = SpeakerRouter(args.alsa_card, args.speaker_volume,
                                   not args.keep_headphones)
        elif sd:
            device = next((idx for kw in SPEAKER_KEYWORDS
                           if (idx := find_output_device(sd, kw)) is not None), None)
            if device is None:
                print("No speaker device found by name; using the default output. "
                      "Use --audio-device to choose one.")
        else:
            print("Can't select speakers without sounddevice; using the default output.")

    player = SoundPlayer(sound, args.volume, device, router)
    if device is not None:
        where = sd.query_devices(device)["name"]
    elif router:
        where = "built-in speakers (ALSA mixer)"
    else:
        where = "default output"
    print(f"Audio: {player.backend} -> {where}")
    return player


def build_notifier(args: argparse.Namespace) -> EmailNotifier | None:
    if args.no_email:
        return None
    if not args.email_config.exists():
        if args.email_config != DEFAULT_EMAIL_CONFIG:
            raise SystemExit(f"Email config not found: {args.email_config}")
        print("Email: off (no email.ini)")
        return None
    to = [a.strip() for a in args.email_to.split(",")] if args.email_to else None
    try:
        notifier = EmailNotifier(args.email_config, to, args.email_cooldown)
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"Invalid email config: {exc}")
    print(f"Email: on -> {', '.join(notifier.to)} (every {notifier.cooldown:g}s max)")
    return notifier


def main() -> None:
    args = parse_args()

    if args.list_audio_devices:
        sd = load_sounddevice()
        if not sd:
            raise SystemExit("sounddevice not available: pip install sounddevice soundfile")
        default_out = sd.default.device[1]
        for idx, name, api in list_output_devices(sd):
            mark = "*" if idx == default_out else " "
            print(f"{mark} {idx:3d}  {name}  [{api}]")
        return

    notifier = build_notifier(args)
    if args.test_email:
        if not notifier:
            raise SystemExit(f"Email not configured: create {args.email_config} "
                             "(copy email.example.ini)")
        cap = open_camera(args.camera)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            raise SystemExit("Could not grab a frame from the camera")
        jpg = cv2.imencode(".jpg", frame)[1].tobytes()
        print(f"Sending test email to {', '.join(notifier.to)}...")
        try:
            notifier.send(jpg, datetime.now())
        except Exception as exc:
            raise SystemExit(f"Email failed: {exc}")
        print("Test email sent.")
        return

    sound = args.sound
    if sound is None:
        sound = DEFAULT_SOUND
        if not sound.exists():
            generate_default_sound(sound)
    if not sound.exists():
        raise SystemExit(f"Sound file not found: {sound}")
    player = build_player(args, sound)

    if args.snapshots:
        args.snapshots.mkdir(parents=True, exist_ok=True)

    torch.set_num_threads(args.threads)
    model = YOLO(args.model)
    cap = open_camera(args.camera)
    stop_button = StopButton()
    window_ready = False

    streak = 0
    streak_start = 0.0
    last_trigger = 0.0
    print(f"Watching camera {args.camera} — sound: {sound}. Click DETENER, press 'q'/Esc or close the window to quit.")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Frame grab failed, retrying...")
                time.sleep(0.5)
                continue

            h, w = frame.shape[:2]
            # Detect every class with class-agnostic NMS, so a box only counts as a cat
            # when "cat" beats look-alikes (dog, teddy bear...) for that same region.
            result = model.predict(frame, conf=args.confidence, imgsz=args.imgsz,
                                   agnostic_nms=True, verbose=False)[0]

            close_cat = False
            for box, conf, cls in zip(result.boxes.xyxy.tolist(), result.boxes.conf.tolist(),
                                      result.boxes.cls.tolist()):
                if int(cls) != COCO_CAT_CLASS:
                    continue
                x1, y1, x2, y2 = map(int, box)
                area = (x2 - x1) * (y2 - y1) / (w * h)
                is_close = area >= args.min_area
                close_cat |= is_close
                color = (0, 0, 255) if is_close else (0, 200, 255)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, f"cat {conf:.2f} area {area:.1%}", (x1, max(y1 - 8, 15)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            now = time.monotonic()
            if close_cat:
                if streak == 0:
                    streak_start = now
                streak += 1
            else:
                streak = 0
            confirmed = (streak >= args.confirm_frames
                         and now - streak_start >= args.confirm_seconds)
            if confirmed and now - last_trigger >= args.cooldown:
                last_trigger = now
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{stamp}] Cat detected! Playing sound.")
                player.play()
                if args.snapshots:
                    name = datetime.now().strftime("cat_%Y%m%d_%H%M%S.jpg")
                    cv2.imwrite(str(args.snapshots / name), frame)
                if notifier:
                    ok_jpg, jpg = cv2.imencode(".jpg", frame)
                    if ok_jpg and notifier.notify(jpg.tobytes(), datetime.now()):
                        print(f"Sending email to {', '.join(notifier.to)}...")

            if not args.no_window:
                status = "CAT!" if close_cat else "watching"
                cv2.putText(frame, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1,
                            (0, 0, 255) if close_cat else (0, 255, 0), 2)
                stop_button.draw(frame)
                cv2.imshow(WINDOW_NAME, frame)
                if not window_ready:
                    cv2.setMouseCallback(WINDOW_NAME, stop_button.on_mouse)
                    window_ready = True
                key = cv2.waitKey(1) & 0xFF
                window_closed = cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1
                if key in (ord("q"), 27) or stop_button.pressed or window_closed:
                    print("Stopped by user.")
                    break
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()
        cap.release()
        cv2.destroyAllWindows()
        if notifier:
            notifier.wait()  # let an in-flight email finish


if __name__ == "__main__":
    main()
