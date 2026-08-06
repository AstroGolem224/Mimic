# Phase 0 — Abnahme

Abgeschlossen 2026-08-05. Hardware: RTX 5090 (Blackwell sm_120), Treiber 610.43.02,
CachyOS, 30 GiB RAM. Software: Python 3.12, torch 2.8.0+cu128, dots.tts 0.2.1,
Checkpoints auf feste HF-Revisionen gepinnt (`revisions.yaml`).

**Ergebnis: dots.tts bleibt. Phase 1 kann geplant werden.**

## Kriterien

| # | Kriterium | Schwelle | Ergebnis | |
|---|---|---|---|---|
| A | Blackwell, E2E-CUDA ohne CPU-Fallback | GPU arbeitet nachweislich | GPU-Auslastung 63 %, VRAM +6222 MiB | **PASS** |
| B | Verblindete Echt/Synthetisch-Unterscheidung | ≤ 8/12 richtig | **12/12 richtig** | **FAIL** |
| B2 | Brauchbarkeit als Matthias' Stimme | ≥ 5/6 Mimic-Proben | `[EN]` 6/6, `[DE]` 5/6, Kontrolle 5/6 | **PASS** |
| C | TTFA p95, warm, n = 50 | < 300 ms | 90.9 ms **ohne Klonen** — siehe Nachtrag | **PASS**¹ |
| D | Akzent-Leakage gegen eigene Baseline | ≤ 2 Treffer von 10 | **1 Treffer** (`ak_09`) | **PASS** |
| E | Kaltstart bis bedienbar | < 60 s | **7.1 s** | **PASS** |

## Nachtrag 2026-08-05 — Kriterium C hat den falschen Pfad gemessen

¹ Die 90.9 ms stammen aus `01_latenz.py`, und dieses Skript rief
`generate_stream` **ohne `prompt_audio_path`** — also ohne Klonen. Gemessen wurde
damit eine Konfiguration, die so nie ausgeliefert wird.

Isoliert nachgemessen (n = 15, gleicher Runtime, gleicher Text):

| | TTFA median |
|---|---|
| ohne `prompt_audio` — was C maß | 87.3 ms |
| mit `prompt_audio` — was der Dienst tut | 184.5 ms |

Und am fertigen Dienst, durch das Frontend über den Unix-Socket, Messpunkt beim
Client (`tools/messreihe_ttfa.py`, n = 60, Modus `mf`):

| | |
|---|---|
| min | 226.1 ms |
| median | 239.5 ms |
| **p95** | **250.0 ms** |
| max | 253.5 ms |

**Kriterium C hält auch auf dem echten Pfad** — 250 ms gegen 300 ms Budget, und die
Verteilung ist eng. Die Zahl in der Tabelle oben bleibt als das stehen, was sie war:
eine Messung am falschen Objekt.

Aufschlüsselung der 250 ms: 87 ms Erzeugung, +97 ms Konditionierung aus der Referenz,
+~62 ms Frontend, Prozessgrenze und Rahmung. Der letzte Posten ist der Preis dafür,
dass der Worker ein eigener Prozess ist — und der ist die Grundlage der
Eindämmungszusage, also gut angelegt.

Nebenbefund: eine frühere Einzelmessung am Dienst zeigte 419 ms. Sie ließ sich in
der Reihe nicht reproduzieren. Wahrscheinliche Ursache war `MemoryHigh=3G`: der
Worker erreicht real 5.9 GiB und wurde dadurch bei jeder Anfrage gedrosselt
(`memory.events high` = 17970). Auf 6G angehoben ist die Drosselung weg (`high 0`) —
die Latenz änderte das allerdings **nicht** messbar, das Reclaim war billig.

## Nachtrag 2026-08-06 — Kriterium C fällt derzeit durch, Ursache offen

Dasselbe Instrument (`tools/messreihe_ttfa.py`, n = 60, Modus `mf`), das am 2026-08-05
p95 **250.0 ms** lieferte, liefert nach Phase 2a–2d und 2b:

| | 2026-08-05 | 2026-08-06 |
|---|---|---|
| min | 226.1 ms | 229.4 ms |
| median | 239.5 ms | 289.3 ms |
| **p95** | **250.0 ms** | **585.0 ms** |
| max | 253.5 ms | 667.1 ms |

