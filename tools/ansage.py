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
import signal
import subprocess
import sys
import time
from pathlib import Path

# 0 = ungekuerzt. Die Ansage soll den ganzen Fliesstext sprechen, nicht seinen
# Anfang; wer sie kuerzer will, setzt hier eine Zeichenzahl.
GRENZE = 0
# Der Dienst nimmt 1000 Zeichen je Anfrage (frontend.MAX_TEXT_CHARS). Laengeres
# wird an Satzgrenzen zerlegt und nacheinander gesprochen.
STUECK_ZEICHEN = 900
TITEL_GRENZE = 60       # der Sitzungstitel steht VOR der Meldung, also kurz halten
WARTE_FRIST_S = 10.0    # so lange wartet die Ansage auf die frische Antwort
WARTE_TAKT_S = 0.2
VERDRAENGUNG_FRIST_S = 2.0        # so lange braucht ein verdraengter Sprecher hoechstens
WARTESCHLANGE_FRIST_S = 300.0     # so lange wartet eine Ansage auf eine fremde Sitzung
MINDEST = 150           # darunter lieber anschneiden als abbrechen
TAIL_BYTES = 1 << 20    # so weit wird ins Transkript zurueckgelesen
KOPFHOERER_FRIST_S = 20
SPRECH_FRIST_S = 180

FERTIG = "Fertig."
OHNE_INHALT = "Fertig. Keine Zusammenfassung im Transkript."


VORGABE_STIMME = "forge"        # Stimme der Standard-Persona


def stimmdatei(sitzung: str = "") -> Path:
    """Mit Sitzung die Datei dieser Sitzung, ohne die gemeinsame.

    Eine Persona gilt fuer die Sitzung, in der sie gewaehlt wurde -- die
    sitzungslose Datei wirkte auf alle Sitzungen zugleich und stellte
    nebenher laufenden Sitzungen die Stimme um. Sie bleibt als Rueckfall
    fuer Handaufrufe ohne Sitzung.
    """
    laufzeit = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    kennung = re.sub(r"[^A-Za-z0-9._-]", "", sitzung)[:64]
    name = f"mimic-ansage.stimme.{kennung}" if kennung else "mimic-ansage.stimme"
    return Path(laufzeit) / name


def stimme(sitzung: str = "") -> str:
    """Umgebung schlaegt Sitzungsdatei schlaegt gemeinsame Datei schlaegt Vorgabe.

    Die Dateien schreiben die Persona-Skills beim Umschalten. Sie liegen im
    Laufzeitverzeichnis und ueberleben keinen Neustart -- danach gilt wieder
    VORGABE_STIMME. Genau deshalb muss die Vorgabe die Standard-Persona sein.
    """
    aus_umgebung = os.environ.get("MIMIC_ANSAGE_STIMME", "").strip()
    if aus_umgebung:
        return aus_umgebung
    dateien = [stimmdatei(sitzung)] if sitzung else []
    dateien.append(stimmdatei())
    for datei in dateien:
        try:
            aus_datei = datei.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if aus_datei:
            return aus_datei
    return VORGABE_STIMME


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


def _endzeilen(pfad: Path) -> list[bytes]:
    """Die letzten Zeilen des Transkripts, jÃ¼ngste zuletzt.

    Nur das Dateiende: eine lange Sitzung wird zweistellig megabyteschwer, und
    der Hook soll den Sitzungsabschluss nicht mit Einlesen bezahlen.
    """
    try:
        with open(pfad, "rb") as datei:
            groesse = datei.seek(0, os.SEEK_END)
            beginn = max(0, groesse - TAIL_BYTES)
            datei.seek(beginn)
            rohdaten = datei.read()
    except OSError:
        return []
    zeilen = rohdaten.split(b"\n")
    return zeilen[1:] if beginn else zeilen     # erste Zeile ist angeschnitten


