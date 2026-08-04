# Mimic

Self-hosted Voice-Cloning-TTS. Engine: [dots.tts](https://github.com/rednote-hilab/dots.tts) (Apache-2.0).

Zwei Konsumenten: **dAImon** (Charakterstufe, Streaming) und **MMC** (Sprachaufnahmen zur Bauzeit).

**Stand: Phase 0 (Spike).** Der Dienst existiert noch nicht. `spike/` ist Wegwerfcode, der
genau eine Frage beantwortet: taugt dots.tts auf einer RTX 5090 für diesen Zweck?

Plan, Glossar und ADRs: `~/Dokumente/UMBRA-Notes/DDs/Mimic/`

## Phase-0-Abnahme

| # | Kriterium | Stand |
|---|---|---|
| A | Blackwell — E2E-CUDA-Lauf ohne CPU-Fallback | ✅ bestanden |
| B | Klang-Treue — verblindete Echt/Synthetisch-Unterscheidung, ≤ 8/12 richtig | offen |
| C | Latenz — TTFA p95 < 300 ms (`mf`, warm, n ≥ 50) | offen |
| D | Akzent-Leakage — ≤ 2 Treffer gegen eigene englische Baseline | offen |
| E | Kaltstart-Bereitschaft — binnen 60 s bedienbar | offen |

## Spike ausführen

```bash
cd spike
uv sync --python 3.12
uv run python fetch_checkpoints.py    # ~10 GB, feste Revisionen
uv run python 00_blackwell.py         # Kriterium A
```