**Der Boden ist unverändert, nur der Schwanz ist explodiert.** Das ist das Muster von
Konkurrenz oder Warteschlangeneffekten, nicht von einem langsamer gewordenen Modell.
Kriterium C und P2-F sind damit **derzeit nicht erfüllt**.

Vier Hypothesen geprüft und **alle vier widerlegt**:

1. *Wiederholung stummer Takes* — in 36 Aufrufen `stumme_takes=0`, kein einziger.
2. *Verschobener Messpunkt* (seit 2a-bis zählt der erste **hörbare** Rahmen) — nachgemessen:
   der erste Rahmen enthält bereits Ton (Spitzen 18479, 27568, 2331 gegen `STUMM_PEAK`
   1842). Erstes Byte und erster Ton fallen zusammen.
3. *GPU-Drosselung* — `nvidia-smi -q -d PERFORMANCE`: kein einziger Grund aktiv.
4. *Frontend / neuer Leser-Thread* — die Streuung steht schon in `ttfa_ms` des **Workers**,
   also vor jeder Frontend-Beteiligung.

Ebenfalls kein Trend über die Aufrufe: eine Rampenmessung über drei Kaltstart-Zyklen
(`tools/messreihe_warmrampe.py`) zeigt Aufruf 1 im Median bei 295 ms, *niedriger* als die
Aufrufe 2, 3 und 5. Es gibt kein Einschwingen — die Werte springen durchgehend.

**Verbleibender Verdacht, unbelegt:** der Nebenläufigkeits-Umbau aus Phase 2b
(Condition-Eigentümerschleife) oder ein Umgebungsfaktor, den ich nicht gefunden habe. Auf
der GPU liegt ein zweiter Mieter (`qwen-tts-gui`, 6.4 GiB), der aber bei 1.4 % CPU nur
Speicher hält.

### Bisect am 2026-08-06 — der Code ist entlastet

Drei Stände, gleiche Maschine, gleiches Instrument, gleicher Korpus, innerhalb einer
Stunde gemessen. Die alten Stände liefen aus `git worktree` gegen dieselbe venv.

| Stand | p95 heute | zum Vergleich |
|---|---|---|
| `be5c6ac` | **685.0 ms** (max 1494) | **derselbe Commit maß am 05.08. 250.0 ms** |
| `d5f651e` (vor Phase 2b) | 576.3 ms | |
| `HEAD` (mit Phase 2b) | 585.0 ms | |

**Derselbe Commit, der gestern 250 ms lieferte, liefert heute 685.** Damit ist weder
Phase 2b noch Phase 2a–2d die Ursache — die Änderung liegt in der Umgebung, nicht im Code.

Zusätzlich ausgeschlossen: **GPU-Takt**. Während der Inferenz gemessen: 2865–2925 MHz von
3105 max bei 94 % Auslastung, P-State P1. Die Karte boostet sauber, kein Throttle-Grund
aktiv.

**Was bleibt, unbelegt:** auf der GPU liegt seit heute ein zweiter Mieter
(`qwen-tts-gui`, 6364 MiB resident, 1.4 % CPU). Gestern war die Karte bis auf den Desktop
leer (1694 MiB). Ein Prozess, der nur Speicher hält, sollte fremde Rechenzeit nicht
verdoppeln — aber es ist der einzige benannte Unterschied, der übrig ist.

### Fortsetzung — neun Hypothesen, neun Widerlegungen

`qwen-tts-gui` beendet, Reihe wiederholt: **p95 950.2 ms, max 1752.8** — also **schlechter**
statt besser. Auch dieser Verdacht ist tot.

Was in allen Läufen stabil bleibt und was nicht:

| | 05.08. | heute, alle Läufe |
|---|---|---|
| min | 226 ms | **228–232 ms** |
| median | 240 ms | **256–289 ms** |
| p95 | 250 ms | 512–950 ms |
| max | 254 ms | 638–1753 ms |

**Boden und Median sind praktisch unverändert. Nur der Schwanz ist neu.** Die Mehrheit der
Aufrufe ist so schnell wie gestern; eine Minderheit ist zwei- bis siebenmal langsamer.

