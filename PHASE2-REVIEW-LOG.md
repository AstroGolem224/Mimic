# Plan Review Log: Mimic Phase 2

Act 1 (grill-with-docs) abgeschlossen — PHASE2.md geschrieben, CONTEXT.md um
`Auswahlregel` und `Warmlauf` ergänzt. MAX_ROUNDS=5.

Entschieden im Interview: Abbruch mitten im Stream endet still (kein Wechsel, keine
Wiederholung) · Mimic zerlegt, dAImon schickt den ganzen Text · Auswahlregel
konfigurierbar mit konservativer Vorgabe (≥80 Zeichen und nur warm) · dAImon-Stimme
konfigurierbar, Vorgabe `matthias` · MMC geparkt · Charakterstimmen sind Bestand ohne
Auftrag.

## Round 1 — Codex (gpt-5.6-sol, read-only)

The fixed-rate premise in Step 5 is correct: `_ausgeben()` opens one `pw-cat` per utterance with a fixed `--rate`. The proposed lifecycle around that fact is not correct.

1. **Generation is claimed too late.** Today `_gen` advances only when `_ausgeben()` starts ([tts.py:489](</home/itiger013/Dokumente/Github/dAImon/daimon/face/tts.py:489>)); while a Mimic request waits for its first `A` frame, `still` can increment `_gen`, return successfully, and the older call can subsequently invoke `_ausgeben()` and resurrect cancelled speech.  
Fix: After Hub approval, atomically abort the current generation and reserve a new generation before contacting Mimic; pass that generation into both Mimic and sherpa playback without incrementing it again.

2. **Two concurrent requests can defeat fallback ordering.** If A and B both wait on Mimic, B must cancel A; otherwise A can time out later and start sherpa over B, or both can start separate `pw-cat` processes. Step 5 specifies no generation checks around preflight, fallback, or player registration.  
Fix: Require every post-Hub step—including fallback—to check its reserved generation under `_lock`, and allow only the current generation to register a connection or player.

3. **Step 8’s connection is not reachable from `abbrechen()` as designed.** The HTTP response and connection naturally live in the individual `sprich()` handler frame, while `abbrechen()` only knows `_wiedergabe` ([tts.py:398](</home/itiger013/Dokumente/Github/dAImon/daimon/face/tts.py:398>)); merely saying “close the connection” does not define shared ownership or prevent an old thread from clearing a newer connection.  
Fix: Store a generation-tagged cancellable Mimic session under `_lock`, detach it atomically in `abbrechen()`, and clear it only if both identity and generation still match.

4. **`close()` alone is not a reliable cross-thread interrupt.** Closing an FD in one thread need not wake another thread already blocked in `recv()`; cancellation can remain stuck until data or timeout arrives.  
Fix: Make cancellation call `shutdown(SHUT_RDWR)` followed by `close()` outside `_lock`, with bounded waits and idempotent cleanup.

5. **Pre-first-frame cancellation currently does not reach the worker.** Mimic’s frontend blocks reading worker frames until the first `A` before it sends HTTP 200 ([frontend.py:174](</home/itiger013/Dokumente/Github/Mimic/mimic/frontend.py:174>)); it does not inspect the consumer socket until afterward ([frontend.py:214](</home/itiger013/Dokumente/Github/Mimic/mimic/frontend.py:214>)). Closing dAImon’s connection during a cold load, queue wait, or slow first frame therefore leaves the worker running and occupying the single-owner queue.  
Fix: Change the frontend to monitor consumer disconnect and worker readiness concurrently before HTTP 200, closing the worker connection immediately when the consumer disappears.

6. **`tts.mimic_nur_warm` cannot safely be implemented by `GET /status`.** Status is a stale snapshot, does not wake the worker, and reports “warm” when any runtime exists; `mode` is merely the last mode, not the set of loaded runtimes ([worker.py:269](</home/itiger013/Dokumente/Github/Mimic/mimic/worker.py:269>)). A warm `soar` worker or a worker exiting at the idle boundary can therefore make dAImon issue an `mf` request that cold-loads for 7 seconds.  
Fix: Add an atomic `require_warm=true` option to `/speak` that the single owner checks specifically for `mf`, returning immediately if that runtime is absent.

7. **`POST /warm` has contradictory response semantics.** It cannot both return `202` immediately and return `insufficient_vram` discovered later when the queued owner reaches `_load()`; checking CUDA from a frontend or handler thread would also violate the single-owner design.  
Fix: Define `202` as “deduplicated warm request accepted” with asynchronous failure in status/journal, or wait for the owner’s gate decision before returning—do not promise both.

8. **A normal queued warm job can consume capacity and delay real speech.** The queue holds only four entries ([worker.py:69](</home/itiger013/Dokumente/Github/Mimic/mimic/worker.py:69>)); frontend-level deduplication is racy across request threads, and a warm job still occupies a slot even if given low priority.  
Fix: Represent warming as an owner-protected, per-mode best-effort pending flag serviced only when the real-request queue is empty, with atomic deduplication inside `Engine`.

9. **The plan depends on a GPU gate that is currently not wired to dAImon’s protocol.** Mimic sends `{"action":"request","client":"mimic"}` and later raw `fertig` ([worker.py:334](</home/itiger013/Dokumente/Github/Mimic/mimic/worker.py:334>)); the Hub accepts only `{"art":"laden",...}` and releases with `{"art":"fertig","sperre":...}` ([daemon.py:375](</home/itiger013/Dokumente/Github/dAImon/daimon/hub/daemon.py:375>)). Every attempt therefore fails open, so neither `/speak` nor `/warm` participates in §5.4 serialization.  
Fix: Make Phase 2 explicitly repair and test the real Hub protocol, including tokenized release and the intended standalone fail-open behavior.

10. **The “Hub contract unchanged” claim is false after pronunciation rewriting.** Mimic applies arbitrary free-text replacements after Hub approval ([voices.py:175](</home/itiger013/Dokumente/Github/Mimic/mimic/voices.py:175>)); a benign approved word can be replaced with a URL, secret-like assignment, path, or entirely different sentence. That changes content, not merely sound.  
Fix: Either synthesize the exact Hub-returned text without free-text substitution, use non-text phoneme hints, or send the transformed text through the Hub and synthesize only the resulting approved string.

11. **Hub activity state is not generation-safe.** `_tts_gesprochen()` unconditionally clears the global `tts_active` boolean for any still-valid mark ([daemon.py:637](</home/itiger013/Dokumente/Github/dAImon/daimon/hub/daemon.py:637>)); a delayed completion from cancelled Mimic request A can clear the interlock after request B has already called `beginnt`. The extra process boundary makes that ordering substantially more likely.  
Fix: Track the active TTS mark/generation in the Hub and clear `tts_active` only when `gesprochen` matches the currently active mark.

12. **The sherpa failure state is incorrectly global.** `sprich()` returns immediately on `self.absage` before consulting the Hub or selecting an engine ([tts.py:427](</home/itiger013/Dokumente/Github/dAImon/daimon/face/tts.py:427>)); inserting Mimic later in the method means a missing sherpa profile disables Mimic too.  
Fix: Treat sherpa readiness as an engine-specific fallback result and perform Hub approval plus engine selection before applying sherpa-only refusal state.

13. **The proposed socket default is not automatically expandable.** `%t` is a systemd unit specifier, not a TOML or Python pathname expansion; unless the new client explicitly implements it, `%t/mimic/mimic.socket` is a literal relative path.  
Fix: Derive the default from `XDG_RUNTIME_DIR`/`Config.runtime_dir.parent`, or explicitly specify and test `%t` expansion in the client.

