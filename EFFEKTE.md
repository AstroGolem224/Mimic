# Recherche: weitere Soundeffekte für Mimic

_2026-08-14. Bericht, keine Umsetzung. Alle Messwerte auf dieser Maschine, 48 kHz,
eine Sekunde Sprache, Blöcke à 8192 Byte; „RT" = Vielfaches der Echtzeit._

## Kernbefund vorweg

Vier der acht gewünschten Effekte stecken bereits im Code, drei davon vollständig.
Und die beiden Abhängigkeiten, die man für den Rest bräuchte, sind **schon installiert**:
`scipy 1.18.0` kommt über librosa mit (`uv.lock:466`), `torch`/`torchaudio` sind harte
Abhängigkeiten (`pyproject.toml:13`). Es ist also keine Frage von „neue Abhängigkeit ja
oder nein", sondern nur, welche der vorhandenen an welcher Stelle angefasst wird.

## Übersicht

| Effekt | Status heute | Verfahren | Zustand / Latenz | Stufe | Aufwand | Wirkung |
|---|---|---|---|---|---|---|
| Pitch Shift | **fertig** `effekte.py:317`, `worker.py:393` | WSOLA + lineares Neuabtasten | halber Rahmen, 15 ms | a | – | – |
| Autotune | **fertig** `effekte.py:393` (`raster`), `worker.py:395` | F0 per AKF, Rundung aufs Halbtonraster | wie oben | a | – | – |
| Detune | **fertig** `effekte.py:349` (`streuung`), `worker.py:394` | Silbenweiser Versatz in Halbtönen, eigener LCG | wie oben | a | – | – |
| Formant | **fertig** `effekte.py:219`, `worker.py:396` | STFT + Cepstrum-Hüllkurve, Achse gestaucht | 1024/256, 21 ms | a | – | – |
| Chorus | **halb** — `kollektiv` `effekte.py:138` ist ein statisches Doppel-Delay | dasselbe, aber Verzögerung per LFO moduliert | Verlaufspuffer, 0 ms | a | S | hoch |
| Choir | **halb** — dieselbe Stelle, ohne Verstimmung | 3–5 Kopien, je eigenes Delay **und** eigener Pitch-Versatz | 3–5 × Klangregler | a | M | hoch |
| Reverb | **fehlt** | Overlap-Add-Faltung mit erzeugter Impulsantwort | IR-Länge, 0 ms Zusatzlatenz | a | M | sehr hoch |
| Distort | **halb** — `tanh` im TV-Effekt `effekte.py:184` | eigene Stufe: Vorverstärkung, Kennlinie, Anti-Alias-Oversampling | zustandslos, 0 ms | a | S | mittel |
| Vocoder | **fehlt** | Filterbank-Vocoder, Träger = Sägezahn aus geschätzter F0 | Filterzustand, 0 ms | b | L | hoch |
| Flanger | fehlt | Chorus mit Rückkopplung, kurzes Delay | wie Chorus | a | S | niedrig |
| Phaser | fehlt | 4–8 Allpässe, LFO auf den Koeffizienten | `sosfilt`-`zi` | b | S | niedrig |
| Bitcrusher | fehlt | Quantisieren + Halten (Downsampling ohne Filter) | Halte-Rest | a | XS | mittel |
| Telefon / Funk | **halb** — TV-Band `effekte.py:39` | Butterworth-Bandpass 300–3400 Hz statt Einpolfilter | `sosfilt`-`zi` | b | XS | mittel |
| Gate / Ducking | fehlt | Hüllkurvenfolger, Schwelle, Attack/Release | zwei Skalare | a | XS | niedrig |
| Whisper | fehlt | STFT, Phase durch Zufallsphase ersetzt | STFT-Rahmen | a | M | mittel |

Stufen: **a** numpy allein · **b** scipy (bereits da) · **c** eigene Audio-Bibliothek ·
**d** Modell. Keiner der Kandidaten landet bei c oder d — siehe „Nicht bauen".

## Gemessen, nicht geschätzt

Bestand (`effekte.py`), damit die Kandidaten unten einen Maßstab haben:

| | ms je Sekunde Audio | RT |
|---|---|---|
| `Effekt roboter` | 0.6 | 1660× |
| `Effekt kollektiv` | 0.7 | 1380× |
| `Effekt tv` (Python-Schleife, `effekte.py:170`) | 7.0 | 143× |
| `Klangregler` nur Tempo | 7.1 | 140× |
| `Klangregler` Pitch + Raster + Streuung | 17.9 | 56× |
| dazu Formant | 22.1 | 45× |

Bausteine für die Kandidaten:

