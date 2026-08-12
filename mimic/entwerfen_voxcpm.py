"""Entwirft Stimmen mit VoxCPM2. Laeuft in einer FREMDEN Umgebung.

Dieses Modul wird NICHT von Mimic importiert. `mimic/entwurf.py` startet es als
Skript in der VoxCPM-venv. Also **kein Import aus dem Paket `mimic`** -- nur
Standardbibliothek plus voxcpm/torch/soundfile. Ein Test wacht darueber.

  python entwerfen_voxcpm.py '{"instruction":"...","text":"...","anzahl":3,"aus":"/tmp/x"}'

Ausgabe: eine JSON-Zeile je Ereignis auf stdout, damit die GUI mitlesen kann.
Dasselbe Protokoll wie entwerfen_qwen.py -- entwurf.py kennt nur dieses.

VoxCPM2 spricht Deutsch nativ (30 Sprachen) und braucht keine Sprachmarke: der
deutsche Text geht direkt hinein. Genau das war der Grund fuer den Wechsel weg
von MOSS-VoiceGenerator, der laut eigenem Paper nur Chinesisch und Englisch
gesehen hat. Preis, gehoert am 2026-08-12: die Aussprache sitzt nicht immer --
"Weg" als Strasse statt als fort, gelegentlich ein englisch gelesenes Wort.
Deshalb mehrere Kandidaten und Abhoeren vor dem Uebernehmen.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Feste Revision, ermittelt 2026-08-12 ueber die HF-API. Dieselbe wie in
# spike2/revisions.yaml -- dort dokumentiert, hier dupliziert, weil spike2/
# laut README Wegwerfcode ist und die App nicht davon abhaengen darf.
MODELL_REPO = "openbmb/VoxCPM2"
MODELL_REVISION = "bffb3df5a29440629464e5e839f4d214c8714c3d"

# Aus der Modellkarte. cfg_value steuert, wie streng die Beschreibung befolgt
# wird, inference_timesteps ist die Zahl der Diffusionsschritte.
CFG = 2.0
SCHRITTE = 10


def melde(**felder: object) -> None:
    print(json.dumps(felder, ensure_ascii=False), flush=True)


def main() -> int:
    auftrag = json.loads(sys.argv[1])
    beschreibung = auftrag["instruction"]
    text = auftrag["text"]
    anzahl = int(auftrag.get("anzahl", 3))
    aus = pathlib.Path(auftrag["aus"])
    aus.mkdir(parents=True, exist_ok=True)

    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from voxcpm import VoxCPM

    geraet = "cuda" if torch.cuda.is_available() else "cpu"
    melde(kind="laden", geraet=geraet)
    pfad = snapshot_download(repo_id=MODELL_REPO, revision=MODELL_REVISION)
    # optimize=False ist torch.compile aus. Phase 0 hat dafuer bei dots.tts
    # 94 s Kaltstart statt 7 s gemessen; wer drei Kandidaten will, zahlt die
    # Kompilierzeit teurer als die gesparte Rechenzeit.
    # load_denoiser=False: der Entrauscher arbeitet auf Referenzaudio, und beim
    # Entwerfen gibt es keins.
    modell = VoxCPM.from_pretrained(pfad, load_denoiser=False, optimize=False,
                                    device=geraet)
    rate = modell.tts_model.sample_rate
    melde(kind="bereit", rate=rate)

    # Die Beschreibung steht in Klammern VOR dem Text, im selben String --
    # so will es die Modellkarte, es gibt kein eigenes Feld dafuer.
    eingabe = f"({beschreibung}){text}"
    for k in range(anzahl):
        try:
            welle = modell.generate(text=eingabe, cfg_value=CFG,
                                    inference_timesteps=SCHRITTE)
        except Exception as fehler:          # noqa: BLE001 -- ein Fehlwurf killt nicht den Lauf
            melde(kind="fehlwurf", nummer=k, grund=f"{type(fehler).__name__}: {fehler}"[:150])
            continue
        ziel = aus / f"kandidat_{k}.wav"
        sf.write(ziel, welle, rate)
        melde(kind="kandidat", nummer=k, datei=str(ziel),
              dauer=round(len(welle) / rate, 1))
    melde(kind="fertig")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as fehler:            # noqa: BLE001 -- die GUI braucht den Grund, nicht den Stack
        melde(kind="fehler", grund=f"{type(fehler).__name__}: {fehler}"[:300])
        sys.exit(1)