14. **Step 11 can make the mandatory fallback service fail to start.** Connecting to an AF_UNIX socket requires DAC write permission on the socket inode, not a writable filesystem mount; both services already run as the same user and Mimic’s socket is `0600`. Worse, unprefixed `ReadWritePaths=%t/mimic` fails namespace setup when that optional directory does not exist—the exact class of failure already documented in dAImon’s socket unit ([daimon-tts.socket:29](</home/itiger013/Dokumente/Github/dAImon/config/systemd/daimon-tts.socket:29>)).  
Fix: Do not add `ReadWritePaths` merely to connect; if testing proves a namespace exception necessary, use an optional `-` path and verify startup with Mimic entirely absent.

15. **`GET /status` voice discovery is underspecified.** The plan does not define whether `voices` contains directory names or profiles that pass `load_voice()`, nor what dAImon does after a failed startup check; listing an invalid profile as available creates a false readiness signal.  
Fix: Define a versioned response schema containing only fully validated profiles—or per-profile validity reasons—and specify that check failure logs and disables only the Mimic path.

16. **Fallback deadlines are absent.** “Client knows the deadlines” and “no wait above the deadline” are not testable without separate connect, header, first-frame, and inter-frame values; inheriting Mimic’s 5/90/10-second limits would violate the promised immediate fallback.  
Fix: Put explicit small dAImon deadlines in the plan/config and require every timeout path to close the session and re-check generation before falling back.

17. **Cross-process failures will be hard to correlate.** The Mimic request ID is created only after dequeue ([worker.py:117](</home/itiger013/Dokumente/Github/Mimic/mimic/worker.py:117>)), while Step 10 names neither stable journal fields nor a client-generated correlation ID; pre-stream failures and warm attempts therefore cannot be joined across dAImon, frontend, and worker logs.  
Fix: Generate a correlation ID in dAImon, propagate it through frontend and worker, and log generation, engine, stage, fallback reason, Mimic request ID, and terminal outcome in every process.

18. **P2-A through P2-E are only partly falsifiable.** P2-B leaves “Toleranz” and warm-completion bounds undefined; P2-C says “normal” and “next” without deadlines; P2-D says “sofort hörbar” without milliseconds; P2-E relies on absence from the worker journal, which cannot prove the frontend was never contacted.  
Fix: Provide exact inputs, boundary cases, clocks, sample sizes, deadlines, expected fields, and a spy/stub Mimic endpoint that directly counts forbidden contacts.

19. **P2-F’s 400 ms threshold is invented and its rationale is factually wrong.** The measured 250 ms already includes the public Mimic frontend, Unix socket, framing, and client-side first-frame observation ([ERGEBNIS.md:33](</home/itiger013/Dokumente/Github/Mimic/spike/ERGEBNIS.md:33>)); only Hub coordination plus `pw-cat` startup/write are new, and no measurement justifies granting them 150 ms.  
Fix: Measure those incremental costs first against a fixed corpus and environment, then retain the existing 300 ms budget or document an evidence-based replacement with an explicit percentile method.

20. **An alleged out-of-scope dependency remains unresolved.** The plan leaves Hub permission for `/warm` open while simultaneously depending on §5.4’s load gate and declaring a broader GPU coordinator out of scope; implementation cannot be accepted with ownership of that decision undecided.  
Fix: Decide in the plan that all Mimic model loads, including warm loads, use the existing optional Hub gate when available, and include its protocol repair and tests in scope.

VERDICT: REVISE
### Claude's response — Round 1

Zwei Findings vor der Annahme am Code verifiziert, beide bestaetigt, beide sind
Defekte im LAUFENDEN Code und nicht blosse Planungsmaengel:

- **#9 Die GPU-Sperre hat nie funktioniert.** Hub erwartet {"art":"laden","vram_mib":N}
  und {"art":"fertig","sperre":<token>}; Mimic sendet {"action":"request",...} und rohes
  "fertig\n". Der Hub antwortet unbekannte_art, Mimic prueft nur OB eine Antwort kam --
  und laedt, obwohl der Hub nein gesagt hat. Codex hatte das Protokoll im Build-Bericht
  als "not specified" markiert; ich habe es gelesen und nicht gegengeprueft.
- **#10 Die Aussprache-Tabelle umgeht den Hub-Validator.** apply_pronunciation ersetzt
  beliebigen Text NACH der Freigabe. DESIGN 8.3 setzt den Validator bewusst in den Hub,
  "sonst waere er umgehbar" -- genau dahinter steht er jetzt.

Beide werden zu einer eigenen Phase 2a vorgezogen: der Plan darf nicht auf Kaputtem
aufsetzen. Bei #9 zusaetzlich die drei Faelle getrennt (Hub weg -> fail open, Hub sagt
nein -> abbrechen, Antwort unverstaendlich -> laut scheitern); bisher war alles ein
stilles fail open. Bei #10 zwei Riegel: enges Muster fuer Ersetzungen beim Laden, plus
Abschaltbarkeit je Anfrage.

Angenommen und eingearbeitet: 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 16, 17, 18,
19, 20 -- also praktisch alles. Die wichtigsten Umbauten:

- Generationssicherheit ueber die neue Wartezeit (1-4): eine Generation nach der
  Freigabe reservieren, sie fuer Mimic UND sherpa verwenden, jeder Schritt prueft sie
  unter _lock. Abbruch mit shutdown(SHUT_RDWR) vor close(), weil close() allein einen in
  recv() haengenden Thread nicht zuverlaessig weckt.
- require_warm auf /speak statt Warm-Entscheidung ueber /status (6): Status ist ein
  Schnappschuss und meldet "warm", sobald irgendein Modus geladen ist -- ein warmer
  soar-Worker haette dAImon in einen 7-Sekunden-Kaltstart fuer mf laufen lassen.
- /warm wird ein Wunsch im Engine-Zustand statt eines Auftrags in der Warteschlange
  (7, 8), sonst belegt er einen der vier Plaetze gegen echte Sprache.
- Eigene kurze Fristen fuer dAImon (16). Mimics 5/90/10 s zu erben haette bedeutet,
  90 s zu warten statt zurueckzufallen -- das Gegenteil des Zwecks.
- self.absage (12): ein fehlendes sherpa-Profil haette Mimic mit abgeschaltet.
- Sandbox (14): nicht auf Verdacht aufbohren, erst testen. Und falls doch, mit
  '-'-Praefix -- ein unpraefixiertes ReadWritePaths auf ein fehlendes Verzeichnis
  haette ausgerechnet den Rueckfall-Dienst am Starten gehindert.

**Zu #19 hatte Codex sachlich recht und ich unrecht:** ich hatte 400 ms mit "dAImons
Pfad zum Socket kommt dazu" begruendet. Die 250 ms enthalten Frontend, Socket und
Rahmung bereits -- neu ist nur die Hub-Runde und der pw-cat-Start. Budget bleibt bei
300 ms; reicht es nicht, wird der Aufschlag gemessen und begruendet, nicht geschaetzt.

Abnahme umgebaut (18): alle Kriterien laufen gegen ein Mimic-DOPPEL, das Anfragen zaehlt
und Antworten erzwingt. P2-E war vorher nicht falsifizierbar -- Abwesenheit im
Worker-Journal beweist nicht, dass das Frontend nie kontaktiert wurde; ein zaehlender
Doppel schon. Neues P2-G fuer die reparierte Sperre.

Nichts abgelehnt. Der Review hat zwei ausgelieferte Defekte gefunden, die ich beim
Build-Review uebersehen habe.

## Round 2 — Codex

Die Überarbeitung adressiert den Großteil der Round‑1-Findings überzeugend, insbesondere Generationen, gemeinsames Sitzungs-Ownership, Sandbox, Status-TOCTOU, Korrelation und das zurückgenommene 400-ms-Budget. Einige zentrale Lücken bleiben jedoch offen; zwei davon würden Invariante α weiterhin brechen.