def sitzungstitel(pfad: Path) -> str:
    """Der Titel, den Claude Code der Sitzung gegeben hat, oder leer.

    Claude Code schreibt den Eintrag laufend neu, auch am Dateiende -- selbst
    in einer 70-MB-Sitzung stand er in den letzten hundert Bytes. Das Lesen des
    Dateiendes reicht also.
    """
    for zeile in reversed(_endzeilen(pfad)):
        if b"custom-title" not in zeile:        # billiger Vorfilter vor dem Parsen
            continue
        try:
            eintrag = json.loads(zeile)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(eintrag, dict) and eintrag.get("type") == "custom-title":
            titel = str(eintrag.get("customTitle") or "").strip()
            if titel:
                return " ".join(titel.split())[:TITEL_GRENZE]
    return ""


def letzte_antwort_mit_kennung(pfad: Path) -> tuple[str, str]:
    """Wie `letzte_antwort`, dazu die uuid des Eintrags.

    Die Kennung entscheidet, ob seit der letzten Ansage ueberhaupt etwas Neues
    dazugekommen ist -- der Textvergleich taugt dafuer nicht, zwei Antworten
    koennen gleich anfangen.
    """
    for zeile in reversed(_endzeilen(pfad)):
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
            return text, str(eintrag.get("uuid") or "")
    return "", ""


def letzte_antwort(pfad: Path) -> str:
    """Text der letzten Assistentennachricht aus dem JSONL-Transkript.

    Subagenten (`isSidechain`) zaehlen nicht -- ihre Antworten sieht der Nutzer
    nicht, sie waeren also eine Ansage ueber Arbeit, die im Hauptstrang gar
    nicht steht.
    """
    for zeile in reversed(_endzeilen(pfad)):
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
# "Drin, 30a5628." wird nach dem Streichen zu "Drin,." -- das Komma stand vor
# dem gestrichenen Wort. Klauselzeichen unmittelbar vor einem Satzende sind
# immer so ein Rest, ebenso doppelte Satzpunkte.
_KLAUSEL_VOR_SATZENDE = re.compile(r"[,;:]+(?=\s*[.!?])")
_DOPPELTES_SATZENDE = re.compile(r"([.!?])[.,;:]+")
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
        zeile = _KLAUSEL_VOR_SATZENDE.sub("", zeile)
        zeile = _DOPPELTES_SATZENDE.sub(r"\1", zeile)
        zeile = zeile.rstrip(_RANDZEICHEN).lstrip(_RANDZEICHEN)
        # Was von der Zeile bleibt, muss noch etwas aussagen: eine Zeile, die
        # nur aus einem Befehl bestand, ist jetzt leer oder ein Rumpf wie "in".
        if not _WORTHALTIG.search(zeile) or _PFADARTIG.match(zeile):
            continue
        saetze.append(zeile if zeile[-1] in ".!?" else zeile + ".")

    fluss = " ".join(" ".join(saetze).split())
    if not fluss:
        return ""
    if not grenze or len(fluss) <= grenze:
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


def _vorspann(pfad: object) -> str:
    """Sitzungstitel als erster Satz, damit hoerbar ist, WER da meldet.

    Mehrere Sitzungen teilen sich eine Audioausgabe; ohne den Titel klingt die
    Meldung aus dem anderen Fenster wie die eigene. Fehlt der Titel -- neue
    Sitzung, noch keiner vergeben -- bleibt es beim blossen "Fertig."
    """
    if not isinstance(pfad, str) or not pfad:
        return ""
    titel = sitzungstitel(Path(pfad))
    if not titel:
        return ""
    return titel if titel[-1] in ".!?" else f"{titel}."


def _mit_vorspann(vorspann: str, kern: str) -> str:
    """Titel davor -- ausser der Text faengt schon damit an.

    Das passiert, sobald eine Antwort die Ansage selbst zitiert; doppelt
    gesprochen klingt es wie ein Aussetzer.
    """
    if not vorspann:
        return kern
    # Der Vergleich laeuft hinter "Fertig.", denn genau dort steht der Titel,
    # wenn die Antwort die Ansage zitiert hat.
    rumpf = kern[len(FERTIG):].lstrip() if kern.startswith(FERTIG) else kern
    marke = vorspann.casefold().rstrip(".!?")
    if kern.casefold().startswith(marke):
        return kern
    if rumpf.casefold().startswith(marke):
        # Das Zitat bringt Titel UND Fertigmeldung schon mit; das eigene
        # "Fertig." davor waere das zweite in einem Satz.
        return rumpf
    return f"{vorspann} {kern}".strip()


