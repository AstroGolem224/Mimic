# Mimic Phase 1 — Spezifikation für die Implementierung

Eingefroren 2026-08-05. Diese Datei ist die Arbeitsanweisung; sie wird während der
Umsetzung nicht verhandelt. Grundlage: `~/Dokumente/UMBRA-Notes/DDs/Mimic/PLAN.md`
(P1-1 … P1-13) und `spike/ERGEBNIS.md` (Phase-0-Messwerte). Begriffe nach
`~/Dokumente/UMBRA-Notes/DDs/Mimic/CONTEXT.md`.

Alles unter `spike/` ist **Wegwerfcode** und wird nicht übernommen. Einzige Ausnahme: die
Erkenntnisse, die hier als Vorgaben stehen.

---

## 1. Was gebaut wird

Ein Dienst, der Text in Matthias' geklonter Stimme ausspricht, über einen Unix-Socket,
mit Streaming. Zwei Prozesse:

```
  Konsument                Frontend (CPU)              Worker (GPU)
  dAImon / CLI  ──UDS──▶  mimic.socket        ──UDS──▶  mimic-worker.socket
                          kein torch, kein VRAM        dots.tts, beendet sich
                          Limits, Validierung          bei Leerlauf
```

**Warum zwei Prozesse:** VRAM und RAM verschwinden zuverlässig erst mit dem Prozessende.
Ein Fehlschlag beim Modellladen — OOM, CUDA-Fehler — tötet den Worker, nicht den Dienst.
Das Frontend bleibt erreichbar und antwortet mit einer Absage, die der Konsument
behandeln kann. Getrennte systemd-Units geben getrennte cgroups, sodass ein
Speicherlimit den Worker trifft und nicht das Frontend.

Das Frontend importiert **niemals** `torch`. Ein Test hält das fest.

---

## 2. Protokoll

### Transport

`AF_UNIX`, `SOCK_STREAM`, Socket-Datei Modus **0600**. **Kein TCP.** Grund: dAImons
`docs/DESIGN.md` §7.2 setzt `RestrictAddressFamilies=AF_UNIX` für `face` und alle
GPU-Worker; ein `127.0.0.1`-Aufruf wäre dort kernelseitig unmöglich. Die Dateirechte
sind die Zugangskontrolle — es gibt keine zusätzliche Authentifizierung und es soll
keine geben, solange das gilt.

HTTP/1.1 über den Unix-Socket, umgesetzt mit `http.server` auf einem
`socketserver.ThreadingUnixStreamServer`. Ein Thread je Verbindung.

### Endpunkte

| Methode | Pfad | Zweck |
|---|---|---|
| `POST` | `/speak` | Synthese, Antwort ist ein Audio-Stream |
| `GET` | `/status` | Zustand, für Diagnose und Bereitschaftsprüfung |

**`POST /speak`**, Body JSON:

```json
{ "text": "…", "voice": "matthias", "mode": "mf" }
```

`mode` ist `"mf"` (Vorgabe, Realtime) oder `"soar"` (Qualität, Batch).
`voice` ist optional, Vorgabe `"matthias"`.

### Antwortrahmen

Die Antwort ist **nicht** rohes PCM. Grund: bei rohem PCM ist ein Inferenzfehler nach
dem ersten Byte von einer sauber kurzen Äußerung nicht unterscheidbar — der Konsument
kann dann nicht entscheiden, ob er auf die Vorgabestufe zurückfallen darf, ohne bereits
Gehörtes zu wiederholen.

HTTP 200 mit `Transfer-Encoding: chunked`, `Content-Type: application/vnd.mimic.frames`.
Der Body ist eine Folge von Rahmen:

```
  1 Byte   Typ      'H' Kopf | 'A' Audio | 'E' Ende
  4 Byte   Länge    big-endian, unsigned
  n Byte   Nutzlast
```

- **`H`** — genau einer, immer der erste. Nutzlast ist UTF-8-JSON:
  `{"v":1,"sample_rate":48000,"channels":1,"format":"s16le","request_id":"…","mode":"mf","voice":"matthias"}`
  Die **Sample-Rate kommt aus dem Runtime** (`rt.sample_rate`), sie wird nicht
  hartkodiert und nicht umgerechnet. dots.tts liefert nativ 48 000 Hz; stilles Resampling
  würde Qualität und Latenz verändern.
