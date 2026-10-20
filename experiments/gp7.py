"""Guitar Pro 7/8 (.gp) files through alphaTab, for export_gp.py and its tests.

A .gp file is a zip whose Content/score.gpif is UTF-8 XML, so Chinese titles survive it; Guitar Pro 5 files do
not (Guitar Pro reads their text in a single-byte code page, also on macOS). No maintained Python library writes
.gp, so this runs alphaTab (CoderLine, MPL-2.0, vendored in experiments/vendor/alphatab/) in V8 embedded in
Python (mini-racer, ISC): alphaTab's Guitar Pro 5 importer reads the .gp5 that export_gp.py wrote, the title and
other text are set again in full Unicode, and its Guitar Pro 7 exporter writes the .gp. Chord symbols are names
only (no empty diagrams), and tempo changes are steps. One fix-up after alphaTab: on a drum track it writes each
note's General MIDI key where Guitar Pro expects the index of the kit piece in the track's drum kit, so the
notes are pointed at the piece whose InputMidiNumbers hold that key (fix_drums).

    gp_bytes = to_gp(gp5_bytes, {"title": "中南海", "artist": "Carsick Cars"})
    summary(gp_bytes)   # {"title", "artist", "bars", "tempo", "signatures", "tracks": [{"name", "notes", ...}]}
"""
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ALPHATAB = Path(__file__).resolve().parent / "vendor" / "alphatab" / "alphaTab.min.js"
VERSION = "1.8.4"   # of the vendored alphaTab, see its README.md

# alphaTab takes a bare V8 for a browser and reaches for `window`; a `process` object makes it take its
# Node.js path, which needs neither window nor document. V8 alone has no TextEncoder / TextDecoder, which
# alphaTab uses for the UTF-8 text of the files, so minimal UTF-8 versions are given here.
SHIM = r"""
globalThis.process = {[Symbol.toStringTag]: 'process', env: {}};
globalThis.TextEncoder = class {
  encode(s) {
    const out = [];
    for (const ch of s) {
      const c = ch.codePointAt(0);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xc0 | c >> 6, 0x80 | c & 63);
      else if (c < 0x10000) out.push(0xe0 | c >> 12, 0x80 | c >> 6 & 63, 0x80 | c & 63);
      else out.push(0xf0 | c >> 18, 0x80 | c >> 12 & 63, 0x80 | c >> 6 & 63, 0x80 | c & 63);
    }
    return new Uint8Array(out);
  }
};
globalThis.TextDecoder = class {
  decode(b) {
    b = b instanceof Uint8Array ? b : new Uint8Array(b.buffer || b);
    let s = '';
    for (let i = 0; i < b.length;) {
      const c = b[i++];
      if (c < 0x80) s += String.fromCharCode(c);
      else if (c < 0xe0) s += String.fromCharCode((c & 31) << 6 | b[i++] & 63);
      else if (c < 0xf0) s += String.fromCharCode((c & 15) << 12 | (b[i++] & 63) << 6 | b[i++] & 63);
      else s += String.fromCodePoint((c & 7) << 18 | (b[i++] & 63) << 12 | (b[i++] & 63) << 6 | b[i++] & 63);
    }
    return s;
  }
};
"""

GLUE = r"""
globalThis.openmusic = {
  bytes(hex) {
    const b = new Uint8Array(hex.length / 2);
    for (let i = 0; i < b.length; i++) b[i] = parseInt(hex.substr(2 * i, 2), 16);
    return b;
  },
  hex(b) {
    const h = new Array(b.length);
    for (let i = 0; i < b.length; i++) h[i] = (b[i] < 16 ? '0' : '') + b[i].toString(16);
    return h.join('');
  },
  load(hex) {
    const settings = new alphaTab.Settings();
    settings.importer.encoding = 'utf-8';
    return alphaTab.importer.ScoreLoader.loadScoreFromBytes(openmusic.bytes(hex), settings);
  },
  toGp(hex, meta) {
    const score = openmusic.load(hex);
    Object.assign(score, meta);
    for (const track of score.tracks)
      for (const staff of track.staves)
        for (const chord of staff.chords?.values() ?? []) { chord.showDiagram = false; chord.showFingering = false; }
    // tempo changes are steps, not ramps (the GP5 importer marks them linear), and bar 1 states its tempo once
    for (const bar of score.masterBars)
      bar.tempoAutomations = bar.tempoAutomations.filter((a, i, all) => {
        a.isLinear = false;
        return all.findIndex(b => b.ratioPosition === a.ratioPosition && b.value === a.value) === i;
      });
    return openmusic.hex(new alphaTab.exporter.Gp7Exporter().export(score, new alphaTab.Settings()));
  },
  summary(hex) {
    const score = openmusic.load(hex);
    return JSON.stringify({
      title: score.title, artist: score.artist, subtitle: score.subTitle, bars: score.masterBars.length,
      tempo: score.tempo, signatures: score.masterBars.map(m => m.timeSignatureNumerator),
      tracks: score.tracks.map(track => {
        const staff = track.staves[0];
        let notes = 0, chords = 0;
        const kit = {};   // drums: General MIDI key of each struck note -> count
        for (const bar of staff.bars)
          for (const voice of bar.voices)
            for (const beat of voice.beats) {
              const struck = beat.notes.filter(n => !n.isTieDestination);
              notes += struck.length;
              if (beat.chordId) chords++;
              if (staff.isPercussion)
                for (const n of struck) {
                  const a = track.percussionArticulations[n.percussionArticulation];
                  const key = a ? a.outputMidiNumber : n.percussionArticulation;   // .gp5: the key itself
                  kit[key] = (kit[key] || 0) + 1;
                }
            }
        return {name: track.name, notes, chords, percussion: staff.isPercussion, tab: staff.showTablature,
                notation: staff.showStandardNotation, tuning: Array.from(staff.stringTuning.tunings), kit};
      }),
    });
  },
};
"""