Ein reproduzierbarer Zusammenhang **besteht** mit der Textlänge:

| Zeichen | median | max |
|---|---|---|
| 7 | 319 ms | 1753 ms |
| 14 | 357 ms | 1669 ms |
| 24 | 402 ms | 1376 ms |
| 63 | 245 ms | 327 ms |
| 70 | 255 ms | 928 ms |
| 100 | **241 ms** | 344 ms |

Kurze Äußerungen sind langsamer und spitzer. Das erklärt den Schwanz aber **nicht
vollständig**: mit einem Korpus aus ausschließlich langen Texten (≥ 89 Zeichen) bleibt p95
bei 512 ms. Und es sind **nicht** die stummen Takes — im selben Lauf nur 4 Wiederholungen
in 60 Aufrufen, verteilt über 7, 14, 106 und 125 Zeichen.

**Neun geprüfte und widerlegte Hypothesen:** stumme Takes · verschobener Messpunkt ·
GPU-Drosselung · Frontend und Leser-Thread · Phase-2b-Code · Phase-2a-Code (Bisect) ·
GPU-Takt · zweiter GPU-Mieter · Verstärkungsberechnung je Anfrage.

**Die Ursache des Schwanzes ist offen.** Was bleibt, liegt innerhalb von dots.tts, torch
oder CUDA und ist ohne Profiling der Modellinnereien nicht zu greifen.

**Ein echter Fund war trotzdem dabei:** `_reference_gain` lief bei **jeder** Anfrage über
710 656 Samples in einer Python-Schleife — gemessen 22 ms, rund zehn Prozent der
Basis-TTFA, für einen Wert, der sich nie ändert. Jetzt zwischengespeichert über
(Gerät, Inode, Größe, mtime): erster Aufruf 22 ms, danach 0 ms.

**Bewertung für den Plan:** Kriterium C ist im aktuellen Maschinenzustand nicht erfüllt,
und zwar nachweislich nicht wegen einer Code-Änderung. Für den dAImon-Pfad ist das
weniger dramatisch als die Zahl suggeriert — dessen Auswahlregel schickt Texte unter
80 Zeichen ohnehin an die Vorgabestufe, und lange Texte liegen im Median bei 241–256 ms.
Vor P2-F gehört die Reihe auf einer frisch gestarteten Maschine wiederholt.

## Zu B und B2 — was wirklich passiert ist

**B ist durchgefallen, maximal.** 12 von 12 Proben korrekt zugeordnet; per Zufall wäre das
1 zu 4096. Der Klon ist zuverlässig als solcher erkennbar. Das wird hier nicht umgedeutet.

Matthias' Diagnose zum Fehlerbild: nicht Timbre, nicht Sprechrhythmus, sondern die
**Aussprache einzelner Wörter** — er sagt „gemördscht", Mimic sagt „ge-mer-get". Also
Anglizismen mit deutscher Beugung. Eng umgrenzte Klasse.

Daraufhin wurde **B2** als Ersatzkriterium formuliert, weil B die falsche Frage gestellt
hatte: gebraucht wird nicht „geht als echte Aufnahme durch", sondern „taugt als Matthias'
Stimme für Game-VO und Assistenz".

**B2 ist zweimal gelaufen und hat sich selbst widersprochen:**

| Lauf | Aufbau | `mimic-en` |
|---|---|---|
| 1 | 12 Proben, nur `[EN]`, direkt nach dem B-Test | **1/6** |
| 2 | 18 Proben, `[DE]` und `[EN]` und echt gemischt | **6/6** |

Dieselbe Bedingung, gegensätzliches Ergebnis. Wahrscheinliche Ursache: in Lauf 1 hatte
Matthias unmittelbar zuvor im B-Test alle zwölf Fälschungen enttarnt und war darauf
geeicht, Fehler zu suchen. Lauf 2 war dreiweg und schwerer zu durchschauen, und die
eingebaute Kontrolle verhielt sich plausibel (er lehnte auch eine seiner eigenen
Aufnahmen ab).

