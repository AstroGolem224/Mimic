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
import time

import numpy as np

# Namen, die in der settings.json eines Profils stehen duerfen. Freie
# Filterketten waeren eine Einladung, dem Worker beliebige Rechenlast
# unterzuschieben -- hier gilt: bekannt oder abgelehnt.
EFFEKTE = ("roboter", "tv", "telefon", "vocoder", "kollektiv")

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
# Fernsprechband, seit 1930 unveraendert: 300 bis 3400 Hz. Steilflankiger als
# das Fernsehband -- ein Telefon klingt duenn, nicht dumpf. Kein Saettigen,
# dafuer mehr Rauschen: die Leitung, nicht der Verstaerker.
_TELEFON_BAND_HZ = (300.0, 3400.0)
_TELEFON_ORDNUNG = 4
_TELEFON_RAUSCHEN = 0.015
# Kanalvocoder. Baender logarithmisch geteilt -- gleichmaessig geteilt laege
# die Haelfte oberhalb der Formanten, die den Vokal ueberhaupt ausmachen. Die
# Huellkurve je Band folgt mit 30 Hz: schnell genug fuer Konsonanten, langsam
# genug, dass die Grundfrequenz nicht durchschlaegt.
# 16 statt 24 Baender: gemessen 13.0 statt 19.1 ms je Sekunde Audio, und der
# Pegel faellt besser aus, weil breitere Baender mehr Oberwellen einfangen.
_VOCODER_BAENDER = 16
# Unten 80 Hz, nicht 180: die Grundfrequenz einer tiefen Maennerstimme liegt
# bei 70 bis 100 Hz, und ein Band, das erst darueber anfaengt, sieht von ihr
# nichts -- gemessen an einem 150-Hz-Ton kam die Ausgabe mit Spitze 278 statt
# 7641 heraus, also praktisch stumm.
_VOCODER_BAND_HZ = (80.0, 7000.0)
_VOCODER_HUELLE_HZ = 30.0
_VOCODER_TRAEGER_HZ = 110.0     # Rueckfall, solange die Stimme stimmlos ist


# Grenzen der Sprechgeschwindigkeit. Unter 0.5 zerfaellt die Prosodie hoerbar,
# ueber 2.0 ist nichts mehr zu verstehen -- beides waere kein Regler, sondern
# eine Falle.
TEMPO_MIN, TEMPO_MAX = 0.5, 2.0
# Tonhoehe in Halbtoenen. Eine Oktave in jede Richtung: darueber hinaus bleibt
# von der Formantlage der Referenz nichts uebrig, das Ergebnis ist Chipmunk
# oder Nebelhorn, nicht mehr dieselbe Stimme mit anderer Lage.
TONHOEHE_MIN, TONHOEHE_MAX = -12.0, 12.0
# Streuung in Halbtoenen: um so viel darf eine Silbe zufaellig danebenliegen.
# 1 ist die Vorlage, 4 ist Zwoelftonmusik.
STREUUNG_MIN, STREUUNG_MAX = 0.0, 4.0
# Wie stark die Tonhoehe aufs Halbtonraster gezogen wird. 0 aus, 1 ganz.
RASTER_MIN, RASTER_MAX = 0.0, 1.0
# Formantverschiebung in Halbtoenen, gleiche Spanne wie die Tonhoehe.
FORMANT_MIN, FORMANT_MAX = -12.0, 12.0
# Die Klangfarbenregler laufen alle von 0 bis 1. Die Kennlinie sitzt im
# jeweiligen DSP-Code, wo sie begruendet werden kann -- nicht im Zahlenbereich,
# wo sie nur eine Zahl waere, die niemand einordnen kann.
HALL_MIN, HALL_MAX = 0.0, 1.0
VERZERRUNG_MIN, VERZERRUNG_MAX = 0.0, 1.0
KRUEMEL_MIN, KRUEMEL_MAX = 0.0, 1.0
BREITE_MIN, BREITE_MAX = 0.0, 1.0
# Die Werte, mit denen es nach GLaDOS klingt: Tonhoehe aufs Raster gezwungen,
# Silben gelegentlich einen Halbton daneben, Formanten zwei Halbtoene hoch.
GLADOS = {"raster": 1.0, "streuung": 1.0, "formant": 2.0, "tonhoehe": 0.0}


def ist_effekt(name: object) -> bool:
    return isinstance(name, str) and name in EFFEKTE


def _zahl(wert: object, mini: float, maxi: float, vorgabe: float) -> float:
    """Reglerwunsch auf einen brauchbaren Wert bringen. Unbrauchbares wird die
    Vorgabe statt eines Fehlers: ein Regler darf nie der Grund sein, dass gar
    nichts kommt."""
    try:
        zahl = float(wert)                      # type: ignore[arg-type]
    except (TypeError, ValueError):
        return vorgabe
    if not math.isfinite(zahl):
        return vorgabe
    return min(maxi, max(mini, zahl))


def tempo_faktor(wert: object) -> float:
    return _zahl(wert, TEMPO_MIN, TEMPO_MAX, 1.0)


def tonhoehe_wert(wert: object) -> float:
    return _zahl(wert, TONHOEHE_MIN, TONHOEHE_MAX, 0.0)


def streuung_wert(wert: object) -> float:
    return _zahl(wert, STREUUNG_MIN, STREUUNG_MAX, 0.0)


def raster_wert(wert: object) -> float:
    return _zahl(wert, RASTER_MIN, RASTER_MAX, 0.0)


def formant_wert(wert: object) -> float:
    return _zahl(wert, FORMANT_MIN, FORMANT_MAX, 0.0)


def hall_wert(wert: object) -> float:
    return _zahl(wert, HALL_MIN, HALL_MAX, 0.0)


def verzerrung_wert(wert: object) -> float:
    return _zahl(wert, VERZERRUNG_MIN, VERZERRUNG_MAX, 0.0)


def kruemel_wert(wert: object) -> float:
    return _zahl(wert, KRUEMEL_MIN, KRUEMEL_MAX, 0.0)


def breite_wert(wert: object) -> float:
    return _zahl(wert, BREITE_MIN, BREITE_MAX, 0.0)


