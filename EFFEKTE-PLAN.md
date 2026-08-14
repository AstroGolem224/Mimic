# Feature-Entwurf: Effektkette für Mimic

_Rev 1, 2026-08-14. Umsetzungsplan zu `EFFEKTE.md`. Alles, was dort unter
„Nicht bauen" steht, ist hier ausgeschlossen — auch Flanger, Phaser, Gate und
Whisper, die dort als Einzeleffekte verworfen sind._

**Anker-Warnung:** `effekte.py` ist seit dem Recherchebericht von 541 auf 709 Zeilen
gewachsen (`raster_wert`, `formant_wert`, `GLADOS`, `_periode`, `_spur`, `_schwerpunkt`).
Die Zeilenangaben in `EFFEKTE.md` sind teilweise veraltet; die hier verwendeten sind gegen
den Stand vom 2026-08-14, 15:00 geprüft. `STREUUNG_MAX` ist heute **4.0 Halbtöne**, nicht
300 Cent.

## 1. Ziel

Sieben neue Klangmittel, ein aufgeräumter Kettenaufbau, keine neue Abhängigkeit im
Wortsinn — `scipy` liegt über librosa bereits im Lock (`uv.lock:466`, Version 1.18.0).

| Was | Form | Neu in |
|---|---|---|
| Reverb | Regler `hall` 0–1 | `Hall` |
| Chorus + Choir | Regler `breite` 0–1 | `Breite` |
| Distortion | Regler `verzerrung` 0–1 | `Verzerrer` |
| Bitcrusher | Regler `kruemel` 0–1 | `Kruemel` |
| Telefon/Funk | Effektname `telefon` | `Effekt` |
| Vocoder | Effektname `vocoder` | `Effekt` |
| TV-Effekt auf `sosfilt` | kein neuer Klang | `Effekt._tv` |

Dazu die drei Befunde aus `EFFEKTE.md` §„Drei Befunde": Kettenordnung, Stummerkennung,
`abschluss()`.

## 2. Getroffene Entscheidungen

| Frage | Entscheidung | Folge |
|---|---|---|
| Parametermodell | **Gemischt.** Stufenloses wird Zahlen-Regler wie `tonhoehe` (Profilwert **plus** Reglerwunsch, addiert, geklemmt). Charakterklänge werden Namen in `EFFEKTE`. | Vier neue Regler, zwei neue Namen. Folgt der heutigen Zweiteilung ohne neues Konzept. |
| Kettenordnung | **Umbauen:** `Klangregler` zuerst, `Effekt` danach. | Profile mit `tonhoehe ≠ 0` **und** gesetztem `effekt` klingen hörbar anders. Heute betrifft das kein ausgeliefertes Profil — vor dem Bau mit `mimic voices` gegenprüfen. |
| Kompatibilität | **Hörbar gleich genügt**, nicht bitgleich. | Erlaubt den `sosfilt`-Umbau und Gleitkomma zwischen den Stufen. Grenze: das Zwischenclipping in `_kollektiv` (`effekte.py:126-130`) **bleibt**, dort ist die Abweichung laut Codekommentar hörbar. |
| Lieferform | Entwurf + Etappenplan, kein Code in diesem Schritt. | Sieben Etappen, jede für sich lieferbar und abnehmbar. |

## 3. Architektur

### 3.1 Das Problem mit dem heutigen Aufbau

`worker._execute` baut zwei Objekte einzeln (`effekt` bei `worker.py:384`, `tempo` bei
`:397`), ruft eines mitten in der Take-Schleife (`:442`), das andere in der `sende()`-Closure
(`:406`), und räumt nur eines am Ende ab (`:477-483`). Mit vier weiteren Stufen wären das
sechs Objekte, sechs Aufrufstellen und fünf Ausspül-Blöcke im Worker. Das ist die Stelle,
an der eine Abstraktion billiger wird als ihr Fehlen.

### 3.2 `Kette` — ein Objekt je Äußerung

Neue Klasse in `effekte.py`, unterhalb von `Klangregler`:

