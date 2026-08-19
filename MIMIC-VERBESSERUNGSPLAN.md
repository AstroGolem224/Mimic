# Mimic Review und Verbesserungsplan

Stand: 18. August 2026
Review-Basis: `main` auf `ca574e1`
Gegenstand: Desktop-UX, UI, GUI-Server, Frontend, Worker, Qwen-Integration,
Profile, Packaging, systemd und Tests

## Kurzfazit

Mimic hat bereits eine bemerkenswert eigenständige, visuell konsistente
Desktop-Oberfläche und eine grundsätzlich gute lokale Architektur. Der normale
Qwen-Ablauf mit `nordom_v3` funktionierte im Browser vollständig. Aufnahme,
Exportziel-vor-Generierung, lokale Unix-Sockets, Eingabevalidierung und die
Trennung von CPU-Frontend und GPU-Worker sind starke Grundlagen.

Die größten Risiken liegen nicht im Design, sondern an den Grenzen zwischen
Zuständen und Prozessen:

1. dots.tts schreibt vollständige Ziel- und Referenztexte ins persistente
   Journal.
2. Der installierte Worker entspricht nicht vollständig dem aktuellen Commit.
3. Aufnahme-, Profil- und Exportzustände sind an einigen Abbruchpfaden nicht
   transaktional.
4. Qwens 240-Sekunden-Frist widerspricht 125-Sekunden-Grenzen in Worker und
   Client; „Stopp“ beendet die Qwen-Rechenarbeit nicht.
5. Die GUI besitzt kein responsives Layout. In einem 390 px breiten Viewport
   lagen Skripteditor und große Teile der Kopfzeile vollständig außerhalb des
   sichtbaren Bereichs.

Es gibt keinen Befund, der den lokalen Einzelplatzbetrieb sofort unbenutzbar
macht. Die Datenschutz- und Datenkonsistenzbefunde sollten trotzdem vor neuen
Funktionen behoben werden.

## Umsetzungsstand vom 19. August 2026

Die unmittelbar behebbaren Befunde dieses Reviews sind in Version 0.9.6
umgesetzt:

- Payload-Logging von dots/loguru ist vor dem Modellimport unterdrückt und mit
  einem Geheimtext-Regressionstest abgesichert.
- Aufnahme-, Profilersetzungs-, Qwen-Quell- und Löschpfade sind transaktional
  beziehungsweise gegen laufende Referenzen gesperrt.
- Qwen besitzt konsistente Fristen, echten Prozessabbruch, begrenzte
  Fremdausgabe, sichere 0600-Laufzeitkopien und strenge WAV-/Pfadprüfung.
- State-Polling ist serialisiert, Export bleibt bis zum expliziten ACK
  wiederholbar und Entwürfe liegen atomar mit 0600 unter `XDG_STATE_HOME`.
- Responsive Layout, Tastatur-/Dialogführung, ARIA-Zustände, Kontrast,
  Fehlermeldungen und Zustandsanzeigen wurden überarbeitet.
- Wheel, Paketversion, mitgelieferte systemd-/Desktop-Dateien und CI-Prüfpfad
  wurden vereinheitlicht. Der GPU-Test restauriert die wirklichen Vorwerte.
- NUL-Byte, offene Dateiressourcen, unbegrenzte Qwen-Ausgabe, fehlendes
  Worker-Body-Limit und verwaiste Qwen-Temporärdateien sind behoben. Die Suite
  läuft mit `PYTHONWARNINGS=error::ResourceWarning`.

Bewusst offen bleiben größere Architektur- und Komfortarbeiten, die nicht als
kleine Fehlerkorrektur sicher einzubauen sind: eine generationenbasierte
Moduswechsel-Zustandsmaschine, Queue-Alterung/Fairness, echte GPU-Abbruch-Smokes,
vollständig gehashte Motor-Lockfiles, automatische DOM-/Browser-Smokes sowie
Suche/Favoriten für sehr große Stimmenlisten. Diese Punkte bleiben im Plan und
sind keine Voraussetzung für den stabilen lokalen Einzelplatzbetrieb.

## Technische Ausarbeitung der vier offenen Folgeprojekte

Diese vier Punkte sind nicht gleichartig. Lockfiles und GPU-Smokes sind
abgegrenzte Lieferpakete. Queue-Fairness und Moduswechsel greifen dagegen in
dieselbe Eigentumsfrage ein: Heute liegt die Queue im GPU-Worker, obwohl genau
dieser Prozess beim Moduswechsel absichtlich endet. Eine Fairnesslogik nur in
dieser flüchtigen Queue wäre weitgehend Wegwerfarbeit. Empfohlen wird daher ein
langlebiger Scheduler im CPU-Frontend, der sowohl Fairness als auch
Worker-Generationen besitzt.

### A. Queue-Fairness ohne Verlust der Echtzeit-Priorität

#### Ist-Zustand und konkrete Fehlerklasse

`Job.priority` ist aktuell schlicht der Index in `MODES = (mf, soar, qwen)`.
Der Heap sortiert deshalb immer alle MF-Jobs vor SOAR und Qwen. Bei einem
kontinuierlichen Strom kurzer MF-Aufträge kann ein älterer Qwen-Auftrag zeitlich
unbegrenzt warten. Zusätzlich zählen abgebrochene, aber noch nicht aus dem Heap
entfernte Jobs vorübergehend gegen `MAX_WAITING=4`.

Die Queue liegt in `Engine.jobs`. Ein kontrollierter `mode_restart` beendet den
gesamten Worker. Nur die gerade aktive Frontend-Anfrage kennt den strukturierten
Retry; weitere Queue-Einträge und ihre Handler-Verbindungen sterben mit dem
Prozess. Fairness allein im Worker behebt diesen Datenverlust nicht.

#### Zielpolicy

Die normale Bevorzugung bleibt erhalten, solange alle Jobs jung sind:

| Klasse | Basisrang | Zweck |
|---|---:|---|
| MF | 0 | interaktives, möglichst direktes Sprechen |
| SOAR | 1 | hochwertigere Batch-Ausgabe |
| Qwen | 2 | langsamster, vollständig blockierender Klonpfad |

Sobald ein Job `QUEUE_AGING_S` erreicht, entscheidet nicht mehr der Motor,
sondern die globale Eingangsreihenfolge. Als Startwert sind 15 Sekunden
vorgesehen. Ein bereits rechnender Job wird nicht präemptiert; die Garantie
beginnt an der nächsten Dispatch-Grenze.

