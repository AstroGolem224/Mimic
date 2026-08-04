"""Aufnahmehelfer. Fuehrt durch alles, was Matthias einsprechen muss.

Drei Saetze Aufnahmen, alle 48 kHz mono verlustfrei:

  referenz   1 Aufnahme, ~10 s -- daraus klont Mimic. Plus Transkript.
  blindtest  6 Aufnahmen -- die echte Haelfte von Kriterium B.
  akzent     10 Aufnahmen -- die englische Baseline fuer Kriterium D.

Aufruf:
  uv run python 02_aufnehmen.py referenz
  uv run python 02_aufnehmen.py blindtest
  uv run python 02_aufnehmen.py akzent
  uv run python 02_aufnehmen.py status

Bedienung je Aufnahme: Enter startet, Enter stoppt. Danach Wiedergabe und
[Enter] behalten / [n] nochmal / [s] ueberspringen.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import yaml

CORPUS = yaml.safe_load(open("corpus.yaml"))
STIMME = os.path.expanduser("~/.local/share/mimic/voices/matthias")
BASELINE = "aufnahmen/baseline"
ECHT = "aufnahmen/echt"
RATE = "48000"


def aufnehmen(pfad: str) -> None:
    """pw-record bis Enter. Kein Timeout -- der Sprecher entscheidet, wann fertig."""
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    input("    [Enter] = Aufnahme START ")
    p = subprocess.Popen(
        ["pw-record", "--rate", RATE, "--channels", "1", "--format", "s16", pfad],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    input("    ...laeuft. [Enter] = STOPP ")
    p.terminate()
    p.wait(timeout=5)


def abspielen(pfad: str) -> None:
    subprocess.run(["pw-cat", "-p", pfad], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def dauer_s(pfad: str) -> float:
    import soundfile as sf
    info = sf.info(pfad)
    return info.frames / info.samplerate


def eine(nr: str, text: str, pfad: str, hinweis: str = "") -> bool:
    """True wenn behalten, False wenn uebersprungen."""
    print(f"\n{'='*72}\n{nr}")
    if hinweis:
        print(f"  {hinweis}")
    print("\n" + textwrap.fill(text, 68, initial_indent="  » ", subsequent_indent="    "))
    print()
    while True:
        aufnehmen(pfad)
        d = dauer_s(pfad)
        print(f"    {d:.1f} s aufgenommen, spiele ab...")
        abspielen(pfad)
        w = input("    [Enter] behalten  [n] nochmal  [s] ueberspringen: ").strip().lower()
        if w == "n":
            continue
        if w == "s":
            os.remove(pfad)
            return False
        return True


def referenz() -> None:
    e = CORPUS["referenz"]
    text = " ".join(e["text"].split())
    print(textwrap.dedent("""
        REFERENZ -- daraus klont Mimic deine Stimme.

        Das hier ist die wichtigste Aufnahme des ganzen Spikes. Wenn sie
        schlecht ist, ist alles danach schlecht, und kein Modellwechsel
        repariert das.

          - ruhiger Raum, Fenster zu, Luefter aus wenn moeglich
          - Mikro nicht anblasen, ~20 cm Abstand
          - normales Sprechtempo, keine Vortragsstimme
          - durchsprechen, nicht Satz fuer Satz
          - ~10 s. Laenger bringt laut README nichts.
    """))
    os.makedirs(STIMME, exist_ok=True)
    wav = f"{STIMME}/ref.wav"
    if eine("REFERENZ", text, wav):
        with open(f"{STIMME}/ref.txt", "w") as f:
            f.write(text + "\n")
        d = dauer_s(wav)
        print(f"\n  {wav}  ({d:.1f} s)")
        print(f"  {STIMME}/ref.txt")
        if not 6 <= d <= 20:
            print(f"  WARNUNG: {d:.1f} s liegt ausserhalb 6-20 s. Nochmal erwaegen.")


def blindtest() -> None:
    texte = {e["id"]: e["text"] for e in CORPUS["de"]}
    ids = CORPUS["blindtest"]
    print(textwrap.dedent(f"""
        BLINDTEST-BASIS -- {len(ids)} Saetze, gesprochen von dir.

        Mimic spricht spaeter dieselben Saetze. Zwoelf Proben werden gemischt
        und du raetst je Probe: echt oder Mimic.

        Sprich genau so, wie du auch sonst sprichst. Wenn du dich hier
        anstrengst besonders deutlich zu sein, verfaelscht das den Test zu
        deinen Gunsten -- und du willst wissen, ob der Klon *dich* trifft.
    """))
    for i, sid in enumerate(ids, 1):
        eine(f"[{i}/{len(ids)}]  {sid}", texte[sid], f"{ECHT}/{sid}.wav")


def akzent() -> None:
    eintraege = CORPUS["akzent_check"]
    print(textwrap.dedent(f"""
        AKZENT-BASELINE -- {len(eintraege)} englische Saetze, gesprochen von dir.

        Das ist der Vergleichsmassstab fuer Kriterium D. Gemessen wird NICHT,
        ob Mimic deutschen Akzent hat -- du hast einen, das ist in Ordnung.
        Gemessen wird, ob Mimic an den markierten Stellen *deutscher* klingt
        als du selbst.

        Also: sprich normal Englisch. Nicht besser als sonst.
    """))
    for i, e in enumerate(eintraege, 1):
        eine(f"[{i}/{len(eintraege)}]  {e['id']}", e["text"],
             f"{BASELINE}/{e['id']}.wav",
             hinweis="Risikostellen: " + ", ".join(e["risiko"]))


def status() -> None:
    ref = f"{STIMME}/ref.wav"
    print(f"\nReferenz   {'OK  ' + f'{dauer_s(ref):.1f} s' if os.path.exists(ref) else 'FEHLT'}"
          f"   {ref}")
    for name, verz, ids in (
        ("Blindtest", ECHT, CORPUS["blindtest"]),
        ("Akzent   ", BASELINE, [e["id"] for e in CORPUS["akzent_check"]]),
    ):
        da = [i for i in ids if os.path.exists(f"{verz}/{i}.wav")]
        fehlt = [i for i in ids if i not in da]
        print(f"{name}  {len(da)}/{len(ids)}" + (f"   fehlt: {', '.join(fehlt)}" if fehlt else "   OK"))


if __name__ == "__main__":
    modi = {"referenz": referenz, "blindtest": blindtest,
            "akzent": akzent, "status": status}
    if len(sys.argv) != 2 or sys.argv[1] not in modi:
        print(__doc__)
        raise SystemExit(2)
    modi[sys.argv[1]]()
