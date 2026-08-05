"""Kleines Fenster zum Vorlesen von Skripten mit mehreren Stimmen.

Format im Textfeld, eine Zeile je Einsatz:

    #matthias_krieger: "Der Turm steht offen."
    #matthias_magier: Weisst du, was das bedeutet?
    Und das hier spricht weiter der Magier.

Die Anfuehrungszeichen sind optional. Eine Zeile ohne Praefix erbt den
Sprecher der Zeile darueber; ganz am Anfang gilt die links ausgewaehlte
Stimme. Leerzeilen und Zeilen ab '//' werden uebersprungen.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

from .cli import request
from .protocol import read_frame
from .voices import VoiceError, close_voice, default_voices_dir, load_voice

SPRECHERPAUSE_MS = 300      # Luft zwischen zwei Einsaetzen


def stille(kopf: dict) -> bytes:
    return bytes(int(kopf["sample_rate"] * SPRECHERPAUSE_MS / 1000) * 2 * kopf["channels"])


@dataclass(frozen=True)
class Einsatz:
    stimme: str
    text: str


def parse_skript(quelle: str, standard: str) -> list[Einsatz]:
    """Zerlegt das Textfeld in Einsaetze. Reine Textverarbeitung, kein Netz."""
    einsaetze: list[Einsatz] = []
    aktuell = standard
    for zeile in quelle.splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("//"):
            continue
        if zeile.startswith("#") and ":" in zeile:
            kopf, rest = zeile[1:].split(":", 1)
            kopf = kopf.strip()
            if kopf:
                aktuell = kopf
                zeile = rest.strip()
        if len(zeile) >= 2 and zeile[0] == zeile[-1] and zeile[0] in "\"'":
            zeile = zeile[1:-1].strip()
        if zeile:
            einsaetze.append(Einsatz(aktuell, zeile))
    return einsaetze


def verfuegbare_stimmen() -> list[str]:
    root = default_voices_dir()
    namen = []
    try:
        eintraege = sorted(eintrag.name for eintrag in root.iterdir() if eintrag.is_dir())
    except FileNotFoundError:
        return []
    for name in eintraege:
        try:
            profil = load_voice(name, root)
        except VoiceError:
            continue
        close_voice(profil)
        namen.append(name)
    return namen


class Abgebrochen(Exception):
    """Der Nutzer hat Stopp gedrueckt."""


def sprich(einsatz: Einsatz, mode: str, senke, abbruch: threading.Event | None = None) -> None:
    """Holt einen Einsatz vom Dienst und schiebt jeden Block sofort in die Senke."""
    antwort = request("POST", "/speak", {"text": einsatz.text, "voice": einsatz.stimme,
                                         "mode": mode})
    if antwort.status != 200:
        fehler = json.loads(antwort.read())
        raise RuntimeError(f"{fehler.get('reason')}: {fehler.get('message')}")
    try:
        art, nutzlast = read_frame(antwort)
        if art != "H":
            raise RuntimeError("Kopfrahmen fehlt")
        kopf = json.loads(nutzlast)
        while True:
            if abbruch is not None and abbruch.is_set():
                # Verbindung zumachen statt austrudeln lassen: der Worker sieht
                # den Abbruch am geschlossenen Socket und bricht die laufende
                # Erzeugung ab, statt sie fuer niemanden zu Ende zu rechnen.
                raise Abgebrochen
            art, nutzlast = read_frame(antwort)
            if art == "A":
                senke(kopf, nutzlast)
            elif art == "E":
                ende = json.loads(nutzlast)
                if ende.get("status") != "ok":
                    raise RuntimeError(f"{ende.get('reason')}: {ende.get('message')}")
                return
    finally:
        verbindung = getattr(antwort, "_mimic_connection", None)
        if verbindung is not None:
            verbindung.close()


class Wiedergabe:
    """Ein pw-cat fuer das ganze Skript.

    Ein Prozess je Einsatz waere einfacher, kostet aber jedes Mal Startzeit
    und macht eine hoerbare Luecke zwischen den Sprechern. Der Rahmenkopf
    liefert die Rate; sie ist ueber alle Stimmen gleich, also reicht einer.
    """

    def __init__(self) -> None:
        self.prozess: subprocess.Popen | None = None
        self.kopf: dict | None = None

    def __call__(self, kopf: dict, pcm: bytes) -> None:
        if self.prozess is None:
            self.kopf = kopf
            self.prozess = subprocess.Popen(
                ["pw-cat", "--playback", "--raw", "--rate", str(kopf["sample_rate"]),
                 "--channels", str(kopf["channels"]), "--format", "s16", "-"],
                stdin=subprocess.PIPE, bufsize=0)
        assert self.prozess.stdin is not None
        self.prozess.stdin.write(pcm)

    def pause(self) -> None:
        if self.prozess is not None and self.kopf is not None:
            self(self.kopf, stille(self.kopf))

    def schliessen(self) -> None:
        if self.prozess is not None and self.prozess.stdin:
            self.prozess.stdin.close()
            self.prozess.wait()
            self.prozess = None

    def abbrechen(self) -> None:
        # Hartes Ende, kein geordnetes Schliessen: pw-cat wuerde seinen Puffer
        # sonst noch ausspielen, und Stopp soll sofort still sein.
        if self.prozess is not None:
            self.prozess.kill()
            self.prozess.wait()
            self.prozess = None


class Sammler:
    """Sammelt PCM fuer das Speichern; schreibt erst am Ende eine WAV."""

    def __init__(self) -> None:
        self.bloecke: list[bytes] = []
        self.kopf: dict | None = None

    def __call__(self, kopf: dict, pcm: bytes) -> None:
        self.kopf = kopf
        self.bloecke.append(pcm)

    def pause(self) -> None:
        if self.bloecke and self.kopf is not None:
            self.bloecke.append(stille(self.kopf))

    def schreiben(self, ziel: Path) -> None:
        if not self.bloecke or self.kopf is None:
            raise RuntimeError("nichts erzeugt")
        vorlaeufig = ziel.with_suffix(ziel.suffix + ".tmp")
        with wave.open(str(vorlaeufig), "wb") as ausgabe:
            ausgabe.setnchannels(self.kopf["channels"])
            ausgabe.setsampwidth(2)
            ausgabe.setframerate(self.kopf["sample_rate"])
            ausgabe.writeframes(b"".join(self.bloecke))
        vorlaeufig.replace(ziel)


def main() -> int:
    import tkinter as tk
    from tkinter import filedialog, ttk

    meldungen: queue.Queue[tuple[str, str]] = queue.Queue()
    abbruch = threading.Event()

    wurzel = tk.Tk()
    wurzel.title("Mimic")
    wurzel.geometry("820x460")
    wurzel.columnconfigure(1, weight=1)
    wurzel.rowconfigure(0, weight=1)

    links = ttk.Frame(wurzel, padding=8)
    links.grid(row=0, column=0, sticky="ns")
    ttk.Label(links, text="Stimmen").pack(anchor="w")
    liste = tk.Listbox(links, width=22, exportselection=False)
    liste.pack(fill="y", expand=True)

    rechts = ttk.Frame(wurzel, padding=8)
    rechts.grid(row=0, column=1, sticky="nsew")
    rechts.rowconfigure(1, weight=1)
    rechts.columnconfigure(0, weight=1)
    ttk.Label(rechts, text='Skript   —   #stimme: "Text"   je Zeile').grid(row=0, column=0, sticky="w")
    feld = tk.Text(rechts, wrap="word", undo=True, height=12)
    feld.grid(row=1, column=0, sticky="nsew", pady=(4, 8))

    mode_wahl = tk.StringVar(value="mf")
    knoepfe = ttk.Frame(rechts)
    knoepfe.grid(row=2, column=0, sticky="ew")
    zustand = ttk.Label(rechts, text="bereit", anchor="w")
    zustand.grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def stimmen_laden() -> None:
        liste.delete(0, tk.END)
        for name in verfuegbare_stimmen():
            liste.insert(tk.END, name)
        if liste.size():
            liste.selection_set(0)

    def gewaehlt() -> str:
        auswahl = liste.curselection()
        return liste.get(auswahl[0]) if auswahl else ""

    def einfuegen(_ereignis=None) -> str:
        # Doppelklick setzt den Sprecherkopf -- schneller als tippen.
        if gewaehlt():
            feld.insert("insert", f'#{gewaehlt()}: ""\n')
            feld.mark_set("insert", "insert -3c")
            feld.focus_set()
        return "break"

    liste.bind("<Double-Button-1>", einfuegen)

    def lauf(ziel: Path | None) -> None:
        einsaetze = parse_skript(feld.get("1.0", tk.END), gewaehlt())
        if not einsaetze:
            meldungen.put(("fertig", "nichts zu sprechen"))
            return
        bekannt = set(verfuegbare_stimmen())
        unbekannt = {e.stimme for e in einsaetze} - bekannt
        if unbekannt:
            meldungen.put(("fertig", "unbekannte Stimme: " + ", ".join(sorted(unbekannt))))
            return
        senke = Sammler() if ziel else Wiedergabe()
        modus = mode_wahl.get()
        try:
            for nummer, einsatz in enumerate(einsaetze, 1):
                meldungen.put(("lauf", f"{nummer}/{len(einsaetze)}  {einsatz.stimme}  [{modus}]"))
                sprich(einsatz, modus, senke, abbruch)
                if nummer < len(einsaetze):
                    senke.pause()
            if isinstance(senke, Sammler):
                senke.schreiben(ziel)  # type: ignore[arg-type]
                meldungen.put(("fertig", f"gespeichert: {ziel}"))
            else:
                senke.schliessen()
                meldungen.put(("fertig", f"{len(einsaetze)} Einsaetze gesprochen"))
        except Abgebrochen:
            if isinstance(senke, Wiedergabe):
                senke.abbrechen()
            meldungen.put(("fertig", "abgebrochen"))
        except Exception as fehler:
            if isinstance(senke, Wiedergabe):
                senke.abbrechen()
            meldungen.put(("fertig", f"Fehler: {fehler}"))

    def starten(ziel: Path | None) -> None:
        abbruch.clear()
        for knopf in (abspielen, speichern):
            knopf.state(["disabled"])
        stopp.state(["!disabled"])
        threading.Thread(target=lauf, args=(ziel,), daemon=True).start()

    def speichern_klick() -> None:
        pfad = filedialog.asksaveasfilename(defaultextension=".wav",
                                            filetypes=[("WAV", "*.wav")])
        if pfad:
            starten(Path(pfad))

    abspielen = ttk.Button(knoepfe, text="Abspielen", command=lambda: starten(None))
    abspielen.pack(side="left")
    speichern = ttk.Button(knoepfe, text="Als WAV speichern", command=speichern_klick)
    speichern.pack(side="left", padx=6)
    stopp = ttk.Button(knoepfe, text="Stopp", command=abbruch.set)
    stopp.pack(side="left")
    stopp.state(["disabled"])
    ttk.Button(knoepfe, text="Stimmen neu lesen", command=stimmen_laden).pack(side="left", padx=6)
    # mf ist Realtime und die Vorgabe, soar rechnet laenger und ist fuer
    # gespeicherte Dateien die bessere Wahl.
    ttk.Label(knoepfe, text="Modus").pack(side="left", padx=(12, 4))
    ttk.Combobox(knoepfe, textvariable=mode_wahl, values=("mf", "soar"),
                 state="readonly", width=6).pack(side="left")

    def pumpe() -> None:
        try:
            while True:
                art, text = meldungen.get_nowait()
                zustand.config(text=text)
                if art == "fertig":
                    for knopf in (abspielen, speichern):
                        knopf.state(["!disabled"])
                    stopp.state(["disabled"])
        except queue.Empty:
            pass
        wurzel.after(60, pumpe)

    stimmen_laden()
    if liste.size():
        feld.insert("1.0", f'#{liste.get(0)}: "Der Turm steht offen, und niemand bewacht ihn."\n')
    pumpe()
    wurzel.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
