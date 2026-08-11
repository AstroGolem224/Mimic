#!/usr/bin/env python3
"""Fertigmeldung per Mimic, wenn Claude Code eine Aufgabe abschliesst.

Claude Code ruft dieses Skript als Stop-Hook auf und schiebt das Hook-JSON
ueber stdin herein. Das Skript zieht die letzte Assistentenantwort aus dem
Transkript, kuerzt sie auf einen sprechbaren Satz und laesst `mimic say` ihn
ueber die Standardsenke des Systems sprechen -- also ueber den
Bluetooth-Kopfhoerer, sobald der verbunden ist. Vorher laeuft
`tools/kopfhoerer.sh --sicherstellen`, das den Kopfhoerer bei Bedarf
zurueckholt und ohne hinterlegte MAC folgenlos durchlaeuft.

Drei Regeln bestimmen den Aufbau:

*Nie die Sitzung aufhalten.* Der Hook laeuft, waehrend Claude Code auf sein
Ende wartet. Gesprochen wird darum in einem abgekoppelten Prozess
(`start_new_session=True`); der Hook selbst ist nach wenigen Millisekunden
fertig, unabhaengig davon, wie lang der Satz ist.

*Nie die Sitzung kaputtmachen.* Kein Dienst, kaputtes Transkript, `mimic` gar
nicht installiert, PipeWire stumm -- nichts davon darf einen Rueckgabewert
!= 0 erzeugen, sonst sieht der Nutzer Hook-Fehler statt seiner Arbeit. Jeder
Pfad endet in 0.

*Nie zwei Stimmen gleichzeitig.* Mehrere Sitzungen teilen sich eine
Audioausgabe. Der sprechende Prozess haelt eine Sperre; wer sie nicht bekommt,
schweigt, statt sich in die laufende Ansage zu legen.

Aufruf von Hand:
    tools/ansage.py --sagen "Test"       # spricht sofort im Vordergrund
    tools/ansage.py --vorschau < h.json  # zeigt nur den Satz, spricht nicht

Stellschrauben ueber die Umgebung:
    MIMIC_ANSAGE_STIMME   Stimmprofil (Vorgabe: forge)
    MIMIC_ANSAGE_STILL=1  schaltet die Ansage ab, ohne den Hook auszubauen
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GRENZE = 650            # Zeichen; darueber wird die Ansage zum Vortrag
MINDEST = 150           # darunter lieber anschneiden als abbrechen
TAIL_BYTES = 1 << 20    # so weit wird ins Transkript zurueckgelesen
KOPFHOERER_FRIST_S = 20
SPRECH_FRIST_S = 180

FERTIG = "Fertig."
OHNE_INHALT = "Fertig. Keine Zusammenfassung im Transkript."


VORGABE_STIMME = "forge"        # Stimme der Standard-Persona


def stimmdatei() -> Path:
    # Eine Datei fuer alle Sitzungen -- zwei parallele Sitzungen mit
    # verschiedenen Personas teilen sich die Stimme. Bei Bedarf nach
    # session_id schluesseln, das Hook-JSON enthaelt sie.
    laufzeit = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(laufzeit) / "mimic-ansage.stimme"


def stimme() -> str:
    """Umgebung schlaegt Persona-Datei schlaegt Vorgabe.

    Die Datei schreiben die Persona-Skills beim Umschalten. Sie liegt im
    Laufzeitverzeichnis und ueberlebt keinen Neustart -- danach gilt wieder
    VORGABE_STIMME. Genau deshalb muss die Vorgabe die Standard-Persona sein.
    """
    aus_umgebung = os.environ.get("MIMIC_ANSAGE_STIMME", "").strip()
    if aus_umgebung:
        return aus_umgebung
    try:
        aus_datei = stimmdatei().read_text(encoding="utf-8").strip()
    except OSError:
        aus_datei = ""
    return aus_datei or VORGABE_STIMME


# ---------------------------------------------------------------- Text bauen

def _bloecke_zu_text(inhalt) -> str:
    """Nimmt `message.content` in beiden Formen: Zeichenkette oder Blockliste."""
    if isinstance(inhalt, str):
        return inhalt
    if not isinstance(inhalt, list):
        return ""
    teile = []
    for block in inhalt:
        # tool_use und thinking sind keine Antwort an den Nutzer und fliegen raus.
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                teile.append(text)
    return "\n".join(teile)


def letzte_antwort(pfad: Path) -> str:
    """Text der letzten Assistentennachricht aus dem JSONL-Transkript.

    Rueckwaerts gelesen und nur das Ende der Datei: eine lange Sitzung wird
    zweistellig megabyteschwer, und der Hook soll den Sitzungsabschluss nicht
    mit Einlesen bezahlen. Subagenten (`isSidechain`) zaehlen nicht -- ihre
    Antworten sieht der Nutzer nicht, sie waeren also eine Ansage ueber
    Arbeit, die im Hauptstrang gar nicht steht.
    """
    try:
        with open(pfad, "rb") as datei:
            groesse = datei.seek(0, os.SEEK_END)
            beginn = max(0, groesse - TAIL_BYTES)
            datei.seek(beginn)
            rohdaten = datei.read()
    except OSError:
        return ""
    zeilen = rohdaten.split(b"\n")
    if beginn:
        zeilen = zeilen[1:]     # erste Zeile ist angeschnitten
    for zeile in reversed(zeilen):
        if not zeile.strip():
            continue
        try:
            eintrag = json.loads(zeile)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(eintrag, dict) or eintrag.get("isSidechain"):
            continue
        if eintrag.get("type") != "assistant":
            continue
        nachricht = eintrag.get("message")
        if not isinstance(nachricht, dict):
            continue
        text = _bloecke_zu_text(nachricht.get("content")).strip()
        if text:                # reine Werkzeugzuege ueberspringen
            return text
    return ""


_CODEBLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
# Der Linktext ist hier fast immer der Dateiname -- ihn zu behalten hiesse,
# genau das vorzulesen, was nicht vorgelesen werden soll.
_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"<?https?://\S+>?")
# Alles in Backticks ist Bezeichner, Befehl oder Pfad. Die Stimme buchstabiert
# es, und das zerhackt den Sprachfluss mehr, als der Inhalt wert ist -- wer den
# genauen Namen braucht, liest ihn im Terminal nach.
_INLINE_CODE = re.compile(r"`[^`]*`")
# Dateiartig ohne Backticks: Pfade mit Schraegstrich oder Tilde und einzelne
# Dateinamen mit bekannter Endung, jeweils mit optionaler :Zeilennummer.
_DATEIARTIG = re.compile(
    r"(?<!\w)(?:~?[\w.@-]*/[\w./@-]+|[\w-]+\.(?:py|sh|md|json|jsonl|toml|txt|ya?ml"
    r"|html|css|js|ts|tsx|wav|mp3|service|socket))(?::\d+)?")
_AUSZEICHNUNG = re.compile(r"[`*_#>]+")
# Nach dem Streichen bleiben Luecken vor Satzzeichen und leere Klammerpaare.
_LUECKE_VOR_SATZZEICHEN = re.compile(r"\s+([,.;:!?])")
_LEERE_KLAMMER = re.compile(r"\(\s*\)|\[\s*\]")
# Ein Wort, das die Stimme als Wort spricht. Bleibt nach dem Streichen keines
# uebrig, war die Zeile nur ein Rahmen um einen Bezeichner ("Siehe:" plus Pfad).
_WORTHALTIG = re.compile(r"[^\W\d_]{3,}")
_AUFZAEHLUNG = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_PFADARTIG = re.compile(r"^\S+/\S+$")
# Doppelpunkt beendet hier bewusst KEINEN Satz: "Der Kern ist der:" und was
# danach kommt gehoeren zusammen, und als eigener Satz gezaehlt wuerde die
# Einleitung zum letzten, was der Nutzer hoert.
_SATZENDE = re.compile(r"(?<=[.!?])\s+")
_RANDZEICHEN = " ·—–-|,;:"


def _kappen(text: str, grenze: int) -> str:
    return text[:grenze].rsplit(" ", 1)[0] + " ..."


def zusammenfassen(text: str, grenze: int = GRENZE) -> str:
    """Aus einer Markdown-Antwort einen sprechbaren Satz machen.

    Gesprochen wird nur Prosa. Codebloecke, Tabellen, URLs, Trennlinien und
    alles Dateiartige liest keine Stimme sinnvoll vor, also fallen sie weg,
    bevor gekuerzt wird -- sonst besteht die Ansage aus dem Anfang eines Diffs,
    aus einer buchstabierten GitHub-Adresse oder aus Pfaden, die den Satzfluss
    zerhacken.
    """
    text = _CODEBLOCK.sub(" ", text)
    text = _LINK.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _DATEIARTIG.sub(" ", text)

    saetze: list[str] = []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile:
            continue
        if zeile.startswith("|") or set(zeile) <= set("-=_ "):
            continue            # Tabellenzeile oder Trennlinie
        zeile = _AUFZAEHLUNG.sub("", zeile)
        zeile = _AUSZEICHNUNG.sub("", zeile).strip()
        # Ein Doppelpunkt am Zeilenende kuendigt etwas an -- fast immer den
        # Codeblock, die Tabelle oder die Liste, die oben schon weggefallen
        # ist. Gesprochen bliebe davon ein Anlauf ins Leere. Weg faellt aber
        # nur die Ankuendigung, nicht die ganze Zeile: "Erledigt. Am PC:"
        # traegt einen fertigen Satz und erst danach den Anlauf.
        # Vor dem rstrip pruefen, das den Doppelpunkt sonst wegnaehme.
        if zeile.endswith(":"):
            schnitt = max(zeile.rfind(zeichen) for zeichen in ".!?")
            if schnitt < 0:
                continue
            zeile = zeile[:schnitt + 1]
        zeile = _LEERE_KLAMMER.sub(" ", zeile)
        zeile = _LUECKE_VOR_SATZZEICHEN.sub(r"\1", " ".join(zeile.split()))
        zeile = zeile.rstrip(_RANDZEICHEN).lstrip(_RANDZEICHEN)
        # Was von der Zeile bleibt, muss noch etwas aussagen: eine Zeile, die
        # nur aus einem Befehl bestand, ist jetzt leer oder ein Rumpf wie "in".
        if not _WORTHALTIG.search(zeile) or _PFADARTIG.match(zeile):
            continue
        saetze.append(zeile if zeile[-1] in ".!?" else zeile + ".")

    fluss = " ".join(" ".join(saetze).split())
    if not fluss:
        return ""
    if len(fluss) <= grenze:
        return fluss

    ergebnis = ""
    for satz in _SATZENDE.split(fluss):
        kandidat = f"{ergebnis} {satz}".strip()
        if len(kandidat) <= grenze:
            ergebnis = kandidat
            continue
        # Der Satz passt nicht mehr. Ihn einfach fallenzulassen ist nur dann
        # richtig, wenn vorher schon etwas Substanzielles gesagt wurde. Sonst
        # hoert der Nutzer einen Anlauf ohne Aussage -- "Gemerged." und Stille,
        # waehrend die eigentliche Meldung im naechsten, langen Satz steckt.
        # Dann lieber angeschnitten sprechen als gar nicht.
        if len(ergebnis) >= MINDEST:
            break
        ergebnis = _kappen(kandidat, grenze)
        break
    return ergebnis


def ansagetext(nutzlast: dict) -> str:
    """Der Satz, den Mimic sprechen soll -- aus dem Hook-JSON abgeleitet."""
    ereignis = nutzlast.get("hook_event_name")

    if ereignis == "Notification":
        meldung = str(nutzlast.get("message") or "").strip()
        return f"Claude wartet. {zusammenfassen(meldung)}".strip() if meldung else "Claude wartet."

    pfad = nutzlast.get("transcript_path")
    antwort = letzte_antwort(Path(pfad)) if isinstance(pfad, str) and pfad else ""
    stand = zusammenfassen(antwort)
    if not stand:
        return OHNE_INHALT
    return f"{FERTIG} {stand}"


# ------------------------------------------------------------------ Sprechen

def _protokoll(ereignis: str, text: str = "", **felder: object) -> None:
    """Eine Zeile je Ansage ins Laufzeitverzeichnis, fuer die Fehlersuche.

    Der Hook laeuft ohne Terminal und schluckt jeden Fehler -- ohne diese Spur
    ist nicht feststellbar, WELCHEN Text er gewaehlt hat und ob er ueberhaupt
    zum Sprechen kam. Die Datei liegt im Laufzeitverzeichnis und ist nach dem
    naechsten Neustart weg.
    """
    laufzeit = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    zusatz = " ".join(f"{name}={wert}" for name, wert in felder.items())
    zeile = f"{time.strftime('%H:%M:%S')} {ereignis} {zusatz} {text[:70]!r}\n"
    try:
        with open(Path(laufzeit) / "mimic-ansage.log", "a", encoding="utf-8") as datei:
            datei.write(zeile)
    except OSError:
        pass


def _sperre():
    """Exklusive Sperre auf die Audioausgabe, oder None, wenn schon jemand spricht."""
    laufzeit = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    try:
        griff = open(Path(laufzeit) / "mimic-ansage.lock", "w")
        fcntl.flock(griff, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return griff
    except OSError:
        return None


def _mimic() -> str | None:
    """`mimic` im PATH, sonst der Pfad, den `uv tool install` benutzt.

    Der Hook erbt die Umgebung von Claude Code, und dort fehlt ~/.local/bin
    oefter, als man denkt -- ohne den zweiten Versuch bliebe die Ansage stumm.
    """
    gefunden = shutil.which("mimic")
    if gefunden:
        return gefunden
    ersatz = Path.home() / ".local/bin/mimic"
    return str(ersatz) if os.access(ersatz, os.X_OK) else None


def sprechen(text: str) -> int:
    """Vordergrundpfad: Kopfhoerer sicherstellen, dann sprechen. Immer 0."""
    griff = _sperre()
    if griff is None:
        _protokoll("verworfen", text, grund="sperre")
        return 0
    _protokoll("spricht", text)

    kopfhoerer = Path(__file__).resolve().parent / "kopfhoerer.sh"
    if os.access(kopfhoerer, os.X_OK):
        try:
            subprocess.run([str(kopfhoerer), "--sicherstellen"], timeout=KOPFHOERER_FRIST_S,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass                # ohne Kopfhoerer wird eben ueber die Boxen gesprochen

    programm = _mimic()
    if not programm:
        return 0
    try:
        subprocess.run([programm, "say", text, "--voice", stimme()], timeout=SPRECH_FRIST_S,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    return 0


# ------------------------------------------------------------------ Einhaengen

EREIGNISSE = ("Stop", "Notification")


def hook_eintrag(programm: str) -> dict:
    return {"type": "command", "command": f'python3 "{programm}"', "timeout": 10}


def _schon_da(gruppen, programm: str) -> bool:
    """Erkennt den eigenen Eintrag wieder -- auch nach einem Pfadwechsel.

    Gesucht wird nach dem Dateinamen, nicht nach dem ganzen Befehl: wer das
    Skript spaeter verschiebt und neu einhaengt, soll keinen zweiten Eintrag
    bekommen, sondern gar keinen -- doppelt eingehaengt spraeche Mimic zweimal.
    """
    marke = Path(programm).name
    for gruppe in gruppen if isinstance(gruppen, list) else []:
        for haken in (gruppe or {}).get("hooks", []) if isinstance(gruppe, dict) else []:
            if isinstance(haken, dict) and marke in str(haken.get("command", "")):
                return True
    return False


def einhaengen(pfad: Path, programm: str) -> tuple[int, str]:
    """Den Hook in eine settings.json eintragen, ohne den Rest anzufassen.

    Zusammenfuehren statt ueberschreiben: in ~/.claude/settings.json steht
    typischerweise schon etwas, und eine verlorene Berechtigungsliste waere ein
    teurer Preis fuer eine Ansage. Kaputtes JSON wird darum nicht geraderueckt,
    sondern abgelehnt -- lieber gar nicht einhaengen als die Datei ersetzen.
    """
    try:
        roh = pfad.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        roh = ""
    except OSError as fehler:
        return 1, f"{pfad} nicht lesbar: {fehler}"

    if roh:
        try:
            einstellungen = json.loads(roh)
        except (json.JSONDecodeError, ValueError) as fehler:
            return 1, f"{pfad} ist kein gueltiges JSON ({fehler}) -- nichts geaendert."
        if not isinstance(einstellungen, dict):
            return 1, f"{pfad} enthaelt kein Objekt -- nichts geaendert."
    else:
        einstellungen = {}

    haken = einstellungen.setdefault("hooks", {})
    if not isinstance(haken, dict):
        return 1, f'{pfad}: "hooks" ist kein Objekt -- nichts geaendert.'

    ergaenzt = []
    for ereignis in EREIGNISSE:
        gruppen = haken.setdefault(ereignis, [])
        if not isinstance(gruppen, list):
            return 1, f'{pfad}: "hooks.{ereignis}" ist keine Liste -- nichts geaendert.'
        if _schon_da(gruppen, programm):
            continue
        gruppen.append({"hooks": [hook_eintrag(programm)]})
        ergaenzt.append(ereignis)

    if not ergaenzt:
        return 0, f"{pfad}: schon eingehaengt, nichts zu tun."

    if roh:
        sicherung = pfad.with_suffix(pfad.suffix + ".vor-ansage")
        try:
            sicherung.write_text(roh + "\n", encoding="utf-8")
        except OSError as fehler:
            return 1, f"Sicherung {sicherung} nicht schreibbar: {fehler}"

    try:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps(einstellungen, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError as fehler:
        return 1, f"{pfad} nicht schreibbar: {fehler}"
    return 0, f"{pfad}: {' und '.join(ergaenzt)} eingehaengt."


def abkoppeln(text: str) -> None:
    """Dasselbe Skript als eigenstaendige Sitzung starten und sofort zurueckkehren."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--sagen", text],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------- Rahmen

