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
