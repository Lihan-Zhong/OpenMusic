# Drums to MIDI

[中文](drums.md) | **English** · [Back to README](../README.en.md)

`transcribe.py` now transcribes the drums too. It sorts every hit on the drum stem into one of 9 kit pieces and writes `transcription/drums.mid`, a General MIDI drum track on channel 10: kick 36, snare 38, high tom 50, mid tom 47, floor tom 43, hi-hat 42, open hi-hat 46, crash 49, ride 51. It uses the same tempo map as the other stems, so the bars line up in Guitar Pro. It also writes `drums.notes.json` for the practice player's drum view, and the Guitar Pro file from the workbench gets a drum track.

## In short

- **Kick and snare are usable as they are**: on real recordings, with the drums split from the mix by Demucs, kick F ≈ 0.9 and snare F ≈ 0.8, so the groove is basically right.
- **Hi-hat is a usable draft**: F ≈ 0.65–0.75. The main mistakes are mixing it up with the ride, and open with closed.
- **Toms and cymbals are hints only**: toms are rare in real songs, and both misses and false hits are common (F 0.2–0.5). Crashes are roughly found; rides are often written as hi-hat.
- Timing is tight: a few milliseconds off the reference on average, 6–8 ms standard deviation.
- **For more accuracy, install the optional ADTOF-pytorch plugin yourself** (README, "Optional plugin"): on real recordings it reaches hi-hat F 0.79 and cymbals 0.83, much better than the built-in model. Its weights are under a non-commercial licence, so it is not distributed with OpenMusic. Scores below under "Optional plugin ADTOF-pytorch".

## How it works

1. **Find the places where something may have been hit**: a log-frequency spectrogram of the drum stem (12 bands per octave, 10 ms frames), then the peaks where many bands get louder at once, at least 30 ms apart. This finds a few more places than there are hits; the next step drops the rest.
2. **Decide what was hit there**: a small neural network (an MLP, 1040 → 256 → 64 → 9) looks at the coarse spectrum (3 bands per octave) from 30 ms before to 150 ms after, plus how loud it still is 150–450 ms later (closed vs open hi-hat, hi-hat vs crash and ride), and gives a probability for each kit piece. One hit can be several pieces at once (kick + hi-hat, snare + crash, ...). It sees the spectrum twice: relative to the loudest part of the stem, and relative to each band's own loudest level, so that different mixes and EQ look more alike.
3. **Tidy up**: per hit, only the likeliest of closed / open hi-hat, of the three toms, and of crash / ride is kept. Toms need a probability of 0.7, the others 0.5. Velocity comes from how many dB the hit is below the piece's loudest hit in the song, measured in that piece's band.
4. Silence: candidates more than 40 dB below the stem's loudest frames are not hits.

The network's weights, `experiments/models/drums.npz` (521 KB), were trained by us with `experiments/train_drums.py` on:

- **Part of E-GMD** (Callender, Hawthorne & Engel, 2020, CC BY 4.0): human performances on an e-drum kit, each recorded through 43 kits, with the MIDI of what was played. From its train split we take 5 grooves and 3 fills per kit: 344 files, 1.7 hours. Each file is also used twice more with random processing (EQ, compression, room reverb, quiet bass/guitar bleed, an MP3-like low-pass), so that it also fits drum stems split from a mix.
- **150 synthetic loops** (`experiments/drum_synth.py`): random synthetic kits playing random grooves. With E-GMD alone the model only knew E-GMD's 43 sampled kits and missed most synthesised hi-hats (hi-hat F only 0.4–0.7 in the synthetic test). With these loops added, synthetic hi-hats reach F 0.93–0.97 and the scores on real recordings stay about the same (see below).

## Why not an existing model

We first surveyed open-source drum transcription. The conditions were code and weights under licences compatible with AGPL-3.0, and installable on macOS arm64 and Windows with Python 3.12:

| Option | What we found | Verdict |
|---|---|---|
| ADTOF (Zehren et al.) | The whole repository (code and models) is CC BY-NC-SA 4.0 (non-commercial); needs TensorFlow and madmom, tested only on Python 3.10, no command line | not distributed with OpenMusic |
| ADTOF-pytorch (port by Xavier Riley) | No LICENSE file yet; in issue #3 the author says the code will be MIT and the weights, derived from ADTOF, stay CC BY-NC-SA 4.0; needs only torch and librosa, has a command line | not distributed with OpenMusic, **can be installed as an optional plugin** (README, "Optional plugin") |
| omnizart | Code MIT, no licence stated for the weights; needs TensorFlow and madmom (madmom's models are CC BY-NC-SA) | not used |
| Magenta Onsets and Frames Drums (E-GMD) | Code Apache-2.0; magenta 2.1.4 pins TensorFlow 2.9.1 and numba 0.49.1, which do not install on Python 3.12 | not used |
| MT3, YourMT3 | Large multi-instrument transcription models (JAX / large PyTorch); we only need drums | too heavy |
| ADT_STR (2026) | Code CC BY-SA 4.0; the model card gives no weight licence and no source for its training samples; pins torch 2.8 | not used |
| DrumScript | Apache-2.0, rule-based, alpha; requires numpy < 2 and brings demucs, music21, reportlab | dependency clash, not used |
| DrumSep (aufr33 & jarredou, MDX23C kit separation) | No formal licence for the weights; the original release page is gone | cannot use |
| inagoy/drumsep (Demucs kit separation) | Repository MIT; weights on Google Drive, training data unknown; only 4 classes (kick, snare, cymbals, toms) | not used |
| LarsNet, madmom's drum models | Non-commercial (CC BY-NC) | cannot use |

So OpenMusic's own method is signal-processing onsets plus a small classifier we train ourselves. It needs no new dependencies (numpy, librosa and scikit-learn are already in use), the training data is CC BY 4.0, and one command reproduces the training.

## Results

F-measure: a hit counts as found within 50 ms (mir_eval's onset matching), summed over all files. Hi-hat = closed + open, toms = the three toms, cymbals = crash + ride; single pieces (e.g. "ride") must also have the right piece.

**Real recordings: MDB Drums** (Southall et al., 2017; hand annotations of 23 studio recordings from MedleyDB, CC BY-NC-SA 4.0; evaluated locally only, never trained on). We use the MIREX 2017 split: the 12 dev tracks were looked at while choosing the design, the 11 test tracks were run once at the end.

| | Kick | Snare | Hi-hat | Toms | Cymbals | Open hi-hat | Crash | Ride |
|---|---|---|---|---|---|---|---|---|
| Test, drums split from the mix (Demucs 6) | **0.90** | **0.82** | **0.64** | 0.16 | 0.49 | 0.33 | 0.29 | 0.43 |
| Test, the original drum stem | 0.95 | 0.85 | 0.63 | 0.37 | 0.62 | 0.45 | 0.35 | 0.62 |
| Dev, drums split from the mix (Demucs 6) | 0.83 | 0.68 | 0.75 | 0.33 | 0.36 | 0.40 | 0.50 | 0.24 |
| Dev, the original drum stem | 0.92 | 0.80 | 0.81 | 0.27 | 0.38 | 0.41 | 0.59 | 0.28 |

- The dev half has 4 jazz tracks (brushes, ride, very soft kick), hence the lower snare score; the test half has few toms and cymbals (75 tom hits).
- Most missed snares are ghost notes; most extra hi-hats are hi-hat pedal hits (which MDB counts as hi-hat) and rides.

**E-GMD performances not used for training** (the same 43 kits, one clip each):

| | Kick | Snare | Hi-hat | Toms | Cymbals |
|---|---|---|---|---|---|
| Validation | 0.94 | 0.83 | 0.80 | 0.83 | 0.85 |
| Test | 0.92 | 0.81 | 0.86 | 0.83 | 0.83 |

**E-GMD only vs with synthetic loops** (MDB dev, drums split by Demucs / the synthetic test):

| | Kick | Snare | Hi-hat | Toms | Cymbals | Synthetic hi-hat |
|---|---|---|---|---|---|---|
| E-GMD only | 0.82 | 0.70 | 0.75 | 0.29 | 0.36 | 0.42–0.67 |
| With 150 synthetic loops (the current model) | 0.83 | 0.68 | 0.75 | 0.33 | 0.36 | 0.93–0.97 |

**Synthetic test** (`experiments/tests/test_drums.py`, a few seconds): two fixed synthetic kits (training used other, random ones) play rock, funk 16ths, a tom fill, ride and open hi-hats, one with reverb and bleed, plus a 160 BPM punk beat, a 20 s break, and a near-silent drum stem. Kick and snare 0.99–1.00, hi-hat 0.93–0.97, toms 0.91–0.98, cymbals 0.53–0.83 (the synthetic ride is not convincing), no false hits in the 17 s gap, timing ±6–8 ms. This shows the whole chain works (onsets, classification, MIDI, tempo map). The scores are optimistic and say little about real songs.

## Optional plugin ADTOF-pytorch

With the plugin installed (README, "Optional plugin"), `transcribe.py` runs its `adtof` command in a separate process and reads the MIDI file it writes: the plugin decides where the hits are and which of its 5 classes they belong to (kick, snare, toms, hi-hat, cymbals); the built-in model's probabilities at the same moment pick the piece within the class (which tom, open or closed hi-hat, crash or ride), and the velocities are estimated as for the built-in model. The same evaluation (`train_drums.py --eval --adtof`, `test_drums.py --adtof`):

| F-measure | Kick | Snare | Hi-hat | Toms | Cymbals | Closed hi-hat | Open hi-hat | Crash | Ride |
|---|---|---|---|---|---|---|---|---|---|
| MDB test, drums split by Demucs: built-in | 0.90 | 0.82 | 0.64 | 0.16 | 0.49 | 0.61 | 0.33 | 0.29 | 0.43 |
| MDB test, drums split by Demucs: plugin | **0.94** | **0.83** | **0.79** | **0.30** | **0.83** | 0.70 | 0.46 | 0.28 | 0.74 |
| MDB test, original drum stem: built-in | 0.95 | 0.85 | 0.63 | 0.37 | 0.62 | 0.55 | 0.45 | 0.35 | 0.62 |
| MDB test, original drum stem: plugin | 0.97 | 0.86 | 0.79 | 0.53 | 0.86 | 0.72 | 0.52 | 0.46 | 0.80 |
| MDB dev, drums split by Demucs: built-in | 0.83 | 0.68 | 0.75 | 0.33 | 0.36 | 0.74 | 0.40 | 0.50 | 0.24 |
| MDB dev, drums split by Demucs: plugin | 0.90 | 0.70 | 0.90 | 0.78 | 0.87 | 0.87 | 0.48 | 0.37 | 0.50 |
| E-GMD test: built-in | 0.92 | 0.81 | 0.86 | 0.83 | 0.83 | 0.85 | 0.60 | 0.59 | 0.84 |
| E-GMD test: plugin | 0.80 | 0.72 | 0.80 | 0.58 | 0.83 | 0.79 | 0.52 | 0.56 | 0.84 |

- On real recordings the plugin is clearly better, above all for hi-hat and cymbals; on e-drum recordings (E-GMD, which the built-in model was trained on) the built-in model is better.
- Caveat: ADTOF's papers use MDB Drums as a test set, but we could not check whether the released weights saw it during training, so the plugin's MDB scores may be somewhat optimistic.
- Synthetic test (`test_drums.py --adtof`): kick 1.00, snare 0.93–1.00, hi-hat 0.94–0.95, toms 0.96–1.00, cymbals 0.40–1.00, no false hits in the gap; timing 6 ms early on average.
- On the user's 3 songs the plugin and the built-in model agree on kick and snare (0.90–1.00); they disagree on hi-hat vs cymbals: in "中南海" the built-in model writes 191 cymbal hits (mostly ride), the plugin calls most of them hi-hat (818 hi-hat, 41 cymbal hits). Going by the scores above, the plugin is the one to trust.
- Speed: about 10 s for a 6-minute song on the Mac's CPU (the first start of PyTorch is slower).

## On the user's songs

There is no reference for the 3 songs in `outputs/mp3` and `outputs/demucs6_full` (drums split by Demucs 6), so we checked:

- **Does the groove make sense**: in Carsick Cars' "中南海", 51% of the snare hits are on beat 2 and 49% on beat 4, the kick is mostly on 1 and 3, the hi-hat plays 8ths with open hi-hats on the "and": a typical rock beat. In 想想's "偷看你的日记" the snare is also on 2 and 4, and the chorus has ride (or hi-hat, hard to tell) 8ths.
- **On the grid**: 89–99% of the hits are within 25 ms of the 16th-note grid.
- **Silence**: "中南海" has no drums from 140 to 306 s, and nothing is written there.
- **MP3 vs WAV stems give the same result?** Kick 0.95–1.00, snare 0.6–1.00, hi-hat 0.4–0.94, ride 0.75–0.93. The intro track (soft, sparse drums) is the least stable, so decisions there sit close to the thresholds.
- 想想's two songs have many "rides" (397–486); some are probably hi-hats. Check them by ear.

## Known gaps

- Ride vs hi-hat and open vs closed hi-hat are often confused: they sound alike, and mostly differ in how long they ring.
- Toms: rare in real songs but frequent in E-GMD's fills, so their threshold is high (0.7), at the cost of many misses.
- Soft ghost notes and hi-hat pedal hits are often missed; rim shots, china/splash as their own pieces, cowbell and tambourine are not recognised.
- Separation quality matters: strong onsets of bass or guitar left in the drum stem are sometimes taken for drums.

## Reproduce

```bash
.venv/bin/python experiments/tests/test_drums.py           # synthetic test, a few seconds
.venv/bin/python experiments/train_drums.py                # download 430 E-GMD files (about 0.8 GB, to data/e-gmd/), train, write models/drums.npz
git clone --depth 1 https://github.com/CarlSouthall/MDBDrums data/MDBDrums   # MDB Drums (about 1.4 GB, CC BY-NC-SA, evaluation only)
.venv/bin/python experiments/train_drums.py --separate demucs6             # split MDB's mixes with Demucs 6
.venv/bin/python experiments/train_drums.py --eval                         # scores on E-GMD validation / test and MDB Drums
```

E-GMD is large (96 GB); `train_drums.py` reads only the files it needs, with HTTP range requests. Training takes about 7 minutes on an M2 Pro, not counting the download, mostly computing features. All random numbers have fixed seeds, so the same files and library versions give nearly the same weights.