Der Auswahlalgorithmus ist bei höchstens vier wartenden Jobs bewusst einfach:

1. Abgebrochene oder abgelaufene Jobs entfernen.
2. Gibt es gealterte Jobs, den mit der kleinsten Sequenznummer wählen.
3. Sonst nach `(Basisrang, Sequenznummer)` wählen.
4. Warmläufe belegen keinen Queue-Platz und dürfen echte Jobs nicht überholen.

Eine dynamische Neusortierung eines Heaps ist dafür unnötig und fehleranfällig;
ein linearer Scan über maximal vier Elemente ist klarer und messbar billiger.

#### Datenvertrag und Diagnose

Jeder Scheduler-Job erhält mindestens:

```text
job_id, correlation_id, mode, submitted_at, queue_deadline,
base_priority, sequence, cancelled, audio_started
```

Der Status wird erweitert um:

```json
{
  "queue": 3,
  "queue_by_mode": {"mf": 1, "soar": 0, "qwen": 2},
  "oldest_wait_ms": 18420,
  "active_job_id": "…",
  "active_correlation_id": "…"
}
```

Die Abschlusszeile protokolliert zusätzlich `queue_policy=priority|aged` und
`queue_age_ms`. Es werden weiterhin keine Nutztexte protokolliert.

#### Testmatrix

- Deterministische Fake-Clock: fortlaufend neue MF-Jobs, älterer Qwen-Job wird
  nach der Aging-Schwelle als Nächstes gewählt.
- FIFO innerhalb derselben Klasse und nach Erreichen der Aging-Schwelle.
- Ein abgebrochener Queue-Job verbraucht sofort keinen Platz mehr.
- Queue-Limit gilt auch unter parallelen Submits exakt.
- Warmlauf wird hinter echten Jobs ausgeführt und nicht als fünfter Job gezählt.
- Statuswerte bleiben unter Submit, Cancel und Dispatch konsistent.

#### Aufwand, Risiko und Abnahme

Im heutigen Worker wäre dies etwa ein Tag Arbeit, sollte dort aber nicht mehr
isoliert umgesetzt werden. Als Teil des Frontend-Schedulers sind 1,5–2 Tage
einzuplanen. Risiko: mittel.

Abgenommen ist der Punkt, wenn ein Qwen- oder SOAR-Job unter kontinuierlicher
MF-Last spätestens an der ersten Dispatch-Grenze nach seiner Aging-Schwelle
ausgewählt wird, kein abgebrochener Job die Queue blockiert und alle
Entscheidungen im Status erklärbar sind.

### B. Generationenbasierte Moduswechsel-Zustandsmaschine

#### Architekturentscheidung

Die wartende Arbeit muss in dem Prozess liegen, der einen GPU-Worker-Neustart
überlebt. Daher wird der CPU-Frontend-Prozess Eigentümer eines einzelnen
`Scheduler`. Der GPU-Worker wird zu einem Single-Flight-Ausführer für genau
einen aktiven Motor. Seine kleine interne Eventqueue für den laufenden Stream
bleibt; die motorübergreifende Jobqueue wandert ins Frontend.

Das vermeidet drei problematische Alternativen:

- Mehrere große Motoren gleichzeitig im Worker würden die bereits gemessene
  RAM-Grenze überschreiten.
- Ein In-Process-Unload gibt CUDA- und Bibliothekszustand nicht zuverlässig
  frei.
- Eine Queue im absichtlich sterbenden Prozess kann nicht verlustfrei sein.

#### Zustände und Invarianten

Der Scheduler besitzt eine monotone `generation` und folgende Phasen:

| Phase | Bedeutung | Erlaubter nächster Zustand |
|---|---|---|
| `cold` | kein bestätigter Worker | `starting`, `failed` |
| `starting` | Zielmotor startet/lädt | `ready`, `failed`, `switching` |
| `ready` | Generation und Motor bestätigt | `running`, `switching` |
| `running` | genau ein Auftrag besitzt den Worker | `ready`, `switching`, `failed` |
| `switching` | alte Generation wird beendet | `starting`, `failed` |
| `failed` | begrenzter Retry ausgeschöpft | `cold` nach neuem Auftrag/Backoff |

Es gelten folgende Invarianten:

1. Höchstens ein aktiver GPU-Auftrag.
2. Ein Worker akzeptiert nur Requests seiner aktuellen Generation und seines
   aktiven Motors.
3. Wartende Jobs bleiben bei einem Worker-Ausgang im Frontend erhalten.
4. Nach dem ersten ausgelieferten Audioframe wird ein Job niemals automatisch
   wiederholt; sonst könnte Audio doppelt gesprochen oder gespeichert werden.
5. Vor dem ersten Audioframe ist genau ein automatischer Infrastruktur-Retry
   erlaubt. Fachliche Fehler und Cancel werden nicht wiederholt.
6. Ein Warmlauf ist erst erfolgreich, wenn `ready` für den angeforderten Motor,
   die Generation und die angeforderte Stimme bestätigt ist.

#### Protokollerweiterung

Frontend und Worker handeln einen kleinen versionsgebundenen Vertrag aus:

```json
{
  "protocol": 2,
  "generation": 17,
  "phase": "ready",
  "active_mode": "qwen",
  "desired_mode": "qwen",
  "worker_pid": 1234
}
```

Jede interne Syntheseanfrage trägt `generation`, `job_id` und die vorhandene
`correlation_id`. Ein alter oder falscher Worker antwortet vor dem Einreihen mit
`stale_generation` beziehungsweise `wrong_mode`. Beide Gründe sind vor dem
ersten Audioframe retrybar; sie lösen keinen generischen 503 aus.

#### Ablauf eines Moduswechsels

1. Scheduler wählt nach Fairnesspolicy den nächsten Job.
2. Passt dessen Modus zur bestätigten Generation, wird er dispatcht.
3. Andernfalls wechselt der Scheduler auf `switching`, nimmt aber weiter Jobs
   in seine eigene Queue an.
4. Eine aktive alte Anfrage wird beendet oder kontrolliert fertiggestellt; der
   alte Worker wird anschließend mit eindeutigem Grund geschlossen.
5. Nach bestätigtem PID-Ausgang wird `generation` erhöht und der neue Motor per
   Socket-Aktivierung gestartet.
6. Erst ein passender Status `ready(generation, mode)` öffnet den Dispatch.
7. Startfehler werden einmal mit Backoff wiederholt; danach erhalten betroffene
   Jobs einen strukturierten Fehler mit Generation und Motor.

