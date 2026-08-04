"""Die zwei Stellen im Spike, die still falsch sein koennen.

Beides sind Funktionen, deren Fehler nicht auffallen wuerden: ein kaputter
Pegelangleich macht Kriterium B unbemerkt zu einem Lautstaerketest, und ein
kaputtes Perzentil laesst Kriterium C bestehen oder durchfallen, ohne dass
irgendwo etwas rot wird.

Aufruf:  uv run python test_spike.py
"""

from __future__ import annotations

import importlib
import numpy as np

blindtest = importlib.import_module("04_blindtest")
latenz = importlib.import_module("01_latenz")


def test_angleichen_bringt_beide_auf_denselben_pegel() -> None:
    rng = np.random.default_rng(0)
    leise = (rng.standard_normal(48000) * 0.01).astype(np.float32)
    laut = (rng.standard_normal(48000) * 0.30).astype(np.float32)

    a, b = blindtest.angleichen(leise), blindtest.angleichen(laut)
    rms = lambda x: 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)))

    # Der Punkt der Uebung: 30 dB Unterschied vorher, keiner nachher.
    assert abs(rms(leise) - rms(laut)) > 20, "Testdaten taugen nicht"
    assert abs(rms(a) - rms(b)) < 0.5, f"Pegel weichen ab: {rms(a):.1f} vs {rms(b):.1f}"
    assert abs(rms(a) - blindtest.ZIEL_RMS_DBFS) < 0.5, "Zielpegel nicht getroffen"


def test_angleichen_begrenzt_peak() -> None:
    # Ein Signal mit sehr niedrigem RMS und einem Ausreisser: die Skalierung
    # auf -23 dBFS RMS wuerde den Ausreisser weit ueber Vollaussteuerung heben.
    x = np.zeros(48000, dtype=np.float32)
    x[::1000] = 0.02
    y = blindtest.angleichen(x)
    assert np.max(np.abs(y)) <= 0.98 + 1e-6, f"Peak {np.max(np.abs(y))} nicht begrenzt"


def test_angleichen_ueberlebt_stille() -> None:
    # Eine leere oder stille Aufnahme darf nicht durch Division durch null
    # den ganzen Test abbrechen.
    y = blindtest.angleichen(np.zeros(1000, dtype=np.float32))
    assert np.all(np.isfinite(y)), "Stille erzeugt NaN/Inf"


def test_perzentil_gegen_numpy() -> None:
    rng = np.random.default_rng(1)
    for n in (1, 2, 7, 50, 199):
        w = list(rng.random(n) * 0.5)
        for p in (0.5, 0.9, 0.95, 0.99):
            eigen = latenz.perzentil(w, p)
            ref = float(np.percentile(w, p * 100))
            assert abs(eigen - ref) < 1e-9, f"n={n} p={p}: {eigen} != {ref}"


def test_perzentil_haengt_nicht_an_der_reihenfolge() -> None:
    w = [0.3, 0.1, 0.9, 0.2, 0.5]
    assert latenz.perzentil(w, 0.95) == latenz.perzentil(sorted(w, reverse=True), 0.95)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} Checks bestanden.")
