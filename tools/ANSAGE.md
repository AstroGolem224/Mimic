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
| `MIMIC_ANSAGE_STIMME` | Stimmprofil, Vorgabe `matthias`. `matthias_dark_lord` geht auch. |
| `MIMIC_ANSAGE_STILL=1` | Schweigt, ohne den Hook auszubauen. Für lange Sitzungen am Schreibtisch. |
| `KOPFHOERER_MAC` | Überschreibt `~/.config/mimic/kopfhoerer`. |

Länge der Ansage: `GRENZE` in `tools/ansage.py`, aktuell 240 Zeichen. Gekürzt
wird an der Satzgrenze, Codeblöcke, Tabellen und Dateipfade fliegen vorher
raus — sonst liest die Stimme den Anfang eines Diffs vor.

## Was noch offen ist

- **Stimme wählen.** Vorgabe ist `matthias`. Ob `matthias_dark_lord` für
  Fertigmeldungen besser trägt, entscheidet das Ohr, nicht der Code.
- **`Notification` behalten oder nicht.** Der Hook meldet auch, wenn Claude auf
  eine Freigabe wartet. Nützlich mit Kopfhörer auf, nervig ohne — der Block ist
  einzeln aus `settings.json` entfernbar.
- **Aufwachzeit des Kopfhörers.** `FRIST=15` in `kopfhoerer.sh` ist geraten.
  Wenn der Kopfhörer aus dem Standby länger braucht, hier nachziehen.
- **Erste Silbe abgeschnitten?** Manche Geräte brauchen nach dem Verbinden
  einen Moment, bis die Senke wirklich trägt. Dann hilft ein `sleep 1` nach dem
  `pactl set-default-sink` in `kopfhoerer.sh`.

## Fehlersuche

| Symptom | Ursache |
|---|---|
| Nichts passiert | Hook nicht geladen — `/hooks` in Claude Code zeigt, was aktiv ist. |
| Hook läuft, kein Ton | `tools/ansage.py --sagen "Test"` von Hand; das zeigt, ob `mimic` oder die Senke klemmt. |
| Ton auf den Boxen | Kopfhörer nicht verbunden oder `pactl` fehlt — `tools/kopfhoerer.sh` laut aufrufen. |
| `worker_unavailable` | Dienst aus: `systemctl --user start mimic.socket mimic-worker.socket`. |
| Zweite Sitzung schweigt | Absicht — die Sperre lässt nur eine Ansage zur Zeit durch. |