def hook_nutzlast() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        rohdaten = sys.stdin.read()
    except OSError:
        return {}
    try:
        wert = json.loads(rohdaten)
    except (json.JSONDecodeError, ValueError):
        return {}
    return wert if isinstance(wert, dict) else {}


def main(argv: list[str] | None = None) -> int:
    zerleger = argparse.ArgumentParser(prog="ansage", description=__doc__)
    zerleger.add_argument("--sagen", metavar="TEXT",
                          help="diesen Text im Vordergrund sprechen (auch der abgekoppelte Pfad)")
    zerleger.add_argument("--vorschau", action="store_true",
                          help="Satz aus dem Hook-JSON nur ausgeben, nicht sprechen")
    zerleger.add_argument("--einhaengen", nargs="?", const="", metavar="SETTINGS_JSON",
                          help="Hook in eine settings.json eintragen "
                               "(Vorgabe: ~/.claude/settings.json)")
    zerleger.add_argument("--stimme", action="store_true",
                          help="das wirksame Stimmprofil ausgeben")
    args = zerleger.parse_args(argv)

    if args.stimme:
        print(stimme())
        return 0

    if args.einhaengen is not None:
        ziel = Path(args.einhaengen) if args.einhaengen else Path.home() / ".claude/settings.json"
        code, meldung = einhaengen(ziel, str(Path(__file__).resolve()))
        print(meldung, file=sys.stderr if code else sys.stdout)
        return code

    if args.sagen is not None:
        return sprechen(args.sagen)

    nutzlast = hook_nutzlast()
    text = ansagetext(nutzlast)
    pfad = nutzlast.get("transcript_path")
    try:
        groesse = os.path.getsize(pfad) if isinstance(pfad, str) else -1
    except OSError:
        groesse = -1
    _protokoll(str(nutzlast.get("hook_event_name") or "?"), text, bytes=groesse)
    if args.vorschau:
        print(text)
        return 0
    if os.environ.get("MIMIC_ANSAGE_STILL") == "1":
        return 0
    abkoppeln(text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # Letzte Leitplanke: ein Hook, der stolpert, darf trotzdem nicht die
        # Sitzung mit einem Fehler bewerfen. Von Hand aufgerufen gilt das
        # Gegenteil -- wer einhaengt, muss sehen, wenn es schiefgeht.
        if len(sys.argv) > 1:
            raise
        raise SystemExit(0)
