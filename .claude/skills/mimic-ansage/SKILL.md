---
name: mimic-ansage
description: >-
  Die Mimic-Ansage einrichten, umstellen und reparieren — der Claude-Code-Stop-Hook,
  der nach jeder erledigten Aufgabe per Mimic-TTS über den Bluetooth-Kopfhörer meldet,
  dass sie fertig ist. Nimm dieses Skill, sobald es um die Ansage, die Fertigmeldung,
  den Stop-Hook, `tools/ansage.py`, `tools/einrichten.sh` oder `tools/kopfhoerer.sh`
  geht — und ganz besonders, wenn die Ansage schweigt, zu viel vorliest, die falsche
  Stimme hat, über die Boxen statt den Kopfhörer läuft, auf einer neuen Maschine
  eingerichtet werden soll oder abgeschaltet gehört. Greift auch bei vagen
  Formulierungen wie »ich höre nichts mehr«, »du sagst mir nicht mehr Bescheid«,
  »andere Stimme«, »das nervt, mach das leiser« oder »warum redet das dauernd«.
---

# Mimic-Ansage

Ein Stop-Hook lässt Mimic sprechen, wenn eine Claude-Code-Aufgabe fertig ist:
»Fertig.« plus die ersten Sätze der letzten Antwort, über den
Bluetooth-Kopfhörer. Einrichtung, Umbau und Fehlersuche laufen über dieses
Skill.

## Die eine Sache, die alles bestimmt

**Die Ansage scheitert grundsätzlich lautlos.** `tools/ansage.py` endet auf
jedem Pfad mit 0 und schluckt jeden Fehler — das ist Absicht und darf nicht
"repariert" werden: ein Hook, der Fehler wirft, bewirft den Nutzer mitten in
seiner Arbeit mit Hook-Fehlern, und das kostet mehr, als eine Fertigmeldung
wert ist.

Die Folge für dich: **Stille ist kein Symptom, sondern sechs verschiedene
Symptome, die identisch aussehen.** Rate nie, welches davon vorliegt. Die
Kette hat sechs Glieder, und jedes einzelne bricht sie vollständig:

```
Hook eingehängt → installierte Kopie aktuell → Dienst läuft
    → Stimmprofil gültig → Kopfhörer verbunden → Senke stimmt
```

`scripts/pruefen.sh` geht genau diese sechs durch und sagt, welches hakt:

```bash
.claude/skills/mimic-ansage/scripts/pruefen.sh
```

Fang damit an, bei *jeder* Beschwerde über die Ansage. Es ist schneller als
jede Vermutung und deckt den Fall ab, den man sonst zuletzt prüft.

## Einrichten

Auf einer neuen Maschine, oder nach Änderungen an `tools/ansage.py`:

```bash
tools/einrichten.sh
```

Installiert nach `~/.local/bin`, hinterlegt die Kopfhörer-MAC, hängt den Hook
in `~/.claude/settings.json` ein, prüft Dienst und Stimmprofil, macht die
Hörprobe. Zweimal aufrufen ist gefahrlos. `--nur-repo` lässt den globalen Hook
weg, dann gilt die Ansage nur im Mimic-Repo über dessen
`.claude/settings.json`.

Was das Skript bewusst **nicht** tut: koppeln. Das ist einmalig, interaktiv,
und `trust` ist der Teil, der zählt — ohne ihn scheitert jedes spätere
unbeaufsichtigte `connect`, und zwar wortlos.

```bash
bluetoothctl
  scan on
  pair  XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX
  quit
```

## Die Falle mit der installierten Kopie

Der Hook ruft `~/.local/bin/mimic-ansage` auf, **nicht** `tools/ansage.py` im
Repo. Jede Änderung am Skript — Stimme, Textlänge, Verhalten — wirkt erst nach
einem erneuten `tools/einrichten.sh`.

Das ist die Ursache Nummer eins für "ich hab's geändert, es passiert aber
nichts". `pruefen.sh` vergleicht beide Fassungen und meldet, wenn die
installierte veraltet ist.

## Stimme wechseln

Die Vorgabe steht als `VORGABE_STIMME` in `tools/ansage.py`; `ansage.py
--stimme` gibt aus, was gerade wirksam ist. Dauerhaft umstellen heißt: Konstante
ändern, dann `tools/einrichten.sh`. Nur für eine Sitzung reicht
`MIMIC_ANSAGE_STIMME` in der Umgebung von Claude Code.

Vor dem Umstellen prüfen, ob das Profil überhaupt existiert — siehe unten, das
ist heimtückischer als es klingt. Profile und ihre Duktus stehen in
`mimic/charaktere.py`.

## Leiser, seltener, aus

