# Ansage: Claude Code meldet sich per Mimic

Wenn Claude Code eine Aufgabe abschließt, spricht Mimic das Ergebnis über den
Bluetooth-Kopfhörer — »Fertig.« plus die ersten Sätze der letzten Antwort. Der
Kopfhörer wird dabei bei Bedarf selbst zurückgeholt.

## Einrichten

```bash
tools/einrichten.sh
```

Das installiert nach `~/.local/bin`, hinterlegt die MAC, hängt den Hook in
`~/.claude/settings.json` ein und macht die Hörprobe. Zweimal aufrufen ist
gefahrlos — jeder Schritt prüft erst, ob er nötig ist. `--nur-repo` lässt den
globalen Hook weg, dann gilt die Ansage nur im Mimic-Repo.

Einen Schritt macht das Skript **nicht**: koppeln. Der ist einmalig, interaktiv
und braucht den PIN-Dialog.

```bash
bluetoothctl
  scan on
  pair  XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX      # ohne trust scheitert jedes spätere connect
  quit
```

Ist noch nichts gekoppelt, sagt `einrichten.sh` genau das und bricht ab. Danach
nochmal aufrufen.

## Teile

| Datei | Aufgabe |
|---|---|
| `tools/einrichten.sh` | Installiert, hinterlegt, hängt ein, probt. |
| `tools/ansage.py` | Hook. Liest das Transkript, kürzt, ruft `mimic say`. |
| `tools/kopfhoerer.sh` | Verbindet den Kopfhörer und setzt die Standardsenke. |
| `.claude/settings.json` | Verdrahtung für dieses Repo (`Stop` und `Notification`). |
| `tests/test_ansage.py` | Textpfad ohne Audio; läuft in `tests/run.sh` mit. |

Der Hook koppelt sich sofort ab und ist nach Millisekunden fertig — die
Sitzung wartet nie auf das Sprechen. Fehlt der Dienst, fehlt `mimic`, ist die
MAC nicht eingetragen: jeder Pfad endet in 0 und schweigt. Eine Sperre in
`$XDG_RUNTIME_DIR/mimic-ansage.lock` verhindert, dass zwei Sitzungen
gleichzeitig sprechen; wer sie nicht bekommt, schweigt.

## Von Hand, Schritt für Schritt

Falls `einrichten.sh` irgendwo hängen bleibt — dasselbe einzeln:

```bash
# MAC hinterlegen; ohne MAC listet kopfhoerer.sh die gekoppelten Geräte
mkdir -p ~/.config/mimic
echo XX:XX:XX:XX:XX:XX > ~/.config/mimic/kopfhoerer

# Verbindung prüfen. Der zweite Lauf muss sofort zurückkommen.
tools/kopfhoerer.sh
tools/kopfhoerer.sh --status

# Text prüfen, ohne zu sprechen: genau der Satz, der später zu hören ist
echo '{"hook_event_name":"Stop","transcript_path":"'"$(ls -t ~/.claude/projects/*/*.jsonl | head -1)"'"}' \
  | python3 tools/ansage.py --vorschau

# Hörprobe; braucht den laufenden Dienst
tools/ansage.py --sagen "Fertig. Zwei Tests repariert, alles grün."

# Installieren und global einhängen
install -Dm755 tools/ansage.py     ~/.local/bin/mimic-ansage
install -Dm755 tools/kopfhoerer.sh ~/.local/bin/kopfhoerer.sh
python3 ~/.local/bin/mimic-ansage --einhaengen
```

`--einhaengen` führt zusammen, statt zu überschreiben: bestehende Einstellungen
bleiben, eine Sicherung landet als `settings.json.vor-ansage` daneben, und
zweimal einhängen ergibt trotzdem nur einen Eintrag. Kaputtes JSON lehnt es ab,
statt die Datei zu ersetzen. Ein Pfad als Argument nimmt eine andere Datei als
`~/.claude/settings.json`.

`kopfhoerer.sh` muss neben `mimic-ansage` liegen — der Hook sucht es im eigenen
Verzeichnis, nicht im PATH.

Für dieses Repo liegt `.claude/settings.json` schon dabei. Claude Code neu
starten, dann zeigt `/hooks`, was aktiv ist.

## Stellschrauben

| Variable | Wirkung |
|---|---|
| `MIMIC_ANSAGE_STIMME` | Stimmprofil, sticht alles andere. Jedes Profil aus `mimic voices` geht. |

Ohne diese Variable gilt `$XDG_RUNTIME_DIR/mimic-ansage.stimme.<session_id>`
-- die Datei schreiben die Persona-Skills beim Umschalten, eine je Sitzung.
Fehlt sie, greift die sitzungslose `mimic-ansage.stimme` als gemeinsame
Vorgabe fuer alle Sitzungen. Beide liegen im Laufzeitverzeichnis und
ueberleben keinen Neustart; danach spricht wieder die Vorgabe `forge`.

