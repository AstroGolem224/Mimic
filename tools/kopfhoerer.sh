#!/bin/sh
# Bluetooth-Kopfhoerer verbinden und zur Standardsenke machen.
#
# Zwei Aufrufer, zwei Erwartungen:
#
#   kopfhoerer.sh                 von Hand oder per Tastenkuerzel. Redet,
#                                 meldet Fehler ehrlich mit Code != 0.
#   kopfhoerer.sh --sicherstellen aus tools/ansage.py, vor jeder Ansage.
#                                 Schweigt, endet immer in 0 und kehrt sofort
#                                 zurueck, wenn ohnehin schon verbunden ist.
#
# Die MAC steht in ~/.config/mimic/kopfhoerer (eine Zeile) oder in
# $KOPFHOERER_MAC. Ohne hinterlegte MAC ist das Skript ein No-Op -- so kann
# der Ansage-Hook es bedenkenlos aufrufen, bevor die Adresse eingetragen ist.
#
# Gekoppelt wird hier nicht. Das Pairing ist einmalig, interaktiv und braucht
# den PIN-Dialog: `bluetoothctl` -> scan on -> pair <MAC> -> trust <MAC>.
# `trust` ist der Teil, der zaehlt: ohne ihn lehnt der Kopfhoerer die
# unbeaufsichtigte Verbindung spaeter ab.

set -eu

KONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/mimic/kopfhoerer"
FRIST=15            # Sekunden, die wir dem Kopfhoerer zum Aufwachen geben

STILL=0
case "${1-}" in
    --sicherstellen) STILL=1 ;;
    --status)        STILL=2 ;;
    "")              ;;
    *) echo "Aufruf: $(basename "$0") [--sicherstellen|--status]" >&2; exit 2 ;;
esac

sage() { if [ "$STILL" -ne 1 ]; then echo "$@"; fi; }
# Im stillen Modus ist Scheitern erlaubt: die Ansage laeuft dann eben ueber
# die Boxen. Ein Rueckgabewert != 0 wuerde nur den Hook verunsichern.
ende() { if [ "$STILL" -eq 1 ]; then exit 0; fi; exit "$1"; }

mac() {
    if [ -n "${KOPFHOERER_MAC-}" ]; then
        echo "$KOPFHOERER_MAC"
    elif [ -r "$KONFIG" ]; then
        sed -e 's/#.*//' -e 's/[[:space:]]//g' "$KONFIG" | grep -m1 . || true
    fi
}

MAC="$(mac)"
if [ -z "$MAC" ]; then
    sage "Keine MAC hinterlegt. Gekoppelte Geraete:"
    [ "$STILL" -eq 1 ] || bluetoothctl devices Paired 2>/dev/null || true
    sage "Eintragen mit:  mkdir -p '${KONFIG%/*}' && echo XX:XX:XX:XX:XX:XX > '$KONFIG'"
    ende 1
fi

if ! command -v bluetoothctl >/dev/null 2>&1; then
    sage "bluetoothctl fehlt -- bluez ist nicht installiert."
    ende 1
fi

verbunden() { bluetoothctl info "$MAC" 2>/dev/null | grep -q "Connected: yes"; }

if [ "$STILL" -eq 2 ]; then
    bluetoothctl info "$MAC" 2>/dev/null || { echo "$MAC ist nicht gekoppelt."; exit 1; }
    exit 0
fi

if ! verbunden; then
    if ! bluetoothctl info "$MAC" >/dev/null 2>&1; then
        sage "$MAC ist nicht gekoppelt -- einmalig 'bluetoothctl' -> pair/trust."
        ende 1
    fi
    sage "Verbinde $MAC ..."
    bluetoothctl connect "$MAC" >/dev/null 2>&1 || true
    # `connect` kehrt zurueck, bevor das Profil steht. Ohne dieses Warten
    # sucht der naechste Schritt eine Senke, die es noch nicht gibt.
    wartezeit=0
    while ! verbunden; do
        wartezeit=$((wartezeit + 1))
        [ "$wartezeit" -gt "$FRIST" ] && { sage "$MAC antwortet nicht."; ende 1; }
        sleep 1
    done
fi

# Standardsenke setzen. WirePlumber schaltet meist von selbst um, aber nicht,
# wenn gerade etwas anderes spielt -- dann bliebe die Ansage auf den Boxen.
if command -v pactl >/dev/null 2>&1; then
    SENKE="$(pactl list short sinks 2>/dev/null \
             | awk -v m="$(echo "$MAC" | tr ':' '_')" '$2 ~ m {print $2; exit}')"
    if [ -n "${SENKE-}" ]; then
        pactl set-default-sink "$SENKE" >/dev/null 2>&1 || true
        sage "Standardsenke: $SENKE"
    else
        sage "Verbunden, aber keine PipeWire-Senke fuer $MAC gefunden."
    fi
else
    sage "Verbunden. (pactl fehlt -- Standardsenke bleibt, wie WirePlumber sie setzt.)"
fi

exit 0
