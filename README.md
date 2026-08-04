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
| C | Latenz — TTFA p95 < 300 ms (`mf`, warm, n ≥ 50) | ✅ **PASS** — 90.9 ms |
| D | Akzent-Leakage — ≤ 2 Treffer gegen eigene englische Baseline | ⏳ braucht Aufnahmen |
| E | Kaltstart-Bereitschaft — binnen 60 s bedienbar | ✅ **PASS** — 7.1 s |


### Messwerte

RTX 5090, Treiber 610.43.02, torch 2.8.0+cu128, Checkpoint `mf`, `MemoryMax=8G` hart.
Betriebspunkt: `optimize=False`, Modellkonstruktion direkt auf der GPU (`laden.py`).

| | `optimize=False` | `optimize=True` |
|---|---|---|
| TTFA p95 | **90.9 ms** | — |
| RTF median | 0.5199 | 0.16 |
| Kaltstart bis erstes Audio | **7.1 s** | 94.1 s (**reißt E**) |
| Marge zu dAImons `GPU_FRIST_S=120` | 30.8× | 1.5× |

**RAM ist der Engpass, nicht VRAM.** Ein Lauf mit den Torch-Defaults wurde vom Kernel
OOM-gekillt (`anon-rss 10.3 GB`). Ursache: `from_pretrained` baut das Modell auf der CPU
in fp32 (~8 GB), bevor es nach cuda/bf16 wandert.

| Ladepfad | RAM-Spitze | RAM Ruhe | Ladezeit |
|---|---|---|---|
| Torch-Defaults | 12479 MiB | 4357 MiB | 14.5 s |
| `torch.device("cuda")` | **5514 MiB** | **2269 MiB** | **4.3 s** |

`soar` + `mf` gleichzeitig: 11.2 GiB VRAM — passt. In RAM ~20 GB — passt **nicht**.


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
