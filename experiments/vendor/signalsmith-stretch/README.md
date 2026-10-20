# Signalsmith Stretch, web release 1.3.2 (MIT)

`SignalsmithStretch.mjs` is the ES-module web build of Signalsmith Stretch (Geraint Luff / Signalsmith Audio),
npm `signalsmith-stretch@1.3.2` (https://www.npmjs.com/package/signalsmith-stretch, tarball sha1
8549203d56c12c1ddf6f4083e4781bab19eae839), identical to `web/release/SignalsmithStretch.mjs` at commit 222093b
of https://github.com/Signalsmith-Audio/signalsmith-stretch. `LICENSE.txt` is that repository's MIT licence (the
npm package ships none). The WASM is inlined; nothing else is needed. Used by `experiments/build_player.py`,
which copies both files next to the player page.

OpenMusic patch (two places, marked `OpenMusic patch`): a `setChannelMask(mask)` method on the worklet, and the
input copy feeds channels whose mask is false as silence. Muted or non-soloed stems then stay out of the shared
phase analysis of the multichannel node, so a soloed stem is stretched as cleanly as on its own node
(`experiments/tests/player_test.py --cpu`). Unpatched file: sha256
97530b11d5bc01015af4cde40d6aa55ff10c40aa1294ca4c8c5762027d517a46.