```python
class Kette:
    def __init__(self, rate: int, *, effekt: str = "", faktor: float = 1.0,
                 halbtoene: float = 0.0, streuung: float = 0.0, raster: float = 0.0,
                 formant: float = 0.0, hall: float = 0.0, breite: float = 0.0,
                 verzerrung: float = 0.0, kruemel: float = 0.0) -> None: ...
    def verarbeite(self, pcm: bytes) -> bytes: ...
    def abschluss(self) -> bytes: ...
    def __bool__(self) -> bool: ...          # False, wenn keine Stufe aktiv ist
```

- Baut **nur** die Stufen, deren Wert vom Neutralwert abweicht. Steht alles neutral, ist
  `bool(kette)` falsch und der Worker baut sie gar nicht erst — dieselbe Ersparnis, die
  `worker.py:397-398` heute von Hand macht.
- `verarbeite` wandelt einmal `int16 → float64` am Eingang und einmal zurück am Ausgang.
  Zwischen den Stufen wird **nicht** gerundet; Ausnahme ist `Effekt._kollektiv`, das sein
  Zwischenclipping intern behält.
- `abschluss()` ruft die Stufen mit Überhang in Kettenreihenfolge ab und hängt die
  Ergebnisse aneinander. Heute hat nur `Klangregler` einen; künftig auch `Hall` und
  `Breite`.

**Stufenprotokoll**, bewusst informell (kein `Protocol`, keine ABC — eine Duck-Typing-Zeile
tut es):

```python
verarbeite(self, proben: np.ndarray) -> np.ndarray     # float64 rein, float64 raus
abschluss(self) -> np.ndarray                          # optional, leeres Feld wenn nichts hängt
```

`Klangregler` und `Effekt` bekommen dafür je eine Feld-Variante ihrer heutigen
Byte-Schnittstelle. Die Byte-Fassung bleibt öffentlich stehen — `demo()` und die Tests
benutzen sie, und ein Bruch dort wäre reine Beschäftigung.

### 3.3 Reihenfolge

```
0  Quelle          tensor_to_pcm, Profilverstärkung
1  Stummerkennung  auf dem ROHEN Signal          ← Befund 2
2  Klangregler     Tempo, Tonhöhe, Raster, Streuung, Formant
3  Verzerrer       verzerrung
4  Kruemel         kruemel
5  Effekt          roboter | tv | telefon | vocoder | kollektiv
6  Breite          breite
7  Hall            hall
8  Begrenzung      _begrenze_feld, einmal am Ausgang
```

Drei Kanten sind fest und begründet:

- **1 vor 2–7.** `spitze` wird heute **nach** `effekt.verarbeite` gemessen
  (`worker.py:441` gegen `:449`). Jede Stufe mit Grundpegel — Hall-Vorhall,
  Vocoder-Träger, TV-Rauschen — hebt das Signal über `STUMM_PEAK` (`worker.py:52`,
  −25 dBFS) und macht die Wiederholung stummer Takes blind. Das ist der Mechanismus aus
  PHASE2 Schritt 2a und Kriterium P2-L.
- **3/4 vor 5.** Ein Bandfilter nach der Sättigung schneidet die erzeugten Obertöne weg;
  umgekehrt ist es das, was den Klang ausmacht.
- **7 zuletzt.** Alles nach dem Hall verwischt ihn. Hall wird gehört, nicht transponiert —
  das ist zugleich der Grund, warum Befund 1 vor Etappe 2 gelöst sein muss.

Vertauschbar und daher nicht festgeschrieben: 3 gegen 4, und 5 gegen 6.

### 3.4 Was bewusst redundant bleibt

`kollektiv` (Effektname) und `breite` (Regler) machen Verwandtes. `kollektiv` bleibt
unangetastet als eingefrorener Preset einer früheren Runde; `breite` ist die stufenlose,
modulierte Fassung und wirkt unabhängig davon. Beide gleichzeitig zu setzen ist erlaubt und
klingt nach zu viel — das ist Sache des Profils, nicht des Codes.

```
# ponytail: kollektiv bleibt als Name stehen, statt auf breite umgebogen zu werden.
# Wenn die Doppelung stoert: kollektiv als Preset breite=0.45 definieren und den
# eigenen Zweig loeschen.
```

## 4. Datenmodell

