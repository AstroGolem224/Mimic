"""Faerbt eine Stimme mit Effektketten ein. Nur ffmpeg, keine neue Abhaengigkeit.

Der Entwurf aus MOSS liefert eine Stimme. Was sie nach Halle, Chor, Daemon oder
kaputtem Lautsprecher klingen laesst, sind Effekte, und die gehoeren hinter die
Synthese -- nicht in den Prompt, wo sie nur zufaellig entstehen.

Alle Ketten arbeiten mono in, mono raus, damit das Ergebnis ohne Umweg als
ref.wav in ein Mimic-Profil kann. gverb und plate liefern stereo, deshalb der
Downmix am Ende jeder Hallkette.

  uv run python faerben.py out/entwurf/bot_kalt_0.wav          # alle Ketten
  uv run python faerben.py out/entwurf/bot_kalt_0.wav halle    # eine Kette

Ausgabe: out/gefaerbt/<name>__<kette>.wav
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

# Hall summiert Energie auf: ohne Begrenzer schlaegt `halle` an die 1.0 an
# und clippt hoerbar. Der Limiter kostet nichts und rettet die Spitzen.
# Der Limiter allein reicht nicht: seine Vorausschau laesst die allerersten
# Transienten durch, drei Abtastwerte lagen bei 1.0. Erst 4 dB runter, dann
# begrenzen.
MONO = "pan=mono|c0=0.5*c0+0.5*c1,volume=-4dB,alimiter=limit=0.95"

# Zweite Eichung, 2026-08-11. Der erste Satz war durchweg zu stark -- von
# zwoelf Ketten war `maschine` die einzige, die Matthias durchgehen liess. Also
# ist `maschine` jetzt der Massstab: unveraendert, und alles andere auf sein
# Mass heruntergezogen.
#
# Was das praktisch heisst:
#   * Hall wird beigemischt, nicht aufgetragen. Der Trockenanteil bleibt vorn
#     (c4, "Dry signal level"), die Fahne sitzt 20 dB darunter statt 8.
#   * Tonhoehen bewegen sich um Halbtoene, nicht um Oktaven. 0.72 war eine
#     andere Person, 0.92 ist dieselbe Person mit mehr Brust.
#   * Bitreduktion bleibt oberhalb der Hoerschwelle fuer Koernigkeit: 12 Bit
#     faerbt, 5 Bit zerstoert.
#
# Die alten, starken Werte stehen je Kette als Kommentar dabei. Wer eine Figur
# braucht, die wirklich bricht, nimmt sie von dort -- aber bewusst, nicht als
# Vorgabe.
KETTEN: dict[str, str] = {
    # Zimmer, kein Saal. Man hoert, dass die Stimme irgendwo steht.
    # Stark war: c0=60|c1=3.5|c2=0.55|c4=-4|c6=-12
    "halle": f"ladspa=f=gverb_1216:p=gverb:c=c0=22|c1=1.1|c2=0.7|c4=0|c6=-22,{MONO}",
    # Grosser Raum, aber die Stimme bleibt vorn und die Fahne bleibt kurz.
    # Stark war: c0=220|c1=9|c2=0.3|c4=-9|c6=-8
    "kathedrale": f"ladspa=f=gverb_1216:p=gverb:c=c0=90|c1=2.6|c2=0.5|c4=0|c6=-19,{MONO}",
    # Blechhall, nur angedeutet: 25 Prozent nass, der Rest trocken.
    # Stark war: plate voll aufgedreht, ohne Mischung.
    "kammer": (
        f"asplit=2[t][n];[n]ladspa=f=plate_1423:p=plate,{MONO}[n1];"
        "[t][n1]amix=inputs=2:weights=3 1:normalize=0,volume=-10dB,alimiter=limit=0.95"
    ),
    # Chor: zwei Nebenstimmen leise darunter, keine drei gleich lauten Koepfe.
    # Stark war: drei gleich laute Kopien plus voller chorus-Filter.
    "chor": (
        "asplit=3[a][b][c];"
        "[a]rubberband=pitch=0.997[a1];"
        "[b]rubberband=pitch=1.004,adelay=17,volume=-8dB[b1];"
        "[c]rubberband=pitch=1.008,adelay=31,volume=-11dB[c1];"
        "[a1][b1][c1]amix=inputs=3:normalize=0,alimiter=limit=0.95"
    ),
    # Tonhoehe: ein bis drei Halbtoene, nicht mehr. Tempo bleibt.
    # Stark war: tief 0.72, sehr_tief 0.55, hoch 1.32
    "tief": "rubberband=pitch=0.92",
    "sehr_tief": "rubberband=pitch=0.84",
    "hoch": "rubberband=pitch=1.08",
    # Daemon: etwas tiefer, etwas Raum, ein Hauch Koernung. Kein Monster.
    # Stark war: pitch=0.58 + grosser Hall + acrusher bits=10
    "daemon": (
        "rubberband=pitch=0.86,"
        f"ladspa=f=gverb_1216:p=gverb:c=c0=30|c1=1.4|c2=0.6|c4=0|c6=-20,{MONO},"
        "acrusher=bits=13:mode=log:aa=1"
    ),
    # Funkgeraet: Band beschnitten, aber nicht uebersteuert.
    # Stark war: highpass 450, lowpass 3200, acrusher bits=8, +3 dB
    "funk": "highpass=f=300,lowpass=f=4200,acrusher=bits=12:mode=log:aa=1,alimiter=limit=0.9",
    # Der Massstab. Unveraendert -- die einzige Kette, die im ersten Durchgang
    # bestanden hat.
    "maschine": "afreqshift=shift=45,flanger=delay=3:depth=4:speed=0.4,acrusher=bits=12:mode=log:aa=1",
    # Glitch: Koernung und leichtes Zittern, keine Zerstoerung.
    # Stark war: acrusher bits=5 ohne Antialiasing, vibrato d=0.35, flanger depth=6
    "glitch": "acrusher=bits=10:mode=log:aa=1,vibrato=f=6:d=0.08,flanger=delay=2:depth=2:speed=1.2",
    # Kunststimme -- die Naeherung an Auto-Tune. Kein echtes Tonhoehenraster:
    # dafuer braeuchte es ein Plugin, das hier nicht liegt. Geblieben ist eine
    # leise Quintschicht und ein Rest Vibrato: kuenstlich, aber nicht albern.
    # Stark war: Quinte nur 9 dB leiser, vibrato d=0.18, voller chorus.
    "kunststimme": (
        "asplit=2[a][b];"
        "[a]anull[a1];"
        "[b]rubberband=pitch=1.498,volume=-18dB[b1];"
        "[a1][b1]amix=inputs=2:normalize=0,"
        "vibrato=f=5:d=0.06,alimiter=limit=0.95"
    ),
}


def faerben(quelle: pathlib.Path, kette: str, ziel: pathlib.Path) -> None:
    filter_ = KETTEN[kette]
    # Ketten mit `;` sind Graphen und brauchen -filter_complex, einfache
    # Ketten kommen mit -af aus. Der Unterschied ist ffmpeg-Syntax, keine
    # Eigenschaft des Effekts.
    schalter = "-filter_complex" if ";" in filter_ else "-af"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(quelle), schalter, filter_, "-ar", "48000", "-ac", "1", str(ziel)],
        check=True,
    )


def main() -> None:
    quelle = pathlib.Path(sys.argv[1])
    gewuenscht = sys.argv[2:] or list(KETTEN)
    aus = pathlib.Path(__file__).resolve().parent / "out" / "gefaerbt"
    aus.mkdir(parents=True, exist_ok=True)

    for kette in gewuenscht:
        ziel = aus / f"{quelle.stem}__{kette}.wav"
        try:
            faerben(quelle, kette, ziel)
            print(ziel.name)
        except subprocess.CalledProcessError:
            print(f"{ziel.name}  FEHLGESCHLAGEN")


if __name__ == "__main__":
    main()
