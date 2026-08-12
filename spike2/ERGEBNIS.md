# Phase 0b — Abnahme

Abgeschlossen 2026-08-12. Hardware: RTX 5090 (Blackwell sm_120), CachyOS.
Software: Python 3.12, torch 2.9.1+cu128, transformers 5.0.0, Checkpoints auf
feste HF-Revisionen gepinnt (`revisions.yaml`).

**Ergebnis: die Kette steht. Entwerfen mit MOSS-VoiceGenerator (1.7B, englisch),
sprechen mit MOSS-TTS-Local-Transformer-v1.5 (4B, deutsch). dots.tts bleibt für
Matthias' eigene Stimme und für Streaming.**

## Die Frage, die dieser Spike beantwortet hat

Kann eine Charakterstimme *entworfen* statt aufgenommen werden, und spricht sie
danach brauchbares Deutsch?

Ja — aber nicht in einem Schritt. Der Entwerfer kann kein Deutsch, die
Sprech-Engine kann kein Entwerfen. Erst die Trennung der beiden Aufgaben löst es.

## Kriterien

| # | Kriterium | Schwelle | Ergebnis | |
|---|---|---|---|---|
| A | Blackwell, echte GPU-Arbeit | VRAM > 1 GiB, Last > 20 % | `sm_120` in `get_arch_list()`, bf16-Matmul und Synthese laufen auf der GPU | **PASS**¹ |
| B | Entwurf brauchbar | ≥ 6 von 12 | 5 Figuren von 24 entworfenen | **FAIL**² |
| C | Deutsch aus englischer Referenz | kein hörbarer Akzent | dots.tts: hörbarer englischer Akzent. MOSS 4B: sauber | **dots FAIL / MOSS PASS** |
| D | Charaktererhalt beim Klonen | ≥ 4 von 6 verblindet | nicht gemessen | — |
| E | Streaming-Latenz | TTFA p95 < 300 ms | nicht gemessen | — |
| F | Rangfolge der Engines | verblindet | offen gehört, nicht verblindet: MOSS 4B vor dots.tts auf deutschem Text mit entworfener Referenz | **teilweise** |

¹ Qualitativ belegt, nicht mit `02_gpu.py` beziffert. Das Skript ist geschrieben,
aber nicht gelaufen — die Frage war vorher entschieden.

² Der Zahl nach durchgefallen, und das bleibt so stehen. Von 12 Entwürfen des
ersten Satzes waren 3 brauchbar (`bot_kalt` → `computer`, `modron_1` → `data`,
`modron_2` → `boardcomputer`), vom zweiten Satz 2 (`sterbende_ki`, `drohne`).
Die Schwelle war zu hoch angesetzt für ein Verfahren, bei dem drei Kandidaten
je Beschreibung entstehen und Nachwerfen nichts kostet. Fünf brauchbare Figuren
aus einem Nachmittag sind praktisch ein Erfolg, formal ein Durchfall. Beides
steht hier.

## Was gemessen wurde und was dabei herauskam

**MOSS-VoiceGenerator (1.7B) kann kein Deutsch.** Die Modellkarte nennt
Chinesisch und Englisch. Zwölf Kandidaten mit zweisprachigem Text: alle
durchgefallen. Ein vierter Satz mit **deutschen Anweisungen** statt englischen
änderte daran nichts Hörbares — die Sprache hängt am Text, nicht an der
Anweisung. Das Modell bleibt trotzdem im Einsatz: als Entwerfer englischer
Timbres ist es gut, und mehr wird nicht von ihm verlangt.

**dots.tts trägt eine englisch entworfene Referenz nicht ins Deutsche.**
Hörbarer englischer Akzent auf den deutschen Wörtern. Das ist kein Fehler von
dots.tts — Phase 0 hat es mit Matthias' eigener deutscher Referenz bestanden.
Es ist die Grenze des Verfahrens: dots.tts hat keine Sprachmarke, es klont, was
es hört.

