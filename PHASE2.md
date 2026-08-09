# Plan: Mimic Phase 2 — dAImon-Anbindung
_Rev 8, 2026-08-05, nach Codex-Runde 7. Begriffe nach
`~/Dokumente/UMBRA-Notes/DDs/Mimic/CONTEXT.md`._

## Goal

dAImon bezieht seine **Charakterstufe** von Mimic. Längere Antworten klingen nach
Matthias, kurze Bestätigungen bleiben bei der **Vorgabestufe** (sherpa-onnx VITS, CPU).
**Invariante α** hält mechanisch: fällt Mimic aus, ist kalt oder zu langsam, spricht
dAImon weiter — ohne Wartezeit, ohne dass ein Mimic-Zustand den Pfad blockiert.

Nebenzweck: der Alltagsgebrauch validiert **B2**, das mit zwei widersprüchlichen Läufen
(1/6 und 6/6) die schwächste Stelle der Kette ist.

## Phase 2a — zwei Defekte im laufenden Code

Diese kommen **vor** der Anbindung, weil der Plan sonst auf Kaputtem aufsetzt. Beide sind
am 2026-08-05 durch Codex' Review gefunden und von Hand am Code bestätigt worden.

1. **Die GPU-Sperre hat nie funktioniert.** `worker.request_gpu_permission` sendet
   `{"action":"request","client":"mimic"}` und gibt mit rohem `fertig\n` frei. dAImons Hub
   erwartet `{"art":"laden","vram_mib":N}` und `{"art":"fertig","sperre":<token>}`
   (`daimon/hub/daemon.py`). Der Hub antwortet `unbekannte_art`; Mimic prüft nur, **ob**
   eine Antwort kam, nicht welche — und lädt, obwohl der Hub gerade nein gesagt hat.
   Fix: echtes Protokoll, **zwei getrennte Rundläufe über je eine eigene Verbindung** —
   `laden` holt den Token, `fertig` schickt ihn später über eine **neue** Verbindung.
   dAImons Referenzclient sagt das ausdrücklich: „Eine Verbindung je Anfrage, nicht eine
   gehaltene" (`daimon/gpu/worker.py`). Wer nur den Closure auf demselben Socket
   korrigiert, dessen `fertig` wird nie gelesen — und die Sperre liegt bis zum Ablauf von
   `GPU_FRIST_S` (120 s) tot herum.
   Die Absagegründe des Hubs bleiben **erhalten**, nicht eingeebnet: er unterscheidet
   `vram`, `fullscreen` und `lade_sperre` bewusst. Mimic meldet nach außen
   `load_denied` mit maschinenlesbarem `hub_reason`; jeder der drei wird getestet.
   Drei Fälle getrennt: Hub nicht erreichbar → fail open (Mimic darf nicht von dAImon
   abhängen, MMC-Batch läuft auch ohne). Hub sagt nein → **abbrechen**. Antwort
   unverständlich → abbrechen und laut ins Journal.

   **Bekannte Folge, beim Bau am 2026-08-05 aufgetaucht:** stirbt der Worker hart
   (`SIGKILL`, cgroup-OOM), kommt er nicht mehr zum `fertig` — und seine Sperre steht bis
   `GPU_FRIST_S` (120 s). Mimic ist in diesem Fenster nicht bedienbar und antwortet
   `load_denied` / `lade_sperre`. Das ist **kein Mimic-Fehler**: dAImons eigene Worker
   haben dieselbe Eigenschaft, und die Frist ist der dafür vorgesehene Auffangmechanismus.
   Für Mimic wiegt sie aber schwerer — dAImons Worker laden in 293–419 ms, Mimic in
   4–9 s, das Fenster ist also rund zwanzigmal größer.
   Gehört in den dAImon-Integrationstask, nicht hierher: der Hub könnte tote Halter
   einsammeln, wenn der `laden`-Aufruf die PID mitschickt. Das ist eine Änderung an
   dAImon und wird dort entschieden.
2. **Die Aussprache-Tabelle umgeht den Hub-Validator.** `voices.apply_pronunciation`
   ersetzt beliebigen Text nach der Freigabe. DESIGN §8.3 setzt den Validator bewusst in
   den Hub, „sonst wäre er umgehbar, sobald ein anderer Produzent Text an die Ausgabe
   schickt" — genau das ist er hier. Ein Eintrag könnte ein freigegebenes Wort durch
   einen Pfad, eine URL oder einen ganz anderen Satz ersetzen.
   Ein Zeichen-Filter reicht **nicht**: „Buchstaben und Leerzeichen" kann immer noch
   einen völlig anderen Satz ergeben. Der Filter schließt gefährliche Zeichen, nicht
   Bedeutungsänderung.
   Fix, entschieden: **auf dem dAImon-Pfad wird nicht ersetzt.** `/speak` bekommt
   `aussprache: true|false`; dAImon sendet **immer `false`** und Mimic spricht exakt den
   Text des Hubs. Die Tabelle bleibt für CLI und Batch, wo kein Hub existiert und der
   Aufrufer den Text ohnehin selbst schreibt. Zusätzlich der Zeichen-Filter beim Laden,
   als zweiter Riegel für diese Pfade.
   Preis, offen benannt: dAImon sagt „ge-mer-get" statt „gemördscht". Das ist das
   Fehlerbild aus Kriterium B, und es bleibt — der Validator wiegt schwerer als die
   Aussprache.

