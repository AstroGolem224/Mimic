"""Referenztexte fuer Charakterstimmen.

Ein Eintrag je Stimmprofil: der Text, den Matthias einspricht, plus eine
Regieanweisung, die den Duktus setzt. Der Text ist die halbe Miete -- dots.tts
klont Timbre *und* Prosodie aus der Referenz, also muss die Referenz schon so
klingen wie das, was spaeter herauskommen soll. Ein neutral vorgelesener
Satz ergibt eine neutrale Stimme, egal wie finster der Inhalt ist.

Laenge: 10 Sekunden, also grob 30 Woerter. Erst mit 20-30 s versucht -- die
Klone klangen schlechter und schnitten Saetze ab. 10-15 s ist ausserdem der
einzige Bereich, der ueberhaupt gemessen ist: die Phase-0-Referenz `matthias`
hat 14.8 s und hat Kriterium B2 bestanden. Technisch begrenzt nicht die
Aufnahmedauer selbst, sondern `max_generate_length=500` Patches in dots.tts,
von denen die Referenz einen Teil belegt (33 s entsprachen 205 Patches, also
grob 0.16 s je Patch) -- bei 10 s bleibt davon praktisch alles fuer die
Ausgabe uebrig.

Jeder Text enthaelt Aussage, Frage und Ausruf, damit die Referenz die
Intonationskurven abdeckt, die im Betrieb vorkommen.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Charakter:
    regie: str
    text: str


CHARAKTERE: dict[str, Charakter] = {
    "matthias_krieger": Charakter(
        regie=(
            "Brust statt Kopf. Tiefer ansetzen als normal, langsamer, jedes Wort steht fuer "
            "sich. Nicht bruellen -- der Krieger muss nicht laut werden, um gehoert zu werden."
        ),
        text=(
            "Drei Tage durch den Pass, ohne Feuer, ohne Schlaf. "
            "Wer von euch steht morgen neben mir? "
            "Keiner. Dann gehe ich eben allein!"
        ),
    ),
    "matthias_magier": Charakter(
        regie=(
            "Leiser und beweglicher als normal. Der Magier denkt beim Sprechen, also darf das "
            "Tempo schwanken -- schnell, wo es ihn freut, langsam, wo er misstraut. Leichte "
            "Neugier in der Stimme, nie Aggression."
        ),
        text=(
            "Interessant. Die Linien laufen nicht zusammen, sie weichen einander aus. "
            "Was passiert wohl, wenn ich hier ansetze? "
            "Nein, warte, fass das nicht an!"
        ),
    ),
    "matthias_dark_lord": Charakter(
        regie=(
            "Ruhig, sehr ruhig. Der dunkle Herrscher hat es nicht eilig und muss niemanden "
            "ueberzeugen. Tief, gleichmaessig, fast freundlich -- die Drohung liegt im Inhalt, "
            "nicht in der Lautstaerke."
        ),
        text=(
            "Du bist weit gereist, um mir das zu sagen. Setz dich, trink etwas. "
            "Weisst du, warum ich dich nicht aufgehalten habe? "
            "Weil ich sehen wollte, wie lange du brauchst!"
        ),
    ),
}
