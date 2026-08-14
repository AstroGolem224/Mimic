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

import numpy as np

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
        # Verlauf der EINGANGSproben statt Ringpuffer mit Schreibkopf: die
        # Kopien lesen nur Vergangenes, nie Ausgegebenes -- damit ist das ein
        # FIR-Filter und in einem Rutsch rechenbar. Ein Ringpuffer zwingt zur
        # Schleife, weil der Index je Probe weiterwandert.
        self.verlauf = np.zeros(int(rate * laengste / 1000) + 1, dtype=np.float64)

    def verarbeite(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        proben = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if self.name == "roboter":
            proben = self._tremolo(proben, _TREMOLO_HZ, _TREMOLO_TIEFE)
        elif self.name == "kollektiv":
            # Zwischen den Stufen auf int16 begrenzen, nicht erst am Ende. Die
            # alte Kette tat das Probe fuer Probe, und bei uebersteuertem
            # Material ist der Unterschied hoerbar: durchgehend in Gleitkomma
            # gerechnet wich das Ergebnis um bis zu 2953 LSB ab.
            proben = _begrenze_feld(self._kollektiv(proben)).astype(np.float64)
            proben = self._tremolo(proben, _KOLLEKTIV_TREMOLO_HZ, _KOLLEKTIV_TIEFE)
        elif self.name == "tv":
            return self._tv_bytes(proben)
        return _begrenze_feld(proben).tobytes()

    # -- Bausteine ---------------------------------------------------------

    def _tremolo(self, proben: np.ndarray, hertz: float, tiefe: float) -> np.ndarray:
        """Amplitudenmodulation. Bei 55 Hz hoert man keine Lautstaerkeschwankung
        mehr, sondern eine metallische Beimischung -- der Rechnerklang.

        Die Phase wird als Feld gerechnet statt Probe fuer Probe aufaddiert. Der
        Rest modulo 1 haelt sie im selben Bereich wie die alte Schleife -- ohne
        ihn liefe das Argument des Kosinus ueber eine Minute Ton in Bereiche, wo
        die Gleitkommaaufloesung merklich groeber ist.
        """
        schritt = hertz / self.rate
        phase = (self.phase + np.arange(len(proben), dtype=np.float64) * schritt) % 1.0
        faktor = 1.0 - tiefe * (0.5 - 0.5 * np.cos(2.0 * np.pi * phase))
        self.phase = float((self.phase + len(proben) * schritt) % 1.0)
        return proben * faktor

    def _kollektiv(self, proben: np.ndarray) -> np.ndarray:
        """Zwei verstimmte Kopien hinter dem Original: viele sprechen dasselbe.

        Die Verzoegerungen liegen unter 30 ms, also unter der Echoschwelle --
        wahrgenommen wird nicht "nochmal", sondern "mehrere".
        """
        vorrat = len(self.verlauf)
        alles = np.concatenate((self.verlauf, proben))
        summe = proben.copy()
        for ms, pegel in _KOLLEKTIV_STIMMEN:
            versatz = int(self.rate * ms / 1000)
            summe += alles[vorrat - versatz:vorrat - versatz + len(proben)] * pegel
        self.verlauf = alles[-vorrat:]
        return summe * 0.7                          # Platz fuer die Kopien

    def _tv_bytes(self, proben: np.ndarray) -> bytes:
        """Schmales Band, weiche Saettigung, Grundrauschen: Lautsprecher von 1985.

        ponytail: die beiden Einpolfilter bleiben eine Python-Schleife. Ein IIR
        haengt Probe fuer Probe an seinem eigenen letzten Ausgang, das laesst
        sich mit numpy allein nicht ausrollen -- dafuer braeuchte es
        scipy.signal.lfilter, und scipy ist keine Abhaengigkeit dieses Projekts
        (numpy schon). Saettigung, Rauschen und Begrenzung liegen ausserhalb der
        Rueckkopplung und werden deshalb als Feld gerechnet. Wenn der TV-Effekt
        je im heissen Pfad stoert: scipy dazunehmen und die Schleife durch zwei
        lfilter-Aufrufe ersetzen.
        """
        hoch = math.exp(-2.0 * math.pi * _TV_HOCHPASS_HZ / self.rate)
        tief = math.exp(-2.0 * math.pi * _TV_TIEFPASS_HZ / self.rate)
        # Ueber eine Python-Liste laufen, nicht ueber das numpy-Feld: jedes
        # proben[i] auf einem ndarray baut ein Skalarobjekt und war damit
        # langsamer als die alte array.array-Schleife (18.6 statt 15.1 ms).
        gefiltert = []
        hochpass, tiefpass = self.hochpass_zustand, self.tiefpass_zustand
        zustand = self.rausch_zustand
        rauschen = []
        for wert in proben.tolist():
            # Einpoliger Tiefpass; sein Ausgang abgezogen ergibt den Hochpass.
            hochpass = wert * (1.0 - hoch) + hochpass * hoch
            tiefpass = (wert - hochpass) * (1.0 - tief) + tiefpass * tief
            gefiltert.append(tiefpass)
            zustand = (zustand * 1103515245 + 12345) & 0x7FFFFFFF
            rauschen.append(zustand)
        self.hochpass_zustand, self.tiefpass_zustand = hochpass, tiefpass
        self.rausch_zustand = zustand

        signal = np.tanh(np.array(gefiltert) / 32768.0 * _TV_SAETTIGUNG) * 32768.0 / _TV_SAETTIGUNG
        signal += (np.array(rauschen, dtype=np.float64) / 0x7FFFFFFF - 0.5) * _TV_RAUSCHEN * 32768.0
        return _begrenze_feld(signal * 1.35).tobytes()  # das Band nimmt Pegel, hier zurueck


def _begrenze_feld(werte: np.ndarray) -> np.ndarray:
    """Wie die alte Probe-fuer-Probe-Begrenzung: erst kappen, dann zur Null hin
    abschneiden. astype(int16) schneidet ab wie int(), nicht wie round()."""
    return np.clip(werte, -32768.0, 32767.0).astype("<i2")


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