- **`A`** — beliebig viele. Nutzlast ist PCM `s16le`, mono, in der Rate aus dem Kopf.
- **`E`** — genau einer, immer der letzte. Nutzlast ist UTF-8-JSON:
  `{"status":"ok","samples":123456}` oder
  `{"status":"error","reason":"<code>","message":"…","samples":123456}`

**Der erste Audio-Rahmen wird geholt, bevor HTTP 200 zugesagt wird.** Schlägt die
Inferenz davor fehl, ist die Antwort ein HTTP-Fehler und kein Stream. Schlägt sie danach
fehl, gilt „stoppen, nicht wiederholen": `E` mit `status:"error"`, und der Konsument
spielt nichts nach.

### Fehler vor dem Stream

HTTP-Status plus JSON-Body `{"reason":"<code>","message":"…"}`. Die `reason`-Codes sind
Teil der Schnittstelle und stabil:

| Status | `reason` | Bedeutung |
|---|---|---|
| 400 | `bad_request` | JSON kaputt, Feld fehlt, `mode` unbekannt |
| 400 | `text_too_long` | über `max_text_chars` |
| 404 | `unknown_voice` | Profil existiert nicht |
| 422 | `invalid_voice_profile` | Profil existiert, ist aber unbrauchbar (siehe §4) |
| 429 | `busy` | Warteschlange voll |
| 503 | `insufficient_vram` | VRAM-Gate hat abgelehnt |
| 503 | `worker_unavailable` | Worker startet nicht oder ist gestorben |
| 504 | `worker_timeout` | Worker antwortet nicht innerhalb der Frist |

Für den Konsumenten sind **alle** davon gleichwertig: Mimic kann nicht, also Vorgabestufe.
Die Unterscheidung dient der Diagnose, nicht der Fallentscheidung.

### `GET /status`

```json
{ "state": "kalt|warm|laedt", "mode": "mf|soar|null", "queue": 0,
  "worker_pid": 1234, "last_load_s": 3.9, "vram_free_mib": 24000, "uptime_s": 812 }
```

Frontend beantwortet das **ohne** den Worker zu wecken. `state:"kalt"` heißt: kein
Worker läuft.

---

## 3. Worker und Speicher

### Lebenszyklus

Der Worker wird **socket-aktiviert** über `mimic-worker.socket` gestartet, nicht vom
Frontend gespawnt. Grund: so liegen Frontend und Worker in getrennten systemd-Units und
damit getrennten cgroups, und systemd übernimmt den Lebenszyklus.

Der Worker setzt eine Leerlauffrist als Timeout auf dem horchenden Socket. Läuft sie ohne
neue Verbindung ab, kehrt `main()` mit `return 0` zurück, der Interpreter endet, der
Kernel räumt VRAM und RAM. **Kein Timer-Thread, kein Signal, kein Aufräumpfad, der
vergessen werden kann.** Wer den Leerlauf hier in einen Zustand statt in ein `return`
umbaut, entfernt die Zusage.

Vorgabe für die Frist: **300 s**, konfigurierbar.

### Modell laden — verbindlich

```python
with torch.device("cuda"):
    rt = DotsTtsRuntime.from_pretrained(repo, revision=rev,
                                        precision="bfloat16", optimize=False)
```

Beide Teile sind gemessen, nicht Geschmack:

- **`torch.device("cuda")`** — ohne diesen Kontext baut `from_pretrained` das Modell auf
  der CPU in fp32 (~8 GB), bevor es nach cuda/bf16 wandert. Gemessen: RAM-Spitze 12479 MiB
  statt 5514 MiB, Ladezeit 14.5 s statt 4.3 s. Auf dieser 30-GiB-Maschine hat der
  Vorgabepfad einen Kernel-OOM ausgelöst.
