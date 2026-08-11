"""Klangfarben fuer Stimmen, die nach Maschine klingen sollen.

Der Klon selbst bleibt sauber -- dots.tts klont Stimmfarbe und Prosodie, keine
Artefakte. Was eine Stimme nach Rechner, altem Fernseher oder Kollektiv klingen
laesst, sind Modulationen, und die gehoeren an die Ausgabe.

Alles hier arbeitet auf 16-Bit-PCM-Bloecken, wie sie der Worker streamt, und
haelt seinen Zustand ueber Blockgrenzen hinweg: eine Tremolo-Phase, die bei
jedem Block neu bei null beginnt, klickt hoerbar. Deshalb ein Objekt je
Aeusserung statt einer Funktion.

Bewusst NICHT hier: Tonhoehe. Ein Pitch-Shift ohne Tempoaenderung braucht einen
Phasenvocoder, kostet Latenz im Streaming und ist ueberfluessig -- die Tonhoehe
ist eine Eigenschaft der Stimme, also verschiebt man die Referenz einmalig
(`ffmpeg -af asetrate=...,atempo=...`) und der Klon spricht dauerhaft hoeher.
"""

from __future__ import annotations

import array
import math

# Namen, die in der settings.json eines Profils stehen duerfen. Freie
# Filterketten waeren eine Einladung, dem Worker beliebige Rechenlast
# unterzuschieben -- hier gilt: bekannt oder abgelehnt.
EFFEKTE = ("roboter", "tv", "kollektiv")

_TREMOLO_HZ = 55.0          # Ringmodulation knapp ueber der Grundfrequenz
_TREMOLO_TIEFE = 0.25       # 0 = aus, 1 = bis zur Stille
_KOLLEKTIV_TREMOLO_HZ = 38.0
_KOLLEKTIV_TIEFE = 0.18
# Zwei Kopien, teilerfremde Verzoegerungen: bei glatten Vielfachen entstuende
# ein Kammfilter mit hoerbarem Grundton statt mehrerer Sprecher.
_KOLLEKTIV_STIMMEN = ((17.0, 0.42), (29.0, 0.30))       # (Verzoegerung ms, Pegel)
# Fernsehband. Unten schneidet der Hochpass die Waerme weg, oben nimmt der
# Tiefpass die Zischlaute -- zusammen ergibt das den Lautsprecher im Gehaeuse.
_TV_HOCHPASS_HZ = 320.0
_TV_TIEFPASS_HZ = 3000.0
_TV_RAUSCHEN = 0.006        # Grundrauschen des Geraets, kaum bewusst hoerbar
_TV_SAETTIGUNG = 1.6        # weiche Kennlinie, wie ein uebersteuerter kleiner Verstaerker


def ist_effekt(name: object) -> bool:
    return isinstance(name, str) and name in EFFEKTE


