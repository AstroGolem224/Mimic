"""Duenner Unix-Socket-Client; Modellwissen bleibt ausschliesslich im Worker."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import wave
from pathlib import Path

from .entwurf import MOTOREN, VORGABE_MOTOR
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


def _umask() -> int:
    """Die eigene umask lesen, ohne sie zu veraendern -- os.umask kann nur setzen."""
    aktuell = os.umask(0o022)
    os.umask(aktuell)
    return aktuell


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
            # Exklusiv angelegt statt fester Name: `ausgabe.wav.tmp` haette
            # eine gleichnamige fremde Datei truncierend geoeffnet und sie im
            # finally-Zweig anschliessend geloescht.
            handle, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                            dir=destination.parent)
            os.close(handle)
            temporary = Path(name)
            # mkstemp legt mit 0600 an, und os.replace vererbt das an die
            # Zieldatei. Vorher entstand sie ueber die umask, also meist 0644.
            # Ohne diese Zeile aendert ein Fehlerfix stillschweigend die Rechte
            # jeder exportierten WAV -- eine Ausgabedatei ist kein Geheimnis.
            os.chmod(temporary, 0o666 & ~_umask())
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
        try:
            dauer = _dauer(Path(profile.wav_path))
        except (OSError, wave.Error):
            close_voice(profile)
            print(name)
            continue
        close_voice(profile)
        # 8-15 s ist der einzige gemessene Bereich (siehe charaktere.py).
        print(f"{name:24} {dauer:5.1f} s{'' if 8 <= dauer <= 15 else '  !'}")
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


def profil_aus_datei(name: str, quelle: Path, text: str, force: bool = False) -> tuple[float, str]:
    """Legt ein Profil aus einer fertigen Audiodatei an. Gibt (Dauer, Hinweis) zurueck.

    ffmpeg macht daraus die 48-kHz-Mono-WAV, die load_voice verlangt -- damit ist
    das Eingangsformat egal (mp3, opus, Stereo, 44.1 kHz). Ansonsten derselbe
    Pfad wie `record`: dieselben Grenzen, dieselben Rechte, dieselbe Endpruefung.

    Herausgeloest aus `importieren`, weil der Entwurfsreiter der GUI denselben
    Weg braucht -- die Rechte- und Grenzlogik darf es kein zweites Mal geben.
    Alles Schiefe kommt als VoiceError, damit beide Aufrufer es gleich melden.
    """
    if not VOICE_RE.fullmatch(name):
        raise VoiceError("unknown_voice", "Name nur a-z, 0-9, _ und -, max. 32 Zeichen")
    if not text:
        raise VoiceError("invalid_voice_profile", f"kein Referenztext fuer {name!r}")
    if not quelle.is_file():
        raise VoiceError("invalid_voice_profile", f"{quelle} gibt es nicht")

    root = default_voices_dir()
    profil = root / name
    if (profil / "ref.wav").exists() and not force:
        raise VoiceError("invalid_voice_profile", f"{name!r} existiert schon")

    profil_anlegen(profil)
    umgewandelt = profil / "ref.wav.tmp"   # gleiches Dateisystem, sonst EXDEV in speichern()
    try:
        wandlung = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(quelle),
             # -f wav explizit: die Endung ist .tmp, daraus raet ffmpeg nichts.
             "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "wav", str(umgewandelt)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if wandlung.returncode != 0:
            raise VoiceError("invalid_voice_profile", f"ffmpeg konnte {quelle.name} nicht wandeln: "
                             f"{wandlung.stderr.decode(errors='replace').strip()}")
        umgewandelt.chmod(0o600)
        dauer = _dauer(umgewandelt)
        if not 3 <= dauer <= 60:
            raise VoiceError("invalid_voice_profile", f"{dauer:.1f} s liegt ausserhalb 3-60 s -- "
                             "der Dienst wuerde das Profil ablehnen")
        # Kein Abbruch: der Dienst nimmt 3-60 s. Aber nur 8-15 s ist erprobt.
        hinweis = "" if 8 <= dauer <= 15 else f"{dauer:.1f} s weicht vom Ziel 10 s ab."
        speichern(profil, umgewandelt, text)
    except (OSError, wave.Error, subprocess.SubprocessError) as exc:
        raise VoiceError("invalid_voice_profile", f"Import fehlgeschlagen: {exc}") from exc
    finally:
        umgewandelt.unlink(missing_ok=True)
        if profil.is_dir() and not any(profil.iterdir()):  # kein Abbruch soll eine Bauruine hinterlassen
            profil.rmdir()

    close_voice(load_voice(name, root))   # Endpruefung durch dieselbe Instanz wie der Dienst
    return dauer, hinweis


def importieren(args: argparse.Namespace) -> int:
    """CLI-Huelle um profil_aus_datei: Namensauflösung, Ausgabe, Rueckgabewert."""
    from .charaktere import CHARAKTERE

    name = args.voice
    charakter = CHARAKTERE.get(name)
    text = args.text or (charakter.text if charakter else None)
    if not text:
        print(f"invalid_voice_profile: kein Referenztext fuer {name!r} -- --text angeben "
              f"oder einen bekannten Charakter nehmen: {', '.join(sorted(CHARAKTERE))}",
              file=sys.stderr)
        return 1
    quelle = Path(args.datei).expanduser()
    try:
        dauer, hinweis = profil_aus_datei(name, quelle, text, args.force)
    except VoiceError as exc:
        nachsatz = " -- --force zum Ueberschreiben" if "existiert schon" in exc.message else ""
        print(f"{exc.reason}: {exc.message}{nachsatz}", file=sys.stderr)
        return 1
    if hinweis:
        print(f"  Hinweis: {hinweis}")
    profil = default_voices_dir() / name
    print(f"  {dauer:.1f} s aus {quelle.name}\n  {profil}/ref.wav\n  {profil}/ref.txt\n"
          f"  Probe:  mimic say \"Test\" --voice {name}")
    return 0


def _vorspielen(pfad: Path) -> None:
    subprocess.run(["pw-cat", "-p", str(pfad)], check=False)


def _ja(frage: str) -> bool:
    try:
        return input(frage).strip().lower() in ("", "j", "ja", "y")
    except EOFError:
        return False


def design(args: argparse.Namespace) -> int:
    """Eine Stimme aus einer Beschreibung, ohne Aufnahme.

    Zwei Schritte: entwerfen, Profil anlegen. Die dritte Stufe von frueher --
    ein englischer Entwurf, den ein zweites Modell eindeutscht -- ist weg,
    seit beide Motoren selbst Deutsch koennen. Sie hat den Akzent ohnehin nur
    weitergereicht statt ihn zu tilgen.
    """
    import time

    from .entwurf import Entwurf, STANDARDTEXT, motor_holen, umgebung_da

    try:
        eintrag = motor_holen(args.motor)
    except ValueError as fehler:
        print(f"{fehler}", file=sys.stderr)
        return 1
    if not umgebung_da(eintrag.name):
        print(f"Umgebung fuer {eintrag.anzeige} fehlt -- einmal "
              f"`mimic setup --entwurf {eintrag.name}`", file=sys.stderr)
        return 1

    entwurf = Entwurf()
    try:
        entwurf.starten(args.beschreibung, args.text or STANDARDTEXT, args.anzahl,
                        eintrag.name)
    except (ValueError, RuntimeError) as fehler:
        print(f"{fehler}", file=sys.stderr)
        return 1

    print(f"Entwerfe {args.anzahl} Kandidaten. Beim ersten Mal laedt das Modell nach.")
    gesehen = 0
    while True:
        stand = entwurf.stand()
        for kandidat in stand["kandidaten"][gesehen:]:
            print(f"  {kandidat['nummer']}: {kandidat['dauer']} s")
        gesehen = len(stand["kandidaten"])
        if not stand["laeuft"]:
            break
        time.sleep(1.0)

    stand = entwurf.stand()
    if stand["fehler"]:
        print(f"Entwerfen fehlgeschlagen: {stand['fehler']}", file=sys.stderr)
        entwurf.schliessen()
        return 1
    if not stand["kandidaten"]:
        print("kein einziger Kandidat entstanden -- Beschreibung aendern und nochmal",
              file=sys.stderr)
        entwurf.schliessen()
        return 1

    try:
        gewaehlt = None
        for kandidat in stand["kandidaten"]:
            pfad = Path(kandidat["datei"])
            print(f"\nKandidat {kandidat['nummer']}")
            _vorspielen(pfad)
            if _ja("  behalten? [J/n] "):
                gewaehlt = pfad
                break
        if gewaehlt is None:
            print("keiner behalten, nichts angelegt")
            return 1

        # Der Probesatz wird woertlich das ref.txt: dots.tts bekommt Referenz
        # und Transkript als Paar, ein anderer Text dort macht den Klon kaputt.
        try:
            dauer, hinweis = profil_aus_datei(args.voice, gewaehlt,
                                              stand["text"], args.force)
        except VoiceError as exc:
            nachsatz = " -- --force zum Ueberschreiben" if "existiert schon" in exc.message else ""
            print(f"{exc.reason}: {exc.message}{nachsatz}", file=sys.stderr)
            return 1
    finally:
        # Raeumt Ordner und einen etwa noch laufenden Prozess weg, auch bei
        # Strg-C mitten im Hoeren.
        entwurf.schliessen()

    if hinweis:
        print(f"  Hinweis: {hinweis}")
    profil = default_voices_dir() / args.voice
    print(f"  {dauer:.1f} s\n  {profil}/ref.wav\n  {profil}/ref.txt\n"
          f"  Probe:  mimic say \"Test\" --voice {args.voice}")
    return 0


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


def setup(args: argparse.Namespace) -> int:
    # Hinter einem Schalter, nicht im Regelweg: die Generator-Umgebung laedt
    # mehrere GB und braucht Minuten, waehrend `mimic setup` sonst in Sekunden
    # durchlaeuft und gefahrlos zweimal aufgerufen werden kann.
    if getattr(args, "entwurf", None) is not None:
        from .entwurf import motor_holen, umgebung_bauen, umgebung_da

        # Ohne Argument beide Motoren, mit Argument nur den genannten. Beide
        # zusammen sind mehrere GB, aber wer entwerfen will, will meist
        # vergleichen koennen.
        gewuenscht = [args.entwurf] if args.entwurf else sorted(MOTOREN)
        for name in gewuenscht:
            try:
                eintrag = motor_holen(name)
            except ValueError as fehler:
                print(f"entwurf: {fehler}", file=sys.stderr)
                return 1
            if umgebung_da(eintrag.name):
                print(f"  {eintrag.name:12} steht schon")
                continue
            try:
                umgebung_bauen(eintrag.name)
            except (RuntimeError, OSError) as fehler:
                print(f"entwurf {eintrag.name}: {fehler}", file=sys.stderr)
                return 1
        return 0

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
    say_parser.add_argument("--voice", default="forge")
    say_parser.add_argument("--mode", choices=("mf", "soar"), default="soar")
    say_parser.add_argument("-o", "--output")
    say_parser.set_defaults(function=say)
    status_parser = commands.add_parser("status")
    status_parser.set_defaults(function=status)
    voices_parser = commands.add_parser("voices")
    voices_parser.set_defaults(function=voices)
    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument("--entwurf", nargs="?", const="", default=None,
                              metavar="MOTOR",
                              help="nur die Generator-Umgebungen bauen (mehrere GB, Minuten). "
                                   f"Ohne Angabe alle: {', '.join(sorted(MOTOREN))}")
    setup_parser.set_defaults(function=setup)
    gui_parser = commands.add_parser("gui")
    gui_parser.set_defaults(function=gui)
    record_parser = commands.add_parser("record")
    record_parser.add_argument("voice")
    record_parser.add_argument("--text", help="eigener Referenztext statt des Charaktertexts")
    record_parser.add_argument("--force", action="store_true", help="bestehendes Profil ersetzen")
    record_parser.set_defaults(function=record)
    import_parser = commands.add_parser("import")
    import_parser.add_argument("voice")
    import_parser.add_argument("datei", help="Audiodatei in beliebigem Format")
    import_parser.add_argument("--text", help="woertliches Transkript der Datei")
    import_parser.add_argument("--force", action="store_true", help="bestehendes Profil ersetzen")
    import_parser.set_defaults(function=importieren)
    design_parser = commands.add_parser("design")
    design_parser.add_argument("beschreibung",
                               help="englische Beschreibung der Stimme -- beide Motoren "
                                    "verstehen die Beschreibung englisch und sprechen "
                                    "den Probesatz deutsch")
    design_parser.add_argument("--voice", required=True, help="Name des neuen Profils")
    design_parser.add_argument("--motor", default=None,
                               help=f"{', '.join(sorted(MOTOREN))} (Vorgabe {VORGABE_MOTOR})")
    design_parser.add_argument("--text", default=None,
                               help="Probesatz; wird woertlich das ref.txt")
    design_parser.add_argument("--anzahl", type=int, default=3)
    design_parser.add_argument("--force", action="store_true")
    design_parser.set_defaults(function=design)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