_ctx = None


def engine():
    """V8 with alphaTab loaded, made once (about 0.1 s)."""
    global _ctx
    if _ctx is None:
        try:
            from py_mini_racer import MiniRacer
        except ImportError as e:
            raise SystemExit(f"mini-racer is needed for .gp files: pip install -r requirements.txt ({e})")
        ctx = MiniRacer()
        ctx.eval(SHIM)
        ctx.eval(ALPHATAB.read_text(encoding="utf-8"))
        ctx.eval(GLUE)
        _ctx = ctx
    return _ctx


def to_gp(gp5, meta):
    """A Guitar Pro 5 file's bytes -> .gp bytes, with the score fields in meta (title, artist, subTitle, ...)
    set in full Unicode."""
    return fix_drums(bytes.fromhex(engine().call("openmusic.toGp", gp5.hex(), meta)))


def fix_drums(data):
    """Point the notes of drum tracks at their kit piece: alphaTab writes the General MIDI key (42 = closed
    hi-hat), Guitar Pro reads the index of the articulation in the track's kit (3 for alphaTab's kit)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [(info, z.read(info)) for info in z.infolist()]
    gpif = next(content for info, content in entries if info.filename == "Content/score.gpif").decode("utf-8")
    root = ET.fromstring(gpif)
    tracks = root.findall("Tracks/Track")
    drums = {i: t for i, t in enumerate(tracks) if t.findtext("InstrumentSet/Type") == "drumKit"}
    if not drums:
        return data
    ids = lambda parent, tag: {e.get("id"): (e.findtext(tag) or "").split() for e in root.findall(parent)}  # noqa: E731
    bar_voices, voice_beats, beat_notes = ids("Bars/Bar", "Voices"), ids("Voices/Voice", "Beats"), ids("Beats/Beat", "Notes")
    index_of = {}   # note id -> {key: articulation index} of its drum kit
    for master in root.findall("MasterBars/MasterBar"):
        bar_ids = master.findtext("Bars").split()
        for track_index, track in drums.items():
            kit = {}
            for i, art in enumerate(track.findall("InstrumentSet/Elements/Element/Articulations/Articulation")):
                for key in (art.findtext("InputMidiNumbers") or "").split():
                    kit.setdefault(key, str(i))
            for voice in bar_voices.get(bar_ids[track_index], []):
                for beat in voice_beats.get(voice, []):
                    for note in beat_notes.get(beat, []):
                        index_of[note] = kit

    def fix(m):
        kit = index_of.get(m.group(1))
        if kit is None:
            return m.group(0)
        return re.sub(r"<InstrumentArticulation>(\d+)</InstrumentArticulation>",
                      lambda a: f"<InstrumentArticulation>{kit.get(a.group(1), a.group(1))}</InstrumentArticulation>",
                      m.group(0))

    gpif = re.sub(r'<Note id="(\d+)">.*?</Note>', fix, gpif, flags=re.S)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info, content in entries:
            z.writestr(info, gpif.encode("utf-8") if info.filename == "Content/score.gpif" else content)
    return out.getvalue()


def summary(data):
    """What alphaTab reads from a .gp (or .gp5) file: title, artist, bars, tempo, time signatures, and per
    track its name, struck notes (not ties), chord symbols, percussion, tab / notation, tuning and, for drums,
    the count of each General MIDI key played."""
    return json.loads(engine().call("openmusic.summary", data.hex()))
