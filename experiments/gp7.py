"""Guitar Pro 7/8 (.gp) files through alphaTab, for export_gp.py and its tests.

A .gp file is a zip whose Content/score.gpif is UTF-8 XML, so Chinese titles survive it (Guitar Pro reads the
text of the older .gp5 format in a single-byte code page, also on macOS). No maintained Python library writes
.gp, so this runs alphaTab (CoderLine, MPL-2.0, vendored in experiments/vendor/alphatab/) in V8 embedded in
Python (mini-racer, ISC). export_gp.py lays the score out with PyGuitarPro and hands it over as Guitar Pro 5 bytes
in memory, with UTF-8 text; alphaTab's Guitar Pro 5 importer reads them and its Guitar Pro 7 exporter writes the
.gp. No .gp5 file is ever written. Chord symbols are names only (no empty diagrams), and tempo changes are steps.
An audio file can go in as Guitar Pro 8's audio track (alphaTab's backing track), placed by sync points, i.e.
where in the audio given bars start. Its bytes are put into the zip here, as they are, rather than passed
through V8, and the parts alphaTab writes differently from Guitar Pro 8 are laid out as in a .gp that Guitar
Pro 8 saved itself (alphaTab's test file guitarpro8/canon-audio-track.gp): the file is stored as
Content/Assets/<id>.<its extension> (alphaTab: Content/Assets/backing-track, no extension, which Guitar Pro 8
plays as silence), the asset lists its original path and id, the audio track's YouTubeVideoUrl / Filter /
FramesPerPixel sit next to its ChannelStrip rather than inside it, Assets comes last, there is no empty
AudioTracks, and meta.json says hasAudio (gp8_audio).
One fix-up after alphaTab: on a drum track it writes each note's General MIDI key where Guitar Pro expects the
index of the kit piece in the track's drum kit, so the notes are pointed at the piece whose InputMidiNumbers
hold that key (fix_drums).

    gp_bytes = to_gp(gp5_bytes, audio=mp3_bytes, sync=[(0, 0.0), (1, 1000.0), ...])   # (bar index, ms)
    dump(gp_bytes)      # what alphaTab reads back: title, bars, audio, and every beat and note of every track
"""
import hashlib
import io
import json
import re
import uuid
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
      else {
        const cp = (c & 7) << 18 | (b[i++] & 63) << 12 | (b[i++] & 63) << 6 | b[i++] & 63;
        s += cp <= 0x10ffff ? String.fromCodePoint(cp) : '\ufffd';   // malformed bytes, as real decoders do
      }
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
  toGp(hex, audio, sync) {
    const score = openmusic.load(hex);
    if (audio) {   // a 1-byte stand-in; gp7.py puts the real file into the zip
      score.backingTrack = new alphaTab.model.BackingTrack();
      score.backingTrack.rawAudioFile = new Uint8Array(1);
      score.applyFlatSyncPoints(sync.map(([bar, ms]) => ({barIndex: bar, barOccurence: 0, barPosition: 0,
                                                            millisecondOffset: ms})));
    }
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
  dump(hex) {
    const score = openmusic.load(hex);
    return JSON.stringify({
      title: score.title, artist: score.artist, subtitle: score.subTitle, instructions: score.instructions,
      tempo: score.tempo, backing_track: !!score.backingTrack,
      sync: score.exportFlatSyncPoints().map(p => [p.barIndex, p.barOccurence, p.barPosition, p.millisecondOffset]),
      bars: score.masterBars.map(m => ({numerator: m.timeSignatureNumerator, denominator: m.timeSignatureDenominator,
                                         key: m.keySignature, minor: m.keySignatureType === 1, ticks: m.calculateDuration(),
                                         tempos: m.tempoAutomations.map(a => [a.value, a.isLinear])})),
      tracks: score.tracks.map(track => {
        const staff = track.staves[0];
        const beats = [];
        staff.bars.forEach((bar, index) => {
          for (const beat of bar.voices[0].beats)
            beats.push({bar: index, tick: beat.absolutePlaybackStart, ticks: beat.playbackDuration,
                        value: beat.duration, dots: beat.dots, tuplet: [beat.tupletNumerator, beat.tupletDenominator],
                        rest: beat.isRest, chord: beat.chord ? beat.chord.name : null,
                        notes: beat.notes.map(n => {
                          const a = track.percussionArticulations[n.percussionArticulation];
                          return {pitch: staff.isPercussion ? (a ? a.outputMidiNumber : n.percussionArticulation) : n.realValue,
                                  string: n.string, fret: n.fret, tie: n.isTieDestination};
                        })});
        });
        return {name: track.name, percussion: staff.isPercussion, channel: track.playbackInfo.primaryChannel,
                mute: track.playbackInfo.isMute, solo: track.playbackInfo.isSolo,
                tab: staff.showTablature,
                notation: staff.showStandardNotation, tuning: Array.from(staff.stringTuning.tunings),
                chords_shown: Array.from(staff.chords?.values() ?? []).filter(c => c.showDiagram).length, beats};
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


ALPHATAB_ASSET = "Content/Assets/backing-track"   # where alphaTab puts the audio track's file
ASSETS = "Content/Assets/"                        # where Guitar Pro 8 does: Content/Assets/<id>.<extension>


def to_gp(gp5, audio=None, sync=(), audio_name="audio.mp3"):
    """Guitar Pro 5 bytes (text in UTF-8) -> .gp bytes; with audio (a file's bytes, any format Guitar Pro 8
    reads; audio_name is its file name, whose extension tells Guitar Pro the format), that file as the audio
    track, placed by sync = [(bar index, ms into the audio where it starts)]."""
    data = bytes.fromhex(engine().call("openmusic.toGp", gp5.hex(), bool(audio), [list(p) for p in sync]))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [(info, z.read(info)) for info in z.infolist() if info.filename != ALPHATAB_ASSET]
    if not audio:
        return rezip(entries, fix_drums)
    asset_id = str(uuid.UUID(bytes=hashlib.sha1(audio).digest()[:16], version=5))  # as Guitar Pro: from the file
    asset = f"{ASSETS}{asset_id}{Path(audio_name).suffix.lower()}"
    file_info = zipfile.ZipInfo(asset)
    file_info.compress_type = zipfile.ZIP_DEFLATED   # as Guitar Pro 8 stores it; folder and meta.json stored
    entries += [(zipfile.ZipInfo(ASSETS), b""), (file_info, audio),
                (zipfile.ZipInfo("meta.json"), b'{\n    "hasAudio": true,\n    "version": "1.0.0"\n}\n')]
    return rezip(entries, lambda gpif: gp8_audio(fix_drums(gpif), asset, asset_id, audio_name))


def audio_entry(z):
    """The name of the audio track's file in an open .gp zip, or None."""
    return next((n for n in z.namelist() if n.startswith(ASSETS) and n != ASSETS), None)


def cdata(text):
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def gp8_audio(gpif, asset, asset_id, audio_name):
    """score.gpif with the audio track's parts laid out as Guitar Pro 8 saves them (see the module docstring)."""
    def element(name, text):
        m = re.search(rf"<{name}>(.*?)</{name}>|<{name}\s*/>", text, re.S)
        return m.group(0) if m else f"<{name}/>"

    track = re.search(r"<BackingTrack>(.*?)</BackingTrack>", gpif, re.S).group(1)
    strip = re.search(r"<ChannelStrip>(.*?)</ChannelStrip>", track, re.S).group(1)
    backing = "<BackingTrack>" + "".join(element(n, track) for n in (
        "IconId", "Color", "Name", "ShortName", "PlaybackState")) + \
        f"<ChannelStrip>{element('Parameters', strip)}</ChannelStrip>" + \
        "".join(element(n, track) for n in ("Enabled", "Source", "AssetId")) + \
        "".join(element(n, strip) for n in ("YouTubeVideoUrl", "Filter", "FramesPerPixel")) + \
        "".join(element(n, track) for n in ("FramePadding", "Semitones", "Cents")) + "</BackingTrack>"
    gpif = gpif.replace(f"<BackingTrack>{track}</BackingTrack>", backing)
    asset_id_attr = re.search(r'<Asset id="([^"]*)"', gpif).group(1)
    gpif = re.sub(r"<Assets>.*?</Assets>", "", gpif, flags=re.S)
    gpif = re.sub(r"<AudioTracks\s*/>|<AudioTracks>\s*</AudioTracks>", "", gpif)
    assets = (f'<Assets><Asset id="{asset_id_attr}"><OriginalFilePath>{cdata(audio_name)}</OriginalFilePath>'
              f"<OriginalFileSha1>{asset_id}</OriginalFileSha1><EmbeddedFilePath>{cdata(asset)}</EmbeddedFilePath>"
              "</Asset></Assets>")
    return gpif.replace("</GPIF>", assets + "</GPIF>")


def rezip(entries, edit_gpif=None):
    """A .gp's bytes from its zip entries [(ZipInfo, bytes)], score.gpif passed through edit_gpif first."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info, content in entries:
            if info.filename == "Content/score.gpif" and edit_gpif:
                content = edit_gpif(content.decode("utf-8")).encode("utf-8")
            z.writestr(info, content)
    return out.getvalue()


def fix_drums(gpif):
    """score.gpif with the notes of drum tracks pointed at their kit piece: alphaTab writes the General MIDI
    key (42 = closed hi-hat), Guitar Pro reads the index of the articulation in the track's kit (3 there)."""
    root = ET.fromstring(gpif)
    tracks = root.findall("Tracks/Track")
    drums = {i: t for i, t in enumerate(tracks) if t.findtext("InstrumentSet/Type") == "drumKit"}
    if not drums:
        return gpif
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

    return re.sub(r'<Note id="(\d+)">.*?</Note>', fix, gpif, flags=re.S)


def dump(data):
    """What alphaTab reads from a .gp file: title, artist, subtitle, instructions, tempo, whether there is an
    audio track and its size in bytes (audio_bytes), the sync points [bar index, occurrence, position in the
    bar, ms]; per bar its time signature, key, length in ticks (960 per quarter) and tempo changes; per track
    its name, drums or not, MIDI channel, mute / solo, tab / notation, tuning, chord diagrams shown, and every
    beat of voice 1 (tick from the start, length, note value, dots, tuplet, rest, chord name, notes: pitch
    (drums: General MIDI key), string (1 = lowest), fret, tie). The audio itself stays out of V8."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [(info, z.read(info)) for info in z.infolist()]
        name = audio_entry(z)
    audio = next((content for info, content in entries if info.filename == name), b"")
    if audio:
        data = rezip([(info, b"\0" if info.filename == name else content) for info, content in entries])
    song = json.loads(engine().call("openmusic.dump", data.hex()))
    song["audio_bytes"], song["audio_file"] = len(audio), name
    return song


def summary(song):
    """A dump boiled down: {track name: number of struck notes (not ties)}."""
    return {t["name"]: sum(not n["tie"] for b in t["beats"] for n in b["notes"]) for t in song["tracks"]}
