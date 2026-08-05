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
| C | TTFA p95, warm, n = 50 | < 300 ms | **90.9 ms** | **PASS** |
| D | Akzent-Leakage gegen eigene Baseline | ≤ 2 Treffer von 10 | **1 Treffer** (`ak_09`) | **PASS** |
| E | Kaltstart bis bedienbar | < 60 s | **7.1 s** | **PASS** |

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
- **`MemoryHigh`/`MemoryMax`** jetzt bezifferbar: ~2.3 GB Ruhe, ~5.5 GB Spitze.
- **Leerlauffrist** — bestimmt, wie oft ein Kaltstart eintritt. Mit 7.1 s Kaltstart ist
  der Druck gering.
- **RSS unter Last** wurde nur beim Laden gemessen, nicht bei paralleler Nutzung.
