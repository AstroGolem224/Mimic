"""Duenner Unix-Socket-Client; Modellwissen bleibt ausschliesslich im Worker."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import textwrap
import wave
from pathlib import Path

from .frontend import UnixHTTPConnection, frontend_socket_path
from .protocol import read_frame
from .voices import VOICE_RE, VoiceError, close_voice, default_voices_dir, load_voice


def open_request(method: str, path: str, body: dict | None = None, *,
                 publish=None, cancelled=None) -> UnixHTTPConnection:
    conn = UnixHTTPConnection(frontend_socket_path(), 125, cancelled=cancelled)
    if publish is not None:
        publish(conn)
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {} if payload is None else {"Content-Type": "application/json",
                                          "Content-Length": str(len(payload))}
    conn.request(method, path, payload, headers)
    return conn


def request(method: str, path: str, body: dict | None = None) -> http.client.HTTPResponse:
    conn = open_request(method, path, body)
    response = conn.getresponse()
    response._mimic_connection = conn  # type: ignore[attr-defined]
    return response


def service_error(response: http.client.HTTPResponse) -> int:
    try:
        value = json.loads(response.read())
        print(f"{value.get('reason', 'worker_unavailable')}: {value.get('message', '')}", file=sys.stderr)
    except Exception:
        print(f"worker_unavailable: HTTP {response.status}", file=sys.stderr)
    return 1


def say(args: argparse.Namespace) -> int:
    try:
        response = request("POST", "/speak", {"text": args.text, "voice": args.voice, "mode": args.mode})
    except OSError as exc:
        print(f"worker_unavailable: {exc}", file=sys.stderr)
        return 1
    if response.status != 200:
        return service_error(response)
    kind, payload = read_frame(response)
    if kind != "H":
        print("worker_unavailable: Kopfrahmen fehlt", file=sys.stderr)
        return 1
    head = json.loads(payload)
    temporary: Path | None = None
    output = None
    player = None
    try:
        if args.output:
            destination = Path(args.output)
            temporary = Path(str(destination) + ".tmp")
            output = wave.open(str(temporary), "wb")
            output.setnchannels(head["channels"])
            output.setsampwidth(2)
            output.setframerate(head["sample_rate"])
        else:
            # "-" und --raw sind beide Pflicht: ohne "-" beendet sich pw-cat mit
            # "filename or - argument missing", ohne --raw sucht es in dem Strom
            # einen Container ("Format not recognised"). Wir schicken nackte PCM.
            # Beides endete beim Schreiben in EPIPE.
            player = subprocess.Popen(["pw-cat", "--playback", "--raw",
                                       "--rate", str(head["sample_rate"]),
                                       "--channels", str(head["channels"]), "--format", "s16", "-"],
                                      stdin=subprocess.PIPE, bufsize=0)
        while True:
            kind, payload = read_frame(response)
            if kind == "A":
                if output:
                    output.writeframesraw(payload)
                else:
                    assert player is not None and player.stdin is not None
                    player.stdin.write(payload)
            elif kind == "E":
                end = json.loads(payload)
                if end.get("status") != "ok":
                    print(f"{end.get('reason', 'worker_unavailable')}: {end.get('message', '')}",
                          file=sys.stderr)
                    return 1
                break
        if output:
            output.close()
            output = None
            os.replace(temporary, Path(args.output))
            temporary = None
        elif player and player.stdin:
            player.stdin.close()
            if player.wait() != 0:
                print("worker_unavailable: pw-cat ist fehlgeschlagen", file=sys.stderr)
                return 1
        return 0
    except (OSError, EOFError, ValueError, json.JSONDecodeError) as exc:
        print(f"worker_unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        if output:
            output.close()
        if temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if player and player.poll() is None:
            # Geordnetes Schliessen spielt PipeWire-Puffer noch aus. Bei Abbruch
            # muss die Wiedergabe deshalb hart enden, sonst hoert der Nutzer Reste.
            player.kill()
            player.wait()


def status(_args: argparse.Namespace) -> int:
    try:
        response = request("GET", "/status")
    except OSError as exc:
        print(f"worker_unavailable: {exc}", file=sys.stderr)
        return 1
    if response.status != 200:
        return service_error(response)
    print(json.dumps(json.loads(response.read()), ensure_ascii=False, indent=2))
    return 0


def voices(_args: argparse.Namespace) -> int:
    root = default_voices_dir()
    try:
        names = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
    except FileNotFoundError:
        names = []
    for name in names:
        try:
            profile = load_voice(name, root)
        except VoiceError:
            continue
        close_voice(profile)
        print(name)
    return 0


def _aufnehmen(ziel: Path) -> None:
    """pw-record bis Enter. Kein Timeout -- der Sprecher entscheidet, wann fertig."""
    input("    [Enter] = Aufnahme START ")
    recorder = subprocess.Popen(
        ["pw-record", "--rate", "48000", "--channels", "1", "--format", "s16", str(ziel)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        input("    ...laeuft. [Enter] = STOPP ")
    finally:
        recorder.terminate()
        recorder.wait(timeout=5)


def _dauer(pfad: Path) -> float:
    with wave.open(str(pfad), "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate else 0.0


def profil_anlegen(profil: Path) -> None:
    profil.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    profil.mkdir(mode=0o700, exist_ok=True)
    profil.chmod(0o700)


def speichern(profil: Path, aufnahme: Path, text: str) -> None:
    """Legt ref.wav/ref.txt mit den Rechten an, die der Dienst verlangt.

    load_voice lehnt alles ab, was nicht 0700/0600 ist -- hier ist die einzige
    Stelle, die das Profil schreibt, also wird es hier auch richtig gesetzt.
    `aufnahme` muss im selben Verzeichnis liegen: os.replace kann nicht ueber
    Dateisystemgrenzen, und /tmp ist hier tmpfs waehrend ~ auf btrfs liegt.
    """
    profil_anlegen(profil)
    ziel = profil / "ref.wav"
    os.replace(aufnahme, ziel)
    ziel.chmod(0o600)
    transkript = profil / "ref.txt"
    transkript.write_text(" ".join(text.split()) + "\n", encoding="utf-8")
    transkript.chmod(0o600)


def record(args: argparse.Namespace) -> int:
    from .charaktere import CHARAKTERE

    name = args.voice
    if not VOICE_RE.fullmatch(name):
        print("unknown_voice: Name nur a-z, 0-9, _ und -, max. 32 Zeichen", file=sys.stderr)
        return 1
    charakter = CHARAKTERE.get(name)
    text = args.text or (charakter.text if charakter else None)
    if not text:
        print(f"invalid_voice_profile: kein Referenztext fuer {name!r} -- --text angeben "
              f"oder einen bekannten Charakter nehmen: {', '.join(sorted(CHARAKTERE))}",
              file=sys.stderr)
        return 1

    root = default_voices_dir()
    profil = root / name
    if (profil / "ref.wav").exists() and not args.force:
        print(f"invalid_voice_profile: {name!r} existiert schon -- --force zum Ueberschreiben",
              file=sys.stderr)
        return 1

    print(f"\n{'=' * 72}\nSTIMME  {name}")
    if charakter:
        print(f"\nRegie:\n  {charakter.regie}")
    print("\nText:")
    print(textwrap.fill(" ".join(text.split()), 68, initial_indent="  » ", subsequent_indent="    "))
    print("\n  Ruhiger Raum, ~20 cm Abstand, am Stueck durchsprechen. Ziel 10 s.\n")

    # Direkt ins Profilverzeichnis: os.replace muss auf demselben Dateisystem
    # bleiben, sonst EXDEV. Die .tmp raeumt der finally-Zweig weg.
    profil_anlegen(profil)
    aufnahme = profil / "ref.wav.tmp"
    try:
        while True:
            _aufnehmen(aufnahme)
            aufnahme.chmod(0o600)
            dauer = _dauer(aufnahme)
            print(f"    {dauer:.1f} s aufgenommen, spiele ab...")
            wiedergabe = subprocess.run(["pw-cat", "--playback", str(aufnahme)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if wiedergabe.returncode != 0:
                print(f"    Wiedergabe fehlgeschlagen: "
                      f"{wiedergabe.stderr.decode(errors='replace').strip()}")
            if not 3 <= dauer <= 60:
                print(f"    {dauer:.1f} s liegt ausserhalb 3-60 s -- der Dienst wuerde das "
                      f"Profil ablehnen. Nochmal.")
                continue
            if not 8 <= dauer <= 15:
                # Nicht hart ablehnen: der Dienst nimmt 3-60 s. Aber ausserhalb
                # 8-15 s wird es unerprobt -- 30 s ergaben hoerbar schlechtere
                # Klone mit abgeschnittenen Saetzen.
                print(f"    Hinweis: {dauer:.1f} s weicht vom Ziel 10 s ab.")
            wahl = input("    [Enter] behalten  [n] nochmal  [a] abbrechen: ").strip().lower()
            if wahl == "n":
                continue
            if wahl == "a":
                return 1
            break
        speichern(profil, aufnahme, text)
    except (OSError, wave.Error, subprocess.SubprocessError) as exc:
        print(f"invalid_voice_profile: Aufnahme fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    finally:
        aufnahme.unlink(missing_ok=True)
        if not any(profil.iterdir()):   # Abbruch soll keine Bauruine hinterlassen
            profil.rmdir()

    try:
        profile = load_voice(name, root)
    except VoiceError as exc:
        print(f"{exc.reason}: {exc.message}", file=sys.stderr)
        return 1
    close_voice(profile)
    print(f"\n  {profil}/ref.wav\n  {profil}/ref.txt\n"
          f"  Probe:  mimic say \"Test\" --voice {name}")
    return 0


# mimic-worker.socket hat OnFailure=mimic-worker-reset.service -- die Reset-Unit
# muss also mit, sonst laeuft der Auffangmechanismus ins Leere.
UNITS = ("mimic.socket", "mimic.service",
         "mimic-worker.socket", "mimic-worker.service", "mimic-worker-reset.service")


def unit_quelle() -> Path | None:
    """Das systemd/-Verzeichnis des Repos -- neben dem Paket oder unter cwd."""
    for kandidat in (Path(__file__).resolve().parent.parent / "systemd", Path.cwd() / "systemd"):
        if all((kandidat / name).is_file() for name in UNITS):
            return kandidat
    return None


def install_units(quelle: Path, ziel: Path) -> list[tuple[str, str]]:
    ziel.mkdir(mode=0o700, parents=True, exist_ok=True)
    ergebnis = []
    for name in UNITS:
        inhalt = (quelle / name).read_bytes()
        pfad = ziel / name
        alt = pfad.read_bytes() if pfad.exists() else None
        if alt == inhalt:
            ergebnis.append((name, "unveraendert"))
            continue
        pfad.write_bytes(inhalt)
        pfad.chmod(0o644)
        ergebnis.append((name, "ersetzt" if alt is not None else "neu"))
    return ergebnis


def _systemctl(*argumente: str) -> int:
    lauf = subprocess.run(["systemctl", "--user", *argumente], capture_output=True, text=True)
    if lauf.returncode != 0:
        print(f"systemctl {' '.join(argumente)}: {lauf.stderr.strip()}", file=sys.stderr)
    return lauf.returncode


def setup(_args: argparse.Namespace) -> int:
    quelle = unit_quelle()
    if quelle is None:
        print("systemd/ nicht gefunden -- mimic setup im Repo-Verzeichnis aufrufen",
              file=sys.stderr)
        return 1
    # Die Units zeigen fest auf %h/.local/bin. Fehlen die Entry-Points, wuerde
    # enable --now klaglos durchlaufen und erst die erste Anfrage scheitern.
    fehlend = [name for name in ("mimic-frontend", "mimic-worker")
               if not (Path.home() / ".local" / "bin" / name).exists()]
    if fehlend:
        print(f"{', '.join(fehlend)} fehlt in ~/.local/bin -- dorthin zeigen die Units.\n"
              f"  uv tool install --python 3.12 {quelle.parent}", file=sys.stderr)
        return 1

    stimmen = default_voices_dir()
    stimmen.mkdir(mode=0o700, parents=True, exist_ok=True)
    stimmen.chmod(0o700)
    print(f"  stimmen      {stimmen}")

    zustaende = install_units(quelle, Path.home() / ".config" / "systemd" / "user")
    for name, zustand in zustaende:
        print(f"  {zustand:12} {name}")

    if _systemctl("daemon-reload"):
        return 1
    if _systemctl("enable", "--now", "mimic.socket", "mimic-worker.socket"):
        return 1
    # enable --now uebernimmt geaenderten Inhalt an einer schon laufenden Socket-Unit
    # nicht -- sonst horcht weiter die alte Fassung.
    if any(zustand == "ersetzt" for _, zustand in zustaende):
        if _systemctl("restart", "mimic.socket", "mimic-worker.socket"):
            return 1

    try:
        response = request("GET", "/status")
        erreichbar = response.status == 200
        response.read()
    except OSError:
        erreichbar = False
    print(f"  dienst       {'erreichbar' if erreichbar else 'NICHT erreichbar'}")
    return 0 if erreichbar else 1


def gui(_args: argparse.Namespace) -> int:
    # Erst hier importieren: das GUI zieht http.server und den Browserstart
    # nach, und die reine CLI soll das nicht bezahlen.
    from .gui import main as gui_main
    return gui_main()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mimic")
    commands = result.add_subparsers(required=True)
    say_parser = commands.add_parser("say")
    say_parser.add_argument("text")
    say_parser.add_argument("--voice", default="matthias")
    say_parser.add_argument("--mode", choices=("mf", "soar"), default="mf")
    say_parser.add_argument("-o", "--output")
    say_parser.set_defaults(function=say)
    status_parser = commands.add_parser("status")
    status_parser.set_defaults(function=status)
    voices_parser = commands.add_parser("voices")
    voices_parser.set_defaults(function=voices)
    setup_parser = commands.add_parser("setup")
    setup_parser.set_defaults(function=setup)
    gui_parser = commands.add_parser("gui")
    gui_parser.set_defaults(function=gui)
    record_parser = commands.add_parser("record")
    record_parser.add_argument("voice")
    record_parser.add_argument("--text", help="eigener Referenztext statt des Charaktertexts")
    record_parser.add_argument("--force", action="store_true", help="bestehendes Profil ersetzen")
    record_parser.set_defaults(function=record)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