**MOSS-TTS-Local-Transformer-v1.5 (4B) trägt sie.** Dieselben zwei Referenzen,
derselbe zweisprachige Text, Sprachmarke `German`: sauber. Die Kontrolle ohne
Referenz ebenfalls. 48 kHz Stereo nativ.

**Effektketten dürfen nicht auf die Referenz.** `spike_ki_gefaerbt` — dieselbe
Stimme mit der `glitch`-Kette als `ref.wav` — lieferte dreimal
`silent_audio: zwei stumme Takes erzeugt`, auch bei einem Zweiwortsatz. Kein
Pegelproblem (rms 0.057). Bitreduktion und Flanger zerstören, woran dots.tts
die Stimme festmacht. Die Ketten gehören an den Ausgang.

**Die Effektketten waren beim ersten Wurf durchweg zu stark.** Von zwölf ließ
Matthias eine durchgehen (`maschine`). Alle anderen sind auf deren Maß
heruntergezogen; die starken Werte stehen als Kommentar in `faerben.py`.

## Stolpersteine, die Zeit gekostet haben

Alle vier sind im Code an ihrer Fundstelle begründet, damit sie kein zweites
Mal gesucht werden müssen.

| Symptom | Ursache | Lösung |
|---|---|---|
| `Unrecognized model in MOSS-Audio-Tokenizer` | der Prozessor reicht `revision` an ein fremdes Repo weiter | beide Repos vorher per `snapshot_download` holen, lokale Pfade übergeben (`laden.py`) |
| dasselbe mit transformers 5.15.0 | Versionsdrift | auf `transformers==5.0.0` gepinnt, wie in der Modellkarte |
| `TorchCodec is required` | torchaudio 2.9 leitet `load`/`save` an torchcodec, das nur ffmpeg 4–8 kann; hier liegt ffmpeg 9 | `soundfile` statt `torchaudio.save`, `torchaudio.load` durch drei Zeilen ersetzt |
| leere oder unzerlegbare Ausgabe des Entwerfers | Sampling bei `temperature 1.5` | bis zu drei Würfe je Kandidat, dann auslassen |

## Was daraus folgt

Zwei Engines, zwei Aufgaben. Das ist keine Verdopplung, sondern die
Aufgabenteilung, die die Messung erzwungen hat:

- **dots.tts** — Matthias' eigene Stimme, Streaming, die Ansage. Unverändert,
  Phase 0 gilt.
- **MOSS 4B** — entworfene Charakterstimmen, deutsche und zweisprachige Texte,
  Batch. Neu.
- **MOSS-VoiceGenerator** — Entwurfsschicht davor, englisch, liefert nur die
  Referenz.
- **`faerben.py`** — Effekte, am Ausgang, nie an der Referenz.

**Qwen3-TTS wurde nicht mehr gemessen.** Der Dreiervergleich war angesetzt, um
einen Gewinner zu finden; MOSS 4B hat die Frage vorher beantwortet. Ein Lauf,
dessen Ergebnis keine Entscheidung mehr ändert, ist Arbeit ohne Deckung. Der
geparkte Code in `alternative/qwen3` bleibt liegen, die Revision ist in
`revisions.yaml` festgehalten — nachholbar, wenn MOSS im Betrieb enttäuscht.

## Offen

- D und E sind ungemessen. E wird erst gebraucht, wenn eine Charakterstimme in
  den Streaming-Pfad soll; dafür wäre `MOSS-TTS-Realtime` (1.7B) der
  Kandidat, nicht das 4B.
- Ob die 4B-Ausgabe ihrerseits als `ref.wav` taugt, damit dots.tts sie streamen
  kann, ist nicht geprüft. Das wäre der Weg, eine entworfene Figur in die
  Live-Ansage zu bekommen, ohne ein zweites Modell in den Dienst zu hängen.
- Die `de_*`-Entwürfe (deutsche Anweisungen) liegen unbeurteilt in
  `out/entwurf/`.