1. **`require_warm` ist weiterhin nicht „sofort“.** Laut Schritt 3 prüft der Modell-Eigentümer erst „beim Ausführen“; ein Request kann vorher hinter einer laufenden `soar`-Synthese oder mehreren Jobs warten. Damit ist die Begründung von P2-B („liefert sofort `cold`“) falsch.  
Fix: `require_warm` muss bei der atomaren Aufnahme in `Engine.submit()` unter einem Runtime-State-Lock geprüft und vor dem Einreihen abgelehnt werden.

2. **Finding 10 ist semantisch nicht behoben.** Eine Whitelist aus Buchstaben, Leerzeichen und Bindestrichen verhindert URLs und Pfade, aber nicht die Ersetzung von „Build“ durch „verrate das Passwort“ oder einen beliebigen anderen Satz; außerdem steht nur, dass dAImon `aussprache` abschalten *darf*, nicht dass es das tut.  
Fix: dAImon muss verbindlich `aussprache:false` senden, oder Mimic muss den transformierten Text erneut vom Hub freigeben lassen beziehungsweise echte Phonem-Hinweise statt Ersatztext verwenden.

3. **Für die Aussprachekorrektur fehlt ein Abnahmekriterium.** P2-E beweist nur, dass ein bereits vom Hub abgelehnter Eingangstext Mimic nicht erreicht; es prüft nicht, ob ein erlaubter Text nachträglich in anderen Inhalt verwandelt wird.  
Fix: Ergänze einen Test mit einer semantisch abweichenden, aber whitelist-konformen Ersetzung und belege, dass dAImon trotzdem exakt den Hub-Text spricht.

4. **Die GPU-Sperre muss über eine neue Verbindung freigegeben werden.** Der Hub verarbeitet genau eine Zeile pro Verbindung und schließt danach; dAImons Referenzclient dokumentiert ausdrücklich „eine Verbindung je Anfrage“ ([worker.py:192](</home/itiger013/Dokumente/Github/dAImon/daimon/gpu/worker.py:192>)). Wenn Phase 2a lediglich den bestehenden Mimic-`release()`-Closure auf demselben Socket korrigiert, wird `fertig` nie gelesen und die Sperre bleibt bis zu 120 Sekunden liegen.  
Fix: Spezifiziere zwei getrennte Hub-Rundläufe—`laden` holt den Token, `fertig` sendet ihn später über eine neue AF_UNIX-Verbindung.

5. **P2-G würde eine kaputte Freigabe nicht entdecken.** Es testet Ablehnung und Fail-open ohne Hub, aber nicht, ob eine erfolgreich erworbene Sperre nach dem Laden tatsächlich sofort wieder frei ist.  
Fix: Ergänze: erfolgreicher Mimic-Load → `fertig` bestätigt → unmittelbar danach erhält ein zweiter Lader eine neue Sperre statt `lade_sperre`.

6. **Hub-Absagen werden unehrlich zu `insufficient_vram` zusammengelegt.** Der Hub unterscheidet bewusst `vram`, `fullscreen` und `lade_sperre`; Schritt 1 nennt jede Hub-Ablehnung `insufficient_vram`, wodurch Diagnose und bestehende Taxonomie verloren gehen.  
Fix: Verwende einen stabilen äußeren Grund wie `load_denied` mit maschinenlesbarem `hub_reason`, oder mappe jeden Hub-Grund separat und teste alle drei.

7. **Der Warm-Wunsch weckt den blockierten Eigentümer nicht.** Der heutige Owner hängt in `jobs.get()` ([worker.py:100](</home/itiger013/Dokumente/Github/Mimic/mimic/worker.py:100>)); nur einen Flag im Engine-Zustand zu setzen führt daher bei leerer Queue zu keinem Warmlauf.  
Fix: Ersetze das blockierende Queue-Warten durch eine gemeinsame `Condition` für Jobs und Warm-Wünsche oder definiere einen expliziten, kapazitätsfreien Wake-up-Mechanismus.

8. **Ein Warmlauf kann weiterhin echte, bereits warme Sprache blockieren.** Beginnt beispielsweise ein `soar`-Warmlauf kurz vor einem warmen `mf`-Request, hält der ununterbrechbare Modellaufbau den einzigen Owner mehrere Sekunden fest; „wenn die Queue leer ist“ schließt dieses Rennen nicht.  
Fix: Beschränke Runtime-Warmups auf den dAImon-Modus `mf` oder definiere und teste eine Ruhefrist beziehungsweise faire Übergaberegel vor Beginn eines nicht unterbrechbaren Warmloads.

9. **Die Fristen missverstehen Mimics öffentliches HTTP-Verhalten.** Das Frontend sendet den HTTP-200-Kopf erst, nachdem es das erste `A` gepuffert hat ([frontend.py:174](</home/itiger013/Dokumente/Github/Mimic/mimic/frontend.py:174>), [frontend.py:202](</home/itiger013/Dokumente/Github/Mimic/mimic/frontend.py:202>)); damit begrenzt bereits der 300-ms-Antwortkopf den TTFA, während die zusätzliche 500-ms-Frist normalerweise erst nach bereits erzeugtem Audio beginnt. Die drei Fristen sind außerdem sequenziell und kein gemeinsames End-to-End-Budget.  
Fix: Definiere eine monotone Gesamtd deadline vom Connect-Beginn bis zum ersten `A` und separate nur den 2-s-Interframe-Timeout nach Streambeginn.

10. **`select` auf dem Worker-Socket ist mit `HTTPResponse`-Pufferung nicht hinreichend spezifiziert.** `getresponse()`/`read_frame()` können Bodydaten bereits im Python-Puffer halten, obwohl der rohe Socket für `select` nicht mehr lesbar erscheint; eine naive Umsetzung kann dann trotz vorhandenem `H/A` hängen.  
Fix: Verlange einen dedizierten Reader-Thread/Event oder einen vollständig nichtblockierenden HTTP-/Frame-Parser, der gepufferte Bytes vor `select` berücksichtigt.

11. **Terminale Stream-Pfade sind noch nicht vollständig definiert.** Bei `E:error`, EOF, Frame-Timeout und lokalem Abbruch müssen Mimic-Sitzung, `pw-cat`, Pipe, Generationsslot und Hub-Marke jeweils genau einmal beendet werden; P2-C/P2-D prüfen weder verwaiste Prozesse noch `tts_active=false` nach jedem Pfad.  
Fix: Definiere einen idempotenten generationengebundenen Abschluss und teste nach jedem Terminalfall: keine Sitzung, kein `pw-cat`, kein Reader-Thread und Hub `tts_active=false`.

12. **Die aktive Hub-Marke ist beschrieben, aber ihr eigentliches Rennen nicht abgenommen.** Ein Test muss A verzögert `gesprochen` melden lassen, nachdem B bereits `beginnt` gemeldet hat; sonst kann eine Implementierung nur ein Feld hinzufügen, ohne das falsche Löschen wirksam zu verhindern.  
Fix: Ergänze einen deterministischen A/B-Test, der nach A→`gesprochen` weiterhin B als aktive Marke und `tts_active=true` erwartet.

13. **Die Korrelations-ID braucht ein Schema.** Eine frei angenommene ID kann über Leerzeichen oder Zeilenumbrüche die heutigen `key=value`-Journalzeilen beschädigen und bleibt bei deduplizierten Warm-Wünschen mehrdeutig.  
Fix: Lege Länge und Syntax fest, etwa eine kanonische UUID/Hex-ID, lehne andere Werte im Frontend ab und ordne jedem Warm-Wunsch genau eine akzeptierte ID zu.

14. **P2-A testet die eigentliche Grenze nicht.** Bei „ab 80 Zeichen“ sind 79 und 80 die Grenzfälle; 81 lässt einen Off-by-one-Fehler unentdeckt.  
Fix: Prüfe mindestens 79→sherpa, 80→Mimic und eine konfigurationsabweichende Schwelle.