def _bandpass(rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Butterworth-Bandpass als Kaskade zweiter Ordnung, samt Nullzustand.

    `sos` statt `b, a`: bei vierter Ordnung und 300 Hz unteren Eckpunkt liegen
    die Pole so dicht am Einheitskreis, dass die ausmultiplizierte Form in
    doppelter Genauigkeit hoerbar driftet. Der Import steht in der Funktion --
    siehe `_tv_bytes`.
    """
    from scipy.signal import butter, sosfilt_zi

    sos = butter(_TELEFON_ORDNUNG, _TELEFON_BAND_HZ, btype="band", fs=rate, output="sos")
    # `sosfilt_zi` liefert den eingeschwungenen Zustand fuer Eingang 1.0; mal
    # null ergibt das die richtige Form mit Ruhe drin.
    return sos, sosfilt_zi(sos) * 0.0


def _vocoder_baender(rate: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Filterbank fuer den Kanalvocoder, je Band `sos` samt Nullzustand."""
    from scipy.signal import butter, sosfilt_zi

    tief, hoch = _VOCODER_BAND_HZ
    kanten = np.geomspace(tief, min(hoch, rate / 2 * 0.95), _VOCODER_BAENDER + 1)
    bank = []
    for unten, oben in zip(kanten[:-1], kanten[1:]):
        sos = butter(2, (unten, oben), btype="band", fs=rate, output="sos")
        bank.append((sos, sosfilt_zi(sos) * 0.0))
    return bank


class Effekt:
    """Zustandsbehafteter Blockfilter. `verarbeite` bekommt und liefert PCM."""

    # Wie laut die Summe der modulierten Baender neben dem Original steht.
    # Gemessen an Sprache und stehenden Toenen: 2.4 trifft den Pegel des
    # Eingangs, ohne dass laute Vokale in die Begrenzung laufen.
    VOCODER_PEGEL = 2.4

    def __init__(self, name: str, rate: int):
        self.name = name
        self.rate = rate
        self.phase = 0.0                    # Tremolo, in Umdrehungen (0..1)
        # Filterzustaende in der Form, die `lfilter`/`sosfilt` als `zi` erwarten:
        # so wandern sie ohne Umrechnung ueber die Blockgrenze.
        self.hochpass_zustand = np.zeros(1)
        self.tiefpass_zustand = np.zeros(1)
        self.wuerfel = np.random.default_rng(12345)     # fester Start: wiederholbar
        self.sos = self.filter_zustand = None
        if name == "telefon":
            self.sos, self.filter_zustand = _bandpass(rate)
        self.bank: list = []
        if name == "vocoder":
            self.bank = _vocoder_baender(rate)
            self.huelle_zustand = [np.zeros(1) for _ in self.bank]
            self.traeger_zustand = [zi.copy() for _, zi in self.bank]
            self.grundton = _Tonhoehenleser(rate)
            self.traeger_phase = 0.0
            self.letzte_f0 = _VOCODER_TRAEGER_HZ
        laengste = max(ms for ms, _ in _KOLLEKTIV_STIMMEN) if name == "kollektiv" else 0.0
        # Verlauf der EINGANGSproben statt Ringpuffer mit Schreibkopf: die
        # Kopien lesen nur Vergangenes, nie Ausgegebenes -- damit ist das ein
        # FIR-Filter und in einem Rutsch rechenbar. Ein Ringpuffer zwingt zur
        # Schleife, weil der Index je Probe weiterwandert.
        self.verlauf = np.zeros(int(rate * laengste / 1000) + 1, dtype=np.float64)

    def verarbeite(self, pcm: bytes) -> bytes:
        """Byte-Fassung fuer Aufrufer ausserhalb der Kette (Tests, `demo`)."""
        if not pcm:
            return pcm
        return _begrenze_feld(self.feld(np.frombuffer(pcm, dtype="<i2").astype(np.float64))).tobytes()

    def feld(self, proben: np.ndarray) -> np.ndarray:
        """Die Fassung, die in der Kette laeuft: Gleitkomma rein, Gleitkomma raus."""
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
            proben = self._tv(proben)
        elif self.name == "telefon":
            proben = self._telefon(proben)
        elif self.name == "vocoder":
            proben = self._vocoder(proben)
        return proben

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

    def _rauschen(self, anzahl: int) -> np.ndarray:
        """Grundrauschen zwischen -0.5 und 0.5, blockweise als Feld.

        Fester Startwert statt Systemzufall: derselbe Text muss zweimal gleich
        klingen, sonst ist Abhoeren wertlos. Vorher stand hier ein eigener LCG
        Probe fuer Probe -- er war der teuerste Teil des TV-Effekts, und ein
        `Generator` ist genauso wiederholbar.
        """
        return self.wuerfel.random(anzahl) - 0.5

    def _tv(self, proben: np.ndarray) -> np.ndarray:
        """Schmales Band, weiche Saettigung, Grundrauschen: Lautsprecher von 1985.

        Die beiden Einpolfilter laufen als `lfilter` mit mitgefuehrtem `zi`
        statt als Python-Schleife -- dieselbe Rechnung, nur nicht Probe fuer
        Probe. Das war die `ponytail:`-Schuld an dieser Stelle: scipy schien
        keine Abhaengigkeit zu sein, liegt aber laengst ueber librosa im Lock.
        Gemessen: 7.0 ms je Sekunde Audio vorher, 0.4 ms danach.

        Der Import steht bewusst IN der Methode. `import scipy.signal` kostet
        kalt 306 ms, und dieses Modul wird auch vom Frontend geladen -- das ist
        der Pfad, um dessen Millisekunden PHASE2 Schritt 3 vor `submit()` kaempft.
        """
        from scipy.signal import lfilter

        hoch = math.exp(-2.0 * math.pi * _TV_HOCHPASS_HZ / self.rate)
        tief = math.exp(-2.0 * math.pi * _TV_TIEFPASS_HZ / self.rate)
        # Einpoliger Tiefpass; sein Ausgang abgezogen ergibt den Hochpassanteil.
        hochpass, self.hochpass_zustand = lfilter(
            [1.0 - hoch], [1.0, -hoch], proben, zi=self.hochpass_zustand)
        gefiltert, self.tiefpass_zustand = lfilter(
            [1.0 - tief], [1.0, -tief], proben - hochpass, zi=self.tiefpass_zustand)

        signal = np.tanh(gefiltert / 32768.0 * _TV_SAETTIGUNG) * 32768.0 / _TV_SAETTIGUNG
        signal += self._rauschen(len(proben)) * _TV_RAUSCHEN * 32768.0
        return signal * 1.35                    # das Band nimmt Pegel, hier zurueck

    def _vocoder(self, proben: np.ndarray) -> np.ndarray:
        """Kanalvocoder: die Stimme gibt die Huellkurven, ein Saegezahn den Ton.

        Nicht zu verwechseln mit dem Phasenvocoder -- der steckt als WSOLA im
        `Klangregler` und verschiebt Tonhoehen. Hier wird die Stimme in 24
        Baender zerlegt, je Band die Lautstaerke abgelesen, und damit dasselbe
        Band eines Saegezahns moduliert. Uebrig bleibt, WAS gesagt wurde -- die
        Formanten -- auf einer Stimme, die es nicht gibt.

        Der Traeger folgt der geschaetzten Grundfrequenz statt auf einem festen
        Ton zu stehen: ein starrer Saegezahn klingt tot, weil die Satzmelodie
        vollstaendig verschwindet. Waehrend stimmloser Stellen bleibt die letzte
        Frequenz stehen -- ein Sprung auf einen Vorgabewert waere bei jedem
        Zischlaut hoerbar.
        """
        from scipy.signal import lfilter, sosfilt

        f0 = self.grundton(proben)
        if f0:
            self.letzte_f0 = f0
        # Saegezahn ueber die Phase: der Sprung von 1 auf -1 ist dieselbe Art
        # Anregung wie die einer Stimmlippe -- viele Oberwellen, alle harmonisch,
        # damit die Filterbank ueberall etwas zu modulieren hat.
        schritt = self.letzte_f0 / self.rate
        phase = (self.traeger_phase + np.arange(len(proben)) * schritt) % 1.0
        self.traeger_phase = float((self.traeger_phase + len(proben) * schritt) % 1.0)
        saegezahn = 2.0 * phase - 1.0

        glatt = math.exp(-2.0 * math.pi * _VOCODER_HUELLE_HZ / self.rate)
        summe = np.zeros(len(proben))
        for i, (sos, analyse_zi) in enumerate(self.bank):
            stimme, analyse_zi = sosfilt(sos, proben, zi=analyse_zi)
            # Huellkurve: gleichgerichtet und einpolig geglaettet. Ein echter
            # Effektivwert braeuchte ein Fenster und damit Latenz.
            huelle, self.huelle_zustand[i] = lfilter(
                [1.0 - glatt], [1.0, -glatt], np.abs(stimme), zi=self.huelle_zustand[i])
            # Der Traeger braucht seinen EIGENEN Filterzustand. Mit dem der
            # Analyse geteilt klingelt das Band bei jedem Blockwechsel.
            traeger, self.traeger_zustand[i] = sosfilt(
                sos, saegezahn, zi=self.traeger_zustand[i])
            summe += traeger * huelle
            self.bank[i] = (sos, analyse_zi)
        # Feste Verstaerkung, nicht blockweise normiert: eine Normierung je
        # Block laesst die Lautstaerke an jeder Blockgrenze springen, und genau
        # das hoert man als Knacken.
        return summe * self.VOCODER_PEGEL

    def _telefon(self, proben: np.ndarray) -> np.ndarray:
        """Fernsprechband 300--3400 Hz, vierter Ordnung, plus Leitungsrauschen.

        Steiler als das Fernsehband und ohne Saettigung: ein Telefon klingt
        duenn, nicht uebersteuert. Der Zustand `filter_zustand` traegt ueber
        Blockgrenzen -- ohne ihn klickt jede Naht.

        Das Rauschen liegt VOR dem Filter, anders als beim Fernseher: dort
        rauscht der Verstaerker hinter dem Band, hier die Leitung davor. Hinter
        dem Filter waere es breitbandig und zoege den spektralen Schwerpunkt aus
        dem Fernsprechband heraus -- hoerbar als Zischeln neben der Stimme.
        """
        from scipy.signal import sosfilt

        laut = proben + self._rauschen(len(proben)) * _TELEFON_RAUSCHEN * 32768.0
        gefiltert, self.filter_zustand = sosfilt(self.sos, laut, zi=self.filter_zustand)
        return gefiltert * 1.6                  # das enge Band nimmt Pegel


class _Neuabtastung:
    """Lineare Interpolation mit Bruchteil-Zustand ueber Blockgrenzen.

    Sie ist der zweite Teil des Tonhoehenschiebers: die WSOLA-Dehnung macht den
    Ton laenger, das Neuabtasten schiebt ihn wieder auf die alte Dauer und nimmt
    die Tonhoehe mit hoch. Linear statt sinc: die Quelle ist Sprache bei 48 kHz,
    da liegt der Aliasfehler weit unter dem, was das Modell selbst produziert.
    """

    def __init__(self) -> None:
        self.rest = np.zeros(0, dtype=np.float64)
        self.phase = 0.0

    def __call__(self, proben: np.ndarray, verhaeltnis: float) -> np.ndarray:
        quelle = np.concatenate((self.rest, proben))
        anzahl = int(np.ceil((len(quelle) - 1 - self.phase) / verhaeltnis)) if len(quelle) > 1 else 0
        if anzahl <= 0:
            self.rest = quelle
            return np.zeros(0)
        stelle = self.phase + np.arange(anzahl) * verhaeltnis
        ganz = stelle.astype(np.int64)
        bruch = stelle - ganz
        aus = quelle[ganz] * (1.0 - bruch) + quelle[ganz + 1] * bruch
        naechste = stelle[-1] + verhaeltnis
        behalten = int(naechste)
        self.rest = quelle[behalten:]
        self.phase = naechste - behalten
        return aus


def _periode(feld: np.ndarray, tief: int, hoch: int, vorher: int = 0) -> tuple[int, float]:
    """Grundperiode in Proben, samt Guete zwischen 0 und 1.

    Normierte Kreuzkorrelation, nicht die nackte Autokorrelation: die nackte
    bevorzugt kurze Verschiebungen (Oktave zu hoch), eine Normierung auf die
    Ueberlappungslaenge bevorzugt lange (gemessen: ein sauberer 225-Hz-Ton kam
    als 74.5 Hz heraus, also die dritte Unterharmonische). Die Normierung auf
    die Energie beider Haelften macht alle Vielfachen der Periode gleich gut --
    deshalb gewinnt am Ende die KUERZESTE Verschiebung, die nahe an den besten
    Wert herankommt.
    """
    laenge = 1 << (2 * len(feld) - 1).bit_length()
    spektrum = np.fft.rfft(feld, laenge)
    akf = np.fft.irfft(spektrum * np.conj(spektrum))[:len(feld)]
    energie = np.concatenate(([0.0], np.cumsum(feld * feld)))
    hoch = min(hoch, len(feld) - 1)
    if hoch <= tief or energie[-1] <= 0.0:
        return 0, 0.0
    stellen = np.arange(tief, hoch)
    links = energie[len(feld) - stellen]
    rechts = energie[len(feld)] - energie[stellen]
    guete = akf[tief:hoch] / np.sqrt(links * rechts + 1e-9)
    beste = float(guete.max())
    if beste <= 0.0:
        return 0, 0.0
    # Nur echte Gipfel zaehlen. Ohne diese Maske genuegte schon eine um 7 %
    # verkuerzte Verschiebung, um ueber die Schwelle zu kommen (ein Sinus
    # korreliert dort noch mit 0.90) -- gemessen kam ein 225-Hz-Ton als 241 Hz
    # heraus, und das Raster rundete ihn auf den falschen Halbton.
    gipfel = np.flatnonzero((guete[1:-1] > guete[:-2]) & (guete[1:-1] >= guete[2:])) + 1
    tauglich = gipfel[guete[gipfel] >= 0.9 * beste]
    if not len(tauglich):
        stelle = int(np.argmax(guete))
    elif vorher:
        # Fortsetzung vor Bestwert: fast gleich gute Gipfel liegen bei einem
        # Vielfachen der Periode, und ohne diesen Vorzug springt die Schaetzung
        # mitten im Vokal eine Oktave -- die Korrektur zoege den Ton dann auf
        # einen Halbton, den es nie gab. Etwas lockerere Schwelle, damit die
        # richtige Fortsetzung nicht knapp herausfaellt.
        nahe = gipfel[guete[gipfel] >= 0.75 * beste]
        stelle = int(nahe[np.argmin(np.abs(np.log((tief + nahe) / vorher)))])
    else:
        stelle = int(tauglich[0])
    return tief + stelle, float(guete[stelle])


class _Formant:
    """Verschiebt die spektrale Huellkurve, ohne die Grundfrequenz zu bewegen.

    Der dritte Schritt der Vorlage: "the formant moved up". Formanten sind die
    Resonanzen des Vokaltrakts -- sie hoeher zu setzen, ohne die Tonhoehe
    mitzunehmen, klingt nach einem kleineren Kopf als der Sprecher hat, und
    genau das ist das Blecherne an GLaDOS. Blosses Umabtasten kann das nicht:
    es zieht Huellkurve und Grundton gemeinsam.

    Verfahren: STFT, Huellkurve per Cepstrum-Glaettung aus dem Log-Betrag, die
    Huellkurve entlang der Frequenzachse gestaucht oder gestreckt, das Spektrum
    mit dem Verhaeltnis beider Huellkurven multipliziert, zurueck. Analyse- und
    Syntheseseite tragen beide ein Hann-Fenster; bei einem Viertel Sprung ist
    die Summe der Quadrate konstant, ein fester Teiler genuegt.
    """

    N = 1024                    # 21 ms bei 48 kHz: feiner als jeder Formant
    HOP = 256
    LIFTER = 30                 # so viele Cepstrum-Koeffizienten sind Huellkurve

    def __init__(self, halbtoene: float):
        self.k = 2.0 ** (halbtoene / 12.0)
        self.fenster = np.hanning(self.N + 1)[:self.N]
        self.norm = float((self.fenster ** 2)[::self.HOP].sum())
        self.puffer = np.zeros(0, dtype=np.float64)
        self.schwanz = np.zeros(self.N - self.HOP)
        self.bins = np.arange(self.N // 2 + 1, dtype=np.float64)
        self.ein = self.aus = 0

    def verarbeite(self, proben: np.ndarray) -> np.ndarray:
        self.ein += len(proben)
        self.puffer = np.concatenate((self.puffer, proben))
        fertig: list[np.ndarray] = []
        while len(self.puffer) >= self.N:
            rahmen = self.puffer[:self.N] * self.fenster
            spektrum = np.fft.rfft(rahmen)
            betrag = np.abs(spektrum)
            log = np.log(betrag + 1e-6)
            cepstrum = np.fft.rfft(log)
            cepstrum[self.LIFTER:] = 0.0
            huelle = np.fft.irfft(cepstrum, len(log))
            # Huellkurve an der gestauchten Frequenzachse ablesen: k>1 holt die
            # Resonanzen von weiter unten nach oben.
            verschoben = np.interp(self.bins / self.k, self.bins, huelle)
            gefiltert = np.fft.irfft(spektrum * np.exp(verschoben - huelle), self.N)
            gefiltert *= self.fenster / self.norm
            gefiltert[:len(self.schwanz)] += self.schwanz
            fertig.append(gefiltert[:self.HOP])
            self.schwanz = gefiltert[self.HOP:]
            self.puffer = self.puffer[self.HOP:]
        aus = np.concatenate(fertig) if fertig else np.zeros(0)
        self.aus += len(aus)
        return aus

    def abschluss(self) -> np.ndarray:
        """Den Ueberhang ausspuelen; mit Nullen, damit der letzte Rahmen voll
        wird, und auf die Eingangslaenge beschnitten -- die Nullen sind Fuellung,
        keine Ausgabe."""
        offen = self.ein - self.aus
        rest = self.verarbeite(np.zeros(self.N))
        return np.concatenate((rest, self.schwanz))[:max(0, offen)]


class _Tonhoehenleser:
    """Grundfrequenz eines Rahmens, mit Gedaechtnis fuer den vorigen Wert.

    Herausgeloest aus `Klangregler`, weil der Vocoder dieselbe Schaetzung
    braucht: sein Traeger muss der Stimme folgen, sonst klingt er tot. Zwei
    Kopien derselben Autokorrelation waeren zwei Orte, an denen dieselbe
    Schwelle nachgezogen werden muesste.

    Das Gedaechtnis (`letzte`) ist genau ein Wert und dient der
    Fortsetzungsregel in `_periode`: ohne ihn springt die Schaetzung
    gelegentlich eine Oktave.
    """

    F0_MIN, F0_MAX = 70.0, 320.0        # Sprachbereich; weiter aussen ist es Rauschen
    STIMMHAFT_RMS = 250.0               # darunter: Stille oder Zischlaut, keine Tonhoehe
    STIMMHAFT_AKF = 0.45                # Gipfel der normierten Autokorrelation

    def __init__(self, rate: int):
        self.rate = rate
        self.letzte: list[float] = []

    def __call__(self, feld: np.ndarray) -> float:
        """Grundfrequenz des Rahmens, 0 bei stimmlos oder still."""
        mitte = feld - feld.mean()
        if math.sqrt(float(np.mean(mitte * mitte))) < self.STIMMHAFT_RMS:
            return 0.0
        vorher = int(self.rate / self.letzte[-1]) if self.letzte else 0
        lag, guete = _periode(mitte, max(1, int(self.rate / self.F0_MAX)),
                              int(self.rate / self.F0_MIN), vorher)
        if not lag or guete < self.STIMMHAFT_AKF:
            self.letzte.clear()
            return 0.0
        # Kein Median ueber mehrere Rahmen: er kostete zwei Rahmen Verzug, und
        # die Korrektur lief der Stimme damit hinterher statt auf ihr zu sitzen
        # (gemessen: Bewegung in 120 ms 0.86 statt 0.48 Halbtoene). Gegen
        # Ausrutscher steht die Fortsetzungsregel in `_periode`.
        self.letzte.append(self.rate / lag)
        del self.letzte[:-1]
        return self.letzte[-1]


class Klangregler:
    """Tempo, Tonhoehe, Tonhoehenraster und Formant in einem Durchgang.

    Das Modell kennt weder Tempo- noch Tonhoehenparameter (`generate_stream`
    hat keine), und die Abspielrate zu verstellen zoege beides zusammen --
    deshalb hier, an derselben Stelle wie die Effekte.

    - `faktor`: Geschwindigkeit, 1.0 unveraendert, 1.5 anderthalbfach so schnell.
    - `halbtoene`: feste Verschiebung der Tonhoehe.
    - `raster`: 0 aus, 1 zwingt jede Silbe auf den naechsten Halbton und haelt
      sie dort. Das ist der GLaDOS-Kern -- laut Valves eigener Anleitung wurde
      Ellen McLains Aufnahme "pitch constrained, pitch modulation suppressed",
      also aufs Halbtonraster gezogen und innerhalb der Silbe flachgebuegelt.
      Kein Vocoder, kein Ringmodulator: es ist Tonhoehenkorrektur.
    - `streuung`: Halbtoene, um die eine neu erkannte Silbe zufaellig daneben
      gesetzt wird -- die Handarbeit der Vorlage ("manually shift individual
      syllables"), die das Ergebnis kuenstlicher macht als reines Autotune.
      Gewuerfelt wird je Silbe, nicht nach der Uhr: ein Wackeln mitten im
      gehaltenen Vokal klingt nach Tonband, nicht nach Maschine.
    - `formant`: Halbtoene, um die die Huellkurve steigt ("formant moved up").

    Verfahren: ueberlappende Rahmen mit Hann-Fenster, Ausgangssprung fest bei
    halber Rahmenlaenge (Hann summiert sich dort zu eins), Eingangssprung um
    `faktor` groesser oder kleiner. Der Suchbereich verschiebt jeden Rahmen auf
    die Stelle, die am besten an den vorigen anschliesst -- ohne ihn brechen
    die Perioden an jeder Naht und die Stimme klingt blechern. Fuer die Tonhoehe
    dehnt der Kern um `faktor / verhaeltnis` und das Neuabtasten holt den Rest;
    die Ausgabedauer haengt damit nur am Tempo, nicht an der Tonhoehe.

    Der Ausgang haengt einen halben Rahmen hinterher; `abschluss` gibt ihn frei.
    """

    RAHMEN_MS = 30.0            # lang genug fuer Stimmbandperioden bis ~70 Hz
    SUCHE_MS = 8.0              # Spielraum der Nahtsuche, gut eine Periode
    BEZUG_HZ = 440.0            # Nullpunkt des Halbtonrasters
    # Analysefenster fuer die Tonhoehe. 40 ms sind vier Perioden bei 100 Hz und
    # damit gerade genug; laenger gemessen (60 ms) verschmiert die Korrektur:
    # vom Vibrato blieb ein Viertel stehen statt eines Sechstels.
    HOEREN_MS = 40.0
    MAX_KORREKTUR = 6.0         # Halbtoene; deckelt Ausrutscher des Schaetzers
    NOTE_NEU_HT = 1.2           # so weit darf eine gehaltene Silbe wandern
    NOTE_MIN_MS = 180.0         # so lange bleibt eine Silbe stehen, egal was kommt
    NOTE_BRUCH_HT = 3.0         # ausser die Stimme springt so weit -- dann ist es eine neue
    STIMMLOS_MS = 60.0          # so lange stimmlos, dann faengt die Silbe frei an

    def __init__(self, faktor: float, rate: int, halbtoene: float = 0.0,
                 streuung: float = 0.0, raster: float = 0.0, formant: float = 0.0):
        self.faktor = faktor
        self.rate = rate
        self.halbtoene = halbtoene
        self.streuung = streuung
        self.raster = raster
        self.n = max(4, int(rate * self.RAHMEN_MS / 1000) // 2 * 2)
        self.hop_aus = self.n // 2
        self.suche = int(rate * self.SUCHE_MS / 1000)
        # Periodisches Hann-Fenster: np.hanning ist symmetrisch und summiert
        # sich bei halber Ueberlappung NICHT zu eins -- das letzte Element weg.
        self.fenster = np.hanning(self.n + 1)[:self.n]
        self.puffer = np.zeros(0, dtype=np.float64)
        self.pos = 0                                    # Lesekopf im Puffer
        self.schwanz = np.zeros(self.hop_aus)           # halber Rahmen Ueberhang
        self.vorlage = np.zeros(self.hop_aus)           # ideale Fortsetzung
        self.ein = self.aus = 0                         # Proben rein / raus
        # Eigener LCG wie beim TV-Rauschen: derselbe Text klingt zweimal gleich.
        # Ein Zufall, den man nicht wiederholen kann, ist beim Abhoeren wertlos.
        self.rausch_zustand = 20260814
        self.abtastung = _Neuabtastung()
        self.formant = _Formant(formant) if formant else None
        self.hoerfenster = int(rate * self.HOEREN_MS / 1000)
        self.grundton = _Tonhoehenleser(rate)
        self.note: float | None = None                  # gehaltene Silbe, gemessen
        self.stufe = 0.0                                # dieselbe Silbe als Ziel
        self.stimmlos = 0
        self.stimmlos_grenze = int(rate * self.STIMMLOS_MS / 1000)
        self.gehalten = 0
        self.halte_grenze = int(rate * self.NOTE_MIN_MS / 1000)
        self.verhaeltnis = 2.0 ** (halbtoene / 12.0)
        self.hoert_hin = bool(raster or streuung)
        self.roh = not (halbtoene or raster or streuung)
        self.hop_ein = max(1, round(self.hop_aus * self.faktor / self.verhaeltnis))

    def _versatz(self) -> float:
        """Ganze Halbtoene daneben, wie von Hand verschobene Silben."""
        if not self.streuung:
            return 0.0
        self.rausch_zustand = (self.rausch_zustand * 1103515245 + 12345) & 0x7FFFFFFF
        return round((self.rausch_zustand / 0x7FFFFFFF * 2.0 - 1.0) * self.streuung)

    def _stimmlage(self, feld: np.ndarray) -> None:
        """Tonhoehe fuer den naechsten Rahmen festlegen und den Sprung ausrichten."""
        korrektur = 0.0
        f0 = self.grundton(feld) if self.hoert_hin else 0.0
        if f0:
            gemessen = 12.0 * math.log2(f0 / self.BEZUG_HZ)
            abstand = abs(gemessen - self.note) if self.note is not None else 99.0
            # Eine Silbe steht mindestens NOTE_MIN_MS, auch wenn die Stimme
            # darin wandert. Ohne diese Sperre folgt das Raster der Satzmelodie
            # in Halbtonstufen -- gerastert, aber immer noch eine Melodie. Die
            # Vorlage klingt anders: die Tonhoehe bleibt liegen und springt.
            # Ein weiter Satz (NOTE_BRUCH_HT) ist trotzdem eine neue Silbe.
            if self.note is None or abstand > self.NOTE_BRUCH_HT or (
                    abstand > self.NOTE_NEU_HT and self.gehalten >= self.halte_grenze):
                self.note = float(round(gemessen))
                self.stufe = self.note + self._versatz()
                self.gehalten = 0
            else:
                self.gehalten += self.hop_aus
            korrektur = self.raster * (self.stufe - gemessen)
            korrektur = min(self.MAX_KORREKTUR, max(-self.MAX_KORREKTUR, korrektur))
            self.stimmlos = 0
        else:
            self.stimmlos += self.hop_aus
            if self.stimmlos > self.stimmlos_grenze:
                self.note = None            # nach der Pause faengt die Silbe frei an
                self.gehalten = 0
        self.verhaeltnis = 2.0 ** ((self.halbtoene + korrektur) / 12.0)
        self.hop_ein = max(1, round(self.hop_aus * self.faktor / self.verhaeltnis))

    def verarbeite(self, pcm: bytes) -> bytes:
        proben = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        self.ein += len(proben)
        self.puffer = np.concatenate((self.puffer, proben))
        return self._ausgeben(self._mahlen(self.suche))

    def abschluss(self) -> bytes:
        """Rest des Puffers und den haengenden halben Rahmen ausgeben.

        Mit Nullen aufgefuellt, damit der letzte Rahmen die Naht noch suchen
        kann, und auf die rechnerische Gesamtlaenge beschnitten -- sonst waere
        jede Aeusserung um den Ueberhang zu lang oder zu kurz.
        """
        self.puffer = np.concatenate((self.puffer, np.zeros(self.n + self.suche)))
        fertig = self._mahlen(self.suche)
        fertig.append(self.schwanz if self.roh
                      else self.abtastung(self.schwanz, self.verhaeltnis))
        self.schwanz = np.zeros(self.hop_aus)
        rest = np.concatenate(fertig)
        ziel = max(0, round(self.ein / self.faktor) - self.aus)
        pcm = self._ausgeben([rest[:ziel]])
        if self.formant is not None:
            pcm += _begrenze_feld(self.formant.abschluss()).tobytes()
        return pcm

    def _mahlen(self, rand: int) -> list[np.ndarray]:
        fertig: list[np.ndarray] = []
        while self.pos + rand + self.n <= len(self.puffer):
            start = self._naht()
            # Erst hoeren, dann schneiden: Tonhoehe und Eingangssprung gelten
            # fuer genau den Rahmen, der gleich hinausgeht.
            if not self.roh:
                # Um den Block herum hoeren, der gleich hinausgeht, nicht ab
                # `start` nach vorn: der Ausgang besteht zur Haelfte aus dem
                # vorigen Rahmen, und ein Fenster, das nur nach vorn schaut,
                # korrigiert um bis zu 45 ms zu frueh. Bei stehendem Ton faellt
                # das nicht auf, bei Sprache war es der Grund, warum das Raster
                # fast nichts bewirkt hat (Abstand zum Halbton 0.18 -> 0.14).
                mitte = start + self.hop_aus // 2
                anfang = max(0, mitte - self.hoerfenster // 2)
                self._stimmlage(self.puffer[anfang:anfang + self.hoerfenster])
            rahmen = self.puffer[start:start + self.n] * self.fenster
            block = self.schwanz + rahmen[:self.hop_aus]
            fertig.append(block if self.roh else self.abtastung(block, self.verhaeltnis))
            self.schwanz = rahmen[self.hop_aus:].copy()
            self.vorlage = self.puffer[start + self.hop_aus:
                                       start + self.hop_aus + self.hop_aus].copy()
            # Der Lesekopf wandert vom SOLL-Punkt weiter, nicht vom verschobenen:
            # sonst summiert sich die Nahtsuche auf und das Tempo laeuft davon
            # (gemessen bei Faktor 0.5: elffache statt doppelter Laenge).
            self.pos += self.hop_ein
        # So viel Vergangenheit stehen lassen, wie das Hoerfenster zurueckreicht.
        schnitt = max(0, self.pos - max(self.suche, self.hoerfenster))
        self.puffer = self.puffer[schnitt:]
        self.pos -= schnitt
        return fertig

    def _ausgeben(self, teile: list[np.ndarray]) -> bytes:
        feld = np.concatenate(teile) if teile else np.zeros(0)
        # `aus` zaehlt vor dem Formanten: der haelt einen Rahmen zurueck, und
        # die Laengenrechnung in `abschluss` haette sonst eine Delle.
        self.aus += len(feld)
        if self.formant is not None:
            feld = self.formant.verarbeite(feld)
        if not len(feld):
            return b""
        return _begrenze_feld(feld).tobytes()

    def _naht(self) -> int:
        """Der Startpunkt im Suchbereich, der am besten an den vorigen Rahmen
        anschliesst. Kreuzkorrelation als ein Matrixprodukt statt einer
        Schleife ueber die Kandidaten -- bei 48 kHz sind das 769 Kandidaten je
        Rahmen, und die Rahmen kommen alle 15 ms."""
        erst = max(0, self.pos - self.suche)
        letzt = min(self.pos + self.suche, len(self.puffer) - self.n)
        if letzt <= erst:
            return max(0, min(self.pos, len(self.puffer) - self.n))
        feld = np.lib.stride_tricks.sliding_window_view(
            self.puffer[erst:letzt + self.hop_aus], self.hop_aus)
        return erst + int(np.argmax(feld @ self.vorlage))


class Verzerrer:
    """Weiche Saettigung: aus einem sauberen Ton wird ein uebersteuerter.

    `tanh(x*g)/tanh(g)` statt hartem Kappen. Der Nenner haelt den Vollausschlag
    bei Vollausschlag, sonst waere der Regler heimlich auch ein Lautstaerkeregler.

    Ehrlich benannt: die Kennlinie erzeugt Obertoene ueber der Nyquistgrenze, die
    als Aliasing zurueckfalten. Bei `tanh` und Sprache bleibt das unter dem, was
    das Modell selbst an Artefakten liefert -- deshalb kein Oversampling. Wer
    hart kappen will, braucht vierfache Abtastung, also viermal die Rechenlast.
    """

    MAX_VERSTAERKUNG = 20.0

    def __init__(self, staerke: float, rate: int):
        self.g = 1.0 + (self.MAX_VERSTAERKUNG - 1.0) * staerke
        self.norm = 32768.0 / math.tanh(self.g)

    def feld(self, proben: np.ndarray) -> np.ndarray:
        return np.tanh(proben / 32768.0 * self.g) * self.norm


class Kruemel:
    """Bitcrusher: grobe Stufen und gehaltene Proben, wie ein Sprachchip von 1982.

    Zwei Groben in einem Regler, weil sie zusammen gehoert werden: weniger Bits
    macht das Quantisierungsrauschen hoerbar, das Halten faltet die Hoehen als
    Aliasing nach unten. Einzeln klingt beides nach Fehler, zusammen nach Geraet.
    """

    MIN_BITS = 4.0
    MAX_HALTEN = 8

    def __init__(self, staerke: float, rate: int):
        bits = 16.0 - (16.0 - self.MIN_BITS) * staerke
        self.stufen = 2.0 ** (bits - 1.0)
        self.halten = 1 + round((self.MAX_HALTEN - 1) * staerke)
        self.phase = 0                  # wie weit die laufende Halteperiode ist
        self.letzter = 0.0              # ihr Wert, fuer den Anfang des naechsten Blocks

    def feld(self, proben: np.ndarray) -> np.ndarray:
        if self.halten > 1:
            proben = self._halten(proben)
        return np.round(proben / 32768.0 * self.stufen) / self.stufen * 32768.0

    def _halten(self, proben: np.ndarray) -> np.ndarray:
        # Der Blockanfang gehoert noch zur Halteperiode des vorigen Blocks --
        # ohne diesen Kopf klickt jede Naht.
        kopf = (-self.phase) % self.halten
        self.phase = (self.phase + len(proben)) % self.halten
        if kopf >= len(proben):
            return np.full(len(proben), self.letzter)
        vorne = np.full(kopf, self.letzter)
        gruppen = proben[kopf::self.halten]
        hinten = np.repeat(gruppen, self.halten)[:len(proben) - kopf]
        self.letzter = float(gruppen[-1])
        return np.concatenate((vorne, hinten))


class Breite:
    """Chorus und Chor in einem Regler: mehrere Kopien, jede leicht verstimmt.

    `kollektiv` (der Effektname) legt zwei Kopien mit FESTER Verzoegerung hinter
    das Original -- das klingt nach mehreren Sprechern, aber nach starr
    aufgenommenen. Was einen Chorus ausmacht, ist die langsam wandernde
    Verzoegerung: wer die Lesestelle bewegt, dehnt und staucht die Welle, und
    das ist Tonhoehe. Deshalb ist hier keine zweite Zeitdehnung noetig -- vier
    volle `Klangregler` haetten 72 ms je Sekunde gekostet, das hier kostet
    einen Bruchteil davon.

    Unter halber Staerke laeuft eine Kopie, darueber kommen weitere dazu: aus
    dem Chorus wird der Chor. Die LFO-Frequenzen sind bewusst teilerfremd --
    bei glatten Vielfachen fielen die Kopien periodisch zusammen und man hoerte
    ein Pumpen statt mehrerer Stimmen.
    """

    STIMMEN = ((13.0, 0.47), (19.0, 0.31), (23.0, 0.73), (29.0, 0.19))  # (Grund ms, LFO Hz)
    TIEFE_MS = 3.0              # Ausschlag der Verzoegerung, bei voller Staerke
    PEGEL = 0.55                # wie laut eine Kopie neben dem Original steht

    def __init__(self, staerke: float, rate: int):
        self.rate = rate
        self.staerke = staerke
        # Eine Kopie bei kleiner Staerke, vier bei voller. Der Sprung passiert
        # nicht auf einmal: die Tiefe waechst stufenlos mit.
        self.anzahl = 1 + round(3.0 * max(0.0, staerke - 0.25) / 0.75)
        self.tiefe = self.TIEFE_MS * min(1.0, staerke * 2.0)
        self.phasen = [0.25 * i for i in range(self.anzahl)]     # feste Startlage
        laengste = max(ms for ms, _ in self.STIMMEN[:self.anzahl]) + self.TIEFE_MS
        self.verlauf = np.zeros(int(rate * laengste / 1000) + 2)

    def feld(self, proben: np.ndarray) -> np.ndarray:
        if not len(proben):
            return proben
        vorrat = len(self.verlauf)
        alles = np.concatenate((self.verlauf, proben))
        stelle = np.arange(len(proben), dtype=np.float64)
        summe = proben.copy()
        for i, (grund_ms, lfo_hz) in enumerate(self.STIMMEN[:self.anzahl]):
            schritt = lfo_hz / self.rate
            phase = (self.phasen[i] + stelle * schritt) % 1.0
            self.phasen[i] = float((self.phasen[i] + len(proben) * schritt) % 1.0)
            versatz = (grund_ms + self.tiefe * np.sin(2.0 * np.pi * phase)) * self.rate / 1000.0
            lese = vorrat + stelle - versatz
            ganz = lese.astype(np.int64)
            bruch = lese - ganz
            summe += (alles[ganz] * (1.0 - bruch) + alles[ganz + 1] * bruch) * self.PEGEL
        self.verlauf = alles[-vorrat:]
        # Auf den Pegel des Originals zurueck, sonst regelt `breite` heimlich
        # auch die Lautstaerke.
        return summe / (1.0 + self.anzahl * self.PEGEL)


class Hall:
    """Nachhall als Faltung mit einer erzeugten Impulsantwort.

    Der naheliegende Weg waeren acht Schroeder-Kammfilter. Gemessen kostet
    schon ein einzelner davon 33.5 ms je Sekunde Audio, die Faltung fuer den
    ganzen Raum 13.7 -- und die Faltung hat keine Rueckkopplung, also auch keine
    Stabilitaetsfrage und keine Frequenz, die sich aufschaukelt.

    Die Impulsantwort wird erzeugt, nicht geladen: abklingendes Rauschen, mit
    einem Einpolfilter gedaempft, damit der Nachhall dunkler ist als das
    Direktsignal -- so verhaelt sich jeder echte Raum, weil Hoehen an Waenden
    und in der Luft schneller verloren gehen. Dateien haetten Pfade bedeutet und
    Pfade Validierung im Frontend; das ist der Preis fuer eine Nuance, die hier
    niemand verlangt hat.

    Latenz: keine. Der Faltungsschwanz wandert in den naechsten Block, nicht der
    Anfang in die Zukunft. `abschluss` gibt ihn frei -- die Aeusserung wird
    dadurch um die Laenge der Impulsantwort laenger, und das ist der Hall.
    """

    DAUER_S = 1.2               # Abklingzeit bis -60 dB
    DUNKEL_HZ = 2600.0          # darueber verliert der Raum schneller

    def __init__(self, staerke: float, rate: int):
        self.staerke = staerke
        laenge = int(rate * self.DAUER_S)
        # Fester Startwert: derselbe Text muss zweimal gleich klingen.
        rauschen = np.random.default_rng(20260814).normal(0.0, 1.0, laenge)
        # Einpoliger Tiefpass ueber das Rauschen. Per `lfilter`, nicht in einer
        # Schleife: die Impulsantwort ist 57 600 Proben lang, und von Hand kostete
        # allein ihr Aufbau 10 ms -- die Haelfte des Budgets fuer eine Sekunde Ton.
        from scipy.signal import lfilter

        daempfung = math.exp(-2.0 * math.pi * self.DUNKEL_HZ / rate)
        gedaempft = lfilter([1.0 - daempfung], [1.0, -daempfung], rauschen)
        # -60 dB am Ende: exp(-t/tau) = 1e-3 fuer t = DAUER_S.
        huelle = np.exp(-np.arange(laenge) / (rate * self.DAUER_S / 6.9))
        antwort = gedaempft * huelle
        # Auf gleiche Energie normieren, damit `staerke` eine Mischung bleibt
        # und nicht heimlich die Lautstaerke regelt.
        self.antwort = antwort / math.sqrt(float(np.sum(antwort * antwort)))
        self.schwanz = np.zeros(laenge - 1)

    def feld(self, proben: np.ndarray) -> np.ndarray:
        """ponytail: die Faltung laeuft auf dem Block, den der Aufrufer bringt,
        ohne eigene Pufferung. Die Last haengt damit an der Blockgroesse --
        gemessen bei 1.2 s Impulsantwort: 22.9 ms je Sekunde Audio bei 2048
        Proben, 11.5 bei 4096, 5.6 bei 8192. Der Worker liefert Modellchunks von
        rund 7400 Proben, liegt also bei etwa 6. Wer davon unabhaengig werden
        will, sammelt intern auf eine feste FFT-Groesse -- und kauft sich damit
        genau diese Groesse an Latenz ein, die es hier gerade nicht gibt.
        """
        from scipy.signal import fftconvolve

        if not len(proben):
            return proben
        voll = fftconvolve(proben, self.antwort)
        voll[:len(self.schwanz)] += self.schwanz
        self.schwanz = voll[len(proben):]
        # Trocken leicht zuruecknehmen, sonst wird die Summe bei voller Staerke
        # lauter statt halliger.
        return proben * (1.0 - 0.3 * self.staerke) + voll[:len(proben)] * self.staerke

    def abschluss(self) -> np.ndarray:
        rest = self.schwanz * self.staerke
        self.schwanz = np.zeros(len(self.schwanz))
        return rest


class Kette:
    """Alle Klangstufen einer Aeusserung als ein Objekt.

    Vorher baute der Worker `Effekt` und `Klangregler` einzeln, rief den einen
    mitten in der Take-Schleife und den anderen in `sende()`, und spuelte nur
    einen davon aus. Mit jeder weiteren Stufe waere das eine Aufrufstelle und
    ein Ausspuel-Block mehr an einem Ort, der von Audio nichts wissen sollte.

    Reihenfolge, und warum sie so und nicht anders ist:

    1. `Klangregler` -- Tempo, Tonhoehe, Raster, Streuung, Formant.
    2. `Verzerrer`, `Kruemel` -- die Kennlinie.
    3. `Effekt` -- die Klangfarbe, benannt statt stufenlos.
    4. `Breite` -- die Verbreiterung.
    5. `Hall` -- der Raum.

    Vorher liefen 1 und 3 andersherum, und damit verschob der Tonhoehenschieber
    die Tremolo-Frequenz gleich mit: dieselbe Stimme klang bei `tonhoehe=+5`
    nach einem anderen Effekt als bei 0 (55 Hz wurden zu 89). Die Farbe gehoert
    an die Ausgabe, nicht in den Tonhoehenschieber hinein.

    Drei Kanten liegen fest, der Rest ist Geschmack: Saettigung VOR dem
    Bandfilter (sonst schneidet der die erzeugten Obertoene gleich wieder weg),
    Hall zuletzt (alles danach wuerde ihn verwischen), Begrenzung ganz zuletzt.

    Zwischen Klangregler und Rest bleibt es bei int16: `_kollektiv` verlaesst
    sich auf sein Zwischenclipping, dort ist der Unterschied laut Messung bis zu
    2953 LSB und hoerbar. Ab Stufe 2 wird durchgehend in Gleitkomma gerechnet
    und erst am Ausgang begrenzt -- jede Zwischenrundung waere Rauschen ohne
    Gegenwert.
    """

    def __init__(self, rate: int, *, effekt: str = "", faktor: float = 1.0,
                 halbtoene: float = 0.0, streuung: float = 0.0,
                 raster: float = 0.0, formant: float = 0.0,
                 verzerrung: float = 0.0, kruemel: float = 0.0,
                 breite: float = 0.0, hall: float = 0.0) -> None:
        self.klang = (Klangregler(faktor, rate, halbtoene, streuung, raster, formant)
                      if faktor != 1.0 or halbtoene or streuung or raster or formant
                      else None)
        # Reihenfolge dieser Liste IST die Signalkette.
        self.stufen: list = []
        if verzerrung:
            self.stufen.append(Verzerrer(verzerrung, rate))
        if kruemel:
            self.stufen.append(Kruemel(kruemel, rate))
        if effekt:
            self.stufen.append(Effekt(effekt, rate))
        if breite:
            self.stufen.append(Breite(breite, rate))
        if hall:
            self.stufen.append(Hall(hall, rate))

    def __bool__(self) -> bool:
        """Falsch, wenn nichts zu tun ist -- dann baut der Aufrufer gar nicht erst."""
        return self.klang is not None or bool(self.stufen)

    def verarbeite(self, pcm: bytes) -> bytes:
        if self.klang is not None:
            pcm = self.klang.verarbeite(pcm)
        return self._farbe(pcm)

    def abschluss(self) -> bytes:
        """Was in den Stufen haengt, in Kettenreihenfolge ausspuelen.

        Der Schwanz einer Stufe muss noch durch alle nachfolgenden -- sonst
        laege der Hall trocken hinter der Aeusserung statt in ihr. Deshalb
        aufsteigend: wenn Stufe j an der Reihe ist, hat sie die Schwaenze aller
        frueheren bereits gesehen.
        """
        rest = self.klang.abschluss() if self.klang is not None else b""
        aus = self._farbe(rest)
        for i, stufe in enumerate(self.stufen):
            schwanz = getattr(stufe, "abschluss", None)
            if schwanz is None:
                continue
            feld = schwanz()
            for weiter in self.stufen[i + 1:]:
                feld = weiter.feld(feld)
            if len(feld):
                aus += _begrenze_feld(feld).tobytes()
        return aus

    def _farbe(self, pcm: bytes) -> bytes:
        if not pcm or not self.stufen:
            return pcm
        proben = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        for stufe in self.stufen:
            proben = stufe.feld(proben)
        return _begrenze_feld(proben).tobytes()


def _begrenze_feld(werte: np.ndarray) -> np.ndarray:
    """Wie die alte Probe-fuer-Probe-Begrenzung: erst kappen, dann zur Null hin
    abschneiden. astype(int16) schneidet ab wie int(), nicht wie round()."""
    return np.clip(werte, -32768.0, 32767.0).astype("<i2")


def _spur(pcm: bytes, rate: int) -> np.ndarray:
    """Tonhoehe je 20 ms in Halbtoenen -- Messwerkzeug der Selbstpruefung."""
    werte = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    fenster = 2048
    aus = []
    for start in range(0, len(werte) - fenster, rate // 50):
        block = werte[start:start + fenster]
        if math.sqrt(float(np.mean(block * block))) < 500:
            continue
        lag, guete = _periode(block - block.mean(), int(rate / 400), int(rate / 60),
                              int(rate / aus[-1]) if aus else 0)
        if lag and guete > 0.45:
            aus.append(rate / lag)
    return np.array(aus)


def _schwerpunkt(pcm: bytes, rate: int) -> float:
    """Spektraler Schwerpunkt: steigt, wenn die Formanten nach oben wandern."""
    werte = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spektrum = np.abs(np.fft.rfft(werte * np.hanning(len(werte))))
    frequenz = np.fft.rfftfreq(len(werte), 1.0 / rate)
    return float((spektrum * frequenz).sum() / spektrum.sum())


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

    # Telefon: das Band schneidet oben und unten, also liegt der spektrale
    # Schwerpunkt eines breitbandigen Eingangs danach im Fernsprechband.
    breit = (np.random.default_rng(7).normal(0, 4000, rate // 2)).astype("<i2").tobytes()
    durchs_band = Effekt("telefon", rate).verarbeite(breit)
    mitte = _schwerpunkt(durchs_band, rate)
    assert 300.0 < mitte < 3400.0, mitte
    assert _schwerpunkt(breit, rate) > 3400.0, "Testvoraussetzung: Eingang ist breitbandig"

    def durch(regler: Klangregler, quelle: bytes) -> bytes:
        aus = b"".join(regler.verarbeite(quelle[i:i + 4096])
                       for i in range(0, len(quelle), 4096))
        return aus + regler.abschluss()

    # Eine Sekunde 200 Hz: lang genug fuer mehrere Tonhoehenspruenge, und die
    # Grundfrequenz ist per FFT eindeutig ablesbar.
    sekunde = (9000 * np.sin(2 * np.pi * 200 * np.arange(rate) / rate)).astype("<i2").tobytes()

    def grundton(pcm: bytes) -> float:
        werte = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        spektrum = np.abs(np.fft.rfft(werte * np.hanning(len(werte))))
        return float(np.fft.rfftfreq(len(werte), 1.0 / rate)[int(np.argmax(spektrum))])

    # Tempo: Dauer folgt dem Faktor, Ton bleibt Ton (keine Stille, kein Clipping).
    for faktor in (0.6, 1.0, 1.5, 2.0):
        aus = durch(Klangregler(faktor, rate), roh)
        erwartet = len(roh) / faktor
        assert abs(len(aus) - erwartet) <= 2, (faktor, len(aus), erwartet)
        werte = array.array("h")
        werte.frombytes(aus)
        assert max(abs(w) for w in werte) > 4000, f"Tempo {faktor} liefert Stille"
        assert max(abs(w) for w in werte) <= 32767, faktor

    # Tonhoehe: Grundton folgt den Halbtoenen, die Dauer bleibt, wo sie war.
    assert abs(grundton(sekunde) - 200.0) < 2.0, grundton(sekunde)
    for halbtoene in (-12.0, -5.0, 3.0, 7.0):
        aus = durch(Klangregler(1.0, rate, halbtoene=halbtoene), sekunde)
        assert abs(len(aus) - len(sekunde)) <= 2, (halbtoene, len(aus))
        soll = 200.0 * 2.0 ** (halbtoene / 12.0)
        assert abs(grundton(aus) - soll) < max(3.0, soll * 0.02), (halbtoene, grundton(aus), soll)

    # Beides zusammen: die Dauer haengt am Tempo, die Tonhoehe nicht.
    aus = durch(Klangregler(1.5, rate, halbtoene=5.0), sekunde)
    assert abs(len(aus) - len(sekunde) / 1.5) <= 2, len(aus)
    assert abs(grundton(aus) - 200.0 * 2.0 ** (5 / 12)) < 6.0, grundton(aus)

    # Raster: ein Ton, der um 40 Cent daneben liegt und dazu schwankt, kommt
    # auf dem Halbton heraus und steht dort still -- "pitch constrained, pitch
    # modulation suppressed", der Kern der Vorlage.
    daneben = 220.0 * 2 ** (0.4 / 12)                   # 40 Cent ueber A3
    zeit = np.arange(2 * rate) / rate
    vibrato = 2 * np.pi * daneben * zeit + 0.35 * np.sin(2 * np.pi * 5.0 * zeit)
    krumm = (9000 * np.sin(vibrato)).astype("<i2").tobytes()
    gerade = durch(Klangregler(1.0, rate, raster=1.0), krumm)
    assert abs(len(gerade) - len(krumm)) <= 2, len(gerade)
    vorher = _spur(krumm, rate)
    nachher = _spur(gerade, rate)
    assert abs(np.median(nachher) - 220.0) < 3.0, np.median(nachher)
    assert np.std(nachher) < 0.35 and np.std(nachher) < np.std(vorher) / 3, (
        np.std(nachher), np.std(vorher))          # Vibrato weg, unter 5 Cent Rest

    # Mehrere Silben hintereinander, jede daneben: jede landet auf ihrem eigenen
    # Halbton, und die Stufen bleiben unterscheidbar (kein Monoton-Brei).
    silben = []
    for hertz in (206.0, 219.0, 233.5, 198.0, 246.0, 211.0):
        laenge = int(rate * 0.25)
        silben.append(9000 * np.sin(2 * np.pi * hertz * np.arange(laenge) / rate))
    melodie = np.concatenate(silben).astype("<i2").tobytes()
    gerastert = durch(Klangregler(1.0, rate, raster=1.0), melodie)
    stufen = 12 * np.log2(_spur(gerastert, rate) / 440.0)
    daneben_ht = np.abs(stufen - np.round(stufen))
    assert float(np.median(daneben_ht)) < 0.12, float(np.median(daneben_ht))
    assert len(set(np.round(stufen).astype(int))) >= 4, sorted(set(np.round(stufen)))

    # Streuung: wuerfelt je Silbe, wiederholbar, und laesst die Dauer stehen.
    gestreut = durch(Klangregler(1.0, rate, streuung=2.0, raster=1.0), melodie)
    nochmal = durch(Klangregler(1.0, rate, streuung=2.0, raster=1.0), melodie)
    assert gestreut == nochmal, "gleicher Text, gleiche Streuung -- muss gleich klingen"
    assert gestreut != gerastert, "Streuung 2 aendert nichts"
    assert abs(len(gestreut) - len(melodie)) <= 2, len(gestreut)

    # Formant: die Huellkurve wandert, der Grundton bleibt stehen. Als Vorlage
    # ein Vokal aus 120-Hz-Oberwellen unter zwei Resonanzen -- ein Saegezahn
    # taugt nicht, dessen 1/f-Huelle sieht verschoben genauso aus wie vorher.
    vokal = np.zeros(len(zeit))
    for oberwelle in range(1, 60):
        hertz = 120.0 * oberwelle
        staerke = (math.exp(-((hertz - 700.0) / 160.0) ** 2)
                   + 0.7 * math.exp(-((hertz - 1600.0) / 260.0) ** 2))
        vokal += staerke * np.sin(2 * np.pi * hertz * zeit + oberwelle)
    vokal = (vokal / np.abs(vokal).max() * 9000).astype("<i2").tobytes()
    verschoben = durch(Klangregler(1.0, rate, formant=7.0), vokal)
    assert abs(len(verschoben) - len(vokal)) <= 2, len(verschoben)
    # Grundton ueber die Periode messen, nicht ueber den staerksten Gipfel: der
    # staerkste Gipfel IST eine Formante und soll ja wandern.
    assert abs(float(np.median(_spur(verschoben, rate))) - 120.0) < 3.0, (
        float(np.median(_spur(verschoben, rate))))
    gewandert = _schwerpunkt(verschoben, rate) / _schwerpunkt(vokal, rate)
    assert 1.2 < gewandert < 1.8, gewandert          # 7 Halbtoene sind Faktor 1.5

    assert tonhoehe_wert("hoch") == 0.0 and streuung_wert(-5) == 0.0
    assert raster_wert(9) == 1.0 and formant_wert(None) == 0.0

    # -- Kette -------------------------------------------------------------
    def durch_kette(kette: Kette, quelle: bytes) -> bytes:
        aus = b"".join(kette.verarbeite(quelle[i:i + 4096])
                       for i in range(0, len(quelle), 4096))
        return aus + kette.abschluss()

    # Leere Kette: nichts zu tun, und das sagt sie auch.
    assert not Kette(rate), "Kette ohne Stufe muss falsch sein"
    assert Kette(rate, effekt="roboter") and Kette(rate, halbtoene=1.0)
    assert durch_kette(Kette(rate), roh) == roh, "leere Kette muss durchreichen"

    # Eine einzelne Stufe in der Kette klingt wie dieselbe Stufe allein.
    allein = Effekt("roboter", rate)
    assert durch_kette(Kette(rate, effekt="roboter"), roh) == allein.verarbeite(roh)
    assert durch_kette(Kette(rate, halbtoene=3.0), sekunde) == durch(
        Klangregler(1.0, rate, halbtoene=3.0), sekunde)

    # Reihenfolge: der Tonhoehenschieber laeuft VOR dem Effekt, also bleibt das
    # Tremolo bei seinen 55 Hz, statt mit hochtransponiert zu werden. Gemessen
    # am Abstand der Seitenbaender um den 200-Hz-Traeger.
    def tremolo_hz(pcm: bytes) -> float:
        werte = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        spektrum = np.abs(np.fft.rfft(werte * np.hanning(len(werte))))
        achse = np.fft.rfftfreq(len(werte), 1.0 / rate)
        traeger = int(np.argmax(spektrum))
        fenster = (achse > achse[traeger] + 20.0) & (achse < achse[traeger] + 200.0)
        return float(achse[fenster][int(np.argmax(spektrum[fenster]))] - achse[traeger])

    gemischt = durch_kette(Kette(rate, effekt="roboter", halbtoene=5.0), sekunde)
    assert abs(tremolo_hz(gemischt) - _TREMOLO_HZ) < 3.0, tremolo_hz(gemischt)

    # -- Hall --------------------------------------------------------------
    # Ein einzelner Impuls muss ueber Blockgrenzen hinweg nachklingen: der
    # Faltungsschwanz wandert in den naechsten Block, nicht in den Papierkorb.
    block = 4096
    impuls = np.zeros(block * 4)
    impuls[10] = 20000.0
    hall = Hall(1.0, rate)
    bloecke = [hall.feld(impuls[i:i + block]) for i in range(0, len(impuls), block)]
    laut = [float(np.abs(teil).max()) for teil in bloecke]
    assert all(wert > 1.0 for wert in laut), laut       # jeder Block traegt Nachhall
    assert len(hall.abschluss()) == int(rate * Hall.DAUER_S) - 1 - (len(impuls) - block * 4)

    # Die Aeusserung wird um genau die Laenge der Impulsantwort laenger.
    mit_hall = durch_kette(Kette(rate, hall=0.7), roh)
    assert len(mit_hall) == len(roh) + 2 * (int(rate * Hall.DAUER_S) - 1), len(mit_hall)
    assert durch_kette(Kette(rate, hall=0.7), roh) == mit_hall, "Hall muss wiederholbar sein"
    assert durch_kette(Kette(rate, hall=0.0), roh) == roh, "hall=0 baut keine Stufe"

    # Rechenlast: unter 20 ms je Sekunde Audio, gemessen mit der Blockgroesse,
    # die der Worker wirklich liefert (Modellchunks von rund 154 ms). Reisst
    # diese Zusage, wandert sie unbemerkt nach oben, bis der Worker haengt.
    lange = (9000 * np.sin(2 * np.pi * 200 * np.arange(rate) / rate)).astype("<i2").tobytes()
    chunk = int(rate * 0.154) * 2
    kette = Kette(rate, hall=1.0)
    kette.verarbeite(lange[:chunk])                     # Import und FFT-Plan warm
    kette = Kette(rate, hall=1.0)
    begonnen = time.perf_counter()
    for i in range(0, len(lange), chunk):
        kette.verarbeite(lange[i:i + chunk])
    kette.abschluss()
    gebraucht = (time.perf_counter() - begonnen) * 1000
    assert gebraucht < 20.0, f"Hall kostet {gebraucht:.1f} ms je Sekunde Audio"

    assert hall_wert(2.0) == 1.0 and hall_wert("viel") == 0.0

    # -- Verzerrung und Kruemel -------------------------------------------
    # Saettigung erzeugt Obertoene, also wandert der spektrale Schwerpunkt nach
    # oben -- und die Dauer bleibt, wo sie war.
    verzerrt = durch_kette(Kette(rate, verzerrung=1.0), sekunde)
    assert len(verzerrt) == len(sekunde), len(verzerrt)
    assert _schwerpunkt(verzerrt, rate) > _schwerpunkt(sekunde, rate) * 1.5, (
        _schwerpunkt(verzerrt, rate), _schwerpunkt(sekunde, rate))
    assert durch_kette(Kette(rate, verzerrung=0.0), sekunde) == sekunde

    # Kruemel bei voller Staerke: hoechstens 2^4 verschiedene Werte, und die
    # Naht zwischen zwei Bloecken haelt die Halteperiode durch.
    gekruemelt = durch_kette(Kette(rate, kruemel=1.0), sekunde)
    assert len(gekruemelt) == len(sekunde), len(gekruemelt)
    werte = np.frombuffer(gekruemelt, dtype="<i2")
    assert len(np.unique(werte)) <= 2 ** Kruemel.MIN_BITS, len(np.unique(werte))
    feld = np.frombuffer(sekunde, dtype="<i2").astype(np.float64)
    # 1001 ist bewusst kein Vielfaches der Halteperiode: genau dort bricht eine
    # Umsetzung, die die Phase nicht ueber die Blockgrenze traegt.
    geteilt = Kruemel(1.0, rate)
    naht = np.concatenate((geteilt.feld(feld[:1001]), geteilt.feld(feld[1001:])))
    assert np.array_equal(naht, Kruemel(1.0, rate).feld(feld)), "Halteperiode bricht an der Naht"
    assert durch_kette(Kette(rate, kruemel=0.0), sekunde) == sekunde

    # -- Breite ------------------------------------------------------------
    breit = durch_kette(Kette(rate, breite=1.0), sekunde)
    assert len(breit) == len(sekunde), len(breit)
    assert breit != sekunde
    assert durch_kette(Kette(rate, breite=1.0), sekunde) == breit, "Breite muss wiederholbar sein"
    assert durch_kette(Kette(rate, breite=0.0), sekunde) == sekunde
    # Der Pegel bleibt, wo er war: `breite` verbreitert, es macht nicht lauter.
    spitze = lambda pcm: int(np.abs(np.frombuffer(pcm, dtype="<i2")).max())
    assert spitze(breit) < spitze(sekunde) * 1.2, (spitze(breit), spitze(sekunde))

    # Keine Klicks an den Blockgrenzen: der Sprung zwischen zwei benachbarten
    # Proben bleibt in der Groessenordnung dessen, was das Quellsignal selbst
    # hergibt. Ein vergessener Zustand zeigt sich hier als Ausreisser.
    def groesster_sprung(pcm: bytes) -> int:
        werte = np.frombuffer(pcm, dtype="<i2").astype(np.int64)
        return int(np.abs(np.diff(werte)).max())

    assert groesster_sprung(breit) < groesster_sprung(sekunde) * 3, (
        groesster_sprung(breit), groesster_sprung(sekunde))

    kette = Kette(rate, breite=1.0)
    kette.verarbeite(sekunde[:chunk])                   # warm
    kette = Kette(rate, breite=1.0)
    begonnen = time.perf_counter()
    for i in range(0, len(sekunde), chunk):
        kette.verarbeite(sekunde[i:i + chunk])
    kette.abschluss()
    gebraucht = (time.perf_counter() - begonnen) * 1000
    assert gebraucht < 30.0, f"Breite kostet {gebraucht:.1f} ms je Sekunde Audio"

    assert breite_wert(3) == 1.0 and verzerrung_wert(None) == 0.0 and kruemel_wert(-1) == 0.0

    # -- Vocoder -----------------------------------------------------------
    # Der Traeger folgt der Stimme: ein 200-Hz-Eingang ergibt einen Ausgang mit
    # Energie bei 200 Hz und dessen Vielfachen, ein 150-Hz-Eingang bei 150. Ein
    # fester Traeger wuerde beide Male dieselbe Frequenz liefern.
    def durch_bloecke(effekt: Effekt, quelle: bytes) -> bytes:
        return b"".join(effekt.verarbeite(quelle[i:i + chunk])
                        for i in range(0, len(quelle), chunk))

    for hertz in (150.0, 200.0, 260.0):
        quelle = (9000 * np.sin(2 * np.pi * hertz * np.arange(rate) / rate)).astype("<i2").tobytes()
        aus = durch_bloecke(Effekt("vocoder", rate), quelle)
        assert len(aus) == len(quelle), (hertz, len(aus))
        assert abs(grundton(aus) - hertz) < 3.0, (hertz, grundton(aus))
        assert 2000 < int(np.abs(np.frombuffer(aus, dtype="<i2")).max()) <= 32767, hertz

    # Blockgrenzen: Analyse- und Traegerfilter tragen ihren Zustand. Ohne den
    # eigenen Zustand des Traegers klingelt jedes Band an jeder Naht.
    stueckweise = durch_bloecke(Effekt("vocoder", rate), sekunde)
    assert groesster_sprung(stueckweise) < 8000, groesster_sprung(stueckweise)

    kette = Kette(rate, effekt="vocoder")
    kette.verarbeite(sekunde[:chunk])                   # warm
    kette = Kette(rate, effekt="vocoder")
    begonnen = time.perf_counter()
    for i in range(0, len(sekunde), chunk):
        kette.verarbeite(sekunde[i:i + chunk])
    kette.abschluss()
    gebraucht = (time.perf_counter() - begonnen) * 1000
    assert gebraucht < 15.0, f"Vocoder kostet {gebraucht:.1f} ms je Sekunde Audio"

    print("effekte: ok")


if __name__ == "__main__":
    demo()