### 4.1 Neue Konstanten in `effekte.py`

Nach dem Muster von `RASTER_MIN, RASTER_MAX = 0.0, 1.0` (`effekte.py:57`):

| Name | Bereich | Neutral | Bedeutung bei 1.0 |
|---|---|---|---|
| `HALL_MIN/MAX` | 0.0–1.0 | 0.0 | voller Nachhall, 1.2 s Abklingzeit |
| `BREITE_MIN/MAX` | 0.0–1.0 | 0.0 | vier Stimmen, ±25 Cent, ±3 ms Modulation |
| `VERZERRUNG_MIN/MAX` | 0.0–1.0 | 0.0 | Vorverstärkung 20, `tanh`-Kennlinie |
| `KRUEMEL_MIN/MAX` | 0.0–1.0 | 0.0 | 4 Bit, Haltefaktor 8 |

Alle vier bekommen einen `_zahl`-Wrapper wie `raster_wert` (`effekte.py:94-99`). Alle vier
sind 0–1, weil das die Reglerleiste lesbar hält und die Kennlinie im DSP-Code sitzt, wo sie
begründet werden kann — nicht im Zahlenbereich.

`EFFEKTE` (`effekte.py:28`) wächst auf
`("roboter", "tv", "telefon", "vocoder", "kollektiv")`.

### 4.2 Stimmprofil

`VoiceProfile` (`voices.py:40-52`) bekommt vier Felder mit Vorgabe `0.0`. Die Validierung
in `_read_settings` ist bereits eine Schleife über ein `grenzen`-Verzeichnis
(`voices.py:269-272`) — vier Zeilen dort, sonst nichts. Unbekannte Felder in `settings.json`
werden heute stillschweigend ignoriert; das bleibt so, und ältere Profile laufen deshalb
unverändert weiter.

### 4.3 `/speak`

Vier neue optionale Zahlenfelder, Vorgabe `0.0`. Das erfüllt die Zusage aus PHASE2
Schritt 2d ohne Versionsfeld: Aufrufer, die sie nicht senden, bekommen exakt das heutige
Verhalten. `frontend.py:266-274` ist die einzige Stelle mit hartem `400` bei
Bereichsverstoß; GUI und Worker klemmen still weiter.

## 5. Die Stufen im Einzelnen

### `Hall` — Reverb

Overlap-Add-Faltung. Impulsantwort wird beim Aufbau **erzeugt**, nicht geladen:
exponentiell abklingendes Rauschen aus dem eigenen LCG (wie `effekte.py:409-411`, damit
derselbe Text zweimal gleich klingt), tiefpassgefiltert, damit der Nachhall dunkler ist als
das Direktsignal. Länge 1.2 s bei `hall = 1.0`, Mischverhältnis linear mit `hall`.

- Zustand: Faltungsschwanz von IR-Länge minus eins.
- Latenz: **null**. Der Schwanz wandert in den nächsten Block, nicht der Anfang in die
  Zukunft.
- `abschluss()`: gibt den Schwanz aus; die Äußerung wird dadurch um bis zu 1.2 s länger.
- Gemessen (`EFFEKTE.md`): 13.7 ms je Sekunde Audio, RT 73×.
- Verworfen: acht Schroeder-Kämme per `lfilter`, gemessen 33.5 ms für **einen** von acht.

### `Breite` — Chorus und Choir in einem Regler

Unter 0.5: eine Delay-Kopie, deren Verzögerung per LFO (0.3–1.5 Hz, ±3 ms) wandert,
abgelesen mit `np.interp` statt ganzzahligem Index. Ab 0.5 kommen zwei weitere Kopien mit
festem Cent-Versatz dazu, erzeugt über je eine `_Neuabtastung` (`effekte.py:204`) —
**nicht** über zusätzliche `Klangregler`. Vier volle Klangregler kosteten 72 ms je Sekunde;
eine Zeitdehnung mit drei Ableseverhältnissen kostet einen Bruchteil davon.

- Zustand: Verlaufspuffer wie `Effekt.verlauf` (`effekte.py:117`), plus LFO-Phase,
  plus `_Neuabtastung`-Rest je Kopie.