class Effekt:
    """Zustandsbehafteter Blockfilter. `verarbeite` bekommt und liefert PCM."""

    def __init__(self, name: str, rate: int):
        self.name = name
        self.rate = rate
        self.phase = 0.0                    # Tremolo, in Umdrehungen (0..1)
        self.hochpass_zustand = 0.0         # letzter Ausgang des Tiefpassanteils
        self.tiefpass_zustand = 0.0
        self.rausch_zustand = 12345         # eigener LCG: deterministisch und billig
        laengste = max(ms for ms, _ in _KOLLEKTIV_STIMMEN) if name == "kollektiv" else 0.0
        self.verzoegerung = array.array("h", bytes(int(rate * laengste / 1000) * 2 + 2))
        self.schreibkopf = 0

    def verarbeite(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        proben = array.array("h")
        proben.frombytes(pcm)
        if self.name == "roboter":
            self._tremolo(proben, _TREMOLO_HZ, _TREMOLO_TIEFE)
        elif self.name == "kollektiv":
            self._kollektiv(proben)
            self._tremolo(proben, _KOLLEKTIV_TREMOLO_HZ, _KOLLEKTIV_TIEFE)
        elif self.name == "tv":
            self._tv(proben)
        return proben.tobytes()

    # -- Bausteine ---------------------------------------------------------

    def _tremolo(self, proben: array.array, hertz: float, tiefe: float) -> None:
        """Amplitudenmodulation. Bei 55 Hz hoert man keine Lautstaerkeschwankung
        mehr, sondern eine metallische Beimischung -- der Rechnerklang."""
        schritt = hertz / self.rate
        phase = self.phase
        for i, wert in enumerate(proben):
            faktor = 1.0 - tiefe * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))
            proben[i] = _begrenze(wert * faktor)
            phase += schritt
            if phase >= 1.0:
                phase -= 1.0
        self.phase = phase

    def _kollektiv(self, proben: array.array) -> None:
        """Zwei verstimmte Kopien hinter dem Original: viele sprechen dasselbe.

        Die Verzoegerungen liegen unter 30 ms, also unter der Echoschwelle --
        wahrgenommen wird nicht "nochmal", sondern "mehrere".
        """
        puffer = self.verzoegerung
        laenge = len(puffer)
        versaetze = [(int(self.rate * ms / 1000), pegel) for ms, pegel in _KOLLEKTIV_STIMMEN]
        kopf = self.schreibkopf
        for i, wert in enumerate(proben):
            summe = float(wert)
            for versatz, pegel in versaetze:
                summe += puffer[(kopf - versatz) % laenge] * pegel
            puffer[kopf] = wert
            kopf = (kopf + 1) % laenge
            proben[i] = _begrenze(summe * 0.7)      # Platz fuer die Kopien
        self.schreibkopf = kopf

    def _tv(self, proben: array.array) -> None:
        """Schmales Band, weiche Saettigung, Grundrauschen: Lautsprecher von 1985."""
        hoch = math.exp(-2.0 * math.pi * _TV_HOCHPASS_HZ / self.rate)
        tief = math.exp(-2.0 * math.pi * _TV_TIEFPASS_HZ / self.rate)
        for i, wert in enumerate(proben):
            # Einpoliger Tiefpass; sein Ausgang abgezogen ergibt den Hochpass.
            self.hochpass_zustand = wert * (1.0 - hoch) + self.hochpass_zustand * hoch
            signal = wert - self.hochpass_zustand
            self.tiefpass_zustand = signal * (1.0 - tief) + self.tiefpass_zustand * tief
            signal = self.tiefpass_zustand
            signal = math.tanh(signal / 32768.0 * _TV_SAETTIGUNG) * 32768.0 / _TV_SAETTIGUNG
            self.rausch_zustand = (self.rausch_zustand * 1103515245 + 12345) & 0x7FFFFFFF
            signal += (self.rausch_zustand / 0x7FFFFFFF - 0.5) * _TV_RAUSCHEN * 32768.0
            proben[i] = _begrenze(signal * 1.35)    # das Band nimmt Pegel, hier zurueck
        return None


def _begrenze(wert: float) -> int:
    if wert > 32767.0:
        return 32767
    if wert < -32768.0:
        return -32768
    return int(wert)


def demo() -> None:
    """Selbstpruefung ohne Hardware: laeuft jeder Effekt sauber und im Rahmen?"""
    rate = 48_000
    ton = array.array("h", [int(9000 * math.sin(i * 0.05)) for i in range(rate // 10)])
    roh = ton.tobytes()
    for name in EFFEKTE:
        effekt = Effekt(name, rate)
        # In zwei Bloecken: der Zustand muss ueber die Grenze tragen.
        erst = effekt.verarbeite(roh[:len(roh) // 2])
        rest = effekt.verarbeite(roh[len(roh) // 2:])
        assert len(erst) + len(rest) == len(roh), name
        werte = array.array("h")
        werte.frombytes(erst + rest)
        assert any(werte), f"{name} liefert Stille"
        assert max(abs(w) for w in werte) <= 32767, name
    assert not ist_effekt("rm -rf"), "unbekannte Namen muessen abgelehnt werden"
    print("effekte: ok")


if __name__ == "__main__":
    demo()