| | ms je Sekunde Audio | RT |
|---|---|---|
| `scipy.signal.sosfilt`, Butterworth 4. Ordnung, mit `zi` | 0.3 | 3119× |
| `fftconvolve`, 1.5 s Impulsantwort, blockweise | 13.7 | 73× |
| `lfilter` als langer Rückkopplungskamm (ein Schroeder-Kamm von acht) | 33.5 | 30× |
| STFT 1024/256 hin und zurück | 1.7 | 579× |

Importkosten, weil `effekte.py` **auch vom Frontend** geladen wird (`frontend.py:19`) und
PHASE2 Schritt 3 dort um jede Zehntelsekunde vor `submit()` kämpft:

| Modul | kalt |
|---|---|
| `numpy` | 44 ms |
| `scipy.signal` | 306 ms |
| `torch` | 516 ms |
| `torchaudio.functional` (nach torch) | 17 ms |

**Folge, bindend:** scipy darf in `effekte.py` nur **innerhalb** der Effektklasse
importiert werden, nie auf Modulebene. Sonst zahlen Frontend, CLI und GUI 306 ms
Startzeit für einen Effekt, den nur der Worker benutzt. Torch scheidet als Effektquelle
ganz aus — siehe unten.

## Je Effekt, was die Tabelle nicht fasst

**Reverb.** Der naheliegende Weg — acht Schroeder-Kämme per `lfilter` — ist mit 33.5 ms
je Kamm der **teuerste** der drei Wege und braucht acht davon. Der billige Weg ist die
Faltung: einmal beim Aufbau eine Impulsantwort erzeugen (exponentiell abklingendes
Rauschen, gefiltert), dann blockweise `fftconvolve` mit Overlap-Add. 13.7 ms je Sekunde
für 1.5 s Nachhall, keine Rückkopplung, keine Stabilitätsfrage, und das Zeug für echte
Raum-IRs (Kirche, Kachelbad) liegt gleich mit drin. Latenz **null**, wenn Overlap-Add
korrekt geschrieben ist — der Schwanz wandert in den nächsten Block, nicht der Anfang in
die Zukunft. Braucht einen `abschluss()` wie `Klangregler`, sonst wird der Hall am
Satzende abgeschnitten.

**Choir.** Kein neuer Algorithmus, sondern N Instanzen des vorhandenen `Klangregler` mit
je eigenem `halbtoene`/`streuung` plus je eigener Verzögerung, aufsummiert. Preis ehrlich:
vier Stimmen à 17.9 ms sind 72 ms je Sekunde Audio, RT 14× — bei einer Äußerung von 5 s
also 0.36 s Rechenzeit auf dem Faden, der das GIL hält. Immer noch weit vom Engpass, aber
nicht mehr geschenkt. Deutlich billiger und fast so gut: **eine** Zeitdehnung, danach
mehrere lineare Neuabtastungen mit leicht verschiedenem Verhältnis (`_Neuabtastung`
`effekte.py:189` kann das schon) — dann kostet die vierte Stimme fast nichts.

**Chorus.** `kollektiv` ist ein Chorus ohne LFO — zwei feste Verzögerungen bei 17 und
29 ms (`effekte.py:36`). Was fehlt, ist die langsame Modulation der Verzögerungszeit
(0.3–1.5 Hz, ±3 ms) und damit die Verstimmung, die einen Chorus ausmacht. Der Umbau ist
klein: Ableselage per `np.interp` statt ganzzahligem Index. Der bestehende
`self.verlauf`-Puffer trägt das unverändert.

**Distort.** Steckt als `tanh` im TV-Effekt (`effekte.py:184`), aber ohne Vorverstärkung
und ohne eigenen Regler. Als eigene Stufe: `tanh(x·g)/tanh(g)` mit `g` von 1 bis 20.
Ehrlich zu nennen: harte Kennlinien erzeugen Obertöne über der Nyquistgrenze, die als
Aliasing zurückfalten. Bei `tanh` und Sprachmaterial ist das mild; bei einer
Hard-Clip-Variante nicht, dann braucht es vierfaches Oversampling — Faktor 4 auf die
Rechenlast, immer noch billig.

**Vocoder.** Der Begriff meint zwei verschiedene Dinge. Der **Phasenvocoder** ist schon
da (`Klangregler`, WSOLA ist sein Verwandter). Gemeint ist vermutlich der
**Kanalvocoder**: 16–24 Bandpässe auf der Sprache, je Band die Hüllkurve, damit ein
Trägersignal (Sägezahn, Rauschen) moduliert — der Roboterklang, der nicht Tremolo ist.
Mit `sosfilt` und `zi` sind 24 Bänder rund 7 ms je Sekunde, RT 140×. Der eigentliche
Aufwand ist nicht Rechenzeit, sondern **Träger**: ein Sägezahn mit fester Tonhöhe klingt
tot, einer mit der geschätzten F0 lebendig — und die F0-Schätzung liegt fertig in
`Klangregler._grundton` (`effekte.py:356`), müsste aber geteilt statt kopiert werden. Das
ist der Umbau, der diesen Effekt zu L statt M macht.