- Latenz: null; die Kopien lesen nur Vergangenes.

### `Verzerrer` — Distortion

`tanh(x·g)/tanh(g)` mit `g = 1 + 19·verzerrung`. Zustandslos. Ehrlich benannt: die Kennlinie
erzeugt Obertöne über der Nyquistgrenze, die als Aliasing zurückfalten. Bei `tanh` und
Sprache ist das mild und bleibt **ohne** Oversampling. Erst wenn jemand eine harte
Clip-Variante will, kostet es Faktor 4.

### `Kruemel` — Bitcrusher

Quantisieren auf `16 − 12·kruemel` Bit, dazu Halten jeder `1 + 7·kruemel`-ten Probe. Beides
als Feldrechnung mit `np.repeat`, Zustand ist der Halte-Rest über die Blockgrenze. Billigste
Stufe im ganzen Plan, ~0.5 ms je Sekunde.

### `Effekt` — `telefon` und der `sosfilt`-Umbau

`tv` (`effekte.py:168-201`) ist die einzige Python-Schleife im Modul, kostet 7.0 ms je
Sekunde statt 0.3, und trägt seit dem Bau eine `ponytail:`-Notiz mit genau diesem
Upgrade-Pfad (`effekte.py:171-178`). Die beiden Einpolfilter werden ein `sosfilt` mit
mitgeführtem `zi`; Sättigung, Rauschen und Begrenzung liegen ohnehin schon außerhalb der
Rückkopplung.

`telefon` ist derselbe Aufbau mit Butterworth-Bandpass 300–3400 Hz vierter Ordnung, ohne
Sättigung, mit stärkerem Rauschen.

**Bindend:** `scipy` wird **innerhalb** der Methode importiert, nie auf Modulebene.
`import scipy.signal` kostet kalt 306 ms, und `effekte.py` wird von `frontend.py:19`
geladen — das ist der Pfad, um dessen Millisekunden PHASE2 Schritt 3 vor `submit()` kämpft.

### `Effekt` — `vocoder`

Kanalvocoder, nicht Phasenvocoder: 24 Bandpässe auf der Sprache, je Band die
Hüllkurve, damit ein Sägezahnträger moduliert. Der Träger folgt der geschätzten
Grundfrequenz — ein fester Sägezahn klingt tot.

Das ist die **einzige** Stufe mit einem Umbau an fremdem Code: die F0-Schätzung liegt heute
als `Klangregler._grundton` (`effekte.py:434`) auf `_periode` (`effekte.py:234`) und ist an
den Klangregler-Zustand gebunden (`self.letzte`, `self.hoerfenster`). Sie wird zu einem
eigenen kleinen Objekt `_Tonhoehenleser`, das beide benutzen. Deshalb steht der Vocoder als
letzte Etappe: er ist der einzige Punkt, an dem eine Regression im **bestehenden** Autotune
möglich ist.

## 6. Etappenplan

Jede Etappe ist für sich lieferbar, testbar und rückbaubar. Reihenfolge ist bindend:
E1 legt das Gerüst, E2 die Filterbasis, auf der E7 aufsetzt.

### E1 — `Kette`, Reihenfolge, Stummerkennung, `abschluss()`

**Kein neuer Klang.** Nur der Umbau, ohne den jede weitere Etappe den Worker weiter
zumüllt.

Dateien: `effekte.py` (neue Klasse `Kette`, Feld-Varianten von `Effekt.verarbeite` und
`Klangregler.verarbeite`), `worker.py:384-410` und `:477-483`.

Der Worker schrumpft auf: `kette = Kette(sample_rate, effekt=profile.effekt, **regler)`,
ein `kette.verarbeite(pcm)` in `sende()`, ein `kette.abschluss()` am Ende. Der
`effekt.verarbeite`-Aufruf bei `worker.py:442` **entfällt** — damit misst `spitze` wieder
das rohe Modellausgangssignal.

_Abnahme:_
1. `tests/run.sh` grün.
2. Eine Stimme mit `effekt="roboter"` und `tonhoehe=0`: Ausgabe **bitgleich** zu vorher.
3. Eine Stimme mit `effekt="roboter"` und `tonhoehe=+5`: Ausgabe **verschieden** zu vorher,
   und die Tremolo-Frequenz im Spektrum liegt jetzt bei 55 Hz statt bei 73 Hz. Das ist
   Befund 1, sichtbar gemacht.