Mehrere schnell wechselnde Wünsche werden koalesziert: `desired_mode` folgt dem
nächsten tatsächlich auswählbaren Job, nicht jedem UI-Klick. Ein Warmlauf darf
einen bereits wartenden Sprechauftrag nicht zu einem zusätzlichen Hin-und-her
zwingen.

#### Migrationsschritte

1. Reine State-Dataclasses, Transition-Validator und Fake-Clock einführen,
   zunächst noch ohne Verhaltensänderung.
2. Worker-Vertrag um Generation und `wrong_mode` ergänzen.
3. Frontend-Scheduler mit Queue und Eventkanal implementieren; bisheriges
   Direkt-Proxying hinter einem temporären Feature-Schalter erhalten.
4. MF-only und gleiche-Modus-Parallelität umstellen.
5. Moduswechsel, Warmlauf und Qwen aktivieren.
6. Worker-Heap entfernen, Feature-Schalter und alten `mode_restart`-Retry
   löschen, sobald die Integrationsmatrix bestanden ist.

#### Integrationsmatrix

- Zwei parallele Jobs `mf → qwen`, `qwen → mf` und `soar → qwen`.
- Drei wartende Modi bei einem absichtlichen Worker-Ausgang.
- Cancel während `starting`, `running` und `switching`.
- Worker stirbt vor Kopfrahmen, nach Kopfrahmen und nach erstem Audioframe.
- Zwei gleichzeitige Warmläufe derselben beziehungsweise verschiedener Modi.
- Veralteter Status einer alten Generation trifft nach dem neuen Status ein.
- Frontend-Neustart: wartende In-Memory-Jobs enden klar als Verbindungsabbruch;
  es wird keine falsche Persistenzgarantie behauptet.

#### Aufwand, Risiko und Abnahme

Aufwand: 4–6 Arbeitstage einschließlich Migration und Integrationstests.
Risiko: hoch, weil Streaming, Cancel, Socket-Aktivierung und Modelllebenszeit
zusammentreffen. Das Paket sollte einen eigenen Commit und einen einfachen
Rollback-Schalter erhalten.

Abnahme: In 100 wiederholten gemischten Fake-Worker-Läufen kein generischer
Fehler und kein verlorener Queue-Job; reale Folge `mf → qwen → soar → mf`
funktioniert ohne manuellen Neustart; Warmlauf bestätigt ausschließlich einen
tatsächlich bereiten Motor; kein Audio wird doppelt ausgeliefert.

### C. Echte GPU-Abbruch-Smokes

#### Was die vorhandenen Tests noch nicht beweisen

Die opt-in GPU-Suite prüft derzeit SIGKILL und `MemoryMax`. Die neuen Unit-Tests
beweisen den Cancel-Mechanismus mit Fake-Prozessen, aber nicht, dass ein echter
CUDA-Kernel aufhört, das Qwen-Kind verschwindet, VRAM/Utilization reagieren und
der nächste reale Auftrag wieder funktioniert.

#### Sichere Testhülle

Die Smokes bleiben ausdrücklich opt-in und laufen nie in der normalen CI:

```text
tests/run.sh --gpu-cancel
```

Vorbedingungen:

- installierte Paketdateien und Units entsprechen dem Checkout;
- `nvidia-smi`, alle drei Motoren und ein Testprofil sind vorhanden;
- kein fremder Compute-Prozess belegt die GPU, andernfalls sauberer Skip;
- effektive `MemoryHigh`, `MemoryMax`, Dienstzustände und PIDs werden gesichert;
- jeder Test besitzt eine harte Gesamtfrist und einen `finally`-Cleanup.

Die Messwerte landen als JSON unter einem temporären Testordner: Zeit bis
Cancel, alte/neue PIDs, Prozess-VRAM, GPU-Auslastung, Restdateien und Ergebnis
des Recovery-Auftrags. Nutztexte werden nicht in das Artefakt geschrieben.

#### Motorbezogene Beweise

| Motor | Abbruchpunkt | Erwarteter Beweis |
|---|---|---|
| MF | nach erstem Audioframe | `outcome=cancelled` ≤ 2 s, Owner bleibt lebend, GPU-Compute fällt ab, nächster MF-Auftrag erfolgreich |
| SOAR | während realer Generierung | wie MF; kein weiterer Audioframe nach Cancel |
| Qwen | vor fertiger Gesamt-WAV | altes Qwen-Kind ≤ 2 s beendet, dessen Prozess-VRAM verschwindet, keine `qwen-*.wav`, Ersatzkind wird bereit, nächster Qwen-Auftrag erfolgreich |

Bei MF/SOAR darf der Modell-VRAM absichtlich belegt bleiben, weil der Worker
warm bleibt. Dort ist „VRAM fällt auf null“ ein falsches Kriterium; relevant
sind Ende der Compute-Auslastung, Cancel-Log und erfolgreiche Wiederverwendung.
Bei Qwen werden GPU-Stopp und Recovery getrennt gemessen: Das alte Kind muss in
höchstens zwei Sekunden weg sein, das Laden des Ersatzkinds darf länger dauern.

Der Test löst Cancel über denselben öffentlichen Pfad wie der GUI-Stopp aus:
laufende `AktiveVerbindung` schließen beziehungsweise `/api/stop` verwenden.
Ein direkter Kill allein wäre kein Nachweis der Produktfunktion.

#### Automatisierung und Flake-Schutz

- GPU- und Kind-PIDs über Prozessbaum plus `nvidia-smi
  --query-compute-apps=pid,used_memory` zuordnen.
- Compute-Rückgang nur bei exklusiver GPU über drei aufeinanderfolgende Samples
  bewerten; bei Fremdlast Skip statt falschem Rot.
- Journal ab einer Cursor-/Zeitmarke lesen, nicht über globale Textsuche.
- Nach jedem Fall einen kurzen Recovery-Satz erzeugen und WAV-Kopf/Dauer prüfen.
- Bei Fehlschlag Units und Grenzen im `finally` restaurieren und Worker stoppen,
  damit kein mehrere GB großer Restprozess bleibt.

Optional kann später ein manuell gestarteter `workflow_dispatch` auf einem
selbst gehosteten GPU-Runner hinzukommen. Normale Pull Requests bleiben
GPU-frei.

#### Aufwand, Risiko und Abnahme

Aufwand: 1,5–2,5 Tage. Risiko: mittel; die Tests sind absichtlich destruktiv für
den Worker, nicht für Nutzerdaten.

