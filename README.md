# Mimic

Self-hosted Voice-Cloning-TTS. Engine: [dots.tts](https://github.com/rednote-hilab/dots.tts) (Apache-2.0).

Zwei Konsumenten: **dAImon** (Charakterstufe, Streaming) und **MMC** (Sprachaufnahmen zur Bauzeit).

**Stand: Phase 0 (Spike).** Der Dienst existiert noch nicht. `spike/` ist Wegwerfcode, der
genau eine Frage beantwortet: taugt dots.tts auf einer RTX 5090 für diesen Zweck?

Plan, Glossar und ADRs: `~/Dokumente/UMBRA-Notes/DDs/Mimic/`

## Phase-0-Abnahme

| # | Kriterium | Stand |
|---|---|---|
| A | Blackwell — E2E-CUDA-Lauf ohne CPU-Fallback | ✅ **PASS** — GPU 63 %, VRAM +6222 MiB |
| B | Klang-Treue — verblindete Echt/Synthetisch-Unterscheidung, ≤ 8/12 richtig | ⏳ braucht Aufnahmen |
| C | Latenz — TTFA p95 < 300 ms (`mf`, warm, n ≥ 50) | ✅ **PASS** — 40.2 ms / 105.7 ms |
| D | Akzent-Leakage — ≤ 2 Treffer gegen eigene englische Baseline | ⏳ braucht Aufnahmen |
| E | Kaltstart-Bereitschaft — binnen 60 s bedienbar | ✅ **PASS** — 58.1 s / 21.9 s |

Zwei Zahlen je Kriterium = `optimize=True` / `optimize=False`.

### Messwerte (RTX 5090, Treiber 610.43.02, torch 2.8.0+cu128, `mf`)

| | `optimize=True` | `optimize=False` |
|---|---|---|
| TTFA p95 | 40.2 ms | 105.7 ms |
| RTF median | 0.1603 | 0.5250 |
| Kaltstart bis erstes Audio | 58.1 s | 21.9 s |
| Marge zu dAImons `GPU_FRIST_S=120` | 2.1× | 6.2× |

`soar` + `mf` gleichzeitig resident: 11.2 GiB von 32 — Moduswechsel braucht keinen
Prozessneustart. `del` + `empty_cache()` gibt bei dots.tts 5530 MiB zurück.

## Spike ausführen

```bash
cd spike
uv sync --python 3.12
uv run python fetch_checkpoints.py    # ~10 GB, feste Revisionen
uv run python 00_blackwell.py         # Kriterium A
uv run python 01_latenz.py            # Kriterium C
uv run python 01_latenz.py --kaltstart  # Kriterium E
uv run python 03_modewechsel.py       # Schritt 7
uv run python test_spike.py           # Selbstchecks
```

### Was noch Aufnahmen braucht (B und D)

```bash
uv run python 02_aufnehmen.py referenz    # ~10 s, daraus klont Mimic
uv run python 02_aufnehmen.py blindtest   # 6 Sätze
uv run python 02_aufnehmen.py akzent      # 10 englische Sätze
uv run python 02_aufnehmen.py status

uv run python 04_blindtest.py erzeugen    # Kriterium B
uv run python 04_blindtest.py hoeren
uv run python 05_akzent.py erzeugen       # Kriterium D
uv run python 05_akzent.py hoeren
```

Aufnahmen landen unter `aufnahmen/` (nicht im Repo) und
`~/.local/share/mimic/voices/matthias/`.