4. Neuer Test: ein Take, dessen erste Chunks stumm sind, während `effekt="tv"` gesetzt ist,
   wird weiterhin als stumm erkannt und wiederholt. Vor dem Umbau schlägt dieser Test fehl,
   sobald das TV-Rauschen über `STUMM_PEAK` liegt — er ist der Beleg für Befund 2.
5. `demo()`: `Kette` ohne aktive Stufe ist `False` und wird im Worker nicht gebaut.

_Rollback:_ ein Commit, keine Datenformat-Änderung.

### E2 — `sosfilt` für `tv`, neuer Name `telefon`

Dateien: `effekte.py` (`_tv_bytes` → `_band`, neuer Zweig, `EFFEKTE`), `voices.py` (nichts —
die Whitelist wird importiert).

_Abnahme:_
1. Alter und neuer `tv`-Effekt weichen über eine Sekunde Testsignal um **≤ 1 % RMS**
   voneinander ab.
2. `import mimic.effekte` bleibt unter 100 ms — messbar, und der Beleg dafür, dass scipy
   nicht auf Modulebene liegt.
3. Last für `tv` fällt von 7.0 auf **unter 1 ms** je Sekunde Audio.
4. `telefon` besteht `demo()`: Blockgrenzen tragen, keine Stille, kein Clipping.
5. Spektraler Schwerpunkt von `telefon` liegt zwischen 300 und 3400 Hz — `_schwerpunkt`
   (`effekte.py:587`) kann das schon.

### E3 — `hall`

Der erste Regler. Volle Verdrahtung, damit die Kette einmal komplett durchgezogen ist und
E4/E5 nur noch kopieren.

Dateien: `effekte.py` (`Hall`, Konstanten, `hall_wert`, `demo()`), `voices.py:40-52` und
`:269-272`, `worker.py:389-398`, `frontend.py:19-20` und `:266-274`, `cli.py:700-711` und
`:77-82`, `gui.py:44` und `:1112-1116`, `gui.html:635-666` und `:1002-1008`,
`tests/test_phase2.py`.

_Abnahme:_
1. Ein einzelner Impuls klingt über **mindestens drei Blöcke** nach.
2. `abschluss()` liefert den Schwanz vollständig; die Gesamtlänge wächst um genau die
   IR-Länge.
3. Derselbe Text zweimal ergibt bitgleiches Ergebnis (LCG, nicht `np.random`).
4. Last unter **20 ms** je Sekunde Audio.
5. Wiring-Test nach dem Muster von `TonhoeheTests` (`tests/test_phase2.py:911`):
   Profilwert plus Reglerwunsch addieren sich; Unsinn fällt auf 0.0 zurück; `hall=1.5`
   ergibt `400` am Frontend.
6. `hall=0` ist **bitgleich** zu einer Kette ohne Hall.

### E4 — `verzerrung` und `kruemel`

Zwei Stufen in einer Etappe, weil sie dieselbe Form haben und beide unter zehn Zeilen DSP
bleiben.

_Abnahme:_
1. `verzerrung=1.0` hebt den spektralen Schwerpunkt messbar (`_schwerpunkt`), ohne die
   Dauer zu ändern.
2. `kruemel=1.0` erzeugt höchstens `2^4` verschiedene Probenwerte in einem Block.
3. Beide bei 0.0 bitgleich zur Kette ohne sie.
4. Wiring-Tests wie E3.

### E5 — `breite`

_Abnahme:_
1. `breite=0` bitgleich zur Kette ohne die Stufe.
2. `breite=1.0`: Last unter **30 ms** je Sekunde Audio.
3. Derselbe Text zweimal gleich (LFO-Phase und Cent-Versatz deterministisch).
4. Kein Klick an Blockgrenzen: die maximale Probendifferenz an jeder Blocknaht bleibt unter
   dem Wert, den dieselbe Stelle im unbearbeiteten Signal hat, mal drei.