Abnahme: Jeder Motor besteht drei Wiederholungen; alle Fristen und PIDs sind im
JSON-Artefakt nachvollziehbar; nach der Suite entsprechen Units und
Speichergrenzen byte- beziehungsweise wertgenau dem Vorzustand; keine
Qwen-Temporärdatei und kein Kindprozess bleibt zurück.

### D. Vollständig gehashte Motor-Lockfiles und Environment-Health

#### Ist-Zustand

`umgebung_bauen()` installiert erst Torch und anschließend offene
Paketbereiche. Damit kann derselbe Commit Wochen später andere Transitivpakete
erhalten. `umgebung_da()` prüft nur, ob `bin/python` existiert; eine halbfertige
oder veraltete Umgebung wird daher als bereit angeboten.

Am 19. August 2026 waren lokal unter anderem folgende Auflösungen aktiv:

| Umgebung | Relevante Auflösung |
|---|---|
| VoxCPM | `voxcpm 2.0.3`, `torch 2.9.1+cu128`, `transformers 5.15.0`, `numpy 2.5.2` |
| Qwen Design | `qwen-tts 0.1.1`, `transformers 4.57.3`, `accelerate 1.12.0`, `numpy 1.26.4`, `numba 0.60.0` |
| Qwen Klon | dieselbe Paketfamilie wie Qwen Design, aber getrennte Laufzeitumgebung |
| MOSS | `transformers 5.0.0`, `accelerate 1.14.0`, `numpy 2.5.2` |

Die vorhandenen `spike2/*/uv.lock` zeigen, dass das Locking mit dem
CUDA-Index funktioniert, sind aber Experimentspuren und keine vom Produkt
verbrauchte Quelle.

#### Zielstruktur

```text
motoren/
  voxcpm/pyproject.toml + uv.lock
  qwen/pyproject.toml + uv.lock
  moss/pyproject.toml + uv.lock
```

Qwen Design und Qwen Klon verwenden denselben Lockinhalt, werden aber weiterhin
in zwei Zielumgebungen installiert. Torch und Torchaudio kommen über einen
expliziten `pytorch-cu128`-Index; sämtliche direkten und transitiven Artefakte
stehen mit SHA-256 im jeweiligen `uv.lock`. Die Modell-Revisions-Hashes in den
Skripten bleiben eine zweite, getrennte Sperre für die Hugging-Face-Snapshots.

#### Reproduzierbarer Baupfad

1. Exakte CPython-3.12-Patchversion für einen Lock-Zyklus festlegen.
2. Neue Umgebung in einem 0700-Geschwisterverzeichnis bauen, niemals direkt im
   produktiven Ziel.
3. `uv sync --frozen --no-dev --no-install-project` mit dem jeweiligen Projekt
   und explizitem `UV_PROJECT_ENVIRONMENT` ausführen.
4. `uv pip check`, Versionsmanifest und einen motorbezogenen Import-Smoke
   ausführen. Der Smoke lädt noch kein mehrgigabytegroßes Modell.
5. Erst nach vollständigem Erfolg die Umgebung atomar austauschen; bei Fehler
   bleibt die alte bytegenau erhalten.
6. Manifest mit Lock-SHA, Python, Plattform, Torch/CUDA, Paketversionen und den
   erwarteten Modellrevisionen als 0600 schreiben.

`umgebung_da()` wird durch einen aussagekräftigen Zustand ersetzt:

```text
missing | building | stale | broken | ready
```

Nur `ready` erscheint als verfügbarer GUI-Motor. `stale` bedeutet, dass die
Umgebung funktioniert, aber nicht zum eingecheckten Lock/Manifest gehört;
`broken` bedeutet fehlgeschlagener Import, falsche Python-Version oder
inkonsistente Pakete. GUI und CLI nennen jeweils den Reparaturbefehl.

Vorgesehene Befehle:

```text
mimic setup --entwurf qwen-klon --check
mimic setup --entwurf qwen-klon --repair
mimic setup --entwurf --check
```

Ein normaler Start repariert oder lädt niemals selbstständig Pakete aus dem
Netz. Lock-Aktualisierungen erfolgen bewusst in einem eigenen Commit: Lock neu
auflösen, frische Geschwisterumgebung bauen, Import-/GPU-Smokes ausführen und
die hörbaren Goldproben manuell vergleichen.

#### CI und Tests

- `uv lock --check` für jedes Motorprojekt.
- Lock-SHA und Motorzuordnung in Unit-Tests.
- Fake-uv-Test beweist `--frozen` und den atomaren Austausch/rollback.
- Manifesttests für fehlend, manipuliert, falsche Python-Version und falsche
  Modellrevision.
- Export der Locks muss zweimal dieselben direkten Versionen und Hashes liefern.
- Kein Netzwerkzugriff in Status-, GUI- oder Worker-Startpfaden.

#### Aufwand, Risiko und Abnahme

Aufwand: 2–3 Tage einschließlich Migration der vorhandenen Umgebungen. Risiko:
niedrig bis mittel; große Downloads machen Rollback und freie-Platz-Prüfung
wichtig.

Abnahme: Zwei Neuinstallationen aus denselben Locks besitzen identische
Manifeste und Paketversionen; Hashmanipulation wird vor Installation abgewiesen;
ein absichtlich unterbrochener Bau lässt die alte Umgebung unberührt; GUI und
`/status` bieten nur `ready`-Motoren an.

### Empfohlene Lieferreihenfolge

| Paket | Inhalt | Aufwand | Abhängigkeit |
|---|---|---:|---|
| F1 | Produktive Motor-Locks, Manifest und Health-Check | 2–3 Tage | keine |
| F2 | Reale GPU-Cancel-Smokes als Ist-Baseline | 1,5–2,5 Tage | F1 für reproduzierbare Umgebungen |
| F3 | Frontend-Scheduler mit Aging und Generationenvertrag | 4–6 Tage | F2 als Sicherheitsnetz |
| F4 | Reale Wechselmatrix und erneute GPU-Smokes | 1–2 Tage | F3 |

Gesamt: realistisch 8,5–13,5 Arbeitstage. F3 ist der einzige hochriskante
Umbau und sollte nicht mit UI-Funktionen oder Paket-Upgrades vermischt werden.
Nach jedem Paket bleiben normale MF-Nutzung und der vorherige Rollback-Punkt
produktiv installierbar.

## Prüfumfang und Evidenz

- Repository, Git-Stand, installierte uv-tool-Umgebung, laufende Prozesse,
  systemd-Units und Journale wurden getrennt geprüft.
