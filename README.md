# Anticat-checker

python checkea con la webcam si tu gato esta por hacer alguna maldad a tu computadora

Detects cats approaching the camera (YOLOv8, COCO "cat" class) and plays a deterrent sound.
Works on Windows and Linux.

## Setup

### Windows (PowerShell)
```powershell
py -3 -m venv .venv
.venv\Scripts\pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\pip install -r requirements.txt
```

### Linux
```bash
python3 -m venv .venv
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt
```
On Linux `sounddevice` also needs PortAudio (`sudo apt install libportaudio2`). Without it the
program falls back to `ffplay` / `mpv` / `aplay`.

## Usage
The YOLO model (`yolov8s.pt`, ~22 MB) is downloaded automatically on the first run.

Replace `.venv\Scripts\python` with `.venv/bin/python` on Linux.
```powershell
.venv\Scripts\python anticat.py                          # default camera + generated sound
.venv\Scripts\python anticat.py --sound my_sound.mp3     # custom sound (wav/mp3/ogg/flac)
.venv\Scripts\python anticat.py --list-audio-devices     # show audio outputs
.venv\Scripts\python anticat.py --audio-device "Altavoces"
.venv\Scripts\python anticat.py --min-area 0.08 --cooldown 20 --snapshots captures
.venv\Scripts\python anticat.py --help                   # all options
```
Click the **DETENER** button in the preview window, press `q`/`Esc`, or close the window to quit.

## Audio output
`--output speakers` (default) plays the alarm on the built-in speakers:
- **Windows**: picks the output device whose name contains "Speaker"/"Altavoces"/"Parlantes".
  If none matches it uses the default output; choose one explicitly with `--audio-device`.
- **Linux**: speakers and headphone jack usually share one output, so it temporarily unmutes
  the speakers (and mutes the headphones) via `amixer` while the alarm plays.

`--output default` uses whatever output the system is currently using.

## Email alerts
When the alarm fires, the snapshot can be emailed to one or more addresses.

1. Copy `email.example.ini` to `email.ini` (git-ignored) and fill in the SMTP account that sends
   the email and the recipients (`to`, comma separated).
2. Test it: `anticat.py --test-email` (sends a camera snapshot and exits).

Email is enabled automatically when `email.ini` exists. Options: `--email-to a@x.com,b@y.com`
(override recipients), `--email-cooldown 120`, `--email-config other.ini`, `--no-email`.
The password can be kept out of the file with the `ANTICAT_SMTP_PASSWORD` environment variable.

Outlook/Hotmail/Live personal accounts no longer allow SMTP with a password, so use e.g. a Gmail
account with an [App Password](https://myaccount.google.com/apppasswords) as the sender
(any address can receive).

## Detection strictness (defaults)
- Model `yolov8s.pt`, confidence >= 0.60, class-agnostic NMS (a box only counts if "cat" beats dog/teddy bear/etc.).
- The cat must be seen continuously for >= 1.5 s **and** >= 5 consecutive frames before the alarm fires.
- Tune with `--confidence`, `--confirm-seconds`, `--confirm-frames`, `--model`.

## Windows quick start
Double-click `anticat.bat`. The first run creates `.venv` and installs everything (needs Python 3.10+
installed with "Add python.exe to PATH"). Arguments are passed through, e.g.:
```bat
anticat.bat --sound miau.mp3 --snapshots capturas
```