**Telefon/Funk und Phaser.** Beide sind derselbe Handgriff: `scipy.signal.sosfilt` mit
mitgeführtem `zi`. Das löst nebenbei die als `ponytail:` markierte Schuld in
`effekte.py:158` — der TV-Effekt ist die einzige Python-Schleife im Modul und mit 7.0 ms
zehnmal teurer als der Rest, weil genau dieses `zi` fehlte. Ein `sosfilt` mit Zustand
ersetzt sie und bringt sie auf 0.3 ms.

**Whisper.** STFT, Betrag behalten, Phase würfeln, zurück. 1.7 ms je Sekunde. Klingt auf
Sprache brauchbar, ist aber ein Effekt, den man einmal hört und nie wieder benutzt.

## Drei Befunde am Bestand, die vor jedem neuen Effekt zu klären sind

1. **Die Kette läuft heute in der falschen Reihenfolge.** `Effekt` wirkt vor dem
   `Klangregler` (`worker.py:442` gegen `:406`). Damit verschiebt der Pitch-Shift die
   Tremolo-Frequenz und die Verzögerungen des `kollektiv` gleich mit: dieselbe Stimme
   klingt bei `tonhoehe=+5` nach einem anderen Effekt als bei `0`. Bei den drei
   vorhandenen Effekten fällt das kaum auf. Ein Reverb hinter dem Pitch-Shift ist dagegen
   Pflicht — Hall wird gehört, nicht transponiert.
2. **Die Stummerkennung sitzt mitten in der Kette.** `spitze` wird **nach**
   `effekt.verarbeite` gemessen (`worker.py:441` gegen `:449`). Jeder neue Effekt, der
   einen Grundpegel hinzufügt — Reverb-Vorhall, Vocoder-Träger, Rauschen —, hebt das
   Signal über `STUMM_PEAK` (`worker.py:52`, entspricht −25 dBFS) und macht die
   Wiederholung stummer Takes blind. Das ist genau der Mechanismus, den PHASE2 Schritt 2a
   und Kriterium P2-L absichern. Neue Effekte gehören deshalb **hinter** die Erkennung,
   in `sende()`.
3. **Nur `Klangregler` hat ein `abschluss()`.** `Effekt` braucht keines, weil keiner der
   drei nachklingt. Reverb, Choir und ein Chorus mit Rückkopplung tun das. Der Worker ruft
   `abschluss()` heute nur für `tempo` (`worker.py:474`) — eine zweite Stufe mit Schwanz
   braucht dort eine eigene Zeile, sonst endet jeder Satz mit einem abgeschnittenen Hall.

## Vorgeschlagene Signalkette

1. **Quelle** — `tensor_to_pcm`, Verstärkung aus dem Profil
2. **Stummerkennung** — unverändertes Signal, wie PHASE2 es verlangt
3. **Zeit & Tonhöhe** — `Klangregler`: Tempo, Pitch, Autotune, Detune, Formant
4. **Farbe** — Distort, Bitcrusher, Telefon-Bandpass, Vocoder _(vertauschbar untereinander,
   außer: Bandpass nach der Sättigung, sonst filtert man die Obertöne wieder weg)_
5. **Verbreiterung** — Chorus, Choir, Flanger, Phaser _(untereinander vertauschbar)_
6. **Raum** — Reverb _(nicht vertauschbar: alles, was danach kommt, verwischt den Hall)_
7. **Begrenzung** — `_begrenze_feld`, einmal am Ende jeder Stufe mit Zwischenclipping

Fest sind nur drei Kanten: 2 vor 3, Raum zuletzt, Begrenzung ganz zuletzt. Der Rest ist
Geschmack.

## Verdrahtungs-Checkliste je neuem Effekt mit Parameter

Am Beispiel eines Reglers `hall` von 0 bis 100 %:

- [ ] `effekte.py` — Klasse mit `verarbeite`/`abschluss`, `HALL_MIN`/`HALL_MAX`,
      `hall_wert()` nach dem Muster von `_zahl` (`effekte.py:62`)
- [ ] `effekte.py` — `demo()` erweitern: Blockgrenzen tragen, keine Stille, kein Clipping,
      derselbe Text klingt zweimal gleich
- [ ] `voices.py:48` — Feld in `Stimmprofil`; `:260` Validierung beim Laden; `:345`
      Rückgabetupel von `_read_settings`
- [ ] `worker.py:389` — Profilwert plus Reglerwert addieren; `:397` Objekt aufbauen;
      `:406` in `sende()` aufrufen; `:474` `abschluss()` anhängen