- Desktop-Browserprüfung bei 1280 × 860 und schmale Prüfung bei 390 × 844.
- Interaktiv geprüft: Stimmenauswahl, Stimmenwerkstatt, leere Eingabe,
  Moduswahl, `nordom_v3` mit Qwen, Fortschritt und Abschlusszustand.
- Der Qwen-Hauptablauf war erfolgreich; es gab keine Browser-Console-Fehler.
- `PYTHONDONTWRITEBYTECODE=1 sh tests/run.sh`: **201 Tests bestanden**.
- `MIMIC_GUI_DEMO=1 uv run python -m mimic.gui`: **gui demo ok**.
- `uv lock --check`, `uv sync --check` und `systemd-analyze --user verify`
  waren ohne Befund.
- Live-Journal, VRAM-Wert, CGroup-Speicher und installierte Paketdateien wurden
  mit dem Checkout verglichen.

## Stärken, die erhalten bleiben sollten

- Sehr klare visuelle Identität; Oberfläche wirkt wie ein Werkzeug und nicht
  wie eine generische Web-App.
- Gute Informationshierarchie im Desktop-Layout: Stimmen, Skript, Wellenform,
  Transport und Telemetrie sind auf einen Blick sichtbar.
- Aufnahmeführung mit Teleprompter, Zielzone, Dauerurteil und direktem Anhören
  ist verständlich und fachlich sinnvoll.
- Native Buttons, sichtbare Fokusrahmen für viele Hauptaktionen, `lang="de"`,
  `Strg+Enter` und Escape-Unterstützung.
- Exportziel wird vor der teuren Generierung gewählt; das verhindert verlorene
  GPU-Zeit bei abgebrochenen Dateidialogen.
- Loopback-GUI, einmaliges Starttoken, SameSite-Cookie, geschützte Unix-Sockets
  und strenge Typ-/Pfadprüfungen sind eine gute Sicherheitsbasis.
- CPU-Frontend und GPU-Worker sind sauber getrennt und ressourcenbegrenzt.
- Fehlerhafte oder stumme Teilstücke werden nicht still als Erfolg ausgegeben.
- MF, SOAR und Qwen funktionieren als gemeinsame Produktoberfläche.

## Priorisierte Befunde

### P1 – sofort bzw. im nächsten Stabilitätszyklus

#### 1. Vollständige Nutzertexte im systemd-Journal

**Evidenz:** Der dots.tts-Logger schreibt den kompletten Generierungsplan mit
Zieltext und `prompt_text` in `journalctl --user -u mimic-worker.service`.
Mimic begrenzt den Fremdlogger vor dem Modellimport nicht
([worker.py](mimic/worker.py), Modellaufrufe um Zeile 548 und Laden um 699).

**Wirkung:** Ein offline arbeitender TTS-Dienst hinterlässt private Inhalte
dauerhaft im Journal. Das verletzt die erwartbare Datenschutzsemantik.

**Maßnahme:** dots/loguru vor dem Modellimport mindestens auf WARNING setzen;
Payload-Logs zusätzlich filtern. Mimics eigene strukturierte Logs behalten nur
Kennungen, Längen, Laufzeiten und Fehlerklassen.

**Aufwand:** klein, 0,5 Tag. **Risiko der Änderung:** niedrig.

**Abnahme:** Ein eindeutiger Geheimtext erscheint weder in stdout/stderr noch
in einem Journal-Capture; Request-ID, Modus, Dauer und Ergebnis bleiben
diagnostizierbar.

#### 2. Installierter Worker ist nicht identisch mit `HEAD`

**Evidenz:** `mimic/worker.py` im Checkout hatte SHA-256 `2330303e…`, die
uv-tool-Installation `5a2b1ee2…`. Der installierten Fassung fehlen die neue
`correlation_id` und die Korrektur des falschen Abbruchgrunds. Die übrigen
Paketdateien und Units waren identisch.

**Wirkung:** Die grüne Suite prüft Quellcode, während der reale Dienst einen
bereits korrigierten Fehler weiter ausführt.

**Maßnahme:** Nach jedem produktiven Commit Wheel bauen, Installation aus genau
diesem Artefakt aktualisieren, Dienste neu starten und ein Manifest vergleichen.

**Aufwand:** klein, 0,5 Tag. **Risiko:** niedrig.

**Abnahme:** Alle installierten `mimic/`-Dateien stimmen mit dem gebauten Wheel;
Frontend- und Worker-Journal tragen bei einem echten Auftrag dieselbe
`correlation_id`; Abbruch loggt keinen falschen `worker_unavailable`-Grund.

#### 3. Eine gestoppte, nicht bestätigte Aufnahme bleibt beim Schließen liegen

**Evidenz:** `Aufnahme.schliessen()` kehrt bei der erwartbaren
`RuntimeError` von `stoppen()` zurück und erreicht `verwerfen()` nicht
([gui.py](mimic/gui.py), Zeilen 382–391).

**Wirkung:** `ref.wav.tmp` und ein Profilordner können zurückbleiben. Das ist
zugleich Datenmüll, ein kaputtes Profil und eine unerwartete Aufbewahrung einer
Sprachaufnahme.

**Maßnahme:** Laufenden Prozess falls nötig stoppen, aber die temporäre Aufnahme
in einem äußeren `finally` immer verwerfen, sofern sie nicht explizit behalten
wurde.

**Aufwand:** sehr klein. **Risiko:** niedrig.

**Abnahme:** Für „gestoppt, nicht entschieden, Anwendung schließen“ bleiben
weder WAV-Tempdatei noch neuer Profilordner zurück.

#### 4. Profilersetzung kann MF/SOAR und Qwen zwei Personen zuordnen

**Evidenz:** `mimic import --force` ersetzt `ref.wav` und `ref.txt`, lässt aber
ein vorhandenes `qwen-source.wav` liegen. Qwen bevorzugt diese alte Quelle
weiter ([cli.py](mimic/cli.py), Profilimport ab Zeile 259;
[worker.py](mimic/worker.py), Referenzauswahl um Zeile 108).

**Wirkung:** MF/SOAR sprechen mit der neuen Person, Qwen weiterhin mit der
alten. Zusätzlich wird eine alte Stimmaufnahme unbemerkt aufbewahrt.

**Maßnahme:** Profile als vollständige, manifestierte Einheit in einem
Geschwisterverzeichnis bauen, vollständig validieren und atomar austauschen.
Generischer Import entfernt alte motorspezifische Quellen oder verlangt eine
explizite Entscheidung.

**Aufwand:** mittel, 1–2 Tage. **Risiko:** mittel.