- **`optimize=False`** — mit `True` steigt der Kaltstart auf 94.1 s und reißt das
  Bereitschaftsbudget. Warm ist `True` schneller (TTFA 40 ms gegen 91 ms), aber beide
  liegen weit unter dem 300-ms-Budget, und der Kaltstart entscheidet.

Checkpoints werden über feste HF-Revisionen geladen (`revisions.yaml` aus `spike/`
übernehmen), zur Laufzeit mit `HF_HUB_OFFLINE=1` und `local_files_only`. **Kein
Netzzugriff im Betrieb.** Fehlt ein Checkpoint, ist das ein Installationsfehler und wird
als solcher gemeldet, nicht nachgeladen.

### VRAM-Gate

Vor dem Laden prüfen, ob genug freier Grafikspeicher da ist. Schwelle **8000 MiB**
(gemessener Bedarf 6222 MiB plus Reserve), konfigurierbar. Reicht es nicht: `503
insufficient_vram`, **kein Ladeversuch**.

Das Gate ist ausdrücklich **keine Garantie**. Zwischen Prüfung und Ladeende kann ComfyUI,
dAImons STT/VLM oder Blender allozieren. Deshalb ist der Entwurf auf Überleben ausgelegt:
stirbt der Worker beim Laden, meldet das Frontend `503` und bleibt selbst am Leben.

Wenn dAImons Hub unter `$XDG_RUNTIME_DIR/daimon/gpu.sock` erreichbar ist, fragt der
Worker dort um Erlaubnis, bevor er lädt, und meldet `fertig` zurück. Ist der Hub nicht
erreichbar, lädt er ohne. **Mimic darf nicht von dAImon abhängen** — MMC-Batch läuft auch,
wenn dAImon aus ist. Der Aufruf ist in eine eigene Funktion zu kapseln, die bei jedem
Fehler still auf „ohne Sperre" zurückfällt.

### Ein Eigentümer, Realtime zuerst

Genau ein Modell-Eigentümer im Worker. Übergänge zwischen `mf` und `soar` sind
serialisiert. Phase 0 hat gemessen, dass beide Checkpoints zusammen ins VRAM passen
(11.2 GiB) — ein Wechsel braucht also **keinen** Prozessneustart, und beide dürfen
gleichzeitig geladen bleiben.

Warteschlange: höchstens **4** wartende Anfragen, darüber `429 busy`. `mf` hat Vorrang
vor `soar`: eine laufende `soar`-Anfrage läuft zu Ende, aber **neue** `soar`-Arbeit
überholt niemals wartende `mf`-Arbeit.

### Speicherlimits

Aus Phase 0: Ruhezustand 2269 MiB, Spitze 5514 MiB.

| Unit | `MemoryHigh` | `MemoryMax` |
|---|---|---|
| `mimic.service` (Frontend) | 256M | 512M |
| `mimic-worker.service` | 3G | 7G |

---

## 4. Stimmprofile

Verzeichnis: `~/.local/share/mimic/voices/<name>/`, Modus 0700, Dateien 0600.

| Datei | Inhalt |
|---|---|
| `ref.wav` | Referenzaufnahme, 48 kHz mono |
| `ref.txt` | wörtliches Transkript, UTF-8 |

**Validierung vor jedem Laden** — ein Profilname aus einer Anfrage ist Fremdeingabe:

- Name gegen `^[a-z0-9][a-z0-9_-]{0,31}$`. Kein `.`, kein `/`, kein `..`.
- Auflösung unterhalb eines bereits geöffneten Verzeichnis-Deskriptors
  (`os.open(voices_dir, O_DIRECTORY)` + `dir_fd=`), damit ein zwischenzeitlich
  eingehängter Symlink nicht greift.
- Symlinks für `ref.wav` und `ref.txt` werden abgelehnt.
- `ref.wav`: höchstens 10 MB, lesbar, 1 Kanal, Dauer 3–60 s. Sonst
  `422 invalid_voice_profile` mit sprechender `message`.
- `ref.txt`: höchstens 4 KB, dekodiert als UTF-8, nicht leer.

Kein Registrierungs-Endpunkt. Eine neue Stimme ist ein neues Verzeichnis.

