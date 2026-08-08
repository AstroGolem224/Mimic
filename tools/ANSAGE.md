# Ansage: Claude Code meldet sich per Mimic

Wenn Claude Code eine Aufgabe abschließt, spricht Mimic das Ergebnis über den
Bluetooth-Kopfhörer — »Fertig.« plus die ersten Sätze der letzten Antwort. Der
Kopfhörer wird dabei bei Bedarf selbst zurückgeholt.

Diese Datei ist die Übergabe: alles bis auf Pairing und Hörprobe ist gebaut,
der Rest steht unten als Ablauf. Alles Weitere braucht die Hardware und läuft
deshalb erst am PC.

## Teile

| Datei | Aufgabe |
|---|---|
| `tools/ansage.py` | Hook. Liest das Transkript, kürzt, ruft `mimic say`. |
| `tools/kopfhoerer.sh` | Verbindet den Kopfhörer und setzt die Standardsenke. |
| `.claude/settings.json` | Verdrahtung für dieses Repo (`Stop` und `Notification`). |
| `tests/test_ansage.py` | Textpfad ohne Audio; läuft in `tests/run.sh` mit. |

Der Hook koppelt sich sofort ab und ist nach Millisekunden fertig — die
Sitzung wartet nie auf das Sprechen. Fehlt der Dienst, fehlt `mimic`, ist die
MAC nicht eingetragen: jeder Pfad endet in 0 und schweigt. Eine Sperre in
`$XDG_RUNTIME_DIR/mimic-ansage.lock` verhindert, dass zwei Sitzungen
gleichzeitig sprechen; wer sie nicht bekommt, schweigt.

## Einrichtung am PC

**1. Kopfhörer einmalig koppeln.** Interaktiv, macht das Skript bewusst nicht:

```bash
bluetoothctl
  scan on
  pair XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX      # ohne trust scheitert jedes spätere connect
  quit
```

**2. MAC hinterlegen.**

```bash
mkdir -p ~/.config/mimic
echo XX:XX:XX:XX:XX:XX > ~/.config/mimic/kopfhoerer
tools/kopfhoerer.sh              # ohne MAC listet es die gekoppelten Geräte
```

**3. Verbindung prüfen.** Zweimal aufrufen — der zweite Lauf muss sofort
zurückkommen, weil schon verbunden ist:

```bash
tools/kopfhoerer.sh
tools/kopfhoerer.sh --status
```

**4. Text prüfen, ohne zu sprechen.** `--vorschau` zeigt genau den Satz, der
später aus dem Kopfhörer kommt:

```bash
echo '{"hook_event_name":"Stop","transcript_path":"'"$(ls -t ~/.claude/projects/*/*.jsonl | head -1)"'"}' \
  | python3 tools/ansage.py --vorschau
```

**5. Hörprobe.** Braucht den laufenden Dienst (`systemctl --user status mimic.socket`):

```bash
tools/ansage.py --sagen "Fertig. Zwei Tests repariert, alles grün."
```

**6. Hook scharf schalten.** Für dieses Repo liegt `.claude/settings.json`
schon da — Claude Code neu starten oder `/hooks` aufrufen, damit es geladen
wird. Für **alle** Projekte gehört derselbe Block nach
`~/.claude/settings.json`, dann aber mit festem Pfad statt
`$CLAUDE_PROJECT_DIR`:

```bash
install -Dm755 tools/ansage.py     ~/.local/bin/mimic-ansage
install -Dm755 tools/kopfhoerer.sh ~/.local/bin/kopfhoerer.sh
```

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python3 $HOME/.local/bin/mimic-ansage", "timeout": 10 } ] }
    ]
  }
}
```

`kopfhoerer.sh` muss dabei neben `mimic-ansage` liegen — der Hook sucht es im
eigenen Verzeichnis.

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