`tools/ansage.py --stimme` zeigt, was fuer die laufende Sitzung gilt,
`--stimme --sitzung <id>` das fuer eine andere.

| `MIMIC_ANSAGE_STILL=1` | Schweigt, ohne den Hook auszubauen. Für lange Sitzungen am Schreibtisch. |
| `KOPFHOERER_MAC` | Überschreibt `~/.config/mimic/kopfhoerer`. |

Länge der Ansage: `GRENZE` in `tools/ansage.py`, aktuell 420 Zeichen. Gekürzt
wird an der Satzgrenze.

Was die Stimme nicht wörtlich vorlesen kann, wird übersetzt statt gestrichen —
`sprechbar()` und `blockbeschreibung()` erledigen das, `--vorschau` zeigt das
Ergebnis ohne Ton:

| Im Text | Gesprochen |
|---|---|
| `/run/user/1000/mimic-ansage.stimme` | »slash run user 1000 mimic ansage punkt stimme« |
| `mimic/cli.py:195` | »mimic cli punkt py Zeile 195« |
| `5341a99`, UUIDs | »eine Kennung« |
| ein Bash-Codeblock | »Ein Bash-Block mit 2 Zeilen, ruft git und python3 auf.« |
| ein Python-Codeblock | »Ein Python-Block mit 4 Zeilen, definiert stimme und stimmdatei.« |
| eine Tabelle | »Eine Tabelle mit 8 Zeilen.« |
| `https://github.com/…/pull/2` | »ein Link auf github punkt com« |
| `mimic_token`, `webbrowser.open` | »mimic token«, »webbrowser punkt open« |
| `SameSite=Strict`, `_fenster()` | »SameSite gleich Strict«, »fenster« |
| `127.0.0.1:1234` | »127.0.0.1 Port 1234« |
| `uv tool install --python 3.12 .` | »ein Befehl« |

Nur der führende Schrägstrich wird gesprochen, innere sind Sprechpausen.
Einzelne Bezeichner in Backticks werden gesprochen — ohne sie bliebe von
»`mimic_token` gilt für `127.0.0.1`« nur ein Satz ohne Subjekt übrig. Ein
mehrteiliger Befehl wird stattdessen angesagt; bildet er einen eigenen Satz,
fällt er ganz weg. Gestrichen wird noch, was auch beschrieben nichts hergibt:
Hashes und UUIDs.

Zwei Regeln halten die Ansage bei vollständigen Gedanken, statt sie am Budget
abzuschneiden:

- Passt der nächste Satz nicht mehr, wird er nur dann fallengelassen, wenn
  vorher schon `MINDEST` Zeichen Substanz gesprochen wurden. Sonst wird er
  angeschnitten — ein kurzer Auftakt wie »Gemerged.« gefolgt von Stille wäre
  keine Meldung.
- Ein Doppelpunkt beendet keinen Satz und bleibt stehen: »Am PC:« kündigt den
  Block an, der jetzt als Beschreibung folgt.

## Was noch offen ist

- **`Notification` behalten oder nicht.** Der Hook meldet auch, wenn Claude auf
  eine Freigabe wartet. Nützlich mit Kopfhörer auf, nervig ohne — der Block ist
  einzeln aus `settings.json` entfernbar.
- **Aufwachzeit des Kopfhörers.** `FRIST=15` in `kopfhoerer.sh` ist geraten.
  Wenn der Kopfhörer aus dem Standby länger braucht, hier nachziehen.
- **Erste Silbe abgeschnitten?** Manche Geräte brauchen nach dem Verbinden
  einen Moment, bis die Senke wirklich trägt. Dann hilft ein `sleep 1` nach dem
  `pactl set-default-sink` in `kopfhoerer.sh`.

## Fehlersuche

Zuerst die Kette durchgehen lassen — sie hat sechs Glieder, und jedes bricht
sie vollständig und lautlos:

```bash
.claude/skills/mimic-ansage/scripts/pruefen.sh
```

Dasselbe Wissen als Skill für Claude Code: `.claude/skills/mimic-ansage/`.


| Symptom | Ursache |
|---|---|
| Nichts passiert | Hook nicht geladen — `/hooks` in Claude Code zeigt, was aktiv ist. |
| Hook läuft, kein Ton | `tools/ansage.py --sagen "Test"` von Hand; das zeigt, ob `mimic` oder die Senke klemmt. |
| Ton auf den Boxen | Kopfhörer nicht verbunden oder `pactl` fehlt — `tools/kopfhoerer.sh` laut aufrufen. |
| `worker_unavailable` | Dienst aus: `systemctl --user start mimic.socket mimic-worker.socket`. |
| Zweite Sitzung schweigt | Absicht — die Sperre lässt nur eine Ansage zur Zeit durch. |