**Abnahme:** Fehler in jedem Bauschritt lassen das alte Profil bytegenau stehen;
nach Force gehören alle Motorreferenzen nachweislich zur neuen Identität.

#### 5. Qwen-Fristen widersprechen sich

**Evidenz:** Qwen darf 240 s laufen (`QWEN_REQUEST_TIMEOUT`), der
Worker-HTTP-Handler wartet nur `REQUEST_TIMEOUT + 5` = 125 s, der CLI-Client
ebenfalls 125 s, das Frontend dagegen 250 s
([worker.py](mimic/worker.py), Zeilen 43 und 942;
[cli.py](mimic/cli.py), Zeile 24; [frontend.py](mimic/frontend.py), Zeile 31).

**Wirkung:** Ein zulässiger kalter oder langer Qwen-Lauf kann bei 125 s
abbrechen, während andere Schichten ihn weiterhin für gültig halten.

**Maßnahme:** Eine gemeinsame modusabhängige Deadline pro Auftrag; Queue-Budget
und Rechenbudget getrennt. Während langer Vorbereitungen Heartbeat/Phase senden.

**Aufwand:** mittel, 1 Tag. **Risiko:** mittel.

**Abnahme:** Vertrags-/Fake-Qwen-Test bei 124, 126 und 240 s; alle Schichten
brechen konsistent oder liefern erfolgreich, ohne verwaisten Auftrag.

#### 6. „Stopp“ beendet Qwen nicht

**Evidenz:** Das Qwen-Kind erzeugt die komplette Satz-WAV blockierend. Der
Parent sieht `job.cancelled` erst nach der Kindantwort
([qwen_dienst.py](mimic/qwen_dienst.py), um Zeile 80;
[worker.py](mimic/worker.py), Zeilen 142 und 552).

**Wirkung:** Die UI wirkt gestoppt, aber GPU und einziger Modell-Owner rechnen
bis zum Satzende weiter. Folgeaufträge scheinen zu hängen.

**Maßnahme:** Kurzfristig Qwen-Kind bei Cancel kontrolliert terminieren und neu
starten; mittelfristig IPC-Abbruch mit Auftrags-ID oder echtes Streaming.

**Aufwand:** mittel bis groß, 1–3 Tage. **Risiko:** mittel bis hoch.

**Abnahme:** Stopp innerhalb höchstens 2 s, GPU-Auslastung fällt ab, temporäre
Dateien verschwinden und der nächste Auftrag läuft ohne manuellen Neustart.

#### 7. Moduswechsel und Warmlauf sind unter Parallelität nicht zuverlässig

**Evidenz:** Ein fremder warmer Modus löst Worker-Neustart aus. Nur der aktuelle
Auftrag erhält den strukturierten Retry; weitere wartende Aufträge verlieren
die Verbindung. `/warm` wiederholt einen Moduswechsel nicht und kann „accepted“
melden, obwohl nur der Worker beendet wurde
([worker.py](mimic/worker.py), Zeilen 236, 362, 457 und 616;
[frontend.py](mimic/frontend.py), um Zeile 305).

**Wirkung:** Sporadische 503-Fehler und ein Warmlauf, der den gewünschten Motor
nicht tatsächlich wärmt.

**Maßnahme:** Moduswechsel als explizite Generation/Zustandsmaschine. Alle
betroffenen Jobs entweder kontrolliert requeue-en oder vor Einreihung mit einer
retrybaren Antwort ablehnen. `/warm` bis zum echten warmen Endzustand führen.

**Aufwand:** groß, 2–3 Tage. **Risiko:** hoch.

**Abnahme:** Integrationstest mit zwei parallelen gemischten Modi; kein Auftrag
endet generisch, Warmlauf bestätigt erst den tatsächlich geladenen Motor.

#### 8. Polling und Exportzustellung sind nicht serialisiert

**Evidenz:** `setInterval` startet alle 220 ms einen neuen asynchronen `tick`,
ohne den vorherigen abzuwarten. `/api/state` verbraucht das einmalige
`download`-Signal bereits beim Lesen
([gui.html](mimic/gui.html), Polling um Zeile 1568;
[gui.py](mimic/gui.py), Zeilen 1088–1098).

**Wirkung:** Verzögerte Antworten können Zustand/Cursor in falscher Reihenfolge
anwenden. Ein fertiger Export kann seinen einzigen Zustellimpuls verlieren.
Transiente Aktionsmeldungen konkurrieren außerdem mit dem Jobstatus.

**Maßnahme:** Maximal ein State-Request gleichzeitig; Cursor monoton. Besser
Long Poll/SSE. Export bleibt bereit, bis der Client explizit bestätigt. Jobstatus
und Toast/Aktionsmeldung in getrennten UI-Zuständen darstellen.

**Aufwand:** mittel, 1–2 Tage. **Risiko:** mittel.

**Abnahme:** Künstlich verzögerte/umgeordnete Antworten erzeugen keine doppelten
Pegel oder Rücksprünge. Export ist bis zur Bestätigung erneut speicherbar.

#### 9. Löschen ist während konfliktträchtiger Aktionen möglich

**Evidenz:** Das Backend sperrt Profil-Löschen nicht gegen laufende Aufnahme,
Entwurf oder einen Mehrsprecherauftrag
([gui.py](mimic/gui.py), Profilpflege um Zeilen 1047–1065).

**Wirkung:** Ein späterer Einsatz kann mitten im Skript scheitern; eine offene
Aufnahmedatei kann unter dem Recorder entfernt werden.

**Maßnahme:** Backend-seitige Aktivitätssperre und Referenzierung aller Stimmen
für die Dauer eines Auftrags. UI-Sperren nur ergänzend.

**Aufwand:** mittel, 1 Tag. **Risiko:** mittel.

**Abnahme:** Konflikt liefert vor jeder Mutation eine verständliche 409-Antwort;
laufender Auftrag und Profil bleiben unverändert.

#### 10. GPU-Test stellt ein veraltetes Produktionslimit wieder her

**Evidenz:** Die Unit verwendet `MemoryMax=10G`; der GPU-Test setzt nach seiner
1-MiB-Probe hart `7G`
([test_gpu_containment.py](tests/test_gpu_containment.py), Zeile 75;
[mimic-worker.service](systemd/mimic-worker.service), Zeile 37).

**Wirkung:** `tests/run.sh --gpu` kann Qwen danach dauerhaft drosseln oder
unzuverlässig machen.

**Maßnahme:** Effektiven Vorwert sichern und exakt wiederherstellen oder das
Runtime-Drop-in entfernen und die Unit neu laden.