## Phase 2a-bis — was die Parallelarbeit aufgerissen hat

Zwischen Rev 1 und Rev 6 ist im Repo unabhängig gearbeitet worden (`f3a26bb` stumme
Takes und Charakterstimmen, `4cacd90` GUI). Zwei davon machen Teile dieses Plans
**falsch**, wenn sie nicht vorher behandelt werden.

2a. **„Erster `A`-Rahmen" ist nicht mehr „erster Ton".** Seit `f3a26bb` sendet der Worker
   einen stummen Take **vollständig**, misst `spitze` und erkennt ihn erst **danach** als
   stumm — dann wiederholt er. Die ganze Engine-Wahl aus Schritt 12 hängt aber daran, dass
   der erste Rahmen hörbares Audio bedeutet: dAImon würde binnen 500 ms auf Mimic
   festlegen und dann sekundenlang Stille abspielen. Zwei stumme Takes hintereinander
   enden sogar mit `status=ok`.
   Fix: Anfangsrahmen zurückhalten, bis `STUMM_PEAK` **überschritten** ist; stumme Takes
   verwerfen statt senden; nach zwei stummen Takes **vor** dem Stream fehlschlagen.
   Damit wird „erster `A`" wieder gleichbedeutend mit „erster Ton", und erst dann trägt
   Schritt 12.
2b. **Zwei residente Runtimes passen nicht in die Unit.** `self.runtimes[mode]` behält
   jeden geladenen Modus, geräumt wird nie. Phase 0 hat gemessen: beide zusammen ~20 GiB
   RAM — `MemoryMax` ist **7G**. Bisher fiel das nicht auf, weil nur ein Modus benutzt
   wurde. Mit der GUI (nutzt `soar`) und dem geplanten `/warm(mf)` lädt Mimic den zweiten
   daneben und der Worker stirbt.
   **Gemessen am 2026-08-05, weil Vermutung hier nicht reicht:**

   | | RSS | VRAM frei |
   |---|---|---|
   | Start | 806 MiB | 30140 |
   | `mf` geladen | 2138 MiB | 19592 |
   | nach `del` + `empty_cache()` | **2139 MiB** | 30006 |
   | nach `malloc_trim` | 2118 MiB | 30004 |

   **VRAM kommt vollständig zurück, CPU-RSS praktisch nichts.** In-Process-Räumen löst
   das Problem also **nicht**. Fix deshalb: **ein residenter Modus, und der Wechsel ist
   ein kontrollierter Worker-Neustart** — der Eigentümer beendet sich (Phase 1s
   Prozessende, das RAM und VRAM nachweislich freigibt), die Socket-Aktivierung startet
   ihn neu, die auslösende Anfrage wird **einmal** wiederholt. Kein Räumen im Prozess.
   Damit trägt Phase 1s Entwurf ausgerechnet aus dem Grund, den ich in Phase 0 als
   „falsch begründet" korrigiert hatte: für VRAM war er das, für RSS nicht.
   Zustandsmaschine explizit, Mutation nur durch den Eigentümer:
   `warm(alt) → beendet sich → cold → loading(neu) → warm(neu) | cold(Fehler)`.
   Scheitert `_load(neu)` an Hub, VRAM oder CUDA, ist der Zustand `cold` — nicht ein
   halb geräumter Zwischenstand. Anfragen für den alten Modus während `loading` bekommen
   `503 cold` wie alle anderen; ein wartender Warmwunsch wird verworfen, nicht
   übernommen.
2c. **Der GUI-Stopp erreicht die Verbindung nicht.** `gui.py` hält sie im Synthesethread,
   der Tk-Rückruf setzt nur ein Event. Währenddessen hängt `getresponse()` oder
   `read_frame()`, der Socket ist unerreichbar und der Worker rechnet weiter — dasselbe
   Loch wie in Schritt 5, nur im anderen Client. Fix: aktive Verbindung threadsicher
   veröffentlichen, Stopp macht `shutdown(SHUT_RDWR)` plus `close()`.
2d. **Das erweiterte `/speak`-Schema muss CLI und GUI unverändert lassen.** Beide senden
   heute nur `text`, `voice`, `mode`. Vorgaben deshalb rückwärtskompatibel:
   `aussprache=true`, `require_warm=false`, Korrelations-ID serverseitig erzeugt, wenn sie
   fehlt. **dAImon sendet explizit** `false` / `true` / eigene ID. CLI und GUI kommen als
   Regressionstests dazu — sonst schaltet eine naheliegende Umsetzung ihre
   Aussprachekorrektur ab oder bricht sie ganz.

## Phase 2b — Mimic-Seite

