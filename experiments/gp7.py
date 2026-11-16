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
Two fix-ups after alphaTab: on a drum track it writes each note's General MIDI key where Guitar Pro expects the
index of the kit piece in the track's drum kit, so the notes are pointed at the piece whose InputMidiNumbers
hold that key (fix_drums); and the score's layout fields are written as Guitar Pro 8 writes them (gp8_layout).

    gp_bytes = to_gp(gp5_bytes, audio=mp3_bytes, sync=[(0, 0.0), (1, 1000.0), ...])   # (bar index, ms)
    dump(gp_bytes)      # what alphaTab reads back: title, bars, audio, and every beat and note of every track
    midi, info = to_midi(gp_bytes)   # alphaTab's player MIDI of a Guitar Pro file, for note blocks (export_nbs.py)
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
  // the score made ready for note blocks (see gp7.to_midi): {score, dead: how many dead notes and dead slaps it
  // has}; with deadOnly, the dead notes and dead slaps are all it keeps, otherwise they are what it leaves out
  forBlocks(hex, deadOnly) {
    const score = openmusic.load(hex);
    // a repeat count is just a number in the file, and alphaTab plays every repetition: keep it to 8 plays
    for (const bar of score.masterBars) if (bar.repeatCount > 8) bar.repeatCount = 8;
    const Slide = alphaTab.model.SlideOutType, Brush = alphaTab.model.BrushType;
    let dead = 0;
    for (const track of score.tracks)
      for (const staff of track.staves)
        for (const bar of staff.bars)
          for (const voice of bar.voices)
            for (const beat of voice.beats) {
              // alphaTab plays the target of a legato slide by bending the note it slides from up or down to it,
              // not by striking it (unless the target has a trill, tremolo or ornament), and a note block cannot
              // bend: as a shift slide's target it is struck, and a tremolo, trill or ornament on the note it
              // slides from now ends there instead of running on at that note's pitch
              for (const note of beat.notes) if (note.slideOutType === Slide.Legato) note.slideOutType = Slide.Shift;
              // alphaTab starts each string of a strum a little after the one before, the last as late as the
              // strum's brushDuration (mostly a few hundredths of a second; a .gp5's slowest stroke is an 8th), and
              // a note block tick is 0.1 s: so that the last strings do not land a tick or more after the chord, a
              // strum's strings start together (an arpeggio keeps its spread)
              if (beat.brushType === Brush.BrushUp || beat.brushType === Brush.BrushDown) beat.brushDuration = 0;
              if (staff.isPercussion) {                  // a drum kit: every hit is a drum, in the main MIDI
                if (deadOnly) while (beat.notes.length) beat.removeNote(beat.notes[0]);
                continue;
              }
              // dead notes (x) and dead slaps have no pitch: alphaTab plays a dead note at its fret's pitch and a
              // dead slap as a 64th on every open string (never the beat's notes), and export_nbs plays them as a
              // click, from a MIDI of their own
              if (beat.deadSlapped) {
                dead++;
                while (beat.notes.length) beat.removeNote(beat.notes[0]);
                if (!deadOnly) beat.deadSlapped = false;
                continue;
              }
              dead += beat.notes.filter(n => n.isDead).length;
              for (const note of beat.notes.slice()) if (note.isDead !== deadOnly) beat.removeNote(note);
            }
    return {score, dead};
  },
  midiOf(score) {
    const midi = new alphaTab.midi.MidiFile();
    midi.format = alphaTab.midi.MidiFileFormat.MultiTrack;
    const handler = new alphaTab.midi.AlphaSynthMidiFileHandler(midi, true);
    new alphaTab.midi.MidiFileGenerator(score, new alphaTab.Settings(), handler).generate();
    return openmusic.hex(midi.toBinary());
  },
  toMidi(hex) {
    // alphaTab's player MIDI of the score as forBlocks left it: bars in playing order (repeats, endings), every
    // voice, tied notes once; one MIDI track per score track (tempo and time signatures on track 0), 960 ticks
    // per quarter
    let {score, dead} = openmusic.forBlocks(hex, false);
    const out = {
      midi: openmusic.midiOf(score), title: score.title, artist: score.artist,
      // an anacrusis (pickup) bar gets no time signature of its own in the MIDI: its length in quarters
      pickup: (bar => bar && bar.isAnacrusis ? bar.calculateDuration() / 960 : 0)(score.masterBars[0]),
      tracks: score.tracks.map(track => ({
        name: track.name, percussion: track.staves.some(s => s.isPercussion), mute: track.playbackInfo.isMute,
        program: track.playbackInfo.program,
        channels: [track.playbackInfo.primaryChannel, track.playbackInfo.secondaryChannel]})),
    };
    score = null;                                        // V8 may collect it before the second load
    out.dead = dead ? openmusic.midiOf(openmusic.forBlocks(hex, true).score) : null;
    return JSON.stringify(out);
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


def to_gp(gp5, audio=None, sync=(), audio_name="audio.mp3", padding=0):
    """Guitar Pro 5 bytes (text in UTF-8) -> .gp bytes; with audio (a file's bytes, any format Guitar Pro 8
    reads; audio_name is its file name, whose extension tells Guitar Pro the format), that file as the audio
    track, placed by sync = [(bar index, ms into the audio where it starts)]. padding = Guitar Pro's frame
    padding in 44.1 kHz frames: negative skips that much of the audio's start, and the sync points then count
    from there (Guitar Pro 8's own canon-audio-track.gp has -72900)."""
    data = bytes.fromhex(engine().call("openmusic.toGp", gp5.hex(), bool(audio), [list(p) for p in sync]))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = [(info, z.read(info)) for info in z.infolist() if info.filename != ALPHATAB_ASSET]
    if not audio:
        return rezip(entries, lambda gpif: gp8_layout(fix_drums(gpif)))
    asset_id = str(uuid.UUID(bytes=hashlib.sha1(audio).digest()[:16], version=5))  # as Guitar Pro: from the file
    asset = f"{ASSETS}{asset_id}{Path(audio_name).suffix.lower()}"
    file_info = zipfile.ZipInfo(asset)
    file_info.compress_type = zipfile.ZIP_DEFLATED   # as Guitar Pro 8 stores it; folder and meta.json stored
    entries += [(zipfile.ZipInfo(ASSETS), b""), (file_info, audio),
                (zipfile.ZipInfo("meta.json"), b'{\n    "hasAudio": true,\n    "version": "1.0.0"\n}\n')]
    return rezip(entries, lambda gpif: gp8_audio(gp8_layout(fix_drums(gpif)), asset, asset_id, audio_name, padding))


def audio_entry(z):
    """The name of the audio track's file in an open .gp zip, or None."""
    return next((n for n in z.namelist() if n.startswith(ASSETS) and n != ASSETS), None)


def cdata(text):
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def gp8_layout(gpif):
    """score.gpif with the score's layout fields as Guitar Pro 8 writes them: the default bars per line
    (ScoreSystemsDefaultLayout) and the bars of each line (ScoreSystemsLayout) as plain text, not in CDATA
    sections as alphaTab writes them, and MultiVoice 1 (alphaTab: "1>"). Guitar Pro 8 does not read the CDATA
    number: a .gp of ours that it saved again had -1955444992 bars per line, and opening that file, Guitar Pro
    laid the score out without end (ScoreEngravingContext::repairFixedLayout) until the Mac ran out of memory."""
    gpif = re.sub(r"<(ScoreSystems(?:Default)?Layout)><!\[CDATA\[([\d ]*)\]\]></\1>", r"<\1>\2</\1>", gpif)
    return gpif.replace("<MultiVoice>1></MultiVoice>", "<MultiVoice>1</MultiVoice>")


def gp8_audio(gpif, asset, asset_id, audio_name, padding=0):
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
        f"<FramePadding>{int(padding)}</FramePadding>" + \
        "".join(element(n, track) for n in ("Semitones", "Cents")) + "</BackingTrack>"
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


MAX_UNPACKED = 64 << 20   # a score's own files (its audio track left out) unpack to a few MB
MAX_V8 = 1 << 30          # alphaTab's memory for one score (a whole song takes about 0.3 GB)


def to_midi(data):
    """(MIDI bytes, info) of a Guitar Pro file (.gp; also .gpx and .gp3-.gp5, which alphaTab reads as well) as
    alphaTab plays it: a type-1 MIDI file with one track per score track (the score's track i is MIDI track i;
    tempo and time signatures on track 0), bars in playing order (repeats and endings, each repeat played at
    most 8 times), every voice, a tied note once, drums as General MIDI keys on channel 10. Made for note blocks,
    it differs from alphaTab's player in three ways: the target of a legato slide is struck (a note block cannot
    bend to it); a strum's strings start together (an arpeggio keeps its spread, but one that mixes dead notes
    with pitched ones spreads each kind on its own, as each MIDI holds only its own notes); and the dead notes (x)
    and dead slaps of pitched tracks, which have no pitch, are left out, into info["dead"]. info = {title, artist,
    pickup (the length in quarters of an anacrusis first bar, or 0), dead (MIDI bytes like the main ones that hold
    only the dead notes and dead slaps, as alphaTab plays them: a dead note at its fret's pitch, a dead slap on
    every open string; None when there are none), tracks: [{name, percussion, mute, program, channels: [primary,
    secondary]}]}. A score with dead notes or dead slaps is loaded a second time for info["dead"], after the first
    is let go. For minecraft/export_nbs.py, which gets
    files from anywhere: a .gp (a zip) whose files would unpack to more than MAX_UNPACKED is refused before
    anything is unpacked, each file is read up to its declared size only, its audio track is not read at all, and
    alphaTab gets the zip rebuilt from those bytes (alphaTab itself unpacks whatever a file holds, whatever size
    it declares); a .gpx declaring more than MAX_UNPACKED is refused too; and V8 may use at most MAX_V8 for it."""
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name, infos = audio_entry(z), z.infolist()
            if sum(i.file_size for i in infos if i.filename != name) > MAX_UNPACKED:
                raise ValueError(f"the score unpacks to more than {MAX_UNPACKED >> 20} MB")
            entries = [(i, b"\0" if i.filename == name else z.open(i).read(i.file_size)) for i in infos]
        data = rezip(entries)
    elif data[:4] == b"BCFZ" and not 0 <= int.from_bytes(data[4:8], "little", signed=True) <= MAX_UNPACKED:
        raise ValueError(f"the score unpacks to more than {MAX_UNPACKED >> 20} MB")
    ctx = engine()
    try:
        out = json.loads(ctx.call("openmusic.toMidi", data.hex(), timeout_sec=120, max_memory=MAX_V8))
    finally:
        ctx.set_hard_memory_limit(0)       # the engine is shared: no limit for to_gp / dump
    out["dead"] = bytes.fromhex(out["dead"]) if out["dead"] else None
    return bytes.fromhex(out.pop("midi")), out


def summary(song):
    """A dump boiled down: {track name: number of struck notes (not ties)}."""
    return {t["name"]: sum(not n["tie"] for b in t["beats"] for n in b["notes"]) for t in song["tracks"]}
