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

### Fremde Stimmen eindeutschen

```bash
uv run mimic setup --entwurf moss          # einmalig, lädt einige GB
uv run mimic eindeutschen ether ~/Downloads/voice_preview_ether.mp3 --force
```

Eine anderswo erzeugte oder gekaufte Stimme spricht Deutsch oft mit fremdem
Einschlag — gerolltes R, englische Vokale. **`mimic import` kann das nicht
heilen**, und dots.tts auch nicht: die Engine hat keine Sprachmarke, sie klont,
was sie hört, Akzent inklusive. Ein Filter hilft nicht, weil ein Akzent kein
Frequenzband ist, sondern eine Aussprache.

`mimic eindeutschen` schiebt einen Schritt davor.
`MOSS-TTS-Local-Transformer-v1.5` (4B, 31 Sprachen) hört die Aufnahme, behält
die Stimmfarbe und spricht den Text mit `language="German"` neu. Erst dessen
Ausgabe wird `ref.wav`. Das Profil entsteht im selben Aufruf, `--force`
ersetzt ein bestehendes.

Am 2026-08-12 und 2026-08-14 an vier Stimmen gegangen (`ether`, `geth`,
`forge` zweimal): der Akzent verschwindet hörbar, die Stimme bleibt erkennbar.
Bei `ether` stand die Alternative daneben — den englischen Mittelteil der
Vorlage wegzuschneiden —, und dieser Weg gewann im Hörvergleich.

Die Dauer der Ausgabe steuert das Modell **nicht**; sie hängt am Sprechtempo
der Vorlage. Derselbe Text kam als 8.0 s und als 23.0 s zurück, und aus 23 s
machte dots.tts Gebrumm. Deshalb wirft der Befehl bis zu dreimal auf die
8–15-Sekunden-Marke (`--wuerfe`) und sagt es ausdrücklich, wenn kein Wurf
hineinfiel, statt das stillschweigend zu importieren.

`--text` bestimmt, was gesprochen wird und damit wörtlich das `ref.txt` —
ohne Angabe der Standardprobesatz aus `entwurf.py`.

#### Eindeutschen Version 2

Falls MOSS trotz Sprachmarke noch den englischen Akzent der Vorlage mitnimmt,
bleibt die erste Fassung unverändert erhalten und Version 2 kann parallel ein
neues Profil erzeugen:

```bash
uv run mimic setup --entwurf qwen-klon
uv run mimic eindeutschen2 nordom_v2 ~/Documents/archived_voices/Nordom.mp3
```

Version 2 nutzt Qwen3-TTS Base im `x-vector-only`-Modus. Aus der englischen
Aufnahme wird nur die Sprecheridentität konditioniert; englische Audio-Codes,
Phonetik und Prosodie gelangen nicht in den deutschen Prompt. Der Zieltext
wird nativ mit der Sprachmarke Deutsch erzeugt. Ohne `--force` ersetzt auch
dieser Befehl kein vorhandenes Profil.

`eindeutschen2` legt zwei getrennte Referenzen an:

- `ref.wav` ist die deutsche Referenz für MF und SOAR.
- `qwen-source.wav` bewahrt die Eingangsstimme für den direkten Qwen-Modus.

Damit kann dieselbe Stimme in der Oberfläche über `MF · SOAR · QWEN` mit allen
drei Motoren gesprochen werden. Qwen läuft wegen nicht vereinbarer Python-
Abhängigkeiten in der eigenen Umgebung `entwurf-venv-qwen-klon`; der Worker
hält diesen Prozess nach dem ersten Laden warm.

```bash
mimic say "Die Analyse ist abgeschlossen." --voice nordom_v2 --mode qwen
```

Ein vorhandenes deutsches Profil kann außerdem als Quelle für eine weitere,
separat benannte Variante dienen. Das ist ein bewusster A/B-Versuch und kein
stilles Ersetzen des Ausgangsprofils:

```bash
mimic eindeutschen2 nordom_v3 ~/.local/share/mimic/voices/nordom_v2/ref.wav
```

Bei der Nordom-Prüfung vom 18. August 2026 wurden beide Wege mit einem
38-Wörter-Text voller deutscher `ch`-, `r`-, `ö`- und `ü`-Laute getestet.
Faster-Whisper Large-v3 erkannte in beiden Ausgaben alle Wörter; die mittlere
Wortkonfidenz betrug 99,58 % für die Originalreferenz und 98,98 % für die
deutsche Referenz. Parakeet TDT 0.6B bestätigte den Text der Originalreferenz
vollständig und kürzte bei der deutschen Variante einmal „deutsch“. ASR prüft
damit die Verständlichkeit, ersetzt aber keinen Hörvergleich des Akzents.

Was **nicht** in die Referenz gehört: Effektketten. Eine mit Bitreduktion und
Flanger gefärbte `ref.wav` ließ dots.tts dreimal in Stille laufen. Effekte
gehören an den Ausgang, nicht an die Vorlage.

MOSS ist absichtlich **kein** Eintrag in `MOTOREN`: die beiden Motoren dort
entwerfen Stimmen aus einer Beschreibung, MOSS klont eine vorhandene. Den
venv-Mechanismus teilen sie sich trotzdem, deshalb `setup --entwurf moss`.
Eine eigene Umgebung braucht es zwingend — `transformers==5.0.0` verträgt sich
weder mit `qwen-tts` (höchstens 4.57.6) noch mit voxcpm.

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

