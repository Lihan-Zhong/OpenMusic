# alphaTab 1.8.4 (MPL-2.0)

`alphaTab.min.js` is the UMD build `dist/alphaTab.min.js` of alphaTab (Daniel Kuschny and contributors), npm
`@coderline/alphatab@1.8.4` (https://www.npmjs.com/package/@coderline/alphatab, tarball sha256
70b2c77abc9698a055d90a11d77308b6b41247936476e969d85770e3d4db4602; file sha256
2d0335501b875453d52359de23cd9cebfcf71aed3d5739f1cf95117acfd52bec), unmodified. Source code:
https://github.com/CoderLine/alphaTab. `LICENSE` is the package's licence (MPL-2.0); the libraries alphaTab
integrates (TinySoundFont, SFZero, Haxe std, SharpZipLib, NVorbis: MIT; libvorbis: BSD-3-Clause) are listed in
the licence header at the top of `alphaTab.min.js`.

Used by `experiments/gp7.py`, which runs it in an embedded V8 engine (mini-racer) to write the score that
`experiments/export_gp.py` lays out (handed over as Guitar Pro 5 bytes in memory, never saved) as a Guitar Pro 7/8
`.gp` file (alphaTab's GP5 importer and GP7 exporter), and to read `.gp` files back for the checks. Nothing else of alphaTab (rendering, player, fonts,
soundfont) is used.