3. **`require_warm` statt Status-Abfrage.** dAImon kann Wärme **nicht** über `GET /status`
   entscheiden: das ist ein Schnappschuss, weckt niemanden, meldet `warm`, sobald
   *irgendein* Runtime geladen ist, und `mode` ist nur der zuletzt benutzte. Ein warmer
   `soar`-Worker würde dAImon in einen 7-Sekunden-Kaltstart für `mf` laufen lassen.
   Also: `/speak` bekommt `require_warm: true`. Geprüft wird **in `Engine.submit()` vor
   dem Einreihen** — sonst steht der Job hinter einem laufenden Kaltstart und reißt die
   Frist, obwohl er sofort abzulehnen wäre. Antwort: `503 cold`.
   **Der Runtime-Lock darf dabei nie während `_load()` gehalten werden.** Sonst wartet
   genau die Anfrage, die schnell abgelehnt werden soll, bis zu sieben Sekunden auf den
   Lock — und das ist der Normalfall kurz nach einem angestoßenen `/warm`. Stattdessen
   veröffentlicht der Eigentümer atomar einen Zustand `loading`; `require_warm` liefert
   darauf sofort `cold`. Ein Test schickt eine Anfrage **mitten in den Warmlauf**.
   **Und der Worker darf `require_warm` beantworten, bevor er Torch überhaupt anfasst.**
   Heute ruft `Engine.__init__` → `write_status("kalt")` → `safe_vram_free_mib()` →
   `import torch` + `torch.cuda.mem_get_info()`, **bevor** der Server `submit()` erreicht.
   Gemessen am 2026-08-05: **0.69–0.75 s**, bei kaltem Seiten-Cache mehr. Ein
   socket-aktivierter Worker könnte die 400 ms aus P2-B also gar nicht halten, egal wie
   schnell die Ablehnung danach ist. Fix: der Anfangsstatus meldet VRAM `null`, und die
   VRAM-Abfrage passiert erst im Eigentümer beim tatsächlichen Laden.