**Konsequenz: B2 ist ein weiches Kriterium, keine Messung.** Lauf 2 ist der
besser konstruierte und trägt die Entscheidung — aber die 6/6 sind keine belastbare Zahl.
Der Befund, der **beide** Läufe überlebt, ist die Schwachstelle: der Code-Switching-Satz.
Er war im ersten Hörtest „ganz übel", und er ist in Lauf 2 die einzige `[DE]`-Ablehnung.

Die echte Validierung ist der Gebrauch in Phase 1, nicht eine dritte Runde Hörtest.

## Betriebspunkt

| | |
|---|---|
| Checkpoint | `mf` für Realtime, `soar` für Batch |
| `optimize` | **`False`** — `True` reißt Kriterium E (94.1 s Kaltstart) |
| Modellkonstruktion | direkt auf der GPU (`laden.py`), **nicht** Torch-Default |
| Sprach-Tag | **`en`**, auch für deutschen Text (`laden.SPRACH_TAG`) |
| Sample-Rate | 48 000 Hz nativ |
| Referenz | ~15 s, 48 kHz mono, plus wörtliches Transkript |

## Messwerte

|  | `optimize=False` | `optimize=True` |
|---|---|---|
| TTFA p95 (n = 50) | **90.9 ms** | 40.2 ms |
| RTF median | 0.5199 | 0.1603 |
| Kaltstart bis erstes Audio | **7.1 s** | 94.1 s |
| Marge zu dAImons `GPU_FRIST_S=120` | 30.8× | 1.5× |

### RAM ist der Engpass, nicht VRAM

Ein Lauf mit den Torch-Defaults wurde vom Kernel OOM-gekillt (`anon-rss 10.3 GB`).
`from_pretrained` baut das Modell auf der CPU in fp32 (~8 GB), bevor es nach cuda/bf16
wandert.

| Ladepfad | RAM-Spitze | RAM Ruhe | Ladezeit |
|---|---|---|---|
| Torch-Defaults | 12479 MiB | 4357 MiB | 14.5 s |
| `torch.device("cuda")` | **5514 MiB** | **2269 MiB** | **4.3 s** |

Nur den dtype-Default zu setzen reicht nicht. Gemessen mit harten cgroup-Limits: der alte
Pfad stirbt bei 10 GB, der neue läuft unter 4 GB.

`soar` + `mf` gleichzeitig: 11.2 GiB VRAM — passt. In RAM ~20 GB — **passt nicht**.

## Was Phase 0 nebenbei erledigt hat

- **Segmentweise Synthese ist tot.** Kurze Fragmente ohne Satzkontext halluziniert das
  Modell voll („ähhh gemerdscht"), Inhalt fällt weg. Damit ist ein Phase-1-Baustein
  gestorben, bevor er geplant wurde — er hätte mit den Chunk-Grenzen des Streamings
  kollidiert.
- **Kein Sprach-Erkenner nötig.** `[EN]` trägt auch reines Deutsch, verblindet bestätigt.
- **Moduswechsel braucht keinen Prozessneustart.** Beide Checkpoints passen ins VRAM.
- **P1-3 war falsch begründet.** `del` + `empty_cache()` gibt bei dots.tts 5530 MiB
  zurück; dAImons whisper-Messung überträgt sich nicht. Prozessende bleibt richtig, aber
  als Robustheitsentscheidung.

## Offen für Phase 1

- **Aussprache-Tabelle** für Anglizismen mit deutscher Beugung. Für MMC-Batch trivial
  (Text steht vorher fest), für dAImon eine kleine Ersetzungstabelle.
- **Leerlauffrist** — bestimmt, wie oft ein Kaltstart eintritt. Mit 7.1 s Kaltstart ist
  der Druck gering.
- **RSS unter paralleler Nutzung** — bisher nur sequenziell gemessen.

Erledigt seit Phase 0:

- `MemoryHigh`/`MemoryMax` sind gesetzt und **an der Realität geeicht**, nicht an der
  08_ram-Zahl: im Dienst liegt die Spitze bei 5.89 GiB, nicht bei den 2.3 GB Ruhezustand
  ohne Klonen. `MemoryHigh=6G` (darunter drosselt der Kernel dauernd), `MemoryMax=7G`
  (wird laut `memory.events` nie erreicht).
- Die TTFA-Messreihe am Socket liegt vor, siehe Nachtrag oben.
