#!/bin/sh
# Ansage am Arbeitsrechner einrichten: installieren, MAC hinterlegen,
# Hook eintragen, Hoerprobe. Alles, was ohne Hardware nicht geht, an einer
# Stelle -- der Rest ist gebaut und geprueft.
#
#   tools/einrichten.sh                     fragt nach dem Kopfhoerer
#   tools/einrichten.sh XX:XX:XX:XX:XX:XX   nimmt die MAC direkt
#   tools/einrichten.sh --nur-repo          ohne globalen Hook
#
# Zweimal aufrufen ist gefahrlos: jeder Schritt prueft erst, ob er noetig ist.
# Was das Skript NICHT tut, ist koppeln -- das ist einmalig, interaktiv und
# braucht den PIN-Dialog. Es sagt dir, wenn es dran ist.

set -eu

WURZEL="$(cd "$(dirname "$0")/.." && pwd)"
ZIEL="$HOME/.local/bin"
KONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/mimic/kopfhoerer"
GLOBAL=1
MAC=""

for argument in "$@"; do
    case "$argument" in
        --nur-repo) GLOBAL=0 ;;
        --hilfe|-h) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) MAC="$argument" ;;
    esac
done

schritt() { echo; echo "== $* =="; }
warnen() { echo "   ! $*"; }

# ---------------------------------------------------------------- 1. Werkzeuge

schritt "Werkzeuge"
fehlt=""
for werkzeug in python3 bluetoothctl; do
    if command -v "$werkzeug" >/dev/null 2>&1; then
        echo "   $werkzeug"
    else
        fehlt="$fehlt $werkzeug"
    fi
done
if [ -n "$fehlt" ]; then
    echo "   Es fehlt:$fehlt" >&2
    exit 1
fi

if command -v mimic >/dev/null 2>&1 || [ -x "$ZIEL/mimic" ]; then
    echo "   mimic"
else
    warnen "mimic ist nicht installiert -- 'uv tool install --python 3.12 .' im Repo."
    warnen "Der Rest wird trotzdem eingerichtet, die Hoerprobe faellt aus."
fi
command -v pactl >/dev/null 2>&1 || \
    warnen "pactl fehlt -- die Standardsenke setzt dann WirePlumber allein."

# ------------------------------------------------------------------ 2. Kopfhoerer

schritt "Kopfhoerer"
if [ -z "$MAC" ] && [ -r "$KONFIG" ]; then
    MAC="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$KONFIG" | grep -m1 . || true)"
    [ -n "$MAC" ] && echo "   $MAC (aus $KONFIG)"
fi

if [ -z "$MAC" ]; then
    GEKOPPELT="$(bluetoothctl devices Paired 2>/dev/null || true)"
    if [ -z "$GEKOPPELT" ]; then
        echo "   Kein Geraet gekoppelt. Das ist der eine Schritt, der von Hand geht:"
        echo
        echo "     bluetoothctl"
        echo "       scan on"
        echo "       pair  XX:XX:XX:XX:XX:XX"
        echo "       trust XX:XX:XX:XX:XX:XX     # ohne trust scheitert jedes spaetere connect"
        echo "       quit"
        echo
        echo "   Danach dieses Skript nochmal aufrufen."
        exit 1
    fi
    echo "$GEKOPPELT" | nl -w3 -s'  '
    printf "   Nummer des Kopfhoerers (Enter bricht ab): "
    read -r wahl || wahl=""
    [ -n "$wahl" ] || { echo "   Abgebrochen."; exit 1; }
    MAC="$(echo "$GEKOPPELT" | sed -n "${wahl}p" | awk '{print $2}')"
    [ -n "$MAC" ] || { echo "   Keine Zeile $wahl." >&2; exit 1; }
fi

mkdir -p "${KONFIG%/*}"
printf '%s\n' "$MAC" > "$KONFIG"
chmod 600 "$KONFIG"
echo "   $MAC -> $KONFIG"

# ---------------------------------------------------------------- 3. Installieren

schritt "Installieren"
install -Dm755 "$WURZEL/tools/ansage.py"     "$ZIEL/mimic-ansage"
install -Dm755 "$WURZEL/tools/kopfhoerer.sh" "$ZIEL/kopfhoerer.sh"
echo "   $ZIEL/mimic-ansage"
echo "   $ZIEL/kopfhoerer.sh"
# Muessen nebeneinander liegen: mimic-ansage sucht kopfhoerer.sh im eigenen
# Verzeichnis, nicht im PATH.

# --------------------------------------------------------------------- 4. Hook

schritt "Hook"
if [ "$GLOBAL" -eq 1 ]; then
    python3 "$ZIEL/mimic-ansage" --einhaengen
    echo "   Gilt fuer alle Projekte. Claude Code neu starten, dann zeigt /hooks es an."
else
    echo "   Uebersprungen (--nur-repo). Im Mimic-Repo greift .claude/settings.json."
fi

# ----------------------------------------------------------------- 5. Hoerprobe

schritt "Hoerprobe"
if ! "$ZIEL/kopfhoerer.sh"; then
    warnen "Kopfhoerer nicht verbunden -- die Ansage kaeme ueber die Boxen."
fi

if command -v mimic >/dev/null 2>&1 || [ -x "$ZIEL/mimic" ]; then
    printf "   Probesatz sprechen? [Enter] ja, [n] nein: "
    read -r antwort || antwort="n"
    if [ "$antwort" != "n" ]; then
        python3 "$ZIEL/mimic-ansage" --sagen \
            "Fertig. Die Ansage steht, du hoerst mich ab jetzt bei jeder erledigten Aufgabe."
    fi
else
    echo "   Ausgelassen, mimic fehlt."
fi

schritt "Steht"
echo "   Stumm schalten:   MIMIC_ANSAGE_STILL=1"
echo "   Andere Stimme:    MIMIC_ANSAGE_STIMME=matthias_dark_lord"
echo "   Was gesprochen wird, Stellschrauben, Fehlersuche: tools/ANSAGE.md"