def ansagetext(nutzlast: dict) -> str:
    """Der Satz, den Mimic sprechen soll -- aus dem Hook-JSON abgeleitet."""
    ereignis = nutzlast.get("hook_event_name")
    pfad = nutzlast.get("transcript_path")
    vorspann = _vorspann(pfad)

    if ereignis == "Notification":
        meldung = str(nutzlast.get("message") or "").strip()
        kern = f"Claude wartet. {zusammenfassen(meldung)}".strip() if meldung else "Claude wartet."
        return _mit_vorspann(vorspann, kern)

    antwort = letzte_antwort(Path(pfad)) if isinstance(pfad, str) and pfad else ""
    stand = zusammenfassen(antwort)
    if not stand:
        return _mit_vorspann(vorspann, OHNE_INHALT)
    return _mit_vorspann(vorspann, f"{FERTIG} {stand}")


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


def _laufzeit() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")


def _sperre():
    """Exklusive Sperre auf die Audioausgabe, oder None, wenn schon jemand spricht."""
    try:
        griff = open(_laufzeit() / "mimic-ansage.lock", "w")
        fcntl.flock(griff, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return griff
    except OSError:
        return None


def _ist_ansage_prozess(pid: int) -> bool:
    """Laeuft unter dieser PID wirklich eine Ansage -- oder schon etwas anderes?

    PIDs werden wiederverwendet. Ohne diese Pruefung koennte eine veraltete
    Datei dazu fuehren, dass ein beliebiger fremder Prozess ein Signal bekommt.
    """
    if pid <= 1:
        return False
    try:
        befehl = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    # Argumentweise und auf den Dateinamen genau: eine Teilzeichenkettensuche
    # ueber die ganze Kommandozeile haelt auch `pytest test_ansage.py` fuer
    # eine Ansage.
    namen = {teil.rsplit(b"/", 1)[-1] for teil in befehl.split(b"\0") if teil}
    return bool(namen & {b"mimic-ansage", b"ansage.py"})


def _besitzer() -> tuple[int, str]:
    """PID und Sitzung dessen, der gerade spricht -- (0, "") wenn unbekannt."""
    try:
        roh = (_laufzeit() / "mimic-ansage.pid").read_text(encoding="utf-8").split()
    except OSError:
        return 0, ""
    try:
        return int(roh[0]), roh[1] if len(roh) > 1 else ""
    except (IndexError, ValueError):
        return 0, ""


def _sperre_holen(sitzung: str):
    """Die Ausgabe belegen. Eigene Sitzung verdraengen, fremde abwarten.

    Zwei Faelle, zwei Regeln. Spricht dieselbe Sitzung noch ihre vorige
    Antwort, ist die neue aktueller -- also verdraengen. Spricht eine ANDERE
    Sitzung, gehoert ihre Meldung einem anderen Fenster und darf nicht
    abgeschnitten werden; dann wird angestanden, bis sie fertig ist.
    """
    griff = _sperre()
    if griff is not None:
        return griff

    pid, fremde = _besitzer()
    if pid and fremde == sitzung:
        _verdraenge_laufende_ansage()
        for _ in range(int(VERDRAENGUNG_FRIST_S / WARTE_TAKT_S)):
            time.sleep(WARTE_TAKT_S)
            griff = _sperre()
            if griff is not None:
                return griff
        return None

    # Fremde Sitzung: anstellen. Gepollt statt blockierend, damit die Frist
    # gilt -- ein haengender Sprecher darf die Schlange nicht ewig halten.
    _protokoll("wartet", "", vor=fremde or "unbekannt")
    frist = time.monotonic() + WARTESCHLANGE_FRIST_S
    while time.monotonic() < frist:
        time.sleep(WARTE_TAKT_S)
        griff = _sperre()
        if griff is not None:
            return griff
    _protokoll("aufgegeben", "", wartete_s=int(WARTESCHLANGE_FRIST_S))
    return None


def _verdraenge_laufende_ansage() -> None:
    """Die vorige Ansage beenden, damit die neue drankommt.

    Eine Ansage von 650 Zeichen braucht Erzeugung plus Sprechzeit; bei zuegiger
    Arbeit ist die naechste Antwort laengst fertig, bevor die vorige zu Ende
    gesprochen ist. Wer dann schweigt, meldet dauerhaft den vorletzten Stand.
    Aktuell schlaegt vollstaendig: die laufende Ansage wird abgebrochen.

    Signal an die Prozessgruppe, nicht an die PID: der sprechende Prozess ist
    per start_new_session Gruppenfuehrer, und `mimic say` haengt als Kind daran.
    """
    pid, _ = _besitzer()
    if not pid or pid == os.getpid() or not _ist_ansage_prozess(pid):
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        _protokoll("verdraengt", pid=pid)
    except OSError:
        return


def abbrechen() -> int:
    """Die laufende Ansage sofort verstummen lassen (Tastenkuerzel-Pfad)."""
    pid, _ = _besitzer()
    if not pid or pid == os.getpid() or not _ist_ansage_prozess(pid):
        return 0
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        _protokoll("abgebrochen", pid=pid)
    except OSError:
        pass
    return 0


def _merke_pid(sitzung: str = "") -> None:
    """Eigene PID und Sitzung hinterlegen, solange die Sperre gehalten wird."""
    try:
        (_laufzeit() / "mimic-ansage.pid").write_text(f"{os.getpid()} {sitzung}\n",
                                                     encoding="utf-8")
    except OSError:
        pass


def _stuecke(text: str, grenze: int = STUECK_ZEICHEN) -> list[str]:
    """Text in sprechbare Haeppchen unter der Grenze des Dienstes.

    Geschnitten wird an Satzgrenzen, nicht an Zeichen: ein Schnitt mitten im
    Satz hoert man. Ein einzelner Satz ueber der Grenze bleibt ganz -- der
    Dienst lehnt ihn dann ab, was seltener vorkommt und weniger stoert als ein
    Satz, der in zwei Haelften gesprochen wird.
    """
    text = text.strip()
    if len(text) <= grenze:
        return [text] if text else []
    stuecke: list[str] = []
    aktuell = ""
    for satz in _SATZENDE.split(text):
        kandidat = f"{aktuell} {satz}".strip()
        if aktuell and len(kandidat) > grenze:
            stuecke.append(aktuell)
            aktuell = satz
        else:
            aktuell = kandidat
    if aktuell:
        stuecke.append(aktuell)
    return stuecke


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


def sprechen(text: str, sitzung: str = "", griff=None) -> int:
    """Vordergrundpfad: Ausgabe belegen, Kopfhoerer sicherstellen, sprechen. Immer 0.

    `griff` uebergibt eine bereits gehaltene Sperre -- der Meldepfad belegt die
    Ausgabe VOR dem Warten auf die Antwort, damit der Text beim Sprechbeginn
    frisch ist und nicht der Stand von vor der Warteschlange.
    """
    if griff is None:
        griff = _sperre_holen(sitzung)
    if griff is None:
        _protokoll("verworfen", text, grund="sperre")
        return 0
    _merke_pid(sitzung)
    _protokoll("spricht", text, sitzung=sitzung or "-")

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
    gewaehlt = stimme(sitzung)
    stuecke = _stuecke(text)
    for nummer, stueck in enumerate(stuecke, 1):
        begonnen = time.monotonic()
        try:
            lauf = subprocess.run([programm, "say", stueck, "--voice", gewaehlt],
                                  timeout=SPRECH_FRIST_S,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError) as fehler:
            # Diese Zweige waren stumm: ein Timeout oder ein fehlendes `mimic`
            # sahen im Protokoll aus wie eine vollstaendig gesprochene Ansage.
            _protokoll("gescheitert", stueck[:40], stueck=f"{nummer}/{len(stuecke)}",
                       fehler=type(fehler).__name__)
            return 0
        if lauf.returncode != 0:
            # Ein gescheitertes Stueck heisst: der Rest wird auch scheitern.
            # Weiterreden hiesse, den Satz mittendrin fortzusetzen.
            _protokoll("abgebrochen", stueck[:40], stueck=f"{nummer}/{len(stuecke)}",
                       code=lauf.returncode)
            return 0
        _protokoll("stueck", "", nummer=f"{nummer}/{len(stuecke)}", zeichen=len(stueck),
                   dauer_s=int(time.monotonic() - begonnen))
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


def _zuletzt_datei(pfad: str) -> Path:
    return _laufzeit() / f"mimic-ansage.{Path(pfad).stem[:64]}.zuletzt"


def melden(pfad: str) -> int:
    """Auf die frische Antwort warten, dann sprechen. Laeuft abgekoppelt.

    Claude Code ruft den Stop-Hook auf, BEVOR die letzte Assistentennachricht
    im Transkript steht -- gemessen am 2026-08-11: die Datei wuchs zwischen
    Hook-Aufruf und Antwort noch um mehrere Kilobyte. Wer sofort liest, bekommt
    die vorige Antwort und meldet dauerhaft einen Stand hinterher.

    Deshalb wird die zuletzt angesagte uuid gemerkt und gewartet, bis eine
    andere erscheint. Kommt in der Frist nichts Neues, wird geschwiegen: eine
    Wiederholung ist schlechter als Stille.
    """
    sitzung = Path(pfad).stem[:64]
    datei = _zuletzt_datei(pfad)
    # Erst die Ausgabe belegen, dann auf die Antwort warten: wer in der
    # Warteschlange steht, soll am Ende den DANN aktuellen Stand melden, nicht
    # den von vor fuenf Minuten.
    griff = _sperre_holen(sitzung)
    if griff is None:
        return 0
    _merke_pid(sitzung)

    try:
        vorher = datei.read_text(encoding="utf-8").strip()
    except OSError:
        vorher = ""
    frist = time.monotonic() + WARTE_FRIST_S
    while True:
        antwort, kennung = letzte_antwort_mit_kennung(Path(pfad))
        if kennung and kennung != vorher:
            break
        if time.monotonic() > frist:
            _protokoll("nichts_neues", antwort, wartete_s=int(WARTE_FRIST_S))
            return 0
        time.sleep(WARTE_TAKT_S)

    stand = zusammenfassen(antwort)
    satz = _mit_vorspann(_vorspann(pfad), f"{FERTIG} {stand}" if stand else OHNE_INHALT)
    try:
        datei.write_text(kennung + "\n", encoding="utf-8")
    except OSError:
        pass
    return sprechen(satz, sitzung, griff=griff)


def abkoppeln(*argumente: str) -> None:
    """Dasselbe Skript als eigenstaendige Sitzung starten und sofort zurueckkehren."""
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *argumente],
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
    zerleger.add_argument("--melden", metavar="TRANSKRIPT",
                          help="auf die frische Antwort warten und sie sprechen (abgekoppelt)")
    zerleger.add_argument("--abbrechen", action="store_true",
                          help="laufende Ansage sofort beenden")
    zerleger.add_argument("--stimme", action="store_true",
                          help="das wirksame Stimmprofil ausgeben")
    zerleger.add_argument("--sitzung", default="", metavar="ID",
                          help="Sitzung, deren Stimme gilt (Vorgabe: die laufende)")
    args = zerleger.parse_args(argv)
    sitzung = args.sitzung or os.environ.get("CLAUDE_CODE_SESSION_ID", "")

    if args.abbrechen:
        return abbrechen()

    if args.stimme:
        print(stimme(sitzung))
        return 0

    if args.einhaengen is not None:
        ziel = Path(args.einhaengen) if args.einhaengen else Path.home() / ".claude/settings.json"
        code, meldung = einhaengen(ziel, str(Path(__file__).resolve()))
        print(meldung, file=sys.stderr if code else sys.stdout)
        return code

    if args.sagen is not None:
        return sprechen(args.sagen, sitzung)

    if args.melden is not None:
        return melden(args.melden)

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
    # Beim Stop wartet der abgekoppelte Prozess auf die Antwort; hier ist sie
    # oft noch nicht geschrieben. Die Nachfrage traegt ihren Text dagegen im
    # Hook-JSON, da gibt es nichts zu warten.
    if nutzlast.get("hook_event_name") != "Notification" and isinstance(pfad, str) and pfad:
        abkoppeln("--melden", pfad)
    else:
        # Die Nachfrage traegt keine Transkriptdatei -- die Sitzung kommt aus
        # dem Hook-JSON, sonst spraeche sie mit der Stimme einer anderen.
        abkoppeln("--sagen", text, "--sitzung", str(nutzlast.get("session_id") or ""))
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