**Aufwand:** sehr klein. **Risiko:** niedrig.

**Abnahme:** MemoryHigh/MemoryMax sind nach Erfolg, Fehler und Unterbrechung
identisch zum Ausgangszustand; Qwen-Kaltstart läuft direkt danach.

#### 11. Schmale Fenster schneiden die Kernfunktion ab

**Evidenz:** Keine Media Query; feste 330-px-Seitenleiste, breite Telemetrie,
viele Transportregler und `body { overflow:hidden }`. Bei 390 × 844 waren
Telemetrie und der komplette Skripteditor rechts außerhalb des sichtbaren
Bereichs. Es gab keine horizontale oder vertikale Rettungsnavigation
([gui.html](mimic/gui.html), Grundlayout ab Zeile 43).

**Wirkung:** Kleine Fenster, 800 × 600 und hoher Desktop-Zoom können die
Texteingabe unbedienbar machen.

**Maßnahme:** Einspalten-Breakpoint, kompakte Telemetrie, scrollbares
Gesamtlayout und Klangregler in eine mobile Schublade. Popups an Viewport
klemmen.

**Aufwand:** mittel bis groß, 2–3 Tage. **Risiko:** mittel.

**Abnahme:** Vollständig bedienbar bei 390 × 844, 800 × 600 und 200 % Zoom;
kein horizontales Abschneiden, Editor mindestens 240 px hoch.

### P2 – wichtige Produkt- und Qualitätsverbesserungen

#### Accessibility und Tastatur

- Geschlossene Stimmenwerkstatt bleibt in Tab-Reihenfolge und Accessibility-
  Baum. Es fehlen `role="dialog"`, `aria-modal`, `inert`, Fokusfalle und
  Fokus-Rückgabe. Beim Öffnen wird immerhin sinnvoll in das Namensfeld fokussiert.
- Modus/Format haben keinen programmatischen Auswahlzustand; Register besitzen
  keine Tab-Semantik; Status/Fehler sind keine Live-Region.
- Formularüberschriften aus `<u>` sind keine echten Labels.
- Ein einfacher Klick auf eine Stimme baut die gesamte Liste neu auf und setzt
  Tastaturfokus auf `body`. Doppelklick funktionierte im Browser, hat aber keine
  gleichwertige sichtbare Tastaturaktion.
- `--text-still: #4e6167` erreicht auf `#0d1216` nur ungefähr **2,9:1** bei
  sehr kleiner Schrift; erforderlich sind 4,5:1.
- Für permanente Drift-/Pulsanimationen fehlt `prefers-reduced-motion`.

**Maßnahme:** Dialog-/Tab-/Radiogruppen-Semantik, echte Labels, Live-Regions,
Fokusmanagement, bestehende Stimmenbuttons nur aktualisieren, Kontrasttoken
anheben und Reduced-Motion-Regel.

**Aufwand:** 2–3 Tage. **Abnahme:** Tastatur-only-Ablauf für Sprechen, Modus,
Aufnahme und Schließen; axe/Accessibility-Scan ohne kritische Befunde; WCAG-AA-
Kontrast; keine erzwungene Bewegung.

#### Entwurfsverlust und Rückmeldung

- Frisches Chromium-Profil und fehlendes Autosave verlieren Skript, Stimme,
  Modus und Klangwerte beim Schließen oder Absturz.
- Fehlerzeile erzwingt Ellipsis und maximal 44 Zeichen; wichtige Ursachen sind
  nicht vollständig les- oder kopierbar.
- MF, SOAR und Qwen werden nicht hinsichtlich Latenz, Qualität, Kaltstart und
  Streamingverhalten erklärt. Kopf-Telemetrie („geladener Modus“) und Auswahl
  („nächster Modus“) können widersprüchlich aussehen.
- Rohzustände und Schreibweisen wie „Einsaetze“ wirken technisch.

**Maßnahme:** Draft unter XDG State mit Wiederherstellungsangebot; Dirty-Warnung;
persistente, aufklappbare Fehlerdetails; kurze Modushilfe und getrennte Labels
„geladen“/„ausgewählt“.

**Aufwand:** 2–3 Tage. **Abnahme:** Crash-Restore eines Skripts; Fehler vollständig
zugänglich; Erstnutzer kann anhand der UI den passenden Motor wählen.

#### Weitere Zustandsrennen

- „Nochmal“ wartet feste 120 ms statt `discard` abzuwarten.
- Aufnahme kann hinter geschlossener Werkstatt bis 90 s unsichtbar weiterlaufen.
- Während eines Jobs bleiben Modus, Stimme, Effekte und Werkstatt veränderbar,
  ohne klar zu sagen, ob Änderungen erst für den nächsten Auftrag gelten.
- Neue MF-Aufträge können ältere Qwen-Aufträge wegen fester Priorität dauerhaft
  überholen.

**Maßnahme:** explizite UI-Zustandsmaschine, await statt Timer, globaler
Aufnahmeindikator, klare „gilt ab nächstem Auftrag“-Semantik und alternde
Queue-Priorität.

**Aufwand:** 1–2 Tage. **Abnahme:** Race-Tests mit verzögerten Antworten;
maximale Wartezeit pro Modus ist definiert und gemessen.

#### Qwen-Verfügbarkeit, Referenzsicherheit und Telemetrie

- Qwen wird auswählbar angezeigt, auch wenn `qwen-klon`-venv fehlt.
- `mimic setup --entwurf` baut ohne Motornamen nicht den Qwen-Eindeutscher.
- Warmlauf verwendet immer Matthias statt der aktuell ausgewählten Stimme.
- `qwen-source.wav` umgeht die strenge FD-/Symlink-/Größen-/Formatprüfung von
  `ref.wav`.
- VRAM „frei“ ist ein Snapshot vor dem Laden; Qwen-Kind-RAM und Peaks fehlen.
  Im Review meldete `/status` 27.321 MiB, gleichzeitig zeigte `nvidia-smi`
  23.969 MiB und die CGroup rund 6,49 GiB.

**Maßnahme:** `/status` liefert verfügbare Modi und Setup-Hilfe; sichere
FD-basierte Validierung auch für Qwen-Quelle; Stimme beim Warmlauf durchreichen;
Telemetrie korrekt als Snapshot benennen oder live inkl. Kind/CGroup messen.

**Aufwand:** 2–3 Tage. **Abnahme:** fehlender Motor ist sichtbar deaktiviert;
manipulierte Qwen-Quelle wird sicher abgelehnt; Telemetrie stimmt innerhalb
definierter Toleranz mit NVML/CGroup überein.