`mimic gui` zeigt links die ladbaren Stimmen und rechts ein Skriptfeld. Beim
Öffnen steht dort der dreiteilige Trainingsabsatz. Text markieren und links
eine Stimme anklicken weist genau dieser Auswahl den Sprecher zu; ein erneuter
Klick ersetzt die Zuweisung. Ohne Auswahl setzt der Klick die Standardstimme.
Im Skript steht der Wechsel als `[stimme]`, Text und Kopf tragen dieselbe,
stabile Farbe (Nordom gelb). Die Köpfe steuern nur den Parser und werden nicht
gesprochen. Das bisherige `#stimme: "Text"` bleibt lesbar, eine Zeile ohne
Präfix erbt den Sprecher von oben, `//` ist ein Kommentar.

Regieaktionen können ebenfalls in eckigen Klammern stehen, etwa
`[sighs]`, `[laughs]` oder `[whispers]`. Anders als ein Name aus der
Stimmenliste bleiben sie im Modelltext, damit ein Motor mit entsprechender
Paralinguistik sie ausführen kann. Das ist eine Motorfähigkeit: die derzeitigen
Clone-Basismodelle garantieren solche Aktionen nicht für jeden Lauf; Mimic
spricht sie nicht fälschlich als Stimmnamen an.

**Abspielen** streamt alles durch ein einziges
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
Ein fertiger Export bleibt bei einem Schreibfehler abrufbar und wird erst nach
erfolgreicher Übernahme bestätigt. Den letzten Skriptentwurf samt Stimme,
Motor, Format und Klangreglern speichert Mimic geschützt unter
`$XDG_STATE_HOME/mimic/gui-draft.json` und stellt ihn beim nächsten Öffnen
wieder her.
**Stopp** (oder `Esc`) killt `pw-cat` und schließt die
Verbindung — der Worker sieht den Abbruch und hört auf zu rechnen
(`outcome=cancelled` im Log); bei Qwen wird auch der isolierte Modellprozess
beendet und sauber neu gestartet. **Warmlauf** lädt den gewählten Motor mit der
gewählten Stimme vor. Der Schalter **Modus** wechselt zwischen `mf` (Realtime,
Vorgabe), `soar` (Batch, besser für gespeicherte Dateien) und `qwen`; Qwen ist
sichtbar deaktiviert, solange `mimic setup --entwurf qwen-klon` fehlt.
`Strg`+`Enter` im Skriptfeld spricht. Kopfzeile zeigt Zustand, Modus,
Warteschlange, freien VRAM und Laufzeit des Dienstes live; das Band unter dem
Skript zeichnet die Pegel der laufenden Ausgabe.

### Stimmenwerkstatt

**Klonen** öffnet die Werkstatt-Schublade. Reiter *Aufnehmen / Datei*: Profilname
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

Im selben Reiter kann statt des Mikrofons eine vorhandene **MP3- oder
WAV-Datei** gewählt werden. Mimic nimmt bis zu 64 MiB an, wandelt sie lokal mit
`ffmpeg` in dieselbe 48-kHz-Mono-Referenz wie eine direkte Aufnahme und zeigt
sie vor dem Speichern zum Anhören. Die üblichen 3–60-s-Grenzen, die
Zielbereichswarnung und die atomare Profilprüfung gelten unverändert. Der
angegebene Referenztext kann von Hand eingetragen oder nach dem Upload lokal
mit faster-whisper erkannt werden; das Ergebnis landet zur Kontrolle im
Textfeld und wird erst mit *Behalten* als `ref.txt` gespeichert.

Die getrennte Transkriptionsumgebung wird einmalig eingerichtet (das Modell
`small` kommt beim ersten Transkribieren und kann mit
`MIMIC_WHISPER_MODEL` geändert werden):

```bash
mimic setup --transkription
```

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

Es ist seit dem 2026-08-14 für einen **anderen** Auftrag zurück, siehe
[Fremde Stimmen eindeutschen](#fremde-stimmen-eindeutschen). Der Unterschied
ist der, an dem die alte Kette scheiterte: dort wurde eine englisch entworfene
Stimme nachträglich gedeutscht, hier bekommt eine **fertige, fremde** Aufnahme
ihren Akzent genommen. Beim Entwerfen gibt es diesen Umweg nicht mehr — die
Motoren oben können Deutsch von sich aus.

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

## Stimmeinstellungen

Ein Stimmprofil ist ein Verzeichnis unter `~/.local/share/mimic/voices/<name>/`
mit `ref.wav`, `ref.txt` und optional `settings.json`. Was dort steht, gilt
dauerhaft für diese Stimme: Sprache und `speaker_scale`, eine Klangfarbe aus
`effekte.EFFEKTE` und die vier Klangregler `tonhoehe`, `raster`, `streuung`
und `formant`. [settings.example.json](settings.example.json) erklärt jeden
Schlüssel samt Grenzen.

Die Regler in der Transportleiste und die Schalter von `mimic say` kommen auf
den Profilwert **drauf**, sie ersetzen ihn nicht — Profil −3 Halbtöne plus
Regler +1 ergibt −2. Eine fest eingestellte Stimme bleibt so nachstellbar.

Der GLaDOS-Klang steckt nicht in einem Effekt, sondern im Raster. Laut Valves
eigener [Anleitung](https://developer.valvesoftware.com/wiki/Creating_a_Portal_AI_Voice)
wurde Ellen McLains Aufnahme *pitch constrained, pitch modulation suppressed,
and the formant moved up* — Tonhöhenkorrektur, kein Vocoder:

```json
{"tonhoehe": 2.0, "raster": 1.0, "streuung": 1.0, "formant": 3.0}
```

`raster` zwingt jede Silbe auf den nächsten Halbton und hält sie dort, bis die
Stimme weit genug wegspringt; `streuung` setzt einzelne Silben zufällig daneben
— die Handarbeit der Vorlage; `formant` hebt die Klangfarbe, ohne die Tonhöhe
mitzunehmen. Zum Ausprobieren ohne Profil: `mimic say "…" --glados`.

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