### Sprach-Tag und Aussprache

Jede Synthese fährt **`language="en"`**, auch für deutschen Text. Phase 0 hat das
verblindet gegen `de` geprüft: `en` 6/6 brauchbar, `de` 5/6, und die einzige
`de`-Ablehnung war der Satz mit englischen Fachbegriffen. Mit `de` bekommen Anglizismen
deutsche Phonetik.

Vor der Synthese läuft eine **Aussprache-Tabelle** über den Text: eine optionale Datei
`~/.local/share/mimic/pronunciation.json` mit `{"gemerged": "gemördscht", …}`, angewandt
als wortweise Ersetzung unter Beachtung der Wortgrenzen und der Groß-/Kleinschreibung am
Wortanfang. Fehlt die Datei, passiert nichts. Grund: Phase 0 hat als verbliebenes
Fehlerbild die Aussprache einzelner Anglizismen mit deutscher Beugung identifiziert
(„ge-mer-get" statt „gemördscht"). Die Tabelle ist der billige Hebel dagegen.

---

## 5. Grenzen und Fristen

| Größe | Wert | Wo |
|---|---|---|
| `max_text_chars` | 1000 | Frontend, vor dem Worker-Kontakt |
| `max_body_bytes` | 64 KiB | Frontend |
| Warteschlange | 4 | Worker |
| Wanduhr je Anfrage | 120 s | Worker, danach `E` mit `status:"error"` |
| Verbindung zum Worker | 2 s | Frontend |
| Antwortkopf vom Worker | 5 s | Frontend |
| erster Audio-Rahmen | 90 s (deckt Kaltstart) | Frontend |
| Abstand zwischen Rahmen | 10 s | Frontend |

Jede gerissene Frist beendet die Antwort und wird als Fehler mit passendem `reason`
gemeldet. Der Konsument entscheidet daraufhin für die Vorgabestufe — das ist **seine**
Aufgabe, nicht Mimics: **Invariante α liegt beim Client.** Mimic muss nur zuverlässig und
schnell *absagen* können.

### Abbruch

Bricht der Konsument die Verbindung ab, muss die Generierung **aufhören**, nicht
weiterrechnen. Umsetzung: der Schreibfehler auf dem Socket setzt ein Cancel-Flag für die
Anfrage, die Generator-Schleife prüft es je Rahmen, der Generator wird im `finally`
geschlossen, ausstehende Rahmen werden verworfen. Ein Test belegt, dass nach einem
Client-Abbruch keine weitere Inferenz läuft.

Für die Wiedergabe gilt clientseitig: geordnetes Schließen eines `pw-cat` spielt dessen
Puffer noch aus und ist hörbar. Der CLI-Client killt daher hart. Das ist im CLI
umzusetzen; für dAImon liegt es in dAImons Koordinator und **nicht** in diesem Auftrag.

---

## 6. CLI

Ein dünner Client auf denselben Socket. **Keine zweite Ladelogik, kein zweiter
Modellpfad** — die CLI importiert `torch` nicht.

```
mimic say "Text" [--voice matthias] [--mode mf|soar] [-o datei.wav]
mimic status
mimic voices
```

Ohne `-o` geht das Audio direkt in `pw-cat` (unbuffered durchreichen, kein Zwischenpuffern
der ganzen Äußerung). Mit `-o` schreibt die CLI eine **echte WAV-Datei** — rohes PCM ist
keine WAV-Datei, der Header wird clientseitig erzeugt. Geschrieben wird nach
`<datei>.tmp` und erst nach vollständigem, fehlerfreiem `E`-Rahmen atomar umbenannt. Eine
abgebrochene Synthese hinterlässt keine halbe Datei.

Exit-Codes: `0` Erfolg, `1` Fehler vom Dienst (`reason` auf stderr), `2` Fehlbedienung.

---

## 7. Beobachtbarkeit

`GET /status` wie in §2. Zusätzlich je Anfrage **eine** strukturierte Zeile ins Journal,
Format `key=value`, mit: `request_id`, `voice`, `mode`, `chars`, `kalt|warm`, `load_s`,
`ttfa_ms`, `audio_s`, `rtf`, `outcome` (`ok|error|cancelled`), `reason` bei Fehlern,
`queue_wait_ms`, `vram_peak_mib`, `rss_peak_mib`.

