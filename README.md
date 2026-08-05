# Mimic

Self-hosted Voice-Cloning-TTS. Engine: [dots.tts](https://github.com/rednote-hilab/dots.tts) (Apache-2.0).

Zwei Konsumenten: **dAImon** (Charakterstufe, Streaming) und **MMC** (Sprachaufnahmen zur Bauzeit).

**Stand: Phase 1 implementiert (2026-08-05).** Frontend und GPU-Worker werden getrennt
über Unix-Sockets aktiviert. `spike/` bleibt unveränderter Wegwerfcode, der die
gemessenen Betriebsentscheidungen dokumentiert.

## Dienst und CLI

```bash
uv tool install --python 3.12 .
install -d -m700 ~/.local/share/mimic/voices
install -Dm644 systemd/mimic.socket ~/.config/systemd/user/mimic.socket
install -Dm644 systemd/mimic.service ~/.config/systemd/user/mimic.service
install -Dm644 systemd/mimic-worker.socket ~/.config/systemd/user/mimic-worker.socket
install -Dm644 systemd/mimic-worker.service ~/.config/systemd/user/mimic-worker.service
systemctl --user daemon-reload
systemctl --user enable --now mimic.socket mimic-worker.socket

uv run mimic say "Hallo" --voice matthias --mode mf
uv run mimic say "Hallo" -o hallo.wav
uv run mimic status
uv run mimic voices
```

Stimmprofile liegen unter `~/.local/share/mimic/voices/<name>/` als `ref.wav`
(48 kHz mono, 3–60 Sekunden) und wörtliches UTF-8-Transkript `ref.txt`.
Der GPU-freie Nachweis läuft mit `bash tests/run.sh`; die destruktiven echten
Eindämmungsproben sind separat über `bash tests/run.sh --gpu` erreichbar.

Plan, Glossar und ADRs: `~/Dokumente/UMBRA-Notes/DDs/Mimic/`

## Phase-0-Abnahme

**Ergebnis: dots.tts bleibt.** Vollständiger Bericht: [spike/ERGEBNIS.md](spike/ERGEBNIS.md)

| # | Kriterium | Ergebnis | |
|---|---|---|---|
| A | Blackwell, kein CPU-Fallback | GPU 63 %, VRAM +6222 MiB | ✅ |
| B | Echt/Synthetisch ununterscheidbar (≤ 8/12) | 12/12 — Klon ist hörbar | ❌ |
| B2 | Brauchbar als Matthias' Stimme (≥ 5/6) | `[EN]` 6/6, `[DE]` 5/6, Kontrolle 5/6 | ✅ |
| C | TTFA p95 < 300 ms | 90.9 ms | ✅ |
| D | Akzent-Leakage ≤ 2/10 | 1 Treffer | ✅ |
| E | Kaltstart < 60 s | 7.1 s | ✅ |

B ist durchgefallen und wird nicht umgedeutet. Der Klon ist erkennbar — an der Aussprache
einzelner Wörter, nicht an Timbre oder Rhythmus. B2 ersetzt es mit der Frage, die zum
Zweck passt, ist aber ein weiches Kriterium: zwei Läufe derselben Bedingung ergaben 1/6
und 6/6. Details in [ERGEBNIS.md](spike/ERGEBNIS.md).

### Betriebspunkt

`mf` (Realtime) / `soar` (Batch) · `optimize=False` · Modellkonstruktion direkt auf der
GPU · Sprach-Tag `en` auch für Deutsch · 48 kHz nativ


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
uv run python 08_ram.py mf            # RSS-Bedarf
uv run python test_spike.py           # Selbstchecks
```

### Was noch Aufnahmen braucht (B und D)

```bash
uv run python 02_aufnehmen.py referenz    # ~10 s, daraus klont Mimic
uv run python 02_aufnehmen.py blindtest   # 6 Sätze
uv run python 02_aufnehmen.py akzent      # 10 englische Sätze
uv run python 02_aufnehmen.py status

uv run python 04_blindtest.py erzeugen    # rendert beide Sprach-Tags
uv run python 04_blindtest.py hoeren      # Kriterium B
uv run python 04_blindtest.py brauchbar   # Kriterium B2
uv run python 05_akzent.py erzeugen       # Kriterium D
uv run python 05_akzent.py hoeren
```

Aufnahmen landen unter `aufnahmen/` (nicht im Repo) und
`~/.local/share/mimic/voices/matthias/`.
