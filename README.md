# Mimic

Self-hosted Voice-Cloning-TTS. Engine: [dots.tts](https://github.com/rednote-hilab/dots.tts) (Apache-2.0).

Zwei Konsumenten: **dAImon** (Charakterstufe, Streaming) und **MMC** (Sprachaufnahmen zur Bauzeit).

**Stand: Phase 1 implementiert (2026-08-05).** Frontend und GPU-Worker werden getrennt
über Unix-Sockets aktiviert. `spike/` bleibt unveränderter Wegwerfcode, der die
gemessenen Betriebsentscheidungen dokumentiert.

## Dienst und CLI

```bash
uv tool install --python 3.12 .
mimic setup

uv run mimic gui
uv run mimic record matthias_krieger
uv run mimic say "Hallo" --voice matthias --mode mf
uv run mimic say "Hallo" -o hallo.wav
uv run mimic status
uv run mimic voices
```

`mimic setup` legt den Stimmenordner an, installiert die fünf Unit-Dateien aus
`systemd/` (`mimic-worker-reset.service` gehört dazu — `mimic-worker.socket`
verweist per `OnFailure` darauf), schaltet die Sockets scharf und prüft am Ende,
ob der Dienst antwortet. Zweimal aufrufen ist gefahrlos; geänderte Units werden
ersetzt und die Sockets dann neu gestartet. Der Befehl braucht das
Repo-Verzeichnis, weil die Unit-Dateien dort liegen.

Stimmprofile liegen unter `~/.local/share/mimic/voices/<name>/` als `ref.wav`
(48 kHz mono, 3–60 Sekunden) und wörtliches UTF-8-Transkript `ref.txt`.
`mimic record <name>` nimmt sie auf: Enter startet, Enter stoppt, danach
Kontrollwiedergabe und behalten/nochmal. Rechte (0700/0600) setzt der Befehl
selbst. Für die Charakterstimmen in `mimic/charaktere.py` liefert er Text und
Regieanweisung mit, jeder andere Name braucht `--text`.

`mimic import <name> <datei> --text "<Transkript>"` legt dasselbe Profil aus
einer fertigen Audiodatei an. ffmpeg macht daraus die 48-kHz-Mono-WAV, das
Eingangsformat ist also egal (mp3, opus, Stereo, 44.1 kHz). Die 3–60-s-Grenze
gilt hart, außerhalb von 8–15 s kommt ein Hinweis. `--force` ersetzt ein
bestehendes Profil.

**Charakterstimmen.** dots.tts klont Prosodie mit, nicht nur Timbre — die
Referenz muss also bereits so klingen wie das Ziel. Die Texte in
`charaktere.py` sind auf **10 s** ausgelegt und enthalten je Aussage, Frage und
Ausruf. Erst mit 20–30 s versucht: die Klone klangen schlechter und schnitten
Sätze ab. 10–15 s ist auch der einzige gemessene Bereich — die Phase-0-Referenz
`matthias` hat 14.8 s.

| Profil | Duktus |
|---|---|
| `matthias_krieger` | tief, langsam, jedes Wort steht für sich |
| `matthias_magier` | leise, beweglich, Tempo schwankt mit dem Denken |
| `matthias_dark_lord` | ruhig und gleichmäßig, Drohung im Inhalt statt in der Lautstärke |

## Fenster

`mimic gui` zeigt links die ladbaren Stimmen und rechts ein Skriptfeld. Eine
Zeile je Einsatz, `#stimme: "Text"` setzt den Sprecher, eine Zeile ohne Präfix
erbt ihn von der Zeile darüber, `//` ist ein Kommentar. Doppelklick auf eine
Stimme fügt den Kopf ein. **Abspielen** streamt alles durch ein einziges
`pw-cat`, **Exportieren** sammelt dieselben Blöcke in eine Datei. Format
daneben umschaltbar zwischen **wav** (roh, wie der Dienst liefert) und **mp3**
(192 kbps, über `lame`, sonst `ffmpeg`; ohne beides ist der Knopf aus,
Bitrate per `MIMIC_MP3_BITRATE`). Ordner und Dateiname fragt der
Speichern-Dialog **vor** dem Auftrag ab — `showSaveFilePicker` braucht eine
frische Nutzeraktion, und die wäre nach zwei Minuten Rechnen verfallen;
nebenbei ist erst-wohin-dann-laufen die bessere Reihenfolge. Der Vorschlag
lautet `mimic-<stimme>-<hhmm>.<endung>`, den Ordner der letzten Wahl merkt
sich der Dialog. Ohne die API landet die Datei wie bisher im
Download-Verzeichnis.
**Stopp** (oder `Esc`) killt `pw-cat` und schließt die
Verbindung — der Worker sieht den Abbruch und hört auf zu rechnen
(`outcome=cancelled` im Log). **Warmlauf** lädt `mf` vor, damit der erste Satz
nicht auf das Modell wartet. Der Schalter **Modus** wechselt zwischen `mf`
(Realtime, Vorgabe) und `soar` (Batch, besser für gespeicherte Dateien).
`Strg`+`Enter` im Skriptfeld spricht. Kopfzeile zeigt Zustand, Modus,
Warteschlange, freien VRAM und Laufzeit des Dienstes live; das Band unter dem
Skript zeichnet die Pegel der laufenden Ausgabe.

### Stimmenwerkstatt

**Klonen** öffnet die Werkstatt-Schublade. Reiter *Neu aufnehmen*: Profilname
(live gegen `VOICE_RE` und die vorhandenen Profile geprüft), eine der Vorlagen
aus `charaktere.py` oder eigener Text, die Regieanweisung als Teleprompter
darüber. Der Aufnahmeknopf startet `pw-record` mit 48 kHz mono s16 direkt ins
Profilverzeichnis — dieselbe Mechanik wie `mimic record`, nur ohne Terminal.
Die Zielzonenleiste zeigt live, wo du stehst: unter 3 s lehnt der Dienst ab,
8–15 s ist der einzige gemessene Bereich (siehe `charaktere.py`), ab 60 s ist
Schluss, und nach 90 s stoppt eine Notbremse eine vergessene Aufnahme. Danach
Take abhören, dann *Behalten* (schreibt `ref.wav`/`ref.txt` mit 0700/0600 und
lässt das Profil vom Dienst gegenprüfen), *Nochmal* oder *Verwerfen*. Ein
abgebrochener erster Versuch lässt kein leeres Verzeichnis zurück. Nach dem
Speichern sprichst du direkt einen Probesatz mit der frischen Stimme.

Reiter *Entwerfen*: eine Stimme aus einer Beschreibung erzeugen, statt sie
einzusprechen. Motor wählen, Beschreibung (englisch) plus Probesatz, drei
Kandidaten je Lauf. Der Probesatz wird wörtlich das `ref.txt` des Profils,
steht also unter denselben Anforderungen wie ein eingesprochener Referenztext:
Aussage, Frage, Ausruf, rund zehn Sekunden. Jeder Kandidat lässt sich anhören
und unter einem Namen behalten — von da an ist es eine Stimme wie jede andere.

Zwei Motoren, beide Apache-2.0, beide sprechen Deutsch **nativ**:

| | Modell | Rate | Urteil vom 2026-08-12 |
|---|---|---|---|
| `voxcpm` | [VoxCPM2][voxcpm], 2B | 48 kHz | die schöneren Stimmen; Aussprache streut (gelegentlich ein englisch gelesenes Wort). Vorgabe |
| `qwen` | [Qwen3-TTS VoiceDesign][qwen], 1.7B | 24 kHz | fehlerfreies Deutsch, dafür halbe Bandbreite als `ref.wav` |

48 kHz ist die Rate, die dots.tts ohnehin liefert — ein VoxCPM-Entwurf geht
also ohne Wandlung als Referenz durch.

**Was hier vorher stand und warum es weg ist.** Bis zum 2026-08-12 entwarf
MOSS-VoiceGenerator, und ein zweites Modell deutschte den englischen Entwurf
ein. Das Ergebnis klang wie ein Nicht-Deutschsprecher, der deutschen Text
phonetisch vorliest. Die Ursache ist strukturell: MOSS-VoiceGenerator hat laut
eigenem Paper nur Chinesisch und Englisch gesehen, die entworfene Stimme *ist*
englisch konzipiert, und jeder Zero-Shot-Klon erbt Akzent und Tempo seiner
Referenz. Drei Stufen stapelten drei Lecks. Ein Modell, das von vornherein
Deutsch kann, spart beide Zwischenstufen — `eindeutschen.py` ist mit ihnen
gegangen.

Kein Motor läuft im Worker: beide bringen eigene torch- und
transformers-Pins mit, dots.tts sitzt auf 4.57.6. Also je eine eigene Umgebung
und ein Subprozess, der nur während des Entwurfs lebt (`mimic/entwurf.py`
steuert, `mimic/entwerfen_<motor>.py` läuft drüben und importiert darum nichts
aus dem Paket). Das Modell belegt sein VRAM nur dann, wenn wirklich entworfen
wird. Gemessen: drei Kandidaten in rund 45 s bei warmem Dateicache, Entwürfe
9.4 bis 13.8 s lang. Sprechauftrag und Entwurf schließen sich gegenseitig aus
— sie teilen sich die Karte.

Der Subprozess bekommt **eine** Pipe: `stderr` läuft in `stdout` mit. Getrennt
blockiert er nach 64 KB im `write()` und steht endgültig — transformers und
tqdm schütten ihre Fortschrittsbalken nach stderr, und ein Lesefaden, der nur
stdout bedient, leert diese Pipe nie. Das Fehlerbild ist tückisch, weil nichts
abstürzt: der Entwurf bleibt einfach für immer bei »Modell lädt«.

Die Umgebung kommt einmalig und ausdrücklich, weil sie mehrere GB lädt und
Minuten braucht, während `mimic setup` sonst in Sekunden durchläuft:

```bash
uv run mimic setup --entwurf
```

[moss]: https://huggingface.co/OpenMOSS-Team/MOSS-VoiceGenerator

Reiter *Verwalten*: alle Profile mit Dauer und Referenztext, defekte mit
Grund. Referenz anhören, Probesatz sprechen, löschen — Löschen ist
zweistufig, der erste Klick schärft nur. Ein bestehendes Profil neu
aufzunehmen ist erlaubt, die Oberfläche warnt vorher, dass die alte Referenz
dabei verloren geht. Aufnahme und Sprechauftrag schließen sich gegenseitig aus.

Die Oberfläche ist HTML (`mimic/gui.html`) und läuft in einem
Chromium-App-Fenster gegen einen Loopback-Server aus `http.server`, der nur
solange lebt wie das Fenster und jede Anfrage gegen ein Zufallstoken prüft.
Grund: echtes Glas, weiche Schatten und Rundungen gibt Tk nicht her, und ein
Toolkit mit eigenem Renderer wäre eine dreistellige Megabyte-Abhängigkeit für
ein Fenster mit vier Knöpfen. Ohne Chromium/Brave/Chrome öffnet `mimic gui`
den Standardbrowser. Weiterhin keine zusätzliche Python-Abhängigkeit.

Starter für die Arbeitsfläche:

```bash
install -Dm755 systemd/mimic.desktop ~/.local/share/applications/mimic.desktop
ln -sfn ~/.local/share/applications/mimic.desktop "$(xdg-user-dir DESKTOP)/mimic.desktop"
```

## Aussprache

`~/.local/share/mimic/pronunciation.json` ersetzt Wörter **vor** der Synthese;
[pronunciation.example.json](pronunciation.example.json) trägt Muster und
Regeln. Zwei Dinge, die man einmal falsch macht:

**Verglichen wird case-insensitiv.** Die Großschreibung am Wortanfang wird in
die Ersetzung übernommen, aber nicht unterschieden — ein Eintrag `Weg` trifft
auch `weg`.

**Bei Homographen ist der Kontext die einzige Handhabe.** *weg* (fort) und
*Weg* (Straße) sind als Wort nicht zu trennen; ein Eintrag `weg` → `weck`
hätte aus jeder Straße ein Gebäck gemacht. Mehrere Wörter sind erlaubt, also
trennt `weg da` → `weck da`, was das Wort allein nicht kann. Gemessen am
2026-08-12: dots.tts sprach »Weg da!« als /veːk/, die Betonung rutschte auf
*da*. Mit dem Eintrag sitzt sie richtig, und »Der Weg ist lang« bleibt
unberührt — beides in einer Äußerung gegengehört.

Abschalten geht je Anfrage über `"aussprache": false` — die Vorgabe ist an.
Sinnvoll für Konsumenten, die ihren Text unverändert gesprochen haben wollen:
eine Ersetzung ist eine Textänderung, und Kriterium B hat den Klon 12/12-mal
an der Aussprache einzelner Wörter erkannt.

## Ansage

Dritter Konsument, klein: `tools/ansage.py` hängt als Stop-Hook in Claude Code
und lässt Mimic sagen, dass eine Aufgabe fertig ist — »Fertig.« plus die ersten
Sätze der letzten Antwort, gekürzt auf einen sprechbaren Satz.
`tools/kopfhoerer.sh` holt vorher den Bluetooth-Kopfhörer zurück und setzt ihn
als Standardsenke. Beides scheitert lautlos, wenn der Dienst aus ist oder keine
MAC hinterlegt wurde: ein Hook darf die Sitzung nicht aufhalten und erst recht
nicht kaputtmachen. Einrichtung und Hörprobe: [tools/ANSAGE.md](tools/ANSAGE.md).

```bash
tools/einrichten.sh     # installiert, hinterlegt die MAC, hängt den Hook ein, probt
```

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
| C | TTFA p95 < 300 ms | 250.0 ms am Socket, mit Klonen (n=60) | ✅ |
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