4. **`POST /warm` mit ehrlicher Semantik.** Nicht „202 und später vielleicht ein Fehler".
   Der Warmlauf ist ein **Wunsch je Modus im Engine-Zustand**, kein Auftrag in der
   Warteschlange — sonst belegt er einen der vier Plätze gegen echte Sprache. Der
   Eigentümer arbeitet ihn ab, **wenn die Warteschlange leer ist**. `/warm` antwortet
   `202` („vermerkt"), `200` („schon warm"), `409` („läuft schon"). Ob es geklappt hat,
   steht danach im Journal und in `/status` — nicht in der Antwort, weil sie zu diesem
   Zeitpunkt niemand kennen kann. Entdopplung liegt im `Engine`, nicht im Frontend, weil
   Frontend-Threads dabei gegeneinander laufen würden.
   **Zwei Fallen, die ein blosses Flag nicht löst:** der Eigentümer hängt heute in
   `jobs.get()` (`worker.py`) — ein Flag im Zustand weckt ihn nie. Also eine gemeinsame
   `Condition` für Jobs *und* Warmwünsche statt der blockierenden Queue. Dabei bleiben
   die eingefrorenen Zusagen aus Phase 1 erhalten: unter der Condition weiterhin ein
   **begrenzter Heap über `(priority, sequence)`**, Kapazität vier, `mf` überholt
   wartende `soar`. Getestet, nicht angenommen — „Condition statt Queue" könnte das
   sonst versehentlich wegnehmen.
   Und ein Warmlauf ist **nicht unterbrechbar**: startet er kurz vor einer warmen
   `mf`-Anfrage, hält er den Eigentümer sekundenlang fest. Deshalb ist `/warm`
   **ausschließlich für `mf` definiert**; jeder andere Modus wird **synchron abgelehnt**,
   nicht als unerledigter Wunsch abgelegt. `soar` wärmt der Batch-Pfad durch seine erste
   echte Anfrage, und der wartet ohnehin.
4a. **Der Warmlauf muss auch den Audio-Pfad bezahlen — beim Bau am 2026-08-05 gemessen.**
   Nur das Modell zu laden reicht nicht: librosa zieht beim **ersten** Laden eines
   Referenzaudios numba durch die JIT-Kompilierung. Im frisch gewärmten Worker gemessen:
   8.7 s allein für `_load_prompt_audio`, TTFA **10 596 ms** — danach 377, 300, 217 ms.
   Ein Warmlauf ohne diesen Teil meldet „warm" und lässt den nächsten Aufruf trotzdem
   zehn Sekunden warten; für dAImon hieße das Frist gerissen, Rückfall auf sherpa,
   Warmlauf umsonst. Der Warmlauf schickt deshalb einmal Referenzaudio durch.
   Danach gemessen: erster Aufruf **676 ms**, zweiter 225 ms.
   **Rest offen:** 676 ms liegen weiter über der 500-ms-Gesamtfrist aus Schritt 13. Die
   erste Äußerung nach einem Warmlauf fällt also noch auf die Vorgabestufe zurück, erst
   die zweite kommt von Mimic. Sauberes Verhalten, aber die Zusage ist nicht ganz
   eingelöst — gehört vor P2-K nachgemessen.

5. **Abbruch vor dem ersten Rahmen muss beim Worker ankommen.** Heute liest das Frontend
   Worker-Rahmen, bis der erste `A` da ist, und sieht erst danach nach dem Konsumenten
   (`frontend.py:174` gegen `:214`). Bricht dAImon während Warteschlange oder langsamem
   erstem Rahmen ab, **rechnet der Worker weiter** und blockiert den einzigen Eigentümer.
   **Nicht versprochen wird Abbruch während des Kaltstarts:** `_load()` prüft kein
   Cancel-Flag und lädt bis zu sieben Sekunden zu Ende. Für dAImon ist der Fall durch
   `require_warm` ausgeschlossen (Schritt 3) — es kommt gar nicht erst in einen
   Kaltstart. Wer das wirklich abbrechbar will, braucht einen eigenen Ladeprozess; das
   ist nicht Teil dieses Plans. Fix: in dieser Phase Konsumenten-Socket und Worker-Antwort gleichzeitig
   überwachen (`select`), und bei Verschwinden des Konsumenten die Worker-Verbindung
   sofort schließen. **Nicht naiv:** `getresponse()` und `read_frame()` können Bytes
   bereits im Python-Puffer halten, während der rohe Socket für `select` leer aussieht —
   eine Umsetzung, die nur auf den Socket schaut, hängt dann trotz vorliegendem Rahmen.
   Also ein Parser, der gepufferte Bytes vor dem `select` berücksichtigt — oder ein
   Leser-Thread, dann aber mit derselben Sorgfalt wie in Schritt 10:
   `shutdown(SHUT_RDWR)` **vor** `close()` und ein begrenztes `join`. Ein blosses
   `close()` weckt einen im `read()` hängenden Thread nicht zuverlässig, im Frontend so
   wenig wie in dAImon.
6. **`GET /status` um `voices` erweitern** — nur Profile, die `load_voice()` **besteht**,
   mit Schema-Version. Ein ungültiges Profil zu listen wäre ein falsches Bereitschafts-
   signal. Scheitert dAImons Startprüfung, wird nur der Mimic-Pfad abgeschaltet und
   protokolliert, nichts anderes.
   **Die Startprüfung bekommt eine harte Gesamtfrist von 300 ms.** Ein erreichbares, aber
   hängendes Frontend würde sonst den Start von `daimon-tts` blockieren — und damit
   sherpa. Das wäre Invariante α gebrochen, ausgerechnet beim Start.

7a. **Fehlerobjekt versioniert erweitern.** `REASON_STATUS` kennt heute weder `cold` noch
   `load_denied`, unbekannte Gründe werden zu `worker_unavailable` eingeebnet, und
   `_error()` verwirft Zusatzfelder (`frontend.py`). Beide neuen Gründe sind **503**, und
   erlaubte strukturierte Zusatzfelder — insbesondere `hub_reason` — reichen Worker und
   Frontend unverändert durch. Ohne das können P2-B und P2-G gar nicht bestehen.
7. **Korrelations-ID durchreichen.** `/speak` und `/warm` nehmen eine vom Aufrufer
   erzeugte ID entgegen; Frontend und Worker führen sie in jeder Journal-Zeile. Ohne sie
   sind Fehler vor dem Stream über drei Prozesse hinweg nicht zusammenzuführen — die
   heutige `request_id` entsteht erst nach dem Aus-der-Schlange-Nehmen.

## Phase 2c — dAImon-Seite

8. **Neues Modul `daimon/face/mimic.py`.** Client und nur Client: verbinden, senden,
   Rahmen lesen, Fristen halten. Keine Wiedergabe, keine Policy. `tts.py` bleibt der
   einzige Ort, der `pw-cat` besitzt.
9. **Generationssicherheit über die neue Wartezeit hinweg.** Das ist die gefährlichste
   Stelle des ganzen Plans: zwischen Hub-Freigabe und erstem Ton liegt jetzt eine
   Wartezeit von bis zu mehreren hundert Millisekunden, in der heute nichts liegt.
   - Nach der Freigabe **einmal** unter `_lock` abbrechen und eine neue Generation
     reservieren; diese Generation gilt für Mimic **und** den sherpa-Rückfall, sie wird
     nicht erneut erhöht.
   - Jeder Schritt danach — Mimic-Anfrage, Rückfall, Registrieren des Players — prüft
     unter `_lock`, ob die eigene Generation noch die aktuelle ist. Nur die aktuelle darf
     eine Verbindung oder einen `pw-cat` registrieren.
   - Sonst kann eine ältere Äußerung, deren Mimic-Frist später abläuft, sherpa **über**
     einer neueren starten, oder zwei `pw-cat` laufen gleichzeitig.
10. **`abbrechen()` bekommt die Mimic-Sitzung, generationsmarkiert.** Heute kennt es nur
    `self._wiedergabe`. Neu: eine abbrechbare Sitzung unter `_lock`, atomar entnommen,
    und nur gelöscht, wenn Identität **und** Generation noch passen — sonst räumt ein
    alter Thread die Sitzung eines neueren weg.
    Abgebrochen wird mit `shutdown(SHUT_RDWR)` **dann** `close()`, außerhalb des Locks:
    ein reines `close()` weckt einen Thread, der bereits in `recv()` hängt, nicht
    zuverlässig.
11. **Sherpa-Ausfall darf Mimic nicht mitnehmen.** `sprich()` kehrt heute bei
    `self.absage` sofort zurück, **vor** dem Hub (`tts.py:427`). Fehlt das
    sherpa-Stimmprofil, wäre damit auch Mimic tot — obwohl es sprechen könnte. Fix:
    Freigabe und Engine-Wahl zuerst, `absage` erst dort auswerten, wo sie gilt, nämlich
    als Ergebnis der Vorgabestufe.
12. **Engine-Wahl vor dem ersten Byte.** `_ausgeben` öffnet ein `pw-cat` je Äußerung, die
    Rate steht beim Öffnen fest (sherpa 22050, Mimic 48000 Hz). Also: Mimic fragen, auf
    Antwortkopf **und** ersten `A`-Rahmen warten, dann `pw-cat` mit der Rate **aus dem
    `H`-Rahmen** öffnen. Kommt es nicht rechtzeitig → `pw-cat` mit 22050 Hz und sherpa.
12a. **Warmlauf anstoßen, ohne den Rückfall zu gefährden.** Reihenfolge ist bindend:
    **erst sherpa starten, dann `/warm`** — nie umgekehrt. Der Aufruf ist best effort,
    bekommt eine eigene harte Frist von 200 ms, und es läuft **höchstens einer**
    gleichzeitig. Sonst könnte ein hängendes `/warm` P2-B aushebeln oder unbegrenzt
    Hintergrund-Threads ansammeln — der Warmlauf soll die Zukunft verbessern, nicht die
    Gegenwart aufhalten.
13. **Eine Gesamtfrist bis zum ersten Ton, nicht drei sequenzielle.** Mimics 5/90/10 s
    sind für einen Batch-Client gedacht; wer sie erbt, wartet 90 s statt zurückzufallen.
    Und drei aufeinanderfolgende Fristen summieren sich statt zu begrenzen.
    Zu beachten: Mimics Frontend sendet den HTTP-200-Kopf **erst, nachdem** der erste
    `A`-Rahmen gepuffert ist. Kopf und erstes Audio sind also dasselbe Ereignis — eine
    getrennte „erster Rahmen"-Frist danach wäre wirkungslos.
    Also: **eine monotone Gesamtfrist von 500 ms** vom Verbindungsbeginn bis zum ersten
    `A`. Danach, im Stream, ein separater Rahmenabstand von 2 s. Jede gerissene Frist
    schließt die Sitzung, prüft die Generation und fällt zurück.
14. **Mimic zerlegt, dAImon nicht.** Der freigegebene Satz geht **als Ganzes** hin;
    `segmente()` bleibt dem sherpa-Pfad. Grund: Konditionieren kostet ~97 ms **je
    Anfrage** (87.3 → 184.5 ms gemessen), drei Segmente wären drei Konditionierungen.
15. **Abbruch mitten im Stream: aufhören.** Halber Satz bleibt halb, Fehler ins Journal.
    Kein Stimmwechsel mitten im Satz, keine Wiederholung. Entspricht `PHASE1.md` §2.
16. **Auswahlregel, konfigurierbar.** Der Socket-Pfad wird aus `XDG_RUNTIME_DIR`
    abgeleitet — `%t` ist ein systemd-Spezifizierer und expandiert in TOML **nicht**.

    | Eintrag | Vorgabe |
    |---|---|
    | `tts.mimic_socket` | aus `XDG_RUNTIME_DIR`, leer = Mimic aus |
    | `tts.mimic_stimme` | `matthias` |
    | `tts.mimic_ab_zeichen` | `80` |
    | `tts.mimic_nur_warm` | `true` → `require_warm` an Mimic |

    Vorgabe-Begründung: sherpa 132 ms [V], Mimic 250 ms warm, 7.1 s kalt. Die 80 sind ein
    **Startwert, kein Messergebnis**.
17. **Hub-Aktivitätszustand generationssicher.** `_tts_gesprochen()` löscht heute das
    globale `tts_active` für jede noch gültige Marke (`daemon.py:637`). Eine verspätete
    Meldung einer abgebrochenen Äußerung A kann damit die Sperre löschen, nachdem B schon
    `beginnt` gemeldet hat — durch die zusätzliche Prozessgrenze deutlich
    wahrscheinlicher. Fix: der Hub merkt sich die **aktive** Marke und löscht nur, wenn
    `gesprochen` zu ihr gehört.
18. **`engine` ehrlich melden**, plus Grund, wenn die Vorgabestufe gewählt wurde, obwohl
    Mimic verfügbar war. Dazu die Korrelations-ID aus Schritt 7.
19. **Sandbox nicht auf Verdacht aufbohren.** Beide Dienste laufen als derselbe Nutzer,
    Mimics Socket ist 0600 — eine `AF_UNIX`-Verbindung braucht DAC-Schreibrecht auf dem
    Socket-Inode, **keinen** beschreibbaren Mount. Zuerst **testen**, ob
    `daimon-tts.service` den Socket unverändert erreicht. Nur falls nicht, eine Ausnahme
    ergänzen — und dann mit `-`-Präfix (`ReadWritePaths=-%t/mimic`), weil ein
    unpräfixierter Pfad die Namespace-Einrichtung scheitern lässt, wenn das Verzeichnis
    fehlt. Das würde ausgerechnet den **Rückfall**-Dienst am Starten hindern.
    Abnahme: `daimon-tts.service` startet sauber, wenn Mimic **gar nicht installiert** ist.
20. **DESIGN §8.2 umschreiben**, Task T−1.12 (Magpie messen) schließen. Siehe ADR 0002.

## Abnahme

**Drei Prüfstände, nicht einer.** Ein Doppel kann weder echte TTFA messen noch belegen,
dass der echte Worker den Hub fragt oder `aussprache:false` befolgt:

| Prüfstand | Wofür |
|---|---|
| **dAImon-Doppel** — zählt Anfragen, erzwingt Absage, Verzögerung, Tod im Stream | Routing, Fristen, Rückfall: P2-A, P2-B, P2-C, P2-E(a), P2-H, P2-I, P2-J |
| **echter Mimic, Stub-Runtime, Fake-Hub** — echter Worker- und Frontendcode, Modell durch steuerbare Attrappe ersetzt | Protokoll, Sperre, Aussprache, **und Abbruch**: P2-E(b), P2-G, **P2-D1, P2-D2**, P2-K, Moduswechsel aus 2b. D1 liest das echte Mimic-Journal, D2 beobachtet Worker-Cancel, `next()` und `emit()` — ein Doppel kann beides nicht. |
| **echtes warmes Modell** | P2-F |
| **echte Runtimes unter `MemoryMax=7G`** — beide Modi, echte cgroup | der Moduswechsel aus 2b und P2-B. Ein Stub kann weder RSS noch die cgroup-Grenze messen; genau dort liegt aber das Problem. |

**Vor dem ersten Lauf eingefroren:** Korpus, Lastzustand, Warmzustand, Perzentil-Methode
und Schwellen. Reißt ein Kriterium, ist das eine **neue Planrevision** — keine nachträgliche
Begründung innerhalb derselben Abnahme.

| # | Kriterium | Bestanden wenn |
|---|---|---|
| P2-A | Auswahl greift | Grenzfälle exakt: **79 Zeichen → sherpa, 80 → Mimic** (bei Schwelle 80), plus ein Lauf mit abweichend konfigurierter Schwelle. Beide Journal-Zeilen vorhanden. |
| P2-B | Kalt wartet nicht | **Gegen den echten Worker aus dem Zustand „Prozess existiert nicht"**, nicht gegen das Doppel — sonst bliebe der Torch-Import vor `submit()` (0.69–0.75 s, Schritt 3) unentdeckt. Vor jedem Lauf wird PID-Abwesenheit und echte Socket-Aktivierung nachgewiesen. Bei kaltem Mimic beginnt Ton in < 400 ms — `require_warm` liefert sofort `cold`, die 500-ms-Gesamtfrist greift gar nicht. `/warm` ist danach angestoßen. n ≥ 20. |
| P2-C | Ausfall unsichtbar, auch langsam | Fünf Fälle, je mit Frist: (a) Dienst gestoppt, (b) Socket da, niemand horcht, (c) `SIGKILL` mitten im Stream, (d) Mimic lebt, verzögert den ersten `A` über die Gesamtfrist, (e) Mimic lebt, stockt im Stream über den Rahmenabstand. In a/b/d spricht dAImon **vollständig** mit sherpa, Ton binnen **800 ms** (Rev 9: die 700 waren Arithmetik ohne Rechnung — 500 ms Mimic-Frist plus gemessene 206 ms sherpa-TTFA passen nicht hinein). In c/e endet der Satz still. In **allen** Fällen wird die nächste Äußerung binnen 800 ms bedient. Je Fall ein maschinenlesbarer Grund. |
| P2-D1 | Abbruch bei laufender Wiedergabe | Neue Äußerung während laufender Mimic-Äußerung: alte Wiedergabe endet in **< 100 ms**, Mimic-Journal zeigt `outcome=cancelled`. |
| P2-D2 | Abbruch **vor** dem ersten Rahmen | Dort gibt es keine Wiedergabe, die enden könnte — also eigene Frist: das Cancel-Flag erreicht den Worker in **< 300 ms**. Gemessen wird **ab beobachtetem Abbruch**: es beginnt **kein neuer `next()`-Aufruf** mehr — auch nicht der eines
**Wiederholungs-Takes** nach einem stummen (`worker.py` startet den nächsten Generator
heute ohne erneute Cancel-Prüfung; ein Testfall erzwingt einen stummen ersten Take) — ein bereits laufender darf zurückkehren und sein Chunk wird **verworfen**, `emit()` gelingt nicht mehr, und gepufferte Rahmen erreichen den Konsumenten nicht. „Keine Yields ab gesetztem Flag" wäre nicht erfüllbar — setzt ein anderer Thread das Flag, während der Eigentümer in `next()` steckt, liefert dieser Aufruf noch einen Chunk. „Keine Rahmen mehr" allein wäre mehrdeutig — ein Rahmen kann vor dem Abbruch im Puffer liegen und erst danach beobachtet werden. Das ist das Loch aus Schritt 5. |
| P2-E | Hub unumgehbar, in beide Richtungen | (a) Vom Hub abgelehnter Text: der Doppel zählt **null** Anfragen. (b) Freigegebener Text plus eine Aussprache-Regel, die ihn semantisch verändert und den Zeichenfilter passiert: dAImon spricht trotzdem **exakt** den Hub-Text. (b) ist der Test, der Schritt 2 absichert — (a) allein prüft ihn nicht. |
| P2-F | TTFA im Budget | **Rev 9: ausgesetzt, nicht erfüllt und nicht ersetzt.** Die 300 ms sind widerlegt, eine neue Schwelle ist heute nicht setzbar — drei Läufe mit gleichem Code und gleichem Korpus lieferten binnen zwanzig Minuten Median 231, 367 und 465 ms. Was feststeht, ist die **Form**: gemessen wird auf dem Korpus ab 80 Zeichen, warm, n ≥ 40, mit zwei Zahlen — **(a)** Median bis zum ersten **hörbaren** Sample und **(b)** Anteil der ersten Rahmen innerhalb der 500-ms-Frist aus Schritt 13. Das Instrument liefert beide (`tools/messreihe_ttfa.py --ab-zeichen 80`). Die Schwellen werden aus dem ersten Lauf auf einer **ruhigen** Maschine festgelegt und dann eingefroren. Bis dahin gilt P2-F als offen. |
| P2-G | Sperre wirkt und gibt frei | (a) Hub verweigert → Mimic lädt **nicht**, meldet `load_denied` mit `hub_reason`, je einmal für `vram`, `fullscreen`, `lade_sperre`. (b) Hub nicht erreichbar → Mimic lädt (fail open). (c) **Erfolgreiches Laden → `fertig` bestätigt → ein zweiter Lader bekommt sofort eine neue Sperre**, nicht `lade_sperre`. Ohne (c) bliebe eine kaputte Freigabe unentdeckt, bis die 120-s-Frist sie zudeckt. (d) **Kaputte Hub-Antwort** — leer, ungültiges JSON, schemafremd: jeweils **kein** Ladeversuch, eigener Diagnosegrund. Schritt 1 verlangt hier fail-closed; ohne (d) wäre das unabgenommen. |
| P2-H | Marke ist generationssicher | Deterministischer A/B-Lauf: A wird abgebrochen, B meldet `beginnt`, **dann** trifft As verspätetes `gesprochen` ein. Erwartung: B bleibt aktive Marke, `tts_active` bleibt `true`. Ohne diesen Test kann eine Umsetzung ein Feld hinzufügen, ohne das falsche Löschen zu verhindern. |
| P2-L | Stumme Takes brechen die Engine-Wahl nicht | Zwei deterministische Stub-Fälle: (a) erster Take stumm, zweiter hörbar → der Konsument bekommt den ersten `A` **erst** mit hörbarem Audio, und der Satzanfang fehlt nicht; (b) beide Takes stumm → **kein** `A`, Fehlschlag **vor** HTTP 200. Dazu eine Obergrenze für die Zeit bis zum ersten hörbaren Sample. Ohne (a)/(b) ist Schritt 2a behauptet, nicht belegt. |
| P2-M | GUI-Stopp greift in jeder Phase | Stopp (a) vor `getresponse()`, (b) während blockiertem `read_frame()`, (c) unmittelbar **vor** Veröffentlichung der Sitzung — im letzten Fall darf der Synthesethread danach **keinen** neuen Socket mehr öffnen. Je Fall: Socket geschlossen, Worker-Cancel binnen Frist, kein übriger Thread. D1/D2 prüfen dAImon, nicht die GUI. |
| P2-K | Warmlauf wirkt wirklich | Zwei getrennte Zweige, nicht einer: **Erfolg** — kalt → sherpa spricht fristgerecht → `/warm` wird binnen 30 s `warm` → die **nächste** lange Äußerung ist `engine=mimic`. **Fehlschlag** — `/warm` scheitert sichtbar → die nächste Äußerung geht fristgerecht an sherpa, mit korreliertem Diagnosegrund. (Rev 5 verlangte in beiden Fällen `engine=mimic`, was nach einem Fehlschlag gerade nicht zugesagt ist.) Gegenfall: ein `/warm`, das nie antwortet, verändert die sherpa-TTFA **nicht** und erzeugt höchstens **einen** Hintergrundaufruf. P2-B beweist nur, dass ein Wunsch angestoßen wurde — ein Eigentümer, der den Condition-Wakeup verliert, käme damit durch. |
| P2-J | Hängendes Mimic blockiert den Start nicht | `/status` wird angenommen, aber nie beantwortet. Erwartung: dAImons Startprüfung endet binnen 300 ms, der Mimic-Pfad wird deaktiviert und protokolliert, `daimon-tts` startet fertig, und sherpa spricht danach in seinem Budget. Die Sandbox-Abnahme in Schritt 19 deckt nur „Mimic nicht installiert" — der schlimmere Fall ist ein Dienst, der da ist und schweigt. |
| P2-I | Terminalpfade räumen auf | Nach **jedem** Ende — `E:error`, EOF, Rahmenfrist, lokaler Abbruch, Erfolg — gilt: keine offene Mimic-Sitzung, kein verwaister `pw-cat`, kein laufender Leser-Thread, und **die beendete Generation besitzt keine aktive Marke mehr**. `tts_active=false` nur dann, wenn **keine neuere Generation spricht** — sonst widerspräche P2-I dem Fall aus P2-H, wo B gerade redet, während A verspätet endet. Abschluss idempotent und generationsgebunden. |

P2-C, P2-D1, P2-D2, P2-G, P2-H und P2-I tragen Invariante α, die Serialisierung und die
Aufräumzusage. Ohne sie keine Abnahme.

**Korrelations-ID:** ist sie vorhanden, muss sie 32-stelliges Hex sein — sonst
abgelehnt. Fehlt sie, erzeugt das Frontend eine (Rückwärtskompatibilität für CLI und GUI,
Schritt 2d). Ein
freier String könnte über Leerzeichen oder Zeilenumbruch die `key=value`-Journalzeilen
zerlegen. Jedem angenommenen Warmwunsch wird genau eine ID zugeordnet.


## Rev 9 — P2-F, nachdem die Verteilung bekannt war (2026-08-09)

Der Plan hat 300 ms festgeschrieben, bevor jemand die Verteilung kannte. Jetzt ist
sie gemessen, und sie sieht anders aus als angenommen.

**Was gemessen wurde.** `erstchunk_ms` — die Zeit bis zum ersten Chunk überhaupt —
liegt in **jedem** Lauf bei 180–205 ms. Die Rechenleistung streut nicht. Die ganze
Streuung sitzt in stiller Vorlaufzeit: dots.tts stellt der Äußerung stochastisch
0 bis 16 Chunks à ~154 ms Stille voran. Zehn Hypothesen wurden dafür geprüft und
widerlegt, darunter Umgebung, GPU-Takt, Nebenläufigkeit und ein zweiter GPU-Mieter;
der Bisect, der den Code zu entlasten schien, hatte dreimal dieselbe eingefrorene
Kopie gemessen. Details in `spike/ERGEBNIS.md`, Nachtrag 2026-08-08.

**Auf dem Korpus, den Mimic wirklich bekommt** (≥ 80 Zeichen, warm, n = 40, Code
mit verworfenem Stillepräfix):

| | Median | p95 | max |
|---|---|---|---|
| Ankunft des ersten Rahmens | 201 ms | 456 ms | 538 ms |
| bis zum ersten hörbaren Sample | 231 ms | 758 ms | 769 ms |

**Warum das Kriterium und nicht die Zahl falsch war.** Ein p95 über *alle*
Äußerungen misst zwei verschiedene Dinge in einem Wert: die Pipeline, die stabil
ist, und die Laune des Modells, die es nicht ist. Und seit Schritt 13 hat der
Schwanz für den Nutzer eine andere Folge als angenommen — er hört keine späte
Äußerung, sondern eine, die sherpa spricht. Die Latenzzusage trägt P2-C, nicht
P2-F. Was P2-F noch zu sichern hat, ist nicht „schnell", sondern **wie oft
Mimics Stimme überhaupt zum Zug kommt**.

Deshalb zwei Zahlen statt einer: ein Median, der die Pipeline bewacht, und ein
Anteil, der die Stimme bewacht. Beide werden auf dem Korpus gemessen, der real
ankommt — kurze Sätze gehen unter der Auswahlregel ohnehin an die Vorgabestufe,
und sie in die Zahl zu mischen hat die alte Messung mit verzerrt.

**Warum trotzdem keine neue Schwelle in diesem Dokument steht.** Drei Läufe,
gleicher Code, gleicher Korpus, innerhalb von zwanzig Minuten:

| Lauf | Median hörbar | Anteil Ankunft < 500 ms | Fremdlast |
|---|---|---|---|
| 1 | 231 ms | ≥ 95 % | zwei Worker, Load 6.6 |
| 2 | 367 ms | 90 % | zwei Worker, Load 6.6 |
| 3 | **465 ms** | **75 %** | ein Worker, Load 4.1 |

Der Lauf mit **weniger** Last war der schlechteste. Eine Schwelle, die aus dieser
Reihe stammt, wäre geraten und nicht gemessen — und der Plan sagt an dieser
Stelle selbst: eingefroren wird **vor** dem ersten Lauf, nicht danach passend
gemacht. Die Reihe gehört auf eine Maschine ohne laufende Agentensitzung; dieselbe
Bedingung blockiert schon die Warmrampe aus Schritt 4a, deren Werkzeug bei Load
über 2.5 von sich aus abbricht.

**Was damit nicht behauptet wird.** Die Stille ist nicht behoben, sie ist
eingeordnet. Verworfen, weil gemessen schlechter: die Referenz-Stille abschneiden
(Median 609 statt 242 ms) und `language="de"` statt `en` (p95 1046 statt 858 ms).
Ein Eingriff in dots.tts bleibt der einzige bekannte Weg zu einem kleinen p95 —
und der ist nicht Teil dieses Plans.

**Offen und bewusst nicht entschieden:** ob 90 % der richtige Anteil sind. Der
Messwert liegt bei ≥ 95 %; die Zusage steht tiefer, damit sie nicht bei jedem
Hintergrund-Build reißt. Nach zwei Wochen Alltag gehört sie nachgezogen —
zusammen mit den 80 Zeichen, die aus demselben Grund ein Startwert sind.

## Risks / open questions

- **B2 bleibt die schwächste Stelle.** Dieser Plan liefert die Alltagsvalidierung, ersetzt
  sie nicht.
- **Betonung einzelner Wörter** bleibt (Kriterium B). Die Tabelle deckt Bekanntes, nicht
  Neues — und wird durch Schritt 2 enger.
- **80 Zeichen sind geraten.** Nach zwei Wochen Alltag nachmessen.
- **Zwei Zerleger** mit getrennten Konstanten (dAImon 12, Mimic 20). Absichtlich getrennt,
  aber eine Falle für den, der sie verwechselt.
- **Offen:** ob der Warmlauf dieselbe Hub-Erlaubnis braucht wie ein echtes Laden. Antwort
  in diesem Plan: **ja** — jeder Ladevorgang geht durch die Sperre, auch der Warmlauf.
  Damit ist Schritt 1 Voraussetzung für Schritt 4, nicht nebenläufig.

## Out of scope

- **MMC-Vertonung** — am 2026-08-05 geparkt. Vorarbeit vorhanden: Erzählertext existiert
  als `BIOME_<n>_NAME`/`_SUBTITLE`/`_DESC` (10 Biome), Aufhänger `arena_main.gd:165`,
  Sprachentscheidung getroffen (nur `de`/`en` vertonen, drei Sprachen bleiben still).
- **Die Charakterstimmen** — Bestand ohne Auftrag.
- Feintuning gegen das Betonungsproblem.
- Ein GPU-Koordinator über Fremdsoftware wie ComfyUI. Die **vorhandene** Hub-Sperre zu
  reparieren ist ausdrücklich **in** scope (Schritt 1) — sie ist kein neuer Koordinator.