Ohne maschinenlesbaren `reason` ist jede Fallback-Diagnose Raten — das ist der Grund für
das Feld, nicht Vollständigkeit.

---

## 8. systemd

Vier Units unter `~/.config/systemd/user/`, als Dateien im Repo unter `systemd/`:

- `mimic.socket` / `mimic.service` — Frontend
- `mimic-worker.socket` / `mimic-worker.service` — Worker

Beide Services: `Restart=no` (kein Neustart in eine OOM-Schleife — die Lehre aus
`comfyui-img.service`), `RestrictAddressFamilies=AF_UNIX`, `NoNewPrivileges=yes`,
`PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=read-only` mit `ReadWritePaths`
für das Stimmverzeichnis, dazu die Speicherlimits aus §3. Kein `[Install]`-Autostart für
den Worker — er wird vom Socket geweckt.

---

## 9. Tests

Kein Framework über die stdlib hinaus. Ausführbar mit `uv run python -m unittest` oder
einem `tests/run.sh`.

**Verbindlich, ohne diese ist die Arbeit nicht fertig:**

1. **Frontend ohne torch.** Nach dem Import des Frontend-Moduls ist `"torch"` nicht in
   `sys.modules`.
2. **Protokoll-Vertrag.** Gegen einen Stub-Worker ohne GPU: Rahmenfolge ist genau
   `H A+ E`, Längen stimmen, Kopf-JSON hat die Pflichtfelder, `E` schließt mit
   `status:"ok"`.
3. **Fehler vor dem Stream.** Jeder `reason`-Code aus §2 ist auslösbar und liefert den
   dokumentierten HTTP-Status.
4. **Pfadtraversierung.** `voice` mit `../`, absolutem Pfad, Symlink und Namen außerhalb
   der Regex werden abgelehnt, ohne dass außerhalb des Stimmverzeichnisses gelesen wird.
5. **Grenzen greifen.** Zu langer Text, zu großer Body und volle Warteschlange liefern
   `text_too_long`, `bad_request`, `busy` — **bevor** GPU belegt wird.
6. **Abbruch stoppt die Inferenz.** Client trennt mitten im Stream; der Stub-Worker
   belegt, dass die Generator-Schleife endet und nicht weiterläuft.
7. **VRAM-Gate.** Bei künstlich gemeldetem knappem Speicher kommt `503
   insufficient_vram` und es findet **kein** Ladeversuch statt.
8. **Worker-Tod, Frontend lebt** *(Eindämmung, verpflichtend)*. Worker wird mitten in
   einer Anfrage `SIGKILL`t. Erwartung: Frontend überlebt, antwortet mit
   `worker_unavailable`, und die nächste Anfrage startet einen neuen Worker.
9. **`MemoryMax`-Erschöpfung** *(Eindämmung, verpflichtend)*. Worker unter einem zu
   knappen Limit starten. Erwartung: Worker stirbt, Frontend überlebt, Antwort ist ein
   maschinenlesbarer Absagegrund.

Tests 8 und 9 belegen die einzige Zusage, auf der Invariante α ruht. Ohne sie ist §3
unbelegt. Dass ein **Konsument** daraufhin auf sherpa-VITS zurückfällt, ist hier **nicht**
zu testen — dAImons Client-Anbindung existiert noch nicht, das gehört in den
dAImon-Integrationstask.

---

## 10. Nicht Teil dieses Auftrags

- Änderungen an dAImons oder MMCs Repository.
- Feintuning, fremde Stimmen, ein Registrierungs-Endpunkt für Stimmen.
- Ein GPU-Koordinator über Fremdsoftware wie ComfyUI hinweg.
- TCP, Netzzugriff im Betrieb, Authentifizierung jenseits der Dateirechte.
- Die MMC-VO-Pipeline (Phase 2).
- Alles unter `spike/` — nicht anfassen, nicht übernehmen, nicht aufräumen.