5. Wiring-Tests wie E3.

### E6 — Reglerleiste aufräumen

Nach E5 stehen **neun** Schieber in der Transportleiste (`gui.html:635-666`). Die Leiste
bricht seit `gui.html:284` um, aber neun in zwei Zeilen ist keine Bedienoberfläche mehr.

Vorschlag, bewusst klein: die fünf Tonhöhen-Regler bleiben sichtbar, die vier neuen kommen
hinter einen `<details>`-Aufklapper „Klangfarbe". Kein Framework, kein Zustand, kein
JavaScript über das hinaus, was `KLANG` (`gui.html:1002`) schon tut.

_Abnahme:_ alle neun Regler weiterhin per Tastatur erreichbar, `aria-label` je Schieber,
Doppelklick-Reset unverändert, `klangwerte()` liefert weiterhin alle neun.

### E7 — `vocoder`

Zuletzt, weil hier als einziges bestehender Code umgebaut wird.

Dateien: `effekte.py` — `_Tonhoehenleser` aus `Klangregler._grundton` (`:434`) und
`self.letzte`/`self.hoerfenster` (`:414-415`) herauslösen; `Klangregler` benutzt ihn;
`Effekt` bekommt den `vocoder`-Zweig.

_Abnahme:_
1. **Regressionstest zuerst:** die Autotune-Prüfungen in `demo()` (Median und
   Standardabweichung der Tonspur beim Raster) liefern vor und nach dem Herauslösen
   **denselben** Wert. Ohne diesen Nachweis wird der Vocoder nicht gebaut.
2. `vocoder` mit einem 200-Hz-Testton: der Ausgang trägt Energie bei 200 Hz und dessen
   Vielfachen, nicht bei einer festen Trägerfrequenz.
3. Last unter **15 ms** je Sekunde Audio bei 24 Bändern.
4. Blockgrenzen tragen (`zi` je Band).

## 7. Rechenlast gesamt

Schlimmster Fall, alles gleichzeitig an, gemessen bzw. abgeschätzt (ms je Sekunde Audio):

| Stufe | ms/s |
|---|---|
| `Klangregler` mit Formant (gemessen) | 22.1 |
| `Verzerrer` + `Kruemel` (geschätzt) | 1.5 |
| `Effekt telefon` nach dem Umbau (gemessen als `sosfilt`) | 0.3 |
| `Breite` (Budget, siehe E5) | 30.0 |
| `Hall` (gemessen) | 13.7 |
| **Summe** | **~68** |

RT rund 15×. Das läuft auf dem Faden, der das GIL hält — bei einer Äußerung von 5 s also
0.34 s Rechenzeit zusätzlich. Unkritisch für die Rahmenfrist von 2 s aus PHASE2 Schritt 13,
aber **nicht** unkritisch für den ersten Rahmen: `Hall` und `Breite` verzögern zwar nicht,
der `Klangregler` hängt aber schon heute einen halben Rahmen (15 ms) nach. Die
500-ms-Gesamtfrist aus PHASE2 Schritt 13 bleibt damit dominiert von den 180–205 ms
Modellvorlauf, nicht von dieser Kette.

`demo()` bekommt in E5 eine Lastprüfung, die bei Überschreiten des Budgets fehlschlägt —
sonst wandert die Zahl unbemerkt nach oben.

## 8. Risiken

| Risiko | Wie behandelt |
|---|---|
| Befund 1 ändert den Klang bestehender Profile | Betrifft nur Profile mit gesetztem `effekt` **und** einem Tonhöhenwert. Am 2026-08-14 über alle `settings.json` in `~/.local/share/mimic/voices` geprüft: **kein einziges betroffen**. Vor E1 erneut prüfen, dann ist der Umbau klangneutral. |
| `scipy` rutscht auf Modulebene | Abnahmekriterium E2.2 misst die Importzeit von `mimic.effekte`. |
| Vocoder-Umbau bricht das Autotune | Abnahmekriterium E7.1 verlangt den Regressionsnachweis **vor** dem Neubau. |
| Neun Regler machen die GUI unbedienbar | E6, eingeplant statt hinterher entdeckt. |
| Neue Stufe hebt den Grundpegel über `STUMM_PEAK` | Strukturell gelöst: nach E1 misst die Erkennung das rohe Signal. Der Test aus E1.4 hält das fest. |
| Effektlast summiert sich unbemerkt | Lastprüfung in `demo()` ab E5. |

