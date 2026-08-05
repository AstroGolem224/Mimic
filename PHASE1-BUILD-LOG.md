# Phase 1 Build Log — Mimic

Verfahren: `/codex-build`. Claude schreibt die Spezifikation und prüft, Codex implementiert.
Spec: `PHASE1.md` (eingefroren). Modell: `gpt-5.6-sol`, effort `medium`, codex-cli 0.145.0.
MAX_FIX_ROUNDS=2.

## Runde 1 — Codex

1457 Zeilen in 15 neuen Dateien: `mimic/` (protocol, voices, frontend, worker, cli),
`systemd/` (vier Units), `tests/` (Tests 1–9 plus `run.sh`), `pyproject.toml`, `uv.lock`.
`spike/` unberührt, wie gefordert.

Codex hat zwei Abweichungen von selbst gemeldet:

- `local_files_only` ist kein Parameter von `DotsTtsRuntime.from_pretrained` in 0.2.1;
  stattdessen `HF_HUB_OFFLINE=1`, gepinnte Revisionen und `RestrictAddressFamilies=AF_UNIX`.
  Sachlich richtig, mein Spec war an der Stelle ungenau.
- Die 120-s-Wanduhrfrist greift zwischen den Yields von `generate_stream`. Ein einzelner
  nie zurückkehrender CUDA-Aufruf ist in Python nicht unterbrechbar. Korrekt beschrieben;
  die Frontend-Fristen fangen den Fall ab.

Dazu fünf unterspezifizierte Punkte benannt, die es selbst entschieden hat: Wire-Protokoll
zum Hub und zum Worker, Statusübergabe per Datei, der Schlüsselname `state=`, und der
Installationsort der Entry-Points. Alle Entscheidungen tragfähig.

### Claude's Verdikt

Diff vollständig gelesen, `bash tests/run.sh` selbst ausgeführt: **7/7 grün**, unabhängig
reproduziert. `spike/` nachweislich unberührt.

Gut gelöst und über die Spec hinaus: `voices.py` nagelt den Inode mit `O_NOFOLLOW`,
`dir_fd` und `/proc/self/fd` fest und hält ihn per `dup` am Leben — ein
Symlink-Tausch zwischen Prüfung und Nutzung greift damit nicht. Der Ladepfad trägt die
Messung als Kommentar, nicht nur den Aufruf.

**Drei Defekte gefunden, direkt behoben statt delegiert** (zusammen ~8 Zeilen; eine
Codex-Runde dafür hätte mehr gekostet als gespart):

1. **`insufficient_vram` tötete den Worker.** Das VRAM-Gate warf `FatalWorkerError`, was
   den Prozess beendet. Folge: läuft `mf` und wird nebenher `soar` angefragt, während
   ComfyUI den Speicher hält, stirbt der Worker und reißt das funktionierende `mf` mit.
   Knapper VRAM ist ein erwarteter, vorübergehender Zustand, kein beschädigter
   Modellzustand. → `WorkerRefusal`.
2. **`samples` doppelt gezählt.** Die gepufferten `A`-Rahmen gingen zweimal in die Summe,
   die im Fehler-`E` gemeldet wird. Kosmetisch, aber falsch.
3. **Das Paket war nicht installierbar.** `name = "mimic-tts"` gegen Verzeichnis `mimic/`,
   ohne `[tool.hatch.build.targets.wheel]`. `uv sync` bricht ab — und damit hätten die
   systemd-Units ihre Entry-Points nie gefunden. Codex' `uv lock --check` fängt das nicht,
   weil dabei kein Wheel gebaut wird. Das belegt, dass Codex den Dienst nie gestartet hat.

**Eine Integrationslücke außerhalb des Codes:** das reale Stimmprofil hatte 755/644, der
Dienst verlangt spec-gemäß 700/600. Profil korrigiert und `spike/02_aufnehmen.py` so
geändert, dass es künftig gleich richtig anlegt.

### End-to-End, selbst gefahren

Weder Codex noch die Testsuite haben je den echten Worker mit echtem Modell laufen lassen —
alle sieben Tests arbeiten gegen einen Stub. Nachgeholt, unter `MemoryMax=8G`:

```
status (kalt) -> voices -> say (kalt) -> say (warm)
Kaltstart  20.9 s inkl. Modellladen (load_s=8.9)
warm       1.5 s fuer 2.08 s Audio
WAV        1 ch, 48000 Hz, 16 bit  -- korrekter Header, atomar geschrieben
Status     kalt -> warm, Sockets beide Modus 0600
Journal    alle geforderten Felder vorhanden
```

### Befund, der über den Build hinausgeht

Die Journal-Zeile zeigt warm `ttfa_ms=419.0`. Kriterium C hatte 90.9 ms gemessen.
Ursache isoliert nachgemessen (n=15, gleicher Runtime, gleicher Text):

| | TTFA median |
|---|---|
| ohne `prompt_audio` — was Kriterium C maß | 87.3 ms |
| mit `prompt_audio` — was der Dienst tut | 184.5 ms |

**Kriterium C hat den ausgelieferten Pfad nicht gemessen.** Das Spike-Skript rief
`generate_stream` ohne Referenzaudio, also ohne Klonen. Das Konditionieren kostet ~97 ms.
185 ms liegen weiterhin unter dem 300-ms-Budget, C ist also nicht widerlegt — aber die
Zahl in `spike/ERGEBNIS.md` beschreibt eine Konfiguration, die so nicht läuft.

Die 419 ms aus dem Dienst sind darüber hinaus unerklärt und beruhen auf **einem** Sample.
Vor einer Aussage über C im Dienst braucht es eine ordentliche Messreihe am Socket.

## Offen

- Tests 8 und 9 (`tests/run.sh --gpu`) sind implementiert, aber weder von Codex noch von
  Claude ausgeführt. Sie brauchen installierte systemd-Units. **Bis dahin ist die
  Eindämmungszusage aus PHASE1.md §3 unbelegt.**
- Messreihe für TTFA am Socket, mit Klonen, n ≥ 50.
- Konditionierung je Stimme zwischenspeichern, falls die Messreihe es rechtfertigt. Diese
  Option war beim ersten Interview verworfen worden mit der Begründung „lohnt nur, wenn
  eine Messung zeigt, dass der Encoder relevant kostet". Die Messung liegt jetzt vor.
