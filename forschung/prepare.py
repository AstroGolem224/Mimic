"""Einmalige Vorbereitung der Experimentierschleife: Korpus und Referenz-Cache.

DIESE DATEI WIRD NICHT VOM AGENTEN EDITIERT (siehe program.md). Sie ist das
Gegenstueck zu prepare.py in karpathys autoresearch: feste Konstanten und
Laufzeit-Hilfen, damit experiment.py klein bleibt.

Aufruf ueber forschung/lauf.sh oder direkt:
    uv run --with resemblyzer --with "setuptools<81" python forschung/prepare.py
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FORSCHUNG = Path(__file__).parent
CACHE = FORSCHUNG / "cache"


@dataclass(frozen=True)
class Probe:
    kennung: str
    stimme: str
    text: str


# Harte Faelle zuerst: genau die Textformen, an denen die Chunk-Naehte im
# August 2026 gerissen sind (Kommaketten ohne Satzende, Listen, lange
# Satzgefuege). Je Stimme auch ein unkritischer Kontrolltext, damit ein
# Experiment, das die harten Faelle verbessert, die einfachen nicht ruiniert.
KORPUS = (
    Probe("krieger_kommakette", "matthias_krieger",
          "Der Turm steht offen, die Tore knarren im Wind, niemand wagt sich hinein, "
          "die Fackeln sind laengst erloschen, das Moos kriecht ueber die Schwellen, "
          "die Raben sitzen stumm auf den Zinnen, der Brunnen im Hof ist versiegt, "
          "und tief unten im Gemaeuer wartet etwas, das keinen Namen mehr hat, "
          "das aelter ist als die Mauern selbst, das die Steine atmen laesst"),
    Probe("krieger_liste", "matthias_krieger",
          "Nehmt mit: Seile, Fackeln, Proviant fuer drei Tage, trockenes Zunderholz, "
          "die Karte des alten Kartographen, zwei Schilde, den eisernen Haken, "
          "Verbandszeug, Salz gegen die Geister, Kreide fuer die Wegzeichen, "
          "und den Schluessel, den der Schmied uns unter Eid gegeben hat"),
    Probe("krieger_satzgefuege", "matthias_krieger",
          "Als wir im Morgengrauen aufbrachen, waehrend der Nebel noch in den Senken "
          "hing und die Voegel schwiegen, weil sie den Sturm frueher spueren als wir, "
          "wussten wir bereits, dass der Weg durch die Schlucht laenger dauern wuerde, "
          "als der Hauptmann uns versprochen hatte, denn die Bruecke war seit dem "
          "Fruehjahr eingestuerzt und der Umweg fuehrte ueber das Geroellfeld."),
    Probe("krieger_absaetze", "matthias_krieger",
          "Die Legende erzaehlt von einer Stimme tief unten im Gemaeuer. Wer sie "
          "hoert, kehrt veraendert zurueck. Manche schweigen fuer immer. Andere "
          "reden wirres Zeug von Zahnraedern und einem Labyrinth aus Ordnung. "
          "Der Hauptmann glaubt kein Wort davon. Ich habe die Stimme gehoert. "
          "Sie zaehlt. Sie zaehlt unablaessig, und sie hat sich verzaehlt."),
    Probe("krieger_kontrolle", "matthias_krieger",
          "Wir brechen im Morgengrauen auf. Der Weg durch die Schlucht ist lang."),
    Probe("nordom_runon", "n0rd0m",
          "This unit has catalogued the anomalies, sorted them by severity, "
          "cross-referenced them with the archive, flagged the contradictions, "
          "discarded the duplicates, weighted the remainder by observed frequency, "
          "and reached a conclusion that the available evidence does not support, "
          "which is itself the four hundred and thirteenth anomaly"),
    Probe("nordom_satzfolge", "n0rd0m",
          "Query. The gears of order turn regardless of observation. Statement. "
          "Nordom persists in its calculations. The census of the sector continues "
          "without interruption. Each anomaly receives a number. Each number "
          "receives a file. The files disagree with one another. This is recorded."),
    Probe("nordom_kontrolle", "n0rd0m",
          "Statement. This unit is operational. Analysis continues."),
)

STIMMEN = tuple(sorted({probe.stimme for probe in KORPUS}))


def cache_pfad(stimme: str) -> Path:
    return CACHE / f"{stimme}.npy"


def main() -> int:
    import numpy as np
    from resemblyzer import VoiceEncoder, preprocess_wav
    from mimic.voices import close_voice, load_voice

    CACHE.mkdir(exist_ok=True)
    encoder = VoiceEncoder(verbose=False)
    for stimme in STIMMEN:
        profil = load_voice(stimme)
        try:
            embedding = encoder.embed_utterance(preprocess_wav(profil.wav_path))
        finally:
            close_voice(profil)
        np.save(cache_pfad(stimme), embedding)
        print(f"referenz={stimme} embedding={embedding.shape} -> {cache_pfad(stimme)}")
    print(f"korpus={len(KORPUS)} proben, stimmen={', '.join(STIMMEN)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