## 9. Umsetzung — Stand 2026-08-14

Alle sieben Etappen gebaut, `tests/run.sh` 193 Tests grün, `python mimic/effekte.py` grün.

| Etappe | Ergebnis | Abweichung vom Plan |
|---|---|---|
| E1 `Kette` | gebaut, Reihenfolge gedreht, Stummerkennung auf dem Rohsignal | Tremolo lag bei falscher Ordnung nicht bei 73, sondern **89 Hz** (Seitenband, nicht Träger) |
| E2 `sosfilt` + `telefon` | tv von **7.0 auf 0.6 ms/s**, telefon 0.6 | Rauschen liegt beim Telefon **vor** dem Filter, nicht dahinter — sonst zog es den Schwerpunkt aus dem Band |
| E3 `hall` | 6.6 ms/s bei echter Blockgröße | Last hängt an der Blockgröße (22.9 / 11.5 / 5.6 bei 2048 / 4096 / 8192 Proben), als `ponytail:` vermerkt |
| E4 `verzerrung`, `kruemel` | zusammen 0.2 ms/s | — |
| E5 `breite` | 2.5 ms/s, Budget war 30 | — |
| E6 Aufklapper | vier Klangfarbenregler hinter `<details>` | **visuell nicht geprüft**, Browser-Pane war nicht darstellbar |
| E7 `vocoder` | 13.1 ms/s, Autotune **bitgleich** vor und nach dem Umbau | 16 statt 24 Bänder (19.1 → 13.1 ms/s); unteres Band von 180 auf **80 Hz**, sonst war eine tiefe Stimme praktisch stumm |

**Gesamtlast, alles gleichzeitig: 50.8 ms je Sekunde Audio, RT 20×** — geschätzt waren 68.
`import mimic.effekte` bleibt bei 33 ms, scipy wird dabei nicht geladen.

**Ein Abnahmekriterium war falsch formuliert.** E2.1 verlangte ≤ 1 % RMS-Abweichung des
TV-Effekts. Gemessen: **2.31 %** — und zwar vollständig, weil das Grundrauschen jetzt aus
einem `Generator` statt aus einem eigenen LCG kommt. Der Filterpfad selbst ist bei
abgeschaltetem Rauschen **bitgleich** (0.00000 %, maximale Abweichung 0 LSB). Zwei
unabhängige Rauschfolgen müssen sich um √2 mal ihren Effektivwert unterscheiden; das
Kriterium hat eine Eigenschaft gefordert, die es gar nicht prüfen wollte. Richtig ist:
*Filterpfad bitgleich, Rauschfolge geändert und weiterhin wiederholbar.*

**Nicht gebaut, obwohl im Entwurf erwähnt:** `kollektiv` bleibt als eigener Effektname
neben `breite` stehen, mit `ponytail:`-Vermerk und Ausbaupfad. Nichts commitet — die
Etappen liegen im Arbeitsbaum.

## 10. Was dieser Plan nicht liefert

- Presets über `GLADOS` (`effekte.py:62`) hinaus. Ein „Kirche"- oder „Funkgerät"-Preset ist
  eine Zeile, sobald die Regler stehen — aber es ist nicht Teil dieser Runde.
- Echte Raum-Impulsantworten als Datei. Dateien heißen Pfade, Pfade heißen Validierung im
  Frontend, und `voices.py` prüft heute nur Zahlen und Namen aus einer Whitelist.
- Flanger, Phaser, Gate, Whisper. Stehen in `EFFEKTE.md` unter „Nicht bauen": je neun
  Verdrahtungsstellen bei einem Bruchteil der Wirkung auf Sprache.
- Automatisierte Lastmessung in `tests/run.sh`. Die Budgets sind `demo()`-Asserts; eine
  Performance-Gate in der Testsuite wäre auf einer Arbeitsmaschine nur eine Quelle für
  falsche Fehlschläge.