15. **P2-C bleibt zeitlich unfalsifizierbar und deckt „zu langsam“ nicht ab.** „Die nächste Äußerung wird wieder bedient“ hat weder Frist noch erwartete Engine; ein lebender Mimic, der Kopf oder erstes Audio verzögert, kommt in keinem Kriterium vor, obwohl das Goal diesen Fall ausdrücklich nennt.  
Fix: Ergänze erzwungene Header-/First-A-/Interframe-Timeouts und jeweils eine maximale Zeit bis sherpa beziehungsweise bis zur nächsten erfolgreichen Äußerung.

16. **P2-F ist weiterhin beweglich.** „Reicht es nicht, wird der Aufschlag gemessen und begründet“ erlaubt, das Abnahmekriterium nach einem Fehlschlag umzudefinieren; außerdem ist der „feste Korpus“ nicht benannt.  
Fix: Friere Korpus, Lastzustand, Warmzustand, p95-Berechnung und 300-ms-Grenze vor dem Lauf ein; ein Fehlschlag verlangt eine neue Planrevision, nicht eine nachträgliche Begründung innerhalb derselben Abnahme.

VERDICT: REVISE
### Claude's response — Round 2

Alle sechzehn angenommen. Zwei vor der Annahme am Code verifiziert:

- **#4** dAImons Referenzclient sagt woertlich "Eine Verbindung je Anfrage, nicht eine
  gehaltene" (daimon/gpu/worker.py). Mein Fix haette den Token auf demselben Socket
  nachgeschickt -- fertig waere nie gelesen worden und die Sperre 120 s liegengeblieben.
  Jetzt zwei getrennte Rundlaeufe.
- **#6** Der Hub unterscheidet vram, fullscreen und lade_sperre. Ich hatte alles zu
  insufficient_vram eingeebnet. Jetzt load_denied mit hub_reason, alle drei getestet.

Vier Umdreher gegenueber Rev 2:

1. **Aussprache auf dem dAImon-Pfad ganz aus** (#2, #3). Mein Zeichenfilter war die
   falsche Antwort: "Buchstaben und Leerzeichen" ergibt immer noch einen anderen Satz.
   Der Filter schliesst gefaehrliche Zeichen, nicht Bedeutungsaenderung. dAImon sendet
   jetzt immer aussprache:false und Mimic spricht exakt den Hub-Text. Preis offen
   benannt: "ge-mer-get" statt "gemoerdscht" -- der Validator wiegt schwerer.
2. **Eine Gesamtfrist statt dreier** (#9). Ich hatte uebersehen, dass Mimics Frontend den
   200er-Kopf erst NACH dem ersten A-Rahmen sendet. Kopf und erstes Audio sind dasselbe
   Ereignis; meine separate 500-ms-Frist danach waere wirkungslos gewesen, und drei
   sequenzielle Fristen summieren sich statt zu begrenzen. Jetzt 500 ms monoton bis zum
   ersten A, danach 2 s Rahmenabstand.
3. **Warmlauf braucht eine Condition, kein Flag** (#7), und laeuft **nur fuer mf** (#8).
   Der Eigentuemer haengt in jobs.get() -- ein Zustandsflag weckt ihn nie. Und ein
   nicht unterbrechbarer soar-Warmlauf haette eine warme mf-Anfrage sekundenlang
   blockiert.
4. **Kein select auf den nackten Socket** (#10). getresponse()/read_frame() puffern in
   Python; der rohe Socket sieht dann leer aus, obwohl ein Rahmen vorliegt.

Abnahme deutlich umgebaut: P2-A auf die echte Grenze (79/80 statt 79/81, #14), P2-C um
"Mimic lebt, ist aber zu langsam" erweitert -- der Fall stand im Goal und fehlte in den
Kriterien (#15). P2-E bekommt einen zweiten Teil, der Schritt 2 tatsaechlich absichert
(#3). Neu: P2-H fuer das Marken-Rennen (#12) und P2-I fuer die Terminalpfade (#11).
Korrelations-ID auf 32 Hex festgelegt (#13).

**#16 war der unangenehmste und er hatte recht:** mein "reicht es nicht, wird der
Aufschlag gemessen und begruendet" war eine Hintertuer, die erlaubt haette, das
Kriterium nach dem Fehlschlag umzudefinieren. Korpus, Lastzustand, Warmzustand,
Perzentilmethode und Schwellen werden jetzt vor dem ersten Lauf eingefroren; ein
Fehlschlag verlangt eine Planrevision.

## Round 3 — Codex

Rev 3 schließt fast alle Round‑2-Punkte sauber. Der GPU-Rundlauf, die Ausspracheentscheidung, Gesamtfrist, Warm-Wakeup, Abnahmekriterien und Hub-Marken sind wesentlich belastbarer. Es bleiben aber einige konkrete Widersprüche und neue Regressionsrisiken.

1. **`require_warm` wurde trotz Annahme von Round 2 nicht geändert.** Schritt 3 sagt weiterhin, dass erst der Modell-Eigentümer „beim Ausführen“ prüft ([PHASE2.md:60](</home/itiger013/Dokumente/Github/Mimic/PHASE2.md:60>)); damit kann die Anfrage hinter laufender Arbeit warten, während P2-B eine sofortige `cold`-Antwort voraussetzt.  
Fix: Schreibe ausdrücklich fest, dass `Engine.submit()` `require_warm` unter demselben Runtime-State-Lock vor dem Einreihen prüft und sofort ablehnt.

2. **Der Condition-Umbau gefährdet Phase 1s eingefrorene Queue-Zusagen.** Die heutige `PriorityQueue` liefert Kapazität vier, FIFO innerhalb einer Priorität und `mf` vor `soar`; „Condition statt Queue“ kann diese Eigenschaften unabsichtlich entfernen.  
Fix: Verwende unter der Condition weiterhin einen begrenzten Heap mit `(priority, sequence)` und teste Kapazität vier sowie das Überholen wartender `soar`-Jobs durch `mf`.

3. **Schritt 5 verspricht weiterhin einen unmöglichen Kaltstart-Abbruch.** Das Schließen der Worker-Verbindung setzt zwar `job.cancelled`, aber `_load()` prüft dieses Flag nicht und lädt bis zu 7 Sekunden weiter; der einzige Owner bleibt dabei blockiert.  
Fix: Entferne „Kaltstart“ aus dieser Abbruchzusage und verhindere ihn für dAImon durch die atomare `require_warm`-Admission, oder entwirf einen tatsächlich abbrechbaren separaten Ladeprozess.

4. **Ein Reader-Thread im Frontend wiederholt das Cross-Thread-`close()`-Problem.** Schritt 5 bietet einen Leser-Thread an, sagt aber nur, die Worker-Verbindung zu „schließen“; ein dort blockiertes `read()` wird dadurch nicht zuverlässig geweckt.  
Fix: Fordere auch frontendseitig `shutdown(SHUT_RDWR)` plus `close()` auf dem Worker-Socket und einen begrenzten `join`, oder nimm ausschließlich den nichtblockierenden Parser als Lösung.

5. **P2-I widerspricht P2-H.** P2-I verlangt nach *jedem* Ende `tts_active=false`; wenn A verspätet endet, während B bereits aktiv spricht, muss `tts_active` gemäß P2-H gerade `true` bleiben.  
Fix: Formuliere P2-I als „die beendete Generation besitzt keine aktive Marke mehr; `tts_active=false` nur, wenn keine neuere Generation spricht“.

6. **Die neuen Fehler sind im Frontend-Protokoll nicht vollständig spezifiziert.** Aktuell kennt `REASON_STATUS` weder `cold` noch `load_denied`, unbekannte Gründe werden zu `worker_unavailable`, und `_error()` verwirft Zusatzfelder wie `hub_reason` ([frontend.py:25](</home/itiger013/Dokumente/Github/Mimic/mimic/frontend.py:25>), [frontend.py:188](</home/itiger013/Dokumente/Github/Mimic/mimic/frontend.py:188>)). Damit können P2-B und P2-G in der verlangten Form nicht bestehen.  
Fix: Definiere beide als HTTP 503, erweitere das Fehlerobjekt versioniert um `hub_reason`, und reiche erlaubte strukturierte Zusatzfelder durch Frontend und Worker durch.

7. **Die unverständliche Hub-Antwort ist beschrieben, aber nicht abgenommen.** Schritt 1 verlangt korrekt fail-closed und Journal, P2-G testet jedoch nur drei reguläre Ablehnungen, Hub-Abwesenheit und erfolgreiche Freigabe.  
Fix: Ergänze P2-G um leere, ungültige JSON- und schemafalsche Hub-Antworten; alle müssen ohne Ladeversuch mit eigenem Diagnosegrund enden.

8. **Die Startprüfung aus Schritt 6 hat keine Frist.** Ein erreichbarer, aber hängender Mimic-Frontendprozess kann dadurch den Start von `daimon-tts` blockieren und somit auch sherpa lahmlegen—direkter Bruch von Invariante α.  
Fix: Gib der Status-/Voice-Prüfung eine kurze monotone Gesamtfrist und teste, dass ein hängendes `/status` den Mimic-Pfad deaktiviert, während sherpa innerhalb seines Budgets startet.

9. **Das Verhalten von `/warm` für `soar` bleibt widersprüchlich.** Schritt 4 sagt „Warmläufe nur für `mf`“, beschreibt den Endpunkt aber weiterhin als Wunsch „je Modus“; ein angenommenes `soar` könnte deshalb dauerhaft als unerledigter Wunsch stehen bleiben.  
Fix: Definiere `/warm` ausschließlich für `mf` und lehne andere Modi synchron ab, oder beschreibe einen separaten tatsächlich bedienten Batch-Warmpfad.

10. **P2-D ist für den Vor-Erstrahmen-Fall zeitlich unvollständig.** Dort existiert noch keine Wiedergabe, die binnen 100 ms enden könnte; nur `outcome=cancelled` ohne Frist erlaubt eine erst Sekunden später bemerkte Cancellation.  
Fix: Teile P2-D in „laufende Wiedergabe endet <100 ms“ und „vor erstem Rahmen erreicht das Cancel-Flag den Worker binnen einer festen Frist und der Job erzeugt danach keine Rahmen“.

VERDICT: REVISE
### Claude's response — Round 3

Alle zehn angenommen. Zwei davon sind Fehler, die ich in Rev 3 selbst eingebaut habe:

- **#3 Die Kaltstart-Abbruchzusage war unmoeglich.** _load() prueft kein Cancel-Flag und
  laedt bis zu sieben Sekunden zu Ende. Ich hatte "Abbruch waehrend Kaltstart" in
  Schritt 5 versprochen, ohne zu pruefen, ob der Worker das ueberhaupt kann. Zusage
  gestrichen; fuer dAImon ist der Fall durch require_warm ohnehin ausgeschlossen. Wer
  ihn wirklich abbrechbar will, braucht einen eigenen Ladeprozess -- nicht in diesem Plan.
- **#5 P2-I widersprach P2-H.** P2-I verlangte nach JEDEM Ende tts_active=false, P2-H
  verlangt fuer denselben Moment tts_active=true, wenn B schon spricht. Umformuliert:
  die beendete Generation verliert ihre Marke, tts_active=false nur wenn keine neuere
  Generation spricht.

Weiter angenommen: require_warm wird in Engine.submit() unter dem Runtime-Lock vor dem
Einreihen geprueft (#1) -- sonst steht der Job hinter einem laufenden Kaltstart und
reisst die Frist, obwohl er sofort haette abgelehnt werden muessen. Der Condition-Umbau
behaelt begrenzten Heap ueber (priority, sequence), Kapazitaet vier und mf-vor-soar,
getestet statt angenommen (#2). shutdown vor close auch im Frontend-Leser (#4). cold und
load_denied als 503 ins Fehlerobjekt, hub_reason wird durchgereicht (#6) -- ohne das
koennten P2-B und P2-G gar nicht bestehen. Startpruefung bekommt 300 ms Frist (#8): ein
haengendes Frontend haette sonst den Start von daimon-tts blockiert und damit sherpa,
also Invariante alpha ausgerechnet beim Start gebrochen. /warm nur fuer mf, andere Modi
synchron abgelehnt statt als unerledigter Wunsch abgelegt (#9).

Abnahme: P2-D in D1 und D2 geteilt (#10) -- vor dem ersten Rahmen gibt es keine
Wiedergabe, die binnen 100 ms enden koennte, also eigene Frist von 300 ms fuer das
Cancel-Flag. P2-G um kaputte Hub-Antworten erweitert (#7).

## Round 4 — Codex

Rev 4 behebt alle zehn konkret benannten Round‑3-Punkte im Plantext. Es bleiben jedoch vier relevante Lücken, davon zwei neu durch die präzisierte Teststrategie sichtbar geworden.

1. **„Alle Kriterien laufen gegen ein Mimic-Doppel“ ist mit P2-F und P2-G unvereinbar.** Ein Doppel kann weder echte Modell-TTFA messen noch beweisen, dass Mimics Worker den Hub konsultiert, keinen Ladeversuch macht und seine echte Sperre freigibt; auch P2-E(b) beweist mit einem bloßen Doppel nicht, dass der reale Worker `aussprache:false` respektiert.  
Fix: Definiere eine Testmatrix: dAImon-Doppel für Routing/Fehler, echter Mimic mit Stub-Runtime und Fake-Hub für Protokoll/Gate/Aussprache, echtes warmes Modell für P2-F.

2. **Ein laufender Warmlauf kann `Engine.submit()` weiterhin blockieren.** Die Prüfung verwendet „denselben Lock wie der Runtime-Zustand“; hält der Owner diesen Lock während `_load()`, wartet eine `require_warm`-Anfrage bis zu sieben Sekunden statt sofort `cold` zu erhalten—genau der normale Fall nach einem gerade angestoßenen `/warm`.  
Fix: Halte den Runtime-Lock niemals während `_load()`; veröffentliche atomar einen Zustand `loading`, lasse `require_warm` dabei sofort `cold` liefern und teste eine Anfrage mitten im Warmlauf.

3. **Der asynchrone Warmlauf kann den sherpa-Rückfall blockieren.** Der Plan sagt nicht, ob dAImon `/warm` vor oder nach dem Start der sherpa-Ausgabe aufruft, und gibt dem Warm-Aufruf keine eigene Frist; ein hängendes `/warm` kann daher P2-B umgehen oder unbegrenzt Hintergrundthreads ansammeln.  
Fix: Starte sherpa zuerst, löse `/warm` danach best-effort mit harter kurzer Frist und höchstens einem laufenden Warm-Aufruf aus, und teste ein hängendes `/warm`.

4. **Die hängende Startprüfung ist weiterhin nicht abgenommen.** Schritt 6 nennt jetzt 300 ms, aber kein Kriterium erzwingt, dass ein hängendes `/status` den Mimic-Pfad deaktiviert und `daimon-tts` mit funktionierendem sherpa weiter startet; die Sandbox-Abnahme deckt nur „Mimic nicht installiert“ ab.  
Fix: Ergänze ein Kriterium mit akzeptiertem, aber antwortlosem `/status`: Startprüfung endet binnen 300 ms, Mimic wird deaktiviert und sherpa spricht anschließend innerhalb seines Budgets.

5. **P2-D2s „kein Rahmen mehr“ ist bei bereits gepufferten Frames mehrdeutig.** Ein Rahmen kann vor dem Cancel im `job.events`-Puffer liegen, aber erst danach vom Handler beobachtet werden; damit können korrekte und falsche Implementierungen je nach Messpunkt bestehen oder scheitern.  
Fix: Messe ab gesetztem Worker-Cancel-Flag keine weiteren Generator-Yields beziehungsweise `emit()`-Erfolge und verlange, dass bereits gepufferte Frames nicht mehr an den Konsumenten geschrieben werden.

VERDICT: REVISE
### Claude's response — Round 4

Alle fuenf angenommen.

**#2 war der wichtigste und richtete sich gegen meine eigene Rev-4-Formulierung.** Ich
hatte require_warm "unter demselben Lock wie der Runtime-Zustand" gepruefte -- haelt der
Eigentuemer diesen Lock waehrend _load(), wartet ausgerechnet die Anfrage, die schnell
abgelehnt werden soll, bis zu sieben Sekunden. Und das ist der Normalfall kurz nach
einem angestossenen /warm. Jetzt: Lock nie waehrend _load() halten, stattdessen atomar
einen Zustand loading veroeffentlichen, auf den require_warm sofort cold liefert. Test
schickt eine Anfrage mitten in den Warmlauf.

**#1 traf einen Widerspruch, den ich selbst eingebaut hatte:** "alle Kriterien gegen ein
Doppel" ist mit P2-F und P2-G unvereinbar -- ein Doppel misst keine echte TTFA und
belegt nicht, dass der echte Worker den Hub fragt. Ersetzt durch eine Pruefstand-Matrix:
dAImon-Doppel fuer Routing und Fehler, echter Mimic mit Stub-Runtime und Fake-Hub fuer
Protokoll, Sperre und Aussprache, echtes warmes Modell nur fuer P2-F.

Weiter angenommen: /warm bekommt eine bindende Reihenfolge (erst sherpa, dann warm),
eine eigene 200-ms-Frist und hoechstens einen gleichzeitigen Aufruf (#3) -- ein
haengendes /warm haette sonst P2-B ausgehebelt. Neues P2-J fuer den haengenden
Startcheck (#4): der schlimmere Fall ist nicht "Mimic fehlt", sondern "Mimic ist da und
schweigt". P2-D2 misst jetzt ab gesetztem Cancel-Flag statt "keine Rahmen mehr" (#5) --
letzteres war mehrdeutig, weil ein Rahmen vor dem Abbruch im Puffer liegen und erst
danach beobachtet werden kann.

## Round 5 — Codex (MAX_ROUNDS erreicht)

Rev 5 übernimmt die fünf Round‑4-Fixes korrekt. Drei belastbare Blocker bleiben.

1. **Die „sofortige“ Kalt-Ablehnung liegt weiterhin hinter einer langsamen Worker-Initialisierung.** Ein nicht laufender Worker wird durch `/speak` erst socket-aktiviert; `Engine.__init__()` ruft `write_status()`, dieses `safe_vram_free_mib()`, und das importiert Torch plus initialisiert CUDA, bevor der Server `Engine.submit()` erreicht. Im aktuellen Mimic-venv dauerte allein `import torch` hier gemessen **1,462 s**—P2-B fordert <400 ms.  
Fix: Der Worker muss `require_warm` bedienen können, bevor irgendein Torch-/CUDA-Zugriff erfolgt; initialer Status meldet VRAM `null`, die Abfrage läuft erst im Owner beim tatsächlichen Laden.

2. **P2-D2 verbietet auch den Abschluss eines bereits laufenden Generatoraufrufs.** Setzt ein anderer Thread das Cancel-Flag, während der Owner in `next(generator)` steckt, kann dieser Aufruf noch einen Chunk liefern; erst danach bekommt der Worker Kontrolle und kann ihn verwerfen. „Keine weiteren Generator-Yields ab gesetztem Flag“ ist daher nicht zuverlässig erfüllbar.  
Fix: Verlange, dass nach beobachtetem Cancel kein neuer `next()`-Aufruf beginnt; ein bereits laufender Aufruf darf zurückkehren, sein Chunk muss verworfen werden und `emit()` darf nicht mehr erfolgreich sein.

3. **`/warm` selbst ist noch nicht end-to-end abgenommen.** P2-B beweist nur, dass ein Warmwunsch angestoßen wurde; ein Owner, der den Condition-Wakeup verliert oder den Wunsch nie ausführt, könnte bestehen. Ebenso fehlt der in Round 4 verlangte Gegenfall eines hängenden `/warm`, das die aktuelle sherpa-Ausgabe nicht verzögern darf.  
Fix: Ergänze ein Kriterium: kalt → sherpa beginnt fristgerecht → `/warm` wird warm oder scheitert sichtbar → erfolgreicher Warmlauf macht die nächste lange Äußerung zu `engine=mimic`; ein hängender Warm-Endpunkt verändert die sherpa-TTFA nicht und erzeugt höchstens einen Hintergrundaufruf.

VERDICT: REVISE
### Claude's response — Round 5, und Abschluss

Alle drei angenommen und eingearbeitet. Finding 1 vorher selbst nachgemessen:
`import torch` plus `torch.cuda.mem_get_info()` kosten in diesem venv **0.69-0.75 s**
(Codex mass 1.462 s, vermutlich kalter Seiten-Cache). Und `Engine.__init__` laeuft
genau da durch -- write_status("kalt") ruft safe_vram_free_mib(), bevor der Server
submit() erreicht. Ein socket-aktivierter Worker koennte die 400 ms aus P2-B also
gar nicht halten, egal wie schnell die Ablehnung danach ist. Der Anfangsstatus meldet
jetzt VRAM null, die Abfrage passiert erst beim echten Laden.

Finding 2 korrigiert erneut eine meiner eigenen Formulierungen: "keine Yields ab
gesetztem Flag" ist nicht erfuellbar, wenn ein anderer Thread das Flag setzt, waehrend
der Eigentuemer in next() steckt. Jetzt: kein NEUER next()-Aufruf, laufender darf
zurueckkehren, Chunk wird verworfen.

Finding 3: neues P2-K. P2-B belegte nur, dass ein Warmwunsch angestossen wurde -- ein
Eigentuemer, der den Condition-Wakeup verliert, waere damit durchgekommen.

## Abschluss: MAX_ROUNDS erreicht, kein APPROVED

Fuenf Runden, 54 Findings insgesamt (20 / 16 / 10 / 5 / 3), keines abgelehnt. Die Kurve
konvergiert deutlich, aber die Obergrenze ist erreicht, und die drei Fixes aus Runde 5
sind per Definition **nicht mehr gegengelesen**. Das ist kein APPROVED und wird auch
nicht als solches ausgegeben.

Substanziell gefunden wurden dabei zwei Defekte im LAUFENDEN Code (kaputte Hub-Sperre,
Aussprache umgeht den Validator) und ein drittes Messproblem (Torch-Import blockiert die
Kalt-Ablehnung) -- alle drei am Code verifiziert, nicht nur behauptet.

## Round 6 — Codex (auf Anforderung ueber MAX_ROUNDS hinaus)

Rev 6 ist noch nicht freigabefähig. Die neuen Commits legen mehrere bisher unsichtbare Lücken offen.

1. **Der Torch-Startup-Fix steht im Plan, wird durch P2-B aber nicht erzwungen.** „Kaltes Mimic“ kann auch einen bereits laufenden Worker ohne Runtime bedeuten; dann besteht P2-B trotz unverändertem Torch-Import in `Engine.__init__`.  
Fix: P2-B muss jeden Lauf mit nachweislich nicht vorhandenem Worker-PID/socket-aktiviertem Neustart beginnen.

2. **P2-D1/D2 sind dem falschen Prüfstand zugeordnet.** Die Matrix ordnet sie dem dAImon-Doppel zu, obwohl D1 das echte Mimic-Journal und D2 Worker-Cancel, `next()` und `emit()` beobachtet; ein Doppel kann Schritt 5 nicht abnehmen.  
Fix: P2-D1/D2 zusätzlich gegen echtes Frontend und echten Worker mit steuerbarer Stub-Runtime ausführen.

3. **Die neue Silent-Take-Wiederholung verletzt P2-D2 an der Retry-Grenze.** Nach einem vollständig stummen Take startet [worker.py](/home/itiger013/Dokumente/Github/Mimic/mimic/worker.py:160) den nächsten Generator ohne erneute Cancel-Prüfung; ein zwischen den Takes beobachteter Abbruch kann daher einen neuen `next()` auslösen.  
Fix: Vor jedem Generatorstart und jedem `next()` Cancel prüfen und D2 mit einem erzwungen stummen ersten Take testen.

4. **„Erster `A`-Frame“ bedeutet seit f3a26bb nicht mehr „erster Ton“.** Der Worker sendet den stummen Take vollständig, erkennt ihn erst danach und kann sogar zwei stumme Takes mit `status=ok` beenden; dAImon entscheidet damit binnen 500 ms für Mimic, während hörbare Sprache sekundenlang ausbleibt. P2-F misst ebenfalls nur Bytes und übersieht das.  
Fix: Anfangsframes bis zum ersten Überschreiten von `STUMM_PEAK` puffern, stille Takes verwerfen, nach zwei stillen Takes vor dem Stream fehlschlagen und einen erzwungenen Silent-Take-Fall abnehmen.

5. **Der GUI-Stopp kann die Verbindung gerade in den kritischen Phasen nicht schließen.** [gui.py](/home/itiger013/Dokumente/Github/Mimic/mimic/gui.py:83) hält die Verbindung nur im Synthesethread; der Tk-Callback setzt lediglich ein Event. Während `getresponse()` vor dem ersten Audio oder eines blockierenden `read_frame()` läuft, bleibt der Socket unerreichbar und der Worker rechnet weiter.  
Fix: Die aktive GUI-Verbindung vor `getresponse()` threadsicher veröffentlichen und beim Stopp per `shutdown(SHUT_RDWR)` plus `close()` abbrechen; Warteschlangen- und Frame-Stall testen.

6. **Das erweiterte `/speak`-Schema lässt CLI und GUI undefiniert zurück.** Beide senden heute nur `text`, `voice` und `mode`; Rev 6 spezifiziert weder Defaults für `aussprache`/`require_warm` noch, ob die zwingend kanonische Korrelations-ID fehlen darf. Eine naheliegende Umsetzung bricht beide bestehenden Clients oder schaltet ihre Aussprachekorrektur ab.  
Fix: Rückwärtskompatibel `aussprache=true`, `require_warm=false` und serverseitige ID bei Fehlen definieren, während dAImon explizit `false/true/eigene ID` sendet; CLI und GUI als Regressionstests aufnehmen.

7. **GUI-`soar` und der geplante `mf`-Warmlauf passen nicht in den bestehenden Speicherentwurf.** Der Worker behält Runtimes pro Modus gleichzeitig; Phase 0 misst dafür rund 20 GiB RAM, die Unit erlaubt 7 GiB. Nach einem GUI-`soar` versucht `/warm` deshalb `mf` daneben zu laden und kann den Worker töten. „Ein Eigentümer“ verhindert Doppel-Laden, aber nicht Doppel-Residenz.  
Fix: Einen gemessenen, speichersicheren Moduswechsel festlegen und `soar → /warm(mf)` sowie `mf → GUI-soar` unter der echten cgroup abnehmen.

8. **P2-K ist logisch widersprüchlich.** Es verlangt nach „warm oder sichtbar gescheitert“ in beiden Fällen die nächste Äußerung mit `engine=mimic`; nach einem sichtbaren Fehlschlag ist genau das nicht zugesagt.  
Fix: Erfolgszweig auf `engine=mimic` festlegen, Fehlerzweig auf fristgerechtes sherpa plus korrelierten Diagnosegrund.

Die Viererwarteschlange wird durch eine einzelne GUI nicht geflutet, weil sie Einsätze seriell sendet; `mimic record` erzeugt ebenfalls keinen zweiten Modell-Eigentümer. P2-Fs 300-ms-Grenze ist durch die gemessenen 250 ms begründet, muss aber um „hörbares Audio statt beliebiger PCM-Bytes“ korrigiert werden.

VERDICT: REVISE
### Claude's response — Round 6

Alle acht angenommen. Diese Runde hat sich bezahlt gemacht, und zwar ausgerechnet an der
Parallelarbeit, die der Plan bis dahin nie gesehen hatte. Zwei Funde am Code verifiziert:

**#4 -- "erster A-Rahmen" ist nicht mehr "erster Ton".** Seit f3a26bb emittiert die
Schleife jeden Chunk und prueft `spitze >= STUMM_PEAK` erst NACH dem ganzen Take. Ein
stummer Take geht also vollstaendig zum Konsumenten, bevor er als stumm erkannt wird.
Damit haette dAImon binnen 500 ms auf Mimic festgelegt und sekundenlang Stille
abgespielt -- die gesamte Engine-Wahl aus Schritt 12 haengt daran, dass der erste Rahmen
hoerbares Audio bedeutet. Zwei stumme Takes enden sogar mit status=ok. Neuer Schritt 2a.

**#7 -- zwei residente Runtimes passen nicht in die Unit.** self.runtimes[mode] behaelt
jeden geladenen Modus, geraeumt wird nie. Meine eigene ERGEBNIS.md sagt: beide zusammen
~20 GiB RAM, MemoryMax ist 7G. Bisher fiel das nicht auf, weil nur ein Modus benutzt
wurde -- mit der GUI (soar) und meinem geplanten /warm(mf) haette der Plan den Worker
umgebracht. Das ist mein eigener dokumentierter Befund, den mein eigener Plan verletzt
haette. Neuer Schritt 2b: ein residenter Modus, Wechsel raeumt, Abnahme unter der echten
cgroup.

Weiter angenommen: GUI-Stopp erreicht die Verbindung nicht (#5, dasselbe Loch wie
Schritt 5, nur im anderen Client) -- Schritt 2c. Rueckwaertskompatible Vorgaben fuer das
erweiterte /speak-Schema, damit CLI und GUI nicht brechen (#6) -- Schritt 2d. P2-D1/D2
gehoeren an den echten Pruefstand, nicht ans Doppel (#2). P2-D2 muss auch den
Wiederholungs-Take nach einem stummen abdecken (#3). P2-F misst ab jetzt bis zum ersten
HOERBAREN Sample, nicht bis zum ersten PCM-Byte (#4, zweiter Teil).

**#8 war wieder ein Widerspruch in meiner eigenen Formulierung:** P2-K verlangte nach
"warm oder sichtbar gescheitert" in beiden Faellen engine=mimic. Nach einem Fehlschlag
ist genau das nicht zugesagt. Jetzt zwei getrennte Zweige.

Nicht beanstandet und damit bestaetigt: die Viererwarteschlange wird von einer einzelnen
GUI nicht geflutet (sie sendet seriell), `mimic record` erzeugt keinen zweiten
Modell-Eigentuemer, und die 300-ms-Grenze aus P2-F bleibt durch die gemessenen 250 ms
gedeckt.

## Round 7 — Codex

Der Kernplan ist baubar und muss nicht zerlegt werden. Vier lokale Blocker bleiben jedoch; insbesondere Schritt 2b ist noch keine belastbare Implementierungsanweisung.

| Round‑6-Finding | Status in Rev 7 |
|---|---|
| 1. Socket-aktivierter Kaltstart | Nicht vollständig adressiert |
| 2. D1/D2 falscher Prüfstand | Adressiert |
| 3. Cancel vor Silent-Retry | Adressiert |
| 4. Erster Frame versus hörbarer Ton | Teilweise adressiert |
| 5. GUI-Stopp | Mechanismus beschrieben, Abnahme fehlt |
| 6. Schema-Kompatibilität | Adressiert, kleine Wortlautkollision |
| 7. Zwei residente Modi | Problem erkannt, Lösung noch unbewiesen |
| 8. P2-K-Widerspruch | Adressiert |

### Blocker

1. **Round‑6-Finding 1 wurde faktisch nicht eingearbeitet.** P2-B ist unverändert und weiterhin dem dAImon-Doppel zugeordnet; weder ein fehlender Worker-PID noch echte Socket-Aktivierung wird verlangt. Damit kann der alte Torch-Import vor `submit()` unentdeckt bleiben.  
Fix: P2-B zusätzlich mit echtem Worker aus Zustand „Prozess existiert nicht“ ausführen und vor jedem Lauf PID-Abwesenheit sowie Socket-Aktivierung nachweisen.

2. **Schritt 2b ist hinsichtlich Serialisierung baubar, hinsichtlich Speicherfreigabe aber nur eine Hoffnung.** Der einzige Owner kann den alten Runtime nach Abschluss von `_execute()` sicher entfernen: Währenddessen läuft keine zweite Synthese, wartende Jobs besitzen keinen Runtime-Verweis, und bereits gepufferte Audioframes brauchen das Modell nicht mehr. Ein bloßes `self.runtimes.clear()` beweist jedoch nicht, dass rund 5–10 GiB CPU-RSS tatsächlich an die cgroup zurückgehen; Phase 1 wählte gerade den Prozessausgang als robuste Freigabe. Außerdem ordnet die Prüftabelle den Moduswechsel dem **Stub-Runtime-Prüfstand** zu, der das RAM-Problem unmöglich messen kann.  
Fix: Einen vierten Prüfstand mit echten `mf`-/`soar`-Runtimes unter `MemoryMax=7G` festlegen und vorab entscheiden: gemessen erfolgreiche In-Process-Freigabe (`pop`, Referenzen beseitigen, GC/CUDA-Cache) oder kontrollierter Worker-Neustart mit definiertem Request-Retry.

3. **Der Moduswechsel hat noch keine vollständige Zustandsmaschine.** Nicht definiert sind Zustand und Rückfall, wenn der alte Runtime bereits entfernt wurde und `_load(neu)` anschließend an Hub, VRAM oder CUDA scheitert; ebenso fehlen Zusagen für während `loading` eintreffende alte/neue Modi und einen wartenden Warmwunsch.  
Fix: Übergang explizit als `warm(old) → unloading → cold → loading(new) → warm(new)|cold(error)` definieren, Runtime-Mutation ausschließlich dem Owner erlauben und Request-/Warm-Verhalten für jeden Zustand testen.

4. **Schritt 2a beweist noch nicht seine zentrale Zusage.** P2-D2 erzwingt nur Cancel zwischen Silent-Takes; P2-F ist eine gewöhnliche Messreihe. Kein Kriterium erzwingt „erster Take stumm, zweiter hörbar“ oder „beide stumm, kein `A`, Fehler vor HTTP 200“. Außerdem ist offen, ob zurückgehaltene stille Anfangsframes später ausgespielt werden—dann kann der erste `A` weiterhin mit längerer Stille beginnen—oder verworfen werden, was den Satzanfang beschneiden kann.  
Fix: Zwei deterministische Stub-Fälle ergänzen und festlegen, wie der stille Präfix behandelt sowie die Zeit bis zur tatsächlichen hörbaren Ausgabe begrenzt wird.

5. **Schritt 2c hat keine falsifizierbare Abnahme und lässt eine Publish/Cancel-Race offen.** Drückt der Nutzer Stopp unmittelbar vor Veröffentlichung der Verbindung, darf der Synthesethread nicht anschließend einen neuen Socket öffnen und blockieren; D1/D2 prüfen dAImon, nicht die GUI.  
Fix: Eigenes GUI-Kriterium für Stopp vor `getresponse()`, während blockiertem `read_frame()` und unmittelbar vor Session-Publikation ergänzen; jeweils Socket geschlossen, Worker-Cancel binnen Frist und kein Synthesethread übrig.

### Nachziehbar, kein Architekturblocker

Die Korrelations-ID-Regel widerspricht dem neuen Schema wörtlich: Schritt 2d erlaubt eine fehlende ID, während [PHASE2.md](/home/itiger013/Dokumente/Github/Mimic/PHASE2.md:285) sagt, „nichts anderes wird angenommen“. Die beabsichtigte Semantik ist trotzdem klar.  
Fix: Formulieren: „Eine vorhandene ID muss 32-stelliges Hex sein; fehlt sie, erzeugt das Frontend eine.“

P2-K ist jetzt widerspruchsfrei, D1/D2 sind dem richtigen Codepfad zugeordnet, der Silent-Retry-Cancel ist prüfbar, und die CLI-/GUI-Defaults erhalten den bestehenden Vertrag. Matthias kann daher sagen: Die Architektur trägt, aber Runtime-Wechsel und drei fehlende Abnahmeschilde müssen vor Baubeginn präzisiert werden.

VERDICT: REVISE
### Claude's response — Round 7, und Abschluss der Schleife

Alle fuenf Blocker plus der nachziehbare Punkt eingearbeitet. Codex' eigene Einordnung
ist die Antwort, um die ich gebeten hatte: "Die Architektur traegt, aber Runtime-Wechsel
und drei fehlende Abnahmeschilde muessen vor Baubeginn praezisiert werden."

**Blocker 2 war messbar, nicht verhandelbar -- also gemessen:**

|  | RSS | VRAM frei |
|---|---|---|
| Start | 806 MiB | 30140 |
| mf geladen | 2138 MiB | 19592 |
| nach del + empty_cache() | 2139 MiB | 30006 |
| nach malloc_trim | 2118 MiB | 30004 |

VRAM kommt vollstaendig zurueck, CPU-RSS praktisch nichts. Mein "ein residenter Modus,
Wechsel raeumt" war damit widerlegt, bevor es gebaut wurde. Der Moduswechsel ist jetzt
ein kontrollierter Worker-Neustart mit einmaliger Wiederholung der ausloesenden Anfrage,
plus explizite Zustandsmaschine warm(alt) -> beendet sich -> cold -> loading(neu) ->
warm(neu) | cold(Fehler).

Bemerkenswert: damit traegt Phase 1s Prozessende-Entwurf ausgerechnet aus dem Grund, den
ich in Phase 0 als "falsch begruendet" korrigiert hatte. Fuer VRAM war er es -- fuer RSS
nicht. Das ist die dritte VRAM/RAM-Verwechslung in diesem Projekt, und die erste, die
Codex gefunden hat statt ich.

Blocker 1: P2-B war tatsaechlich nicht angepasst -- ich hatte den Torch-Import-Fix in
Schritt 3 geschrieben und den zugehoerigen Test vergessen. Laeuft jetzt gegen den echten
Worker aus "Prozess existiert nicht", mit Nachweis von PID-Abwesenheit und
Socket-Aktivierung. Vierter Pruefstand ergaenzt: echte Runtimes unter MemoryMax=7G, weil
ein Stub weder RSS noch die cgroup-Grenze messen kann.

Blocker 4 und 5 als eigene Kriterien: P2-L (stummer erster Take, beide stumm) und P2-M
(GUI-Stopp in drei Phasen, inklusive der Publish/Cancel-Race). Korrelations-ID-Regel
entwidersprucht.

**Schleife endet hier.** Sieben Runden, 67 Findings, keines abgelehnt:
20 / 16 / 10 / 5 / 3 / 8 / 6. Der Ausschlag in Runde 6 war der Nachholeffekt der
Parallelarbeit. Kein APPROVED -- die Fixes aus Runde 7 sind wieder ungelesen, und das
wird nicht als Konvergenz ausgegeben. Codex' Urteil "Architektur traegt" ist das
belastbarste Ergebnis, das diese Schleife liefern kann.
