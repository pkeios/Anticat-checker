"""Startup window to pick the camera and the alarm output before monitoring starts."""

import base64
import configparser
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

import cv2

MAX_CAMERAS = 5
PREVIEW_SIZE = (320, 240)
DEFAULT_OUTPUT_LABEL = "Salida predeterminada de Windows"


def probe_cameras(backend: int) -> list[tuple[int, bytes]]:
    """Return (index, PNG thumbnail) for each camera index that delivers frames."""
    found = []
    for idx in range(MAX_CAMERAS):
        cap = cv2.VideoCapture(idx, backend)
        frame = None
        if cap.isOpened():
            for _ in range(5):  # the first frames are often black while the camera warms up
                ok, img = cap.read()
                if ok:
                    frame = img
        cap.release()
        if frame is not None:
            thumb = cv2.resize(frame, PREVIEW_SIZE)
            found.append((idx, cv2.imencode(".png", thumb)[1].tobytes()))
    return found


def load_settings(path: Path) -> dict[str, str]:
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return dict(cfg["anticat"]) if cfg.has_section("anticat") else {}


def save_settings(path: Path, camera: int, audio: str) -> None:
    cfg = configparser.ConfigParser()
    cfg["anticat"] = {"camera": str(camera), "audio": audio}
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)


def choose_settings(outputs: list[tuple[int, str]], play_test: Callable[[int | None], None],
                    settings_path: Path, camera_backend: int) -> tuple[str, int | None] | None:
    """Show the startup window and return (camera, audio device), or None if it was closed.

    outputs lists (device index, name) of the audio outputs; the returned audio device is
    None for the system default output. play_test(device) plays the alarm once and raises
    if that output can't be opened. The choice is remembered in settings_path.
    """
    saved = load_settings(settings_path)
    cameras: list[tuple[int, tk.PhotoImage]] = []
    probed: list[list[tuple[int, bytes]]] = []
    result = None

    root = tk.Tk()
    root.title("anticat")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid()

    ttk.Label(frm, text="Cámara").grid(row=0, column=0, sticky="w")
    camera_box = ttk.Combobox(frm, state="readonly", width=42)
    camera_box.set("Buscando cámaras...")
    camera_box.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 8))
    preview = ttk.Label(frm)
    preview.grid(row=2, column=0, columnspan=2, pady=(0, 12))

    ttk.Label(frm, text="Salida de la alarma").grid(row=3, column=0, sticky="w")
    audio_labels = [DEFAULT_OUTPUT_LABEL] + [name for _, name in outputs]
    audio_box = ttk.Combobox(frm, state="readonly", width=30, values=audio_labels)
    audio_box.set(saved["audio"] if saved.get("audio") in audio_labels else DEFAULT_OUTPUT_LABEL)
    audio_box.grid(row=4, column=0, sticky="we", pady=(2, 16))

    def selected_device() -> int | None:
        i = audio_box.current()
        return None if i <= 0 else outputs[i - 1][0]

    def test_sound() -> None:
        try:
            play_test(selected_device())
        except Exception as exc:
            messagebox.showerror("anticat", f"No se pudo reproducir el sonido en esa salida:\n{exc}",
                                 parent=root)

    def show_preview(_event=None) -> None:
        preview.configure(image=cameras[camera_box.current()][1])

    def start() -> None:
        nonlocal result
        camera = cameras[camera_box.current()][0]
        save_settings(settings_path, camera, audio_box.get())
        result = (str(camera), selected_device())
        root.destroy()

    ttk.Button(frm, text="Probar sonido", command=test_sound).grid(row=4, column=1, padx=(8, 0),
                                                                  pady=(2, 16))
    start_button = ttk.Button(frm, text="Iniciar", command=start, state="disabled")
    start_button.grid(row=5, column=0, columnspan=2, sticky="we")
    camera_box.bind("<<ComboboxSelected>>", show_preview)

    # Opening cameras takes a while, so probe them without freezing the window
    probe = threading.Thread(target=lambda: probed.append(probe_cameras(camera_backend)),
                             daemon=True)

    def fill_cameras() -> None:
        if probe.is_alive():
            root.after(100, fill_cameras)
            return
        found = probed[0] if probed else []
        if not found:
            camera_box.set("No se encontró ninguna cámara")
            return
        cameras.extend((idx, tk.PhotoImage(data=base64.b64encode(png))) for idx, png in found)
        camera_box["values"] = [f"Cámara {idx}" for idx, _ in cameras]
        indexes = [str(idx) for idx, _ in cameras]
        camera_box.current(indexes.index(saved["camera"]) if saved.get("camera") in indexes else 0)
        show_preview()
        start_button.state(["!disabled"])

    probe.start()
    root.after(100, fill_cameras)
    root.mainloop()
    return result