- [ ] `frontend.py:19` Import, `:266` Bereichsprüfung im `regler`-Verzeichnis
- [ ] `cli.py:635` `--hall`-Flag, `:77` in die Anfrage
- [ ] `gui.py:44` Import, `:1112` in `regler`
- [ ] `gui.html:633` `<label class="tempo">` mit `<input type="range">`, `:1002` Eintrag in
      der `KLANG`-Liste (dort stehen schon fünf Regler; die Leiste bricht seit
      `gui.html:284` um)
- [ ] `tests/test_phase2.py` — Wiring-Test, der den Wert von CLI und GUI bis zum Worker
      verfolgt

Neun Stellen. Das ist der Grund, warum die Empfehlung unten auf wenige, große Effekte
setzt statt auf viele kleine.

## Empfehlung

1. **Reverb** — Overlap-Add-Faltung mit erzeugter Impulsantwort, ein Regler `hall`
   (0–100 %), fest bei 1.2 s Abklingzeit.
   _Fertig, wenn:_ `demo()` zeigt, dass ein einzelner Impuls über mindestens drei Blöcke
   nachklingt, `abschluss()` den Schwanz vollständig liefert, die Gesamtlänge der Äußerung
   um genau die IR-Länge wächst, und die Rechenlast unter 20 ms je Sekunde Audio bleibt.
2. **`sosfilt`-Umbau des TV-Effekts, plus Telefon-Band als zweiter Effektname.** Kein
   neuer Klang, sondern die Voraussetzung für Vocoder und Phaser — und er löst die
   `ponytail:`-Schuld aus `effekte.py:158`.
   _Fertig, wenn:_ das Ergebnis des alten und neuen TV-Effekts über eine Sekunde
   Testsignal um höchstens 1 % voneinander abweichen, `scipy` **nicht** auf Modulebene
   importiert wird (messbar: `import mimic.effekte` bleibt unter 100 ms), und die Last
   von 7.0 auf unter 1 ms je Sekunde fällt.
3. **Chorus/Choir als ein gemeinsamer Regler** `breite` (0–100 %): unter 50 % moduliert er
   die vorhandenen `kollektiv`-Verzögerungen per LFO, darüber kommen zwei verstimmte
   Neuabtastungen dazu.
   _Fertig, wenn:_ bei `breite=0` das Ergebnis **bitgleich** zum heutigen `kollektiv` ist,
   bei 100 % die Last unter 30 ms je Sekunde bleibt, und derselbe Text zweimal gleich
   klingt.

Danach neu bewerten. Vocoder ist der stärkste verbleibende Kandidat, aber er verlangt den
Umbau der F0-Schätzung zu einem geteilten Baustein — das ist eine eigene Runde und keine
Ergänzung.

## Nicht bauen

| | Warum |
|---|---|
| **pedalboard** | GPLv3 (JUCE-Kern). Das relizenziert Mimic. Für einen Hall, den 40 Zeilen numpy können, ist das kein Handel. |
| **torchaudio-Effekte** (`flanger`, `phaser`, `overdrive`, `lfilter`) | Sind da und wären gratis — aber alle arbeiten auf dem **ganzen** Tensor ohne Zustandsübergabe (`lfilter` nimmt kein `zi`). Blockweise angewandt klicken sie an jeder Naht, und genau das ist die Eigenschaft, die `effekte.py` seit dem ersten Kommentar vermeidet. Dazu zieht ein Import Torch in Frontend und CLI (516 ms). |
| **Schroeder-Kammfilter-Reverb per `lfilter`** | Gemessen 33.5 ms für **einen** von acht Kämmen. Die Faltung macht dasselbe achtmal billiger. |
| **Echte Raum-Impulsantworten als Dateien** | Erst wenn der erzeugte Hall zu synthetisch klingt. Dateien heißen Pfade, Pfade heißen Validierung im Frontend, und `voices.py` prüft heute nur Zahlen und Namen aus einer Whitelist. |
| **Freie Filterketten in `settings.json`** | `effekte.py:26` verbietet das ausdrücklich und hat recht: es wäre eine Einladung, dem Worker beliebige Rechenlast unterzuschieben. Jeder neue Effekt bleibt ein Name aus `EFFEKTE` plus Zahlen mit Grenzen. |
| **Modellbasierte Effekte** (neuronaler Vocoder, Stimmumwandlung) | Zweites Modell neben dots.tts, in einer Unit mit `MemoryMax=7G`, in der laut PHASE2 §2b schon zwei Runtimes nicht nebeneinander passen. Kein Weg dahin, der nicht die Speicherzusage bricht. |
| **Flanger, Phaser, Gate, Whisper einzeln** | Jeder kostet dieselben neun Verdrahtungsstellen wie ein großer Effekt, bei einem Bruchteil der Wirkung auf Sprache. Wenn, dann später als Varianten eines bestehenden Reglers, nicht als eigene Namen. |