| Wunsch | Handgriff |
|---|---|
| Ganz stumm, Hook bleibt | `MIMIC_ANSAGE_STILL=1` in der Umgebung |
| Keine Meldung bei Freigabe-Nachfragen | `Notification`-Block aus `~/.claude/settings.json` raus |
| Kürzere Ansagen | `GRENZE` in `tools/ansage.py` runter, dann neu einrichten |
| Längere Ansagen | `GRENZE` hoch. Bricht die Ansage mitten im Gedanken ab, ist eher `MINDEST` die Stellschraube — siehe `tools/ANSAGE.md` |
| Ganz weg | beide Blöcke aus `settings.json`, `~/.local/bin/mimic-ansage` löschen |

Wenn jemand sagt, die Ansage "nervt" oder "redet zu viel", frag nach, welches
davon gemeint ist — das sind vier verschiedene Eingriffe, und der falsche
schaltet mehr ab als gewollt.

## Fehlersuche

`scripts/pruefen.sh` zuerst. Was die Meldungen bedeuten:

**"Stimmprofil fehlt" ist oft eine Lüge.** `mimic voices` listet nur Profile,
die `load_voice` durchwinkt — `cli.voices()` überspringt jeden `VoiceError`
wortlos. Ein aufgenommenes Profil mit falschen Rechten (nicht 0700/0600),
falscher Samplerate (nicht 48 kHz mono), Dauer außerhalb 3–60 s oder leerem
`ref.txt` taucht dort **nicht** auf, obwohl es existiert. Wer daraufhin `mimic
record` laufen lässt, überschreibt eine funktionierende Aufnahme und behebt
nichts.

Unterscheide also immer: liegt `~/.local/share/mimic/voices/<name>/` da?

- **Verzeichnis fehlt** → wirklich nicht aufgenommen, `mimic record <name>`
- **Verzeichnis da, nicht gelistet** → abgelehnt. Den Grund im Klartext gibt
  nur der Dienst: `mimic say "Probe" --voice <name>`

**Text prüfen, ohne zu sprechen.** `--vorschau` zeigt exakt den Satz, der aus
dem Kopfhörer käme — der schnellste Weg, Textprobleme von Audioproblemen zu
trennen:

```bash
echo '{"hook_event_name":"Stop","transcript_path":"'"$(ls -t ~/.claude/projects/*/*.jsonl | head -1)"'"}' \
  | python3 tools/ansage.py --vorschau
```

**Weitere Muster:**

| Symptom | Meist die Ursache |
|---|---|
| Gar nichts, auch kein Prozess | Hook nicht geladen — Claude Code neu starten, `/hooks` zeigt Aktives |
| Dienst grün, trotzdem still | Senke oder Stimmprofil, nicht Mimic |
| Ton auf den Boxen | Kopfhörer nicht verbunden oder `pactl` fehlt — `tools/kopfhoerer.sh` laut aufrufen |
| Erste Silbe abgeschnitten | Senke trägt noch nicht; `sleep 1` nach `pactl set-default-sink` in `kopfhoerer.sh` |
| Zweite Sitzung schweigt | Absicht — die Sperre in `$XDG_RUNTIME_DIR` lässt nur eine Ansage zur Zeit durch |
| Liest Code oder Tabellen vor | Sollte nicht passieren; `zusammenfassen()` in `tools/ansage.py` filtert das, mit Tests in `tests/test_ansage.py` |
| Kopfhörer verbindet nicht mehr | `trust` fehlt oder ging verloren — `bluetoothctl info <MAC>` zeigt `Trusted:` |

## Wenn du am Code arbeitest

`tests/run.sh` deckt den Textpfad GPU-frei ab (`tests/test_ansage.py`). Der
Sprechpfad braucht Hardware und bleibt der Hörprobe überlassen.

Drei Eigenschaften sind bewusst so gebaut und sollten Änderungen überleben —
jede stammt aus einer Art, wie so ein Hook schiefgeht:

- **Endet immer in 0.** Sonst sieht der Nutzer Hook-Fehler statt seiner Arbeit.
- **Koppelt sich ab** (`start_new_session=True`). Sonst wartet die Sitzung auf
  das Ende des gesprochenen Satzes; so sind es 35 ms.
- **Hält eine Sperre.** Sonst reden zwei Sitzungen gleichzeitig.

Ein Sonderfall, der leicht verlorengeht: die Leitplanke ganz unten in
`ansage.py` schluckt Ausnahmen nur beim argumentlosen Hook-Aufruf. Von Hand
aufgerufen — `--einhaengen`, `--sagen` — sollen Fehler sichtbar sein, sonst
geht ein Absturz beim Einrichten als Erfolg durch.

Ausführlicher, inklusive Einrichtung von Hand: `tools/ANSAGE.md`.