#### Reproduzierbarkeit und Release

- Zusatz-venvs nutzen breite Versionsbereiche und gelten schon bei vorhandener
  Python-Datei als bereit.
- Wheel enthält die systemd-Ressourcen nicht; `mimic setup` ist an Checkout/CWD
  gebunden.
- `pyproject.toml` meldet 0.9.4, `mimic.__version__` 0.1.0.
- Keine eingecheckte CI prüft Wheelbau, frische Installation, Unit-Dateien und
  Installationsparität.

**Maßnahme:** Lockfile je Motor, echte Health-Prüfung, Units als Paketressource,
eine Versionsquelle und CI-/Deploy-Gate.

**Aufwand:** 3–5 Tage. **Abnahme:** frisches HOME kann Wheel installieren und
`mimic setup` außerhalb des Repos ausführen; defektes venv wird erkannt; CI
prüft Manifestparität.

### P3 – Wartbarkeit und Politur

- `gui.html` enthält ein echtes NUL-Byte in `voices.join(...)`; Werkzeuge
  behandeln die Datei deshalb als Binärdatei.
- Suite ist grün, meldet aber mehrere `ResourceWarning` wegen offener Dateien.
- Qwen sammelt Nicht-JSON-Ausgabe bis zum nächsten Ereignis unbegrenzt.
- Interner Worker-Endpunkt begrenzt `Content-Length` nicht.
- Nach SIGKILL/OOM können `qwen-*.wav` im Runtime-Verzeichnis bleiben.
- Es fehlen DOM-/Browser-Regressionstests für Fokus, Layout, Dialog und Export.
- Stimmenliste benötigt bei weiterem Wachstum Suche, Favoriten oder Gruppen.
- Skriptköpfe und unbekannte Stimmen könnten vor dem Start mit Zeilennummern
  validiert werden.

**Aufwand:** zusammen 2–3 Tage.

**Abnahme:** HTML ist Text ohne NUL; Suite läuft mit
`PYTHONWARNINGS=error::ResourceWarning`; Body-Limits und Tempbereinigung sind
getestet; Browser-Smokes decken Desktop und kleinste unterstützte Größe ab.

## Umsetzungsphasen

### Phase 0 – Vertrauen und sichere Ausgangslage (0,5–1,5 Tage)

1. Payload-Logging abstellen und Datenschutztest ergänzen.
2. Aktuelles Wheel installieren, Dienste neu starten, Manifestparität prüfen.
3. GPU-Test stellt den vorherigen Speicherwert exakt wieder her.
4. Aufnahme-Cleanup beim Schließen korrigieren.

**Gate:** Keine Nutzertexte im Journal; Checkout, Installation und laufender
Prozess sind nachweislich derselbe Stand; kein temporäres Sprachmaterial nach
Abbruch.

### Phase 1 – Zustands- und Qwen-Korrektheit (4–7 Tage)

1. Gemeinsamer Deadline-Vertrag für Queue, Worker, Frontend und Client.
2. Echter Qwen-Abbruch und Kindprozess-Neustart.
3. Moduswechsel-/Warmlauf-Zustandsmaschine mit Paralleltests.
4. Atomare, manifestierte Profile und sichere Qwen-Referenz.
5. Serialisiertes State-Polling, bestätigte Exportzustellung, persistente
   Aktionsmeldungen.
6. Lösch-/Aufnahme-/Entwurfssperren und „Nochmal“ ohne Timer-Race.

**Gate:** Fehler- und Abbruchtests lassen keine Prozesse, Dateien oder
halbfertigen Zustände zurück. Gemischte parallele Modi enden strukturiert.

### Phase 2 – UX, Responsive und Accessibility (4–7 Tage)

1. Responsive Einspaltenansicht und scrollbares Layout.
2. Stimmenauswahl ohne DOM-Neubau, explizite „Ins Skript“-Aktion.
3. Dialog-/Tab-/Label-/Live-Region-Semantik, Fokusfalle und Fokus-Rückgabe.
4. Kontrast und Reduced Motion.
5. Draft-Autosave, Fehlerdetails und verständliche Modushilfe.
6. Globale Anzeige für Aufnahme und klarer Änderungszeitpunkt während Jobs.

**Gate:** Keyboard-only, 390 × 844, 800 × 600 und 200 % Zoom bestehen den
Abnahmelauf; keine kritischen Accessibility-Befunde; Crash-Restore funktioniert.

### Phase 3 – Reproduzierbare Auslieferung (3–5 Tage)

1. Fake-Qwen-Protokolltest plus opt-in GPU-Smoke für MF/SOAR/Qwen.
2. Zusatzumgebungen sperren und Health-Check einführen.
3. Wheel mit Units/Desktop-Datei vollständig deploybar machen.
4. CI: Lockcheck, 3.12-Tests, Wheel, frische Installation, Entry-Points,
   systemd-Verify, Manifestparität und Browser-Smokes.
5. Versionen/README/Messwerte synchronisieren; NUL/Warnings bereinigen.

**Gate:** Ein frisches System kann ausschließlich aus dem Artefakt installiert
werden. Ein Release scheitert automatisch bei Datei-, Versions- oder
Dokumentationsdrift.

## Aufwand und Reihenfolge

Für eine Person sind **etwa 12–20 Arbeitstage** realistisch, abhängig davon, ob
Qwen zunächst durch Prozessneustart abgebrochen wird oder echtes Streaming
erhält. Empfohlene Reihenfolge:

1. Phase 0 vollständig.
2. In Phase 1 zuerst Profile/Recording/Export, dann Deadline/Cancel/Moduswechsel.
3. Responsive und Accessibility als zusammenhängende UI-Arbeit, nicht als
   nachträgliche Einzelpatches.
4. Release-Gates direkt nach den neuen Integrationsverträgen, damit der
   installierte Stand nicht erneut vom getesteten Stand abweicht.

## Produktziel nach Umsetzung

Mimic bleibt das gleiche charaktervolle lokale Stimmenwerk, gewinnt aber vier
klare Zusagen:

- **privat:** gesprochene Inhalte verlassen weder Rechner noch flüchtigen
  Verarbeitungsweg;
- **verlustfrei:** Aufnahme, Profil und Export sind atomar und wiederholbar;
- **verständlich:** Status, Modus und Fehler sagen ehrlich, was gerade gilt;
- **reproduzierbar:** getesteter Commit, installiertes Artefakt und laufender
  Dienst sind nachweislich identisch.
