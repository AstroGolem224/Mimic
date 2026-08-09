#!/bin/sh
# Die Ansage-Kette Glied fuer Glied pruefen.
#
# Warum als Skript und nicht als Checkliste im Skill: die Ansage scheitert
# immer lautlos, und ihre sechs Glieder brechen sie einzeln und vollstaendig.
# Von aussen sieht jeder Bruch gleich aus -- naemlich nach Stille. Wer raet,
# prueft in der falschen Reihenfolge und landet beim Bluetooth, waehrend der
# Worker tot ist. Sechs Zeilen Ausgabe sind schneller als jede Vermutung.
#
# Aufruf:  .claude/skills/mimic-ansage/scripts/pruefen.sh
# Endet in 1, wenn mindestens ein Glied bricht.

set -u

WURZEL="$(cd "$(dirname "$0")/../../../.." && pwd)"
ZIEL="$HOME/.local/bin"
INSTALLIERT="$ZIEL/mimic-ansage"
QUELLE="$WURZEL/tools/ansage.py"
GLOBAL="$HOME/.claude/settings.json"
LOKAL="$WURZEL/.claude/settings.json"
KONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/mimic/kopfhoerer"
STIMMEN="$HOME/.local/share/mimic/voices"

SCHADEN=0
ok()   { echo "  ok    $*"; }
bruch() { echo "  BRUCH $*"; SCHADEN=1; }
hinweis() { echo "        $*"; }

echo "Ansage-Kette in $WURZEL"
echo

# ------------------------------------------------------------ 1. Hook eingehaengt
echo "1. Hook eingehaengt"
GEFUNDEN=0
for datei in "$GLOBAL" "$LOKAL"; do
    if [ -r "$datei" ] && grep -q "ansage" "$datei" 2>/dev/null; then
        ok "$datei"
        GEFUNDEN=1
    fi
done
if [ "$GEFUNDEN" -eq 0 ]; then
    bruch "weder $GLOBAL noch $LOKAL nennen die Ansage"
    hinweis "tools/einrichten.sh   (oder --nur-repo)"
fi

# --------------------------------------------------- 2. Installierte Kopie aktuell
echo
echo "2. Installierte Kopie"
if [ ! -x "$INSTALLIERT" ]; then
    bruch "$INSTALLIERT fehlt"
    hinweis "tools/einrichten.sh"
elif ! cmp -s "$INSTALLIERT" "$QUELLE"; then
    # Der haeufigste Fall von "ich hab's geaendert, es passiert nichts":
    # der Hook ruft die Kopie, nicht das Repo.
    bruch "$INSTALLIERT weicht von tools/ansage.py ab -- der Hook nutzt die alte Fassung"
    hinweis "tools/einrichten.sh"
else
    ok "aktuell"
fi

# ------------------------------------------------------------------- 3. Dienst
echo
echo "3. Dienst"
MIMIC="$(command -v mimic || echo "$ZIEL/mimic")"
if [ ! -x "$MIMIC" ]; then
    bruch "mimic ist nicht installiert"
    hinweis "uv tool install --python 3.12 ."
elif GRUND="$("$MIMIC" status 2>&1 >/dev/null)"; then
    ok "antwortet"
else
    bruch "antwortet nicht: $GRUND"
    hinweis "systemctl --user start mimic.socket mimic-worker.socket"
fi

# --------------------------------------------------------------- 4. Stimmprofil
echo
echo "4. Stimmprofil"
if [ -x "$INSTALLIERT" ]; then
    STIMME="$(python3 "$INSTALLIERT" --stimme 2>/dev/null)"
else
    STIMME="$(python3 "$QUELLE" --stimme 2>/dev/null)"
fi
STIMME="${STIMME:-unbekannt}"
if [ ! -x "$MIMIC" ]; then
    bruch "ohne mimic nicht pruefbar ($STIMME)"
elif "$MIMIC" voices 2>/dev/null | grep -qx "$STIMME"; then
    ok "$STIMME"
elif [ -d "$STIMMEN/$STIMME" ]; then
    # `mimic voices` verschweigt defekte Profile: cli.voices() ueberspringt
    # jeden VoiceError wortlos. Da liegt also eine Aufnahme, die der Dienst
    # ablehnt -- neu aufnehmen waere hier genau der falsche Reflex.
    bruch "$STIMME liegt unter $STIMMEN/, wird aber abgelehnt"
    hinweis "Grund im Klartext:  mimic say \"Probe\" --voice $STIMME"
    hinweis "Typisch: Rechte nicht 0700/0600, ref.wav nicht 48 kHz mono, Dauer ausserhalb 3-60 s"
else
    bruch "$STIMME ist nicht aufgenommen"
    hinweis "mimic record $STIMME"
fi

# ---------------------------------------------------------------- 5. Kopfhoerer
echo
echo "5. Kopfhoerer"
MAC=""
[ -r "$KONFIG" ] && MAC="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$KONFIG" | grep -m1 . || true)"
if [ -z "$MAC" ]; then
    # Kein Bruch: ohne MAC spricht die Ansage ueber die Boxen. Das ist eine
    # Entscheidung, kein Defekt.
    ok "keine MAC hinterlegt -- Ansage laeuft ueber die Standardausgabe"
elif ! command -v bluetoothctl >/dev/null 2>&1; then
    bruch "bluetoothctl fehlt, MAC ist aber hinterlegt ($MAC)"
elif bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; then
    ok "$MAC verbunden"
    bluetoothctl info "$MAC" 2>/dev/null | grep -q "Trusted: yes" \
        || hinweis "aber nicht getrustet -- das naechste automatische connect scheitert"
elif bluetoothctl info "$MAC" >/dev/null 2>&1; then
    bruch "$MAC gekoppelt, aber nicht verbunden"
    hinweis "tools/kopfhoerer.sh"
else
    bruch "$MAC ist nicht gekoppelt"
    hinweis "bluetoothctl -> scan on, pair, trust"
fi

# --------------------------------------------------------------------- 6. Senke
echo
echo "6. Standardsenke"
if ! command -v pactl >/dev/null 2>&1; then
    ok "pactl fehlt -- Senke setzt WirePlumber allein (meist richtig)"
elif [ -z "$MAC" ]; then
    ok "$(pactl get-default-sink 2>/dev/null || echo unbekannt)"
else
    SENKE="$(pactl get-default-sink 2>/dev/null || true)"
    case "$SENKE" in
        *"$(echo "$MAC" | tr ':' '_')"*) ok "$SENKE" ;;
        "") bruch "keine Standardsenke ermittelbar" ;;
        *)  bruch "Standardsenke ist $SENKE, nicht der Kopfhoerer"
            hinweis "tools/kopfhoerer.sh   (setzt sie)" ;;
    esac
fi

echo
if [ "$SCHADEN" -eq 0 ]; then
    echo "Kette steht. Bleibt es trotzdem still, den Text pruefen:"
    echo "  python3 tools/ansage.py --vorschau < hook.json"
else
    echo "Mindestens ein Glied bricht -- oben steht welches."
fi
exit "$SCHADEN"
