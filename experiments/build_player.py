"""Build the practice player: <stems_root>/player.html (Chinese, the default) and player.en.html (English).

One page per stems folder, with a song picker. It plays the separated stems of a song in sync and is made for
practising and transcribing by ear: per-stem volume up to +12 dB (a limiter keeps the sum from clipping),
mute / solo, pitch-preserving speed for the whole song and per section (speed regions drawn on the waveform),
an A-B loop with a speed trainer, pitch shift, a metronome on the detected beats with a count-in, and, when
analyze.py / transcribe.py have run, tempo, key, a chord lane and a piano roll of the transcribed notes.

Reads <stems_root>/<song>/{vocals,drums,bass,guitar,piano,other}.{mp3,flac,wav} and, if present,
<song>/analysis.json, <song>/transcription/<stem>.{notes.json,mid} and <song>/transcription/<song>.gp
(export_gp.py). Re-run it after adding songs or running the analysis. The libraries are vendored in
experiments/vendor/ and copied to <stems_root>/_player/:
Signalsmith Stretch 1.3.2 (MIT) does the time-stretch and pitch shift, wavesurfer.js 7.12.12 (BSD-3-Clause)
draws the waveform, the regions and the time ruler. Serve the folder over http and open player.html
(browsers only run the audio worklet from http://, not from file://):

    python experiments/build_player.py                      # outputs/mp3
    python experiments/build_player.py outputs/demucs6_full
    python -m http.server 8765                              # in the project folder, then open
                                                            # http://localhost:8765/outputs/mp3/player.html
"""
import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import quote

from common import ROOT

STEM_ORDER = ("vocals", "drums", "bass", "guitar", "piano", "other")
EXT_PREFERENCE = (".mp3", ".flac", ".wav")  # MP3 first: a fifth of the bytes to fetch, sample-exact when decoded
PAGES = {"zh": "player.html", "en": "player.en.html"}
ASSET_DIR = "_player"  # the page imports ./_player/<file>
VENDOR = ROOT / "experiments" / "vendor"
ASSETS = {  # name next to the page -> vendored source (the licence files travel with the code)
    "SignalsmithStretch.mjs": VENDOR / "signalsmith-stretch" / "SignalsmithStretch.mjs",
    "LICENSE-signalsmith-stretch.txt": VENDOR / "signalsmith-stretch" / "LICENSE.txt",
    "wavesurfer.esm.js": VENDOR / "wavesurfer" / "wavesurfer.esm.js",
    "regions.esm.js": VENDOR / "wavesurfer" / "regions.esm.js",
    "timeline.esm.js": VENDOR / "wavesurfer" / "timeline.esm.js",
    "LICENSE-wavesurfer.txt": VENDOR / "wavesurfer" / "LICENSE",
}
# "multi" = one stretcher with 2 channels per stem (Woodshed's design): the stems share one phase analysis, so
# they add up like the original mix; stems that are muted or not soloed are fed to it as silence (a two-line
# patch in the vendored worklet), so a soloed stem sounds as clean as if it were stretched alone.
# "stems" = one stereo stretcher per stem: clean solos too, but the full mix sounds phasey (tests/player_test.py
# --cpu measures both). Both are sample-aligned. ?engine=stems in the page URL switches for a comparison.
ENGINE = "multi"
SERVER_PORT = 8765

TEXT = {
    "zh": {
        "title": "练习播放器", "song": "歌曲", "position": "位置", "key": "调",
        "switch": "<b>中文</b> | <a href='player.en.html'>English</a>", "home": "← 工作台", "lang_code": "zh",
        "filewarn": "这个页面要通过本地服务器打开（浏览器不允许从 file:// 运行音频处理模块）。"
                    "在项目文件夹里运行 <code>.venv/bin/python -m http.server {port}</code>"
                    "（Windows：<code>.venv\\Scripts\\python -m http.server {port}</code>），"
                    "然后在浏览器打开 <code>{url}</code>。",
        "no_songs": "这个文件夹里还没有分好轨的歌曲。",
        "loading": "正在载入第 {i}/{n} 轨：{stem} …", "loaded": "已载入 {n} 轨，{dur}，解码后约占内存 {mb} MB。",
        "load_error": "载入失败：{msg}",
        "stems": {"vocals": "人声", "drums": "鼓", "bass": "贝斯", "guitar": "吉他", "piano": "钢琴/键盘", "other": "其他"},
        "play": "▶ 播放", "pause": "❚❚ 暂停", "play_title": "播放 / 暂停（空格）",
        "back": "⏮", "back_title": "回到开头，开着循环时回到 A（Home）",
        "loop": "A-B 循环", "loop_title": "循环开关（L）",
        "loop_clear": "✕ 清除循环", "loop_clear_title": "删掉这个 A-B 循环（Esc；在「设 A-B 循环」模式下也可以双击开着的循环）",
        "loop_none": "还没有循环：在波形上拖一段（短的先放大波形），或按 L / A / B", "loop_off": "（已关）", "loop_outside": "（播放头在循环外）",
        "set_a": "设 A", "set_a_title": "把循环起点设在播放头（A 键）",
        "set_b": "设 B", "set_b_title": "把循环终点设在播放头（B 键）",
        "countin": "预备拍", "countin_title": "按播放后先数一小节", "countin_now": "预备 {n}",
        "metro": "节拍器", "metro_title": "按检测到的拍子打点，重拍更响，跟着变速走（M）", "metro_vol": "节拍器音量", "metro_vol_title": "节拍器音量（双击回到默认）",
        "speed": "速度", "speed_reset": "原速", "speed_title": "双击回到原速；[ ] 键每次 ±5%",
        "pitch": "变调", "semitones": "半音", "cents": "音分", "pitch_reset": "原调", "pitch_title": "双击回到原调",
        "speed_in_region": "播放头在变速段 {a}–{b} 里，这里按 {pct} 播放；上面的整体速度只管变速段以外。",
        "trainer": "速度练习",
        "trainer_text": "循环每 {every} 遍加快 {step}%，从 {start}% 一直到 {target}%（整个循环都用这个速度，不管变速段）",
        "trainer_need_loop": "先设一个 A-B 循环。",
        "trainer_pass": "第 {n} 遍 · 现在 {now} · 目标 {target}", "trainer_done": "已到目标速度 {target} ✓",
        "trainer_bad": "起始速度要比目标速度慢，才有得练。",
        "trainer_fields": {"every": "每几遍加速", "step": "每次加快（%）", "start": "起始速度（%）", "target": "目标速度（%）"},
        "regions": "变速段",
        "regions_hint": "在波形上选“新建变速段”再拖动（短的先放大波形），就能给这一段单独设速度；段外用上面的整体速度。",
        "region_new": "新变速段 {a}–{b}：{pct}。要改速度，在下面「变速段」里改。",
        "regions_empty": "还没有变速段。",
        "drag": "在波形上拖动：", "drag_loop": "设 A-B 循环", "drag_speed": "新建变速段",
        "snap": "对齐到", "snap_bar": "小节", "snap_beat": "拍", "snap_off": "不对齐", "zoom": "波形缩放",
        "notes_of": "扒谱：", "notes_none": "不显示", "span": "卷帘范围", "seconds": "{n} 秒", "now_notes": "正在响：",
        "kit": {"49": "强音镲", "51": "叮叮镲", "46": "开镲", "42": "踩镲", "50": "一嗵", "47": "二嗵",
                "43": "落地嗵", "38": "军鼓", "36": "底鼓"},
        "mixer": "分轨音量", "master": "总音量", "mute": "静音", "solo": "独奏",
        "reset": "音量复位", "reset_title": "所有分轨回到 0 dB，取消静音和独奏", "fader_title": "双击回到 0 dB",
        "almost_silent": "几乎无声", "almost_silent_title": "分轨模型在这首歌里几乎没分出这一轨",
        "midi": "MIDI", "midi_title": "下载这一轨的扒谱 MIDI（可导入 Guitar Pro / MuseScore；原调、原速，播放器里的变调和变速不会写进去）",
        "gp": "Guitar Pro",
        "gp_title": "下载整首歌的 Guitar Pro 谱（.gp，Guitar Pro 7、8 用）：所有分轨在一个文件里，吉他、贝斯带六线谱，上面有和弦名；"
                    "原调、原速；音频轨是人声 + 吉他 + 其他",
        "gp_choose": "⬇ Guitar Pro…", "gp_choose_title": "选好谱里垫的音频轨，再生成、下载 Guitar Pro 谱",
        "gp_h": "下载 Guitar Pro 谱",
        "gp_hint": "谱里可以垫一条音频轨。贝斯、鼓、人声旋律的 MIDI 扒得准，由 Guitar Pro 演奏；吉他要靠耳朵听，"
                   "所以默认垫人声、吉他和其他。",
        "gp_stems": "分轨混音：", "gp_original": "原曲（整首录音）", "gp_none": "不放音频（文件最小）",
        "gp_go": "生成并下载", "gp_cancel": "取消", "gp_busy": "正在生成，需要几秒…", "gp_done": "已下载：{name}",
        "gp_err": "生成失败：{msg}", "gp_pick": "至少勾一轨，或者选「不放音频」。",
        "gp_label_original": "原曲", "gp_label_none": "无音频",
        "limiting": "防爆音：压低 {db} dB",
        "limiter_title": "音量调得很大时自动压低一点，避免破音（超过 −2 dBFS 的部分会被压下来）",
        "bar_beat": "第 {bar} 小节 第 {beat} 拍", "pickup_beat": "弱起 第 {beat} 拍", "beat_only": "第 {beat} 拍",
        "bpm_now": "现在 {bpm} BPM（{pct}）", "bpm_alt": "也可能是 {alts} BPM", "unstable": "速度有起伏",
        "meter": "{n}/4 拍", "key_low": "把握不大（{conf}%），也可能是 {alts}", "key_conf": "把握 {conf}%",
        "key_orig": "原调 {key}", "major": " 大调", "minor": " 小调",
        "pick_song": "先在上面选一首歌。", "song_missing": "播放器里还没有「{name}」：在工作台的「练习播放器」一栏重新生成（或运行 experiments/build_player.py），再从上面选歌。",
        "chord": "和弦", "chord_next": "下一个：{c}", "transposed": "（已按变调 {k} 移调）",
        "no_analysis": "这首歌还没做分析：在工作台这首歌的「速度 / 调」一栏点「运行」（或运行 experiments/analyze.py）后，"
                       "这里会显示速度、调和和弦，也能用节拍器、预备拍和按小节对齐。",
        "no_notes": "还没有扒谱：在工作台这首歌的「MIDI」一栏点「运行」（或运行 experiments/transcribe.py）后，"
                    "这里会显示每轨的音符，也能下载 MIDI 和 Guitar Pro 谱。",
        "region_row": "{a} – {b}", "bars": "第 {a}–{b} 小节", "bar_one": "第 {a} 小节", "pickup": "弱起", "bars_pickup": "弱起–第 {b} 小节", "play_here": "▶", "play_here_title": "从这一段开头播放",
        "delete": "删除", "region_speed": "这一段的速度（%）",
        "restored": "已恢复上次的练习设置：{what}", "reset_song": "本曲恢复默认",
        "reset_song_confirm": "把这首歌的循环、变速段、速度、变调、音量、节拍器和预备拍都恢复默认？",
        "r_speed": "速度 {pct}", "r_pitch": "变调 {k}", "r_solo": "独奏：{s}", "r_mute": "静音：{s}", "r_gain": "音量已调",
        "r_loop": "A-B 循环", "r_loop_bars": "（{bars}）", "r_regions": "{n} 个变速段", "r_here": "此处变速段 {pct}", "list_sep": "、",
        "keys": "快捷键：", "key_list": {"space": "空格 播放/暂停", "l": "L 循环开关", "ab": "A / B 设循环起点/终点",
                                    "esc": "Esc 清除循环", "speed": "[ ] 速度 −/+5%", "bars": "← → 前后一小节",
                                    "secs": "← → 前后 2 秒", "m": "M 节拍器", "home": "Home 回到开头"},
        "credits": "音频引擎 Signalsmith Stretch（MIT）· 波形 wavesurfer.js（BSD-3）· "
                   "设计参考 Woodshed（MIT）、BandBuddy（Apache-2.0）、Steminize（MIT）",
    },
    "en": {
        "title": "Practice player", "song": "Song", "position": "Position", "key": "Key",
        "switch": "<a href='player.html'>中文</a> | <b>English</b>", "home": "← Workbench", "lang_code": "en",
        "filewarn": "Open this page through a local server (browsers do not run the audio worklet from file://). "
                    "In the project folder run <code>.venv/bin/python -m http.server {port}</code> "
                    "(Windows: <code>.venv\\Scripts\\python -m http.server {port}</code>), "
                    "then open <code>{url}</code>.",
        "no_songs": "There are no separated songs in this folder yet.",
        "loading": "Loading stem {i}/{n}: {stem} …", "loaded": "Loaded {n} stems, {dur}, about {mb} MB of memory once decoded.",
        "load_error": "Could not load: {msg}",
        "stems": {"vocals": "Vocals", "drums": "Drums", "bass": "Bass", "guitar": "Guitar", "piano": "Piano/keys", "other": "Other"},
        "play": "▶ Play", "pause": "❚❚ Pause", "play_title": "Play / pause (Space)",
        "back": "⏮", "back_title": "Back to the start, or to A when looping (Home)",
        "loop": "A-B loop", "loop_title": "Loop on/off (L)",
        "loop_clear": "✕ Clear loop",
        "loop_clear_title": "Remove this A-B loop (Esc; in “Set A-B loop” mode you can also double-click the loop while it is on)",
        "loop_none": "No loop yet: drag on the waveform (zoom in for short ones), or press L / A / B", "loop_off": " (off)",
        "loop_outside": " (playhead outside the loop)",
        "set_a": "Set A", "set_a_title": "Put the loop start at the playhead (key A)",
        "set_b": "Set B", "set_b_title": "Put the loop end at the playhead (key B)",
        "countin": "Count-in", "countin_title": "Count one bar before playback starts", "countin_now": "Count-in {n}",
        "metro": "Metronome", "metro_title": "Clicks on the detected beats, louder on the downbeat, follows speed changes (M)",
        "metro_vol": "Metronome volume", "metro_vol_title": "Metronome volume (double-click to reset)",
        "speed": "Speed", "speed_reset": "100%", "speed_title": "Double-click for 100%; the [ ] keys step ±5%",
        "pitch": "Pitch", "semitones": "semitones", "cents": "cents", "pitch_reset": "Original key",
        "pitch_title": "Double-click for the original key",
        "speed_in_region": "The playhead is in the speed region {a}–{b}, which plays at {pct}; the overall speed only "
                           "applies outside speed regions.",
        "trainer": "Speed trainer", "trainer_text": "Speed up {step}% each time the loop has played {every}×, from {start}% up "
                        "to {target}% (this speed applies to the whole loop, speed regions included)",
        "trainer_need_loop": "Set an A-B loop first.",
        "trainer_pass": "Pass {n} · now {now} · target {target}", "trainer_done": "Reached the target speed, {target} ✓",
        "trainer_bad": "The start speed must be slower than the target, or there is nothing to train.",
        "trainer_fields": {"every": "Loop passes per step", "step": "Step (%)", "start": "Start speed (%)", "target": "Target speed (%)"},
        "regions": "Speed regions",
        "regions_hint": "Choose “New speed region” and drag on the waveform (zoom in for short ones) to give that section "
                        "its own speed; outside the regions the overall speed above applies.",
        "region_new": "New speed region {a}–{b}: {pct}. Change its speed under “Speed regions” below.",
        "regions_empty": "No speed regions yet.",
        "drag": "Drag on the waveform to:", "drag_loop": "Set A-B loop", "drag_speed": "New speed region",
        "snap": "Snap to", "snap_bar": "bars", "snap_beat": "beats", "snap_off": "off", "zoom": "Waveform zoom",
        "notes_of": "Transcription:", "notes_none": "Hide", "span": "Roll width", "seconds": "{n} s", "now_notes": "Sounding: ",
        "kit": {"49": "Crash", "51": "Ride", "46": "Open hi-hat", "42": "Hi-hat", "50": "High tom", "47": "Mid tom",
                "43": "Floor tom", "38": "Snare", "36": "Kick"},
        "mixer": "Stem volumes", "master": "Master", "mute": "Mute", "solo": "Solo",
        "reset": "Reset mix", "reset_title": "All stems back to 0 dB, no mute or solo", "fader_title": "Double-click for 0 dB",
        "almost_silent": "almost silent", "almost_silent_title": "The separation found almost nothing for this stem in this song",
        "midi": "MIDI", "midi_title": "Download this stem's transcription as MIDI (opens in Guitar Pro / MuseScore; original "
                                      "key and tempo, the player's pitch and speed changes are not applied)",
        "gp": "Guitar Pro",
        "gp_title": "Download the whole song as one Guitar Pro file (.gp, for Guitar Pro 7 and 8): every stem a "
                    "track, tab for guitar and bass, chord names above; original key and tempo; the audio track is "
                    "vocals + guitar + other",
        "gp_choose": "⬇ Guitar Pro…", "gp_choose_title": "Choose the audio track for the Guitar Pro file, then build and download it",
        "gp_h": "Download a Guitar Pro file",
        "gp_hint": "The file can carry an audio track. The bass, drums and vocal-melody MIDI are accurate, so Guitar Pro "
                   "plays them; the guitar has to be worked out by ear, so the default track is vocals, guitar and other.",
        "gp_stems": "Mix of stems:", "gp_original": "The original song (the whole recording)",
        "gp_none": "No audio (smallest file)", "gp_go": "Build and download", "gp_cancel": "Cancel",
        "gp_busy": "Building, this takes a few seconds…", "gp_done": "Downloaded: {name}", "gp_err": "Could not build it: {msg}",
        "gp_pick": "Tick at least one stem, or choose “No audio”.",
        "gp_label_original": "original", "gp_label_none": "no audio",
        "limiting": "Clip guard: −{db} dB",
        "limiter_title": "When the mix gets very loud it is turned down a little so it never distorts (anything above "
                         "−2 dBFS is pulled down)",
        "bar_beat": "Bar {bar}, beat {beat}", "pickup_beat": "Pickup, beat {beat}", "beat_only": "Beat {beat}",
        "bpm_now": "now {bpm} BPM ({pct})", "bpm_alt": "or maybe {alts} BPM", "unstable": "tempo varies",
        "meter": "{n}/4", "key_low": "unsure ({conf}%), maybe {alts}", "key_conf": "{conf}% sure",
        "key_orig": "original {key}", "major": " major", "minor": " minor",
        "pick_song": "Pick a song above to start.",
        "song_missing": "“{name}” is not in this player yet: rebuild it in the workbench’s “Practice player” column (or run "
                        "experiments/build_player.py), then pick it above.",
        "chord": "Chord", "chord_next": "next: {c}", "transposed": " (transposed {k})",
        "no_analysis": "This song has not been analyzed yet: click “Run” in its “Tempo / key” column in the workbench (or "
                       "run experiments/analyze.py) to see tempo, key and chords here and to use the metronome, count-in "
                       "and bar snapping.",
        "no_notes": "No transcription yet: click “Run” in its “MIDI” column in the workbench (or run "
                    "experiments/transcribe.py) to see each stem's notes here and download MIDI and Guitar Pro.",
        "region_row": "{a} – {b}", "bars": "bars {a}–{b}", "bar_one": "bar {a}", "pickup": "pickup", "bars_pickup": "pickup–bar {b}", "play_here": "▶", "play_here_title": "Play from the start of this region",
        "delete": "Delete", "region_speed": "Speed of this region (%)",
        "restored": "Your last settings for this song are back: {what}", "reset_song": "Reset this song",
        "reset_song_confirm": "Reset this song's loop, speed regions, speed, pitch, volumes, metronome and count-in?",
        "r_speed": "speed {pct}", "r_pitch": "pitch {k}", "r_solo": "solo: {s}", "r_mute": "muted: {s}", "r_gain": "volumes changed",
        "r_loop": "A-B loop", "r_loop_bars": " ({bars})", "r_regions": "speed regions: {n}", "r_here": "here: speed region {pct}",
        "list_sep": ", ",
        "keys": "Keys: ", "key_list": {"space": "Space play/pause", "l": "L loop on/off", "ab": "A / B set loop start/end",
                                    "esc": "Esc clear loop", "speed": "[ ] speed −/+5%", "bars": "← → one bar back/forward",
                                    "secs": "← → 2 s back/forward", "m": "M metronome", "home": "Home back to start"},
        "credits": "Audio engine Signalsmith Stretch (MIT) · waveform wavesurfer.js (BSD-3) · "
                   "design ideas from Woodshed (MIT), BandBuddy (Apache-2.0), Steminize (MIT)",
    },
}


def find_songs(root):
    """Every <root>/<song>/ folder with at least one stem, and which analysis/transcription files it has."""
    songs = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        stems = []
        for stem in STEM_ORDER:
            path = next((folder / f"{stem}{e}" for e in EXT_PREFERENCE if (folder / f"{stem}{e}").exists()), None)
            if path:
                stems.append({"name": stem, "file": quote(path.name)})
        if not stems:
            continue
        found = lambda rel: (folder / rel).exists()  # noqa: E731
        songs.append({
            "name": folder.name,
            "base": quote(folder.name) + "/",
            "stems": stems,
            "analysis": "analysis.json" if found("analysis.json") else None,
            "notes": {s["name"]: quote(f"transcription/{s['name']}.notes.json") for s in stems
                      if found(f"transcription/{s['name']}.notes.json")},
            "midi": {s["name"]: quote(f"transcription/{s['name']}.mid") for s in stems
                     if found(f"transcription/{s['name']}.mid")},
            "gp": quote(f"transcription/{folder.name}.gp") if found(f"transcription/{folder.name}.gp") else None,
        })
    return songs


def page_url(out, page):
    """Where the page is served by `python -m http.server` started in the project folder (or in `out`)."""
    try:
        rel = (out / page).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = page
    return f"http://localhost:{SERVER_PORT}/{quote(rel)}"


def render(lang, songs, out):
    text = dict(TEXT[lang])
    text["filewarn"] = text["filewarn"].format(port=SERVER_PORT, url=page_url(out, PAGES[lang]))
    config = {"lang": lang, "engine": ENGINE, "songs": songs, "text": text}
    blob = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__LANG__", "zh-CN" if lang == "zh" else "en")
                .replace("__TITLE__", text["title"])
                .replace("__SWITCH__", text["switch"])
                .replace("__HOME__", text["home"])
                .replace("__LANG_CODE__", text["lang_code"])
                .replace("__FILEWARN__", text["filewarn"])
                .replace("__CONFIG__", blob))


def copy_assets(out):
    dest = out / ASSET_DIR
    dest.mkdir(parents=True, exist_ok=True)
    for name, src in ASSETS.items():
        target = dest / name
        if not target.exists() or target.read_bytes() != src.read_bytes():
            shutil.copyfile(src, target)


def build(stems_root):
    stems_root = Path(stems_root)
    songs = find_songs(stems_root)
    copy_assets(stems_root)
    for lang, name in PAGES.items():
        (stems_root / name).write_text(render(lang, songs, stems_root), encoding="utf-8")
    return songs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stems_root", type=Path, nargs="?", default=ROOT / "outputs" / "mp3")
    args = parser.parse_args()
    if not args.stems_root.is_dir():
        parser.error(f"{args.stems_root} is not a folder")
    songs = build(args.stems_root)
    for s in songs:
        extras = (["analysis.json"] if s["analysis"] else []) + ([f"notes: {', '.join(s['notes'])}"] if s["notes"] else [])
        extras += ["Guitar Pro"] if s["gp"] else []
        print(f"{s['name']}: {len(s['stems'])} stems" + (f" ({'; '.join(extras)})" if extras else ""))
    print(f"wrote {args.stems_root / PAGES['zh']} and {PAGES['en']}; open {page_url(args.stems_root, PAGES['zh'])}")


PAGE = r"""<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenMusic · __TITLE__</title>
<style>
:root { --bg:#f6f6f3; --panel:#ffffff; --panel2:#f0f0ec; --text:#1c1c1e; --muted:#6c6c72; --line:#e2e2dd;
  --accent:#2f6fdf; --on-accent:#fff; --accent-soft:rgba(47,111,223,.14); --warn:#c2410c; --on-warn:#fff; --wave:#b8c2d6;
  --solo:#a16207; --on-solo:#fff;
  --slow:rgba(124,58,237,.22); --fast:rgba(234,138,22,.25); --loop:rgba(22,163,74,.20); --loop-off:rgba(120,120,120,.12);
  --slow-solid:#7c3aed; --fast-solid:#ea8a16; }
@media (prefers-color-scheme: dark) { :root { --bg:#131315; --panel:#1c1c20; --panel2:#26262b; --text:#ececef;
  --muted:#9d9da5; --line:#2f2f35; --accent:#6c9bff; --on-accent:#0b1220; --accent-soft:rgba(108,155,255,.18);
  --warn:#fb923c; --on-warn:#1a0d00; --solo:#eab308; --on-solo:#1c1300; --slow:rgba(167,139,250,.26);
  --slow-solid:#a78bfa; --fast-solid:#fb923c;
  --wave:#3d4862; --loop-off:rgba(160,160,160,.12); } }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif; }
main, header { max-width: 1120px; margin: 0 auto; padding: 0 16px; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding-top: 14px; }
header h1 { font-size: 18px; margin: 0; font-weight: 650; }
header h1 span { color: var(--muted); font-weight: 400; }
header .lang { margin-left: auto; color: var(--muted); }
header .home { font-size: 14px; white-space: nowrap; }
a { color: var(--accent); }
select, input, button { font: inherit; color: inherit; }
select, input[type=number] { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 3px 6px; }
input[type=number] { width: 4.2em; }
button { background: var(--panel2); border: 1px solid var(--line); border-radius: 7px; padding: 5px 10px; cursor: pointer; }
button:hover { border-color: var(--accent); }
button.on { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
button:disabled { opacity: .45; cursor: default; }
label { cursor: pointer; white-space: nowrap; }
input[type=range] { accent-color: var(--accent); }
#song { max-width: 100%; min-width: 0; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; margin: 12px 0; }
.status { color: var(--muted); margin: 10px 0 0; min-height: 1.4em; }
.status.err { color: var(--warn); }
.hint { color: var(--muted); font-size: 12.5px; }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
.facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.fact { background: var(--panel2); border-radius: 8px; padding: 8px 10px; min-width: 0; }
.fact small { display: block; color: var(--muted); font-size: 12px; }
.fact b { display: block; font-size: 20px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fact span { display: block; color: var(--muted); font-size: 12px; min-height: 1.2em; overflow-wrap: anywhere; }
.fact.chord b { font-size: 26px; color: var(--accent); }
#wave { margin-top: 2px; }
#wave ::part(region-content) { font-size: 12px; font-weight: 600; color: var(--text); }
.tools { margin-top: 8px; font-size: 13px; }
#detail { display: block; width: 100%; height: 300px; margin-top: 10px; border-radius: 8px; background: var(--panel2); cursor: pointer; }
#detail.short { height: 74px; }
.transport { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px;
  position: sticky; top: 0; z-index: 20; box-shadow: 0 2px 8px rgba(0,0,0,.08); }
#play { min-width: 7.5em; min-height: 36px; font-weight: 650; background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.loopbox, .ab { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; }
/* the readouts get their own one-line row, so the bar keeps its height and the buttons stay put */
.tstatus { flex-basis: 100%; display: flex; gap: 4px 16px; min-height: 1.45em; min-width: 0; color: var(--muted); font-size: 13px; }
#loop-info { flex: 1 1 0; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
#chips { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#speed-note { min-height: 2.9em; margin: 8px 0 0; }
.ctl .under { justify-self: start; }
#restored button { margin-left: 6px; padding: 2px 8px; font-size: 12.5px; }
footer button { padding: 2px 8px; font-size: 12px; }
#gp-dialog { border: 1px solid var(--line); border-radius: 12px; background: var(--panel); color: var(--text); padding: 18px;
  max-width: min(520px, calc(100vw - 32px)); box-shadow: 0 10px 40px rgba(0,0,0,.25); }
#gp-dialog::backdrop { background: rgba(0,0,0,.35); }
#gp-dialog h2 { font-size: 16px; }
#gp-dialog .opt { display: block; margin: 10px 0 4px; white-space: normal; }
#gp-stems { display: flex; flex-wrap: wrap; gap: 6px 14px; margin: 4px 0 4px 24px; }
#gp-dialog .row { margin-top: 14px; }
.ctl { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 6px 10px; }
.ctl input[type=range] { width: 100%; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 28px; }
h2 { font-size: 14px; margin: 0 0 8px; font-weight: 650; }
.regions .item { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 5px 0; border-top: 1px solid var(--line); }
.regions .item:first-of-type { border-top: 0; }
.regions .swatch { width: 12px; height: 12px; border-radius: 50%; border: 1px solid var(--muted); }
.regions .item.now { background: var(--accent-soft); }
.regions .item.flash { animation: flash 1.5s ease-out; }
@keyframes flash { from { background: var(--accent-soft); } }
.mixer .stem { display: grid; grid-template-columns: 7.2em auto auto minmax(110px, 1.1fr) 4.4em minmax(0, 1.6fr) 3.4em;
  align-items: center; gap: 6px 8px; padding: 5px 0; border-top: 1px solid var(--line); }
.mixer .stem:first-of-type { border-top: 0; }
.mixer .name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mixer .name::before { content: ""; display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: var(--c); margin-right: 6px; }
.mixer button.ms { padding: 2px 8px; font-size: 12px; font-weight: 600; }
.mixer button.ms.on.solo { background: var(--solo); border-color: var(--solo); color: var(--on-solo); }
.mixer .strip .tag { position: absolute; left: 8px; top: 50%; transform: translateY(-50%); font-size: 11.5px; color: var(--muted); }
.mixer .fader { width: 100%; }
.mixer .db { font-variant-numeric: tabular-nums; text-align: right; font-size: 13px; }
.mixer .strip { position: relative; height: 26px; background: var(--panel2); border-radius: 5px; overflow: hidden; cursor: pointer; }
.mixer .strip canvas { width: 100%; height: 100%; display: block; }
.mixer .strip i { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); left: 0; }
.mixer .midi { font-size: 12px; text-align: center; }
.mixer .stem.silent .name, .mixer .stem.silent .strip { opacity: .4; }
.master { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin-top: 8px; padding-top: 8px; border-top: 2px solid var(--line); }
.master input[type=range] { width: 200px; max-width: 100%; }
#limiter { font-size: 12.5px; padding: 2px 8px; border-radius: 999px; background: var(--panel2); color: var(--muted); font-variant-numeric: tabular-nums; }
#limiter.active { background: var(--warn); color: var(--on-warn); }
footer { color: var(--muted); font-size: 12px; margin: 10px 0 30px; }
footer p { margin: 4px 0; }
code { background: var(--panel2); padding: 1px 4px; border-radius: 4px; }
@media (max-width: 600px) { .transport { position: static; box-shadow: none; } }
@media (max-width: 760px) {
  #speed-note { min-height: 4.4em; }
  .facts { grid-template-columns: 1fr 1fr; }
  .cols { grid-template-columns: 1fr; }
  .mixer .stem { grid-template-columns: minmax(0, 1fr) auto auto 4.4em 3.4em; }
  .mixer .fader { grid-column: 1 / -1; grid-row: 2; }
  .mixer .strip { grid-column: 1 / -1; grid-row: 3; }
  #detail { height: 220px; }
}
</style>
</head>
<body>
<header>
  <a id="home" class="home" href="/?lang=__LANG_CODE__" hidden>__HOME__</a>
  <h1>OpenMusic <span>· __TITLE__</span></h1>
  <select id="song"></select>
  <div class="lang">__SWITCH__</div>
</header>
<main>
<script>
// Opened from the workbench (experiments/app.py adds ?from=workbench, and the workbench page is at "/" of the same
// server)? Then offer the way back to it. The flag is kept for this tab, since the 中文 / English switch drops the
// query string; no request needed.
(() => {
  let ref = null;
  try { ref = document.referrer ? new URL(document.referrer) : null; } catch (e) {}
  let from = new URLSearchParams(location.search).get('from') === 'workbench'
    || (ref !== null && ref.origin === location.origin && ref.pathname === '/');
  try {
    if (from) sessionStorage.setItem('om-from-workbench', '1');
    from = from || sessionStorage.getItem('om-from-workbench') === '1';
  } catch (e) {}
  document.getElementById('home').hidden = !from;
})();
</script>
<div id="filewarn" class="panel" hidden><p>__FILEWARN__</p></div>
<div id="app">
  <p id="status" class="status"></p>
  <p id="restored" class="status" hidden><span id="restored-text"></span><button id="reset-song"></button></p>
  <section class="panel" id="info" hidden>
    <div class="facts">
      <div class="fact"><small id="l-pos"></small><b id="f-pos">0:00.0</b><span id="f-bar"></span></div>
      <div class="fact" id="fact-tempo"><small>BPM</small><b id="f-bpm">–</b><span id="f-bpm-note"></span></div>
      <div class="fact" id="fact-key"><small id="l-key"></small><b id="f-key">–</b><span id="f-key-note"></span></div>
      <div class="fact chord" id="fact-chord"><small id="l-chord"></small><b id="f-chord">–</b><span id="f-next"></span></div>
    </div>
    <p id="no-analysis" class="hint" hidden></p>
  </section>
  <section class="panel transport" id="transport" hidden>
    <button id="play"></button>
    <button id="back"></button>
    <span class="loopbox">
      <label id="loop-box"><input type="checkbox" id="loop"> <span id="l-loop"></span></label>
      <span class="ab"><button id="set-a"></button><button id="set-b"></button></span>
      <button id="loop-clear"></button>
    </span>
    <label id="countin-box"><input type="checkbox" id="countin"> <span id="l-countin"></span></label>
    <span id="metro-box"><label><input type="checkbox" id="metro"> <span id="l-metro"></span></label>
      <input type="range" id="metro-vol" min="-30" max="6" step="1" value="-6"></span>
    <div class="tstatus"><span id="loop-info"></span><span id="chips"></span></div>
  </section>
  <section class="panel" id="timeline" hidden>
    <div id="wave"></div>
    <div class="row tools">
      <span id="l-drag"></span>
      <label><input type="radio" name="drag" value="loop" checked> <span id="l-drag-loop"></span></label>
      <label><input type="radio" name="drag" value="speed"> <span id="l-drag-speed"></span></label>
      <label id="snap-box"><span id="l-snap"></span> <select id="snap"></select></label>
      <label><span id="l-zoom"></span> <input type="range" id="zoom" min="1" max="30" step="1" value="1"></label>
    </div>
    <canvas id="detail"></canvas>
    <div class="row tools" id="detail-tools">
      <label id="roll-box"><span id="l-notes"></span> <select id="roll"></select></label>
      <label><span id="l-span"></span> <input type="range" id="span" min="3" max="30" step="1" value="10"> <span id="span-v"></span></label>
      <span id="roll-midi"></span>
      <span id="roll-gp"></span>
    </div>
    <p id="no-notes" class="hint" hidden></p>
    <p id="shortcuts" class="hint"></p>
  </section>
  <section class="panel cols" id="speedpanel" hidden>
    <div>
      <div class="ctl">
        <label for="speed" id="l-speed"></label><input type="range" id="speed" min="50" max="150" step="1" value="100">
        <span><input type="number" id="speed-n" min="50" max="150" step="1" value="100">% <button id="speed-reset"></button></span>
        <label for="pitch" id="l-pitch"></label><input type="range" id="pitch" min="-12" max="12" step="1" value="0">
        <span><input type="number" id="pitch-n" min="-12" max="12" step="1" value="0"> <span id="l-semi"></span></span>
        <span></span><span class="under"><button id="pitch-reset"></button></span>
        <span><input type="number" id="cents" min="-50" max="50" step="1" value="0"> <span id="l-cents"></span></span>
      </div>
      <p class="hint" id="speed-note"></p>
    </div>
    <div>
      <label><input type="checkbox" id="trainer"> <b id="l-trainer"></b></label>
      <p class="hint" id="trainer-text"></p>
    </div>
  </section>
  <section class="panel regions" id="regionpanel" hidden>
    <h2 id="l-regions"></h2>
    <p class="hint" id="regions-hint"></p>
    <div id="regionlist"></div>
  </section>
  <section class="panel mixer" id="mixer" hidden>
    <h2 id="l-mixer"></h2>
    <div id="stems"></div>
    <div class="master">
      <b id="l-master"></b>
      <input type="range" id="master" min="-30" max="6" step="0.5" value="0">
      <span id="master-db" class="db"></span>
      <button id="reset"></button>
      <span id="limiter" hidden></span>
    </div>
  </section>
  <footer><p><button id="reset-song-2"></button></p><p id="credits"></p></footer>
  <dialog id="gp-dialog">
    <h2 id="gp-h"></h2>
    <p class="hint" id="gp-hint"></p>
    <label class="opt"><input type="radio" name="gp-audio" value="stems"> <span id="gp-l-stems"></span></label>
    <div id="gp-stems"></div>
    <label class="opt"><input type="radio" name="gp-audio" value="original"> <span id="gp-l-original"></span></label>
    <label class="opt"><input type="radio" name="gp-audio" value="none"> <span id="gp-l-none"></span></label>
    <p class="status" id="gp-status"></p>
    <div class="row"><button id="gp-go" class="on"></button><button id="gp-cancel"></button></div>
  </dialog>
</div>
</main>
<script id="config" type="application/json">__CONFIG__</script>
<script>
// Classic script so it also runs from file://, where the module below cannot load.
if (location.protocol === 'file:') {
  document.getElementById('filewarn').hidden = false;
  document.getElementById('app').hidden = true;
}
</script>
<script type="module">
import SignalsmithStretch from './_player/SignalsmithStretch.mjs';
import WaveSurfer from './_player/wavesurfer.esm.js';
import RegionsPlugin from './_player/regions.esm.js';
import TimelinePlugin from './_player/timeline.esm.js';

const CFG = JSON.parse(document.getElementById('config').textContent);
const T = CFG.text;

// ---------- small helpers ----------------------------------------------------------------------------------
const $ = id => document.getElementById(id);
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const fill = (s, v) => s.replace(/\{(\w+)\}/g, (_, k) => v[k] ?? '');
const MIN_DB = -30, MAX_DB = 12;                 // fader bottom means silent
const dbToGain = db => db <= MIN_DB ? 0 : 10 ** (db / 20);
const fmtDb = db => db <= MIN_DB ? '−∞ dB' : (db > 0 ? '+' : db < 0 ? '−' : '') + Math.abs(db).toFixed(1) + ' dB';
const fmtTime = s => { s = Math.max(0, s); const m = Math.floor(s / 60); return m + ':' + (s - 60 * m).toFixed(1).padStart(4, '0'); };
const pct = r => Math.round(r * 100) + '%';
const NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
const SHARP = {Db: 'C#', Eb: 'D#', Gb: 'F#', Ab: 'G#', Bb: 'A#', Cb: 'B', Fb: 'E', 'E#': 'F', 'B#': 'C'};
const noteName = p => NAMES[((p % 12) + 12) % 12] + (Math.floor(p / 12) - 1);
const DRUM_ROWS = [49, 51, 46, 42, 50, 47, 43, 38, 36];   // GM drum notes, top to bottom of the drum lane
const pcOf = n => NAMES.indexOf(SHARP[n] || n);
const shiftName = (n, k) => NAMES[(pcOf(n) + k % 12 + 12) % 12];
// Spelling as in analyze.py: keys as musicians name them (Bb major, not A#), chord roots with flats in flat keys
// and for the borrowed b2 b3 b6 b7 of a major key (or the b2 of a minor key), sharps otherwise.
const FLATS = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'Gb', 'G', 'Ab', 'A', 'Bb', 'B'];
const SPELL_MAJOR = ['C', 'Db', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'];
const SPELL_MINOR = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'G#', 'A', 'Bb', 'B'];
const FLAT_KEYS = new Set(['5major', '10major', '3major', '8major', '1major', '2minor', '7minor', '0minor', '5minor', '10minor', '3minor']);
const spellKey = (pc, mode) => (mode === 'minor' ? SPELL_MINOR : SPELL_MAJOR)[(pc % 12 + 12) % 12];
function spellNote(pc, key) {         // key: {pc, mode} of the key heard, or null
  pc = (pc % 12 + 12) % 12;
  if (!key) return NAMES[pc];
  const minor = key.mode === 'minor', degree = ((pc - key.pc) % 12 + 12) % 12;
  const flatKey = FLAT_KEYS.has(key.pc + (minor ? 'minor' : 'major')) && !(minor && degree === 11);
  return (flatKey || (minor ? [1] : [1, 3, 8, 10]).includes(degree) ? FLATS : NAMES)[pc];
}
const transposeChord = (label, k, key = null) => !k || !label || label === 'N' ? label
  : label.replace(/(^|\/)([A-G][#b]?)/g, (_, pre, root) => pre + spellNote(pcOf(root) + k, key));
function lowerBound(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; } return lo; }
const STEM_COLORS = {vocals: '#e4572e', drums: '#8b5cf6', bass: '#0d9488', guitar: '#e39b0b', piano: '#3b82f6', other: '#8a8f98'};
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode: settings are not kept */ } },
};
function setStatus(msg, isError = false) { const el = $('status'); el.textContent = msg; el.classList.toggle('err', isError); }

// ---------- audio engine -----------------------------------------------------------------------------------
// Graph design from Woodshed's src/js/engine.js (MIT, github.com/KyleKincer/woodshed): stems are decoded to
// AudioBuffers (sample-exact, also for VBR MP3) and played by Signalsmith Stretch nodes; each stem's GainNode sits
// AFTER the stretcher, so faders, mute and solo act at once and can boost above 100 %. Master -> limiter with
// BandBuddy's timing (Apache-2.0, github.com/dourgey/BandBuddy: ratio 20, attack 3 ms, release 80 ms) but a -2 dB
// threshold, so that even a +12 dB boost settles below the soft clipper, which only catches the attack overshoot.
const STRETCH_CONFIG = {blockMs: 120, intervalMs: 30, splitComputation: true};
const LIMIT_DB = -2, LIMIT_RATIO = 20;
// The Web Audio compressor adds make-up gain (spec: (1 / gain at 0 dBFS) ^ 0.6); take it back out.
const MAKEUP_DB = 0.6 * -LIMIT_DB * (1 - 1 / LIMIT_RATIO);

function softClipCurve(n = 4096, knee = 0.94, ceiling = 0.99) {
  const c = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1) * 2 - 1, a = Math.abs(x);
    c[i] = Math.sign(x) * (a <= knee ? a : knee + (ceiling - knee) * Math.tanh((a - knee) / (ceiling - knee)));
  }
  return c;  // inputs beyond +-1 map to the end points, so the output never exceeds the ceiling
}

async function createEngine(ctx, nStems, mode) {
  const out = new GainNode(ctx);                                  // master fader
  const limiter = new DynamicsCompressorNode(ctx, {threshold: LIMIT_DB, knee: 0, ratio: LIMIT_RATIO, attack: 0.003, release: 0.08});
  const trim = new GainNode(ctx, {gain: 10 ** (-MAKEUP_DB / 20)});
  const clip = new WaveShaperNode(ctx, {curve: softClipCurve(), oversample: '2x'});
  const music = new GainNode(ctx), metro = new GainNode(ctx);    // music: short fades at seek/pause
  music.connect(out); metro.connect(out);
  out.connect(limiter).connect(trim).connect(clip).connect(ctx.destination);
  const gains = Array.from({length: nStems}, () => { const g = new GainNode(ctx); g.connect(music); return g; });
  // One input that stays unconnected (= play from loaded buffers). With numberOfInputs 0 the 1.3.2 worklet
  // throws while inactive (it indexes the missing input list) and the browser stops the node for good.
  const opts = ch => ({numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [ch]});
  let nodes;
  if (mode === 'multi') {          // one stretcher, 2 channels per stem: one phase analysis for all stems
    const node = await SignalsmithStretch(ctx, opts(2 * nStems));
    const split = new ChannelSplitterNode(ctx, {numberOfOutputs: 2 * nStems});
    node.connect(split);
    gains.forEach((g, i) => {
      const m = new ChannelMergerNode(ctx, {numberOfInputs: 2});
      split.connect(m, 2 * i, 0); split.connect(m, 2 * i + 1, 1); m.connect(g);
    });
    nodes = [node];
  } else {                         // one stereo stretcher per stem, all fed the same schedule
    nodes = [];
    for (const g of gains) { const node = await SignalsmithStretch(ctx, opts(2)); node.connect(g); nodes.push(node); }
  }
  for (const node of nodes) await node.configure(STRETCH_CONFIG);
  // Silent stems must not take part in the shared analysis, or a soloed quiet stem would get the phases of a
  // loud muted one (measured: piano -7 to -14 dB spectral fidelity). setChannelMask is the vendored patch.
  const setAudible = flags => { if (mode === 'multi') nodes[0].setChannelMask(flags.flatMap(f => [f, f])); };
  return {ctx, mode, nodes, gains, music, metro, out, limiter, trim, clip, setAudible, latency: await nodes[0].latency()};
}

// Decode one stem at a time and hand its samples to the stretcher by transfer (no copy); the AudioBuffer is
// emptied by the transfer and dropped. On the way, keep 100 loudness points per second for the waveform.
async function loadStems(song, engine, onStem) {
  const ctx = engine.ctx, hop = Math.round(ctx.sampleRate / 100);
  const multi = engine.mode === 'multi', pending = [], strips = [];
  let power = null, frames = 0;
  for (let i = 0; i < song.stems.length; i++) {
    onStem(i);
    const resp = await fetch(song.base + song.stems[i].file);
    if (!resp.ok) throw new Error(resp.status + ' ' + resp.url);
    const buf = await ctx.decodeAudioData(await resp.arrayBuffer());
    const L = buf.getChannelData(0), R = buf.numberOfChannels > 1 ? buf.getChannelData(1) : L.slice();
    const bins = Math.ceil(buf.length / hop), strip = new Float32Array(bins);
    if (!power || power.length < bins) { const p = new Float32Array(bins); if (power) p.set(power); power = p; }
    for (let b = 0; b < bins; b++) {
      let s = 0; const end = Math.min(buf.length, (b + 1) * hop);
      for (let j = b * hop; j < end; j++) s += L[j] * L[j] + R[j] * R[j];
      s /= 2 * (end - b * hop); power[b] += s; strip[b] = Math.sqrt(s);
    }
    strips.push(strip);
    frames = Math.max(frames, buf.length);
    const chans = [L, R];
    if (multi) pending.push(chans);
    else await addBuffers(engine.nodes[i], chans);
  }
  if (multi) {  // a single addBuffers call with equal-length channels (the wrapper reads one buffer list correctly)
    const all = pending.flatMap(ch => ch.map(a => a.length === frames ? a : padTo(a, frames)));
    await addBuffers(engine.nodes[0], all);
  }
  const peaks = Array.from(power, p => Math.sqrt(p));
  return {duration: frames / ctx.sampleRate, peaks, strips};
}
const padTo = (a, n) => { const b = new Float32Array(n); b.set(a); return b; };
async function addBuffers(node, chans) {
  try { await node.addBuffers(chans, chans.map(a => a.buffer)); }
  catch { await node.addBuffers(chans.map(a => a.slice())); }   // a browser that cannot transfer decoded data
}

function clickBuffer(ctx, freq, amp) {
  const n = Math.round(ctx.sampleRate * 0.04), b = new AudioBuffer({length: n, sampleRate: ctx.sampleRate}), d = b.getChannelData(0);
  for (let i = 0; i < n; i++) { const t = i / ctx.sampleRate; d[i] = amp * Math.sin(2 * Math.PI * freq * t) * Math.exp(-t * 90); }
  return b;
}

// ---------- transport: one time map drives every stretcher, the playhead and the metronome --------------------
// A segment says: from context time `output` on, play input (song) time `input` onward at `rate`. All
// stretchers get the same segments, and each computes its read position from the shared audio clock, so stems
// cannot drift apart. Only the next change is ever pending: the 1.3.2 wrapper's schedule() drops pending
// changes, so the planner commits the next speed-region edge or loop wrap shortly before it is due (one
// stretcher latency plus a margin ahead, from a setTimeout loop that keeps running in background tabs).
const LEAD_MARGIN = 0.08, PLAN_AHEAD = 0.2, CLICK_AHEAD = 0.25, TICK_MS = 25;

class Transport {
  constructor(engine, duration, rhythm) {
    Object.assign(this, {e: engine, ctx: engine.ctx, duration, ...rhythm});
    this.lead = engine.latency + LEAD_MARGIN;
    this.segs = [{output: 0, input: 0, rate: 1, semis: 0, active: false}];
    this.log = [];                                       // every committed segment, for the tests
    this.playing = false; this.pausedAt = 0; this.endAt = null;
    this.speed = 1; this.semis = 0; this.regions = []; this.loop = {on: false, a: 0, b: 0};
    this.trainer = {on: false, start: 0.7, step: 0.05, every: 1, target: 1, passes: 0};
    this.metroOn = false; this.countInOn = false; this.countInfo = null;
    this.want = null; this.timer = null; this.clicks = []; this.clickedTo = 0;
    this.clickHi = clickBuffer(this.ctx, 1760, 0.9); this.clickLo = clickBuffer(this.ctx, 1320, 0.55);
    this.onchange = () => {};
  }
  rateAt(x) {
    const L = this.loop;   // while the speed trainer runs, its speed rules the whole loop, speed regions included
    if (this.trainer.on && L.on && x >= L.a && x < L.b) return this.speed;
    for (const r of this.regions) if (x >= r.start && x < r.end) return r.speed;
    return this.speed;
  }
  beatInterval(x) {
    const b = this.beats, k = clamp(lowerBound(b, x), 2, b.length - 2);
    return b.length > 4 ? (b[k + 1] - b[k - 2]) / 3 : 60 / (this.bpm || 120);
  }
  // Song time at context time t (the segment rules above; during a count-in or a seek it shows the target).
  position(t = this.ctx.currentTime) {
    const w = this.want;                                 // a seek or pause still waiting for its turn
    if (w?.kind === 'jump') return w.input;
    if (w?.kind === 'stop' && w.seekTo != null) return w.seekTo;
    if (!this.playing) return this.pausedAt;
    let s = this.segs[0], next = null;
    for (const seg of this.segs) { if (seg.output <= t) s = seg; else { next = seg; break; } }
    if (next && next.jump) return next.input;
    return s.active ? Math.min(this.duration, s.input + (t - s.output) * s.rate) : s.input;
  }
  play() {
    const w = this.want;
    if (w?.kind === 'stop') { this.want = null; this.pausedAt = w.seekTo ?? this.pausedAt; }  // pressed again before
    else if (this.playing) return this.ctx.resume();                                       // the pause took effect
    let x = w?.kind === 'jump' ? w.input : this.pausedAt;
    if (x >= this.duration - 0.05) x = this.loop.on ? this.loop.a : 0;
    this.request('jump', {input: x, play: true, countIn: this.countInOn && this.beats.length > 1});
    return this.ctx.resume();
  }
  pause() { this.request('stop'); }
  seek(x) {
    x = clamp(x, 0, this.duration);
    const ci = this.countInfo, counting = this.playing && ci && this.ctx.currentTime < ci.from + ci.n * ci.step;
    if (this.want?.kind === 'stop') this.want.seekTo = x;
    else if (!this.playing && !this.want) { this.pausedAt = x; this.onchange('seek'); }
    else this.request('jump', counting ? {input: x, countIn: true} : {input: x});    // a seek in a count-in counts again
  }
  changed() { this.request('continue'); }       // speed, pitch, regions or loop edited
  request(kind, opts = {}) {
    const w = this.want, now = this.ctx.currentTime;
    if (this.playing && (kind === 'stop' || kind === 'jump') && w?.kind !== 'stop') {
      // silent at once, also when the stretchers must first finish a pending change (no blips on quick seeks)
      if (kind === 'stop') opts = {seekTo: this.position(now), ...opts};
      ramp(this.e.music.gain, 0, now); this.cancelClicks(now);
    }
    if (w && kind === 'continue') { /* the waiting request reads the new settings when it is applied */ }
    else if (w?.kind === 'jump' && kind === 'jump') this.want = {...w, ...opts};
    else if (w?.kind === 'jump' && kind === 'stop') this.want = {kind, seekTo: w.input};
    else this.want = {kind, ...opts};
    this.tick();
  }
  tick() {
    clearTimeout(this.timer); this.timer = null;
    const now = this.ctx.currentTime;
    if (this.endAt !== null && now >= this.endAt) {
      this.endAt = null; this.playing = false; this.pausedAt = this.duration; this.onchange('pause');
    }
    const last = this.segs[this.segs.length - 1], pending = last.output > now ? last : null;
    if (this.want) {
      const w = this.want;
      // Replacing the pending change is only safe while the stretchers have not started it yet.
      const blocked = pending && (pending.output - now < this.lead || (pending.jump && w.kind === 'continue'));
      if (!blocked) {
        if (pending) { this.segs.pop(); pending.dropped = true; this.undoTrainerStep(pending); }
        this.want = null;
        this.apply(w, now, pending);
      }
    } else if (this.playing && !pending) {
      this.planNext(now);
    }
    if (this.playing && (!this.want || this.want.kind === 'continue')) this.scheduleClicks(now);
    if (this.playing || this.want) this.timer = setTimeout(() => this.tick(), TICK_MS);
  }
  apply(w, now, dropped) {
    const T = now + this.lead, music = this.e.music.gain;
    this.endAt = null;
    if (w.kind === 'stop') {
      if (!this.playing) { if (w.seekTo != null) { this.pausedAt = w.seekTo; this.onchange('seek'); } return; }
      this.pausedAt = w.seekTo ?? (dropped?.jump ? dropped.input : this.position(now));
      this.playing = false; this.countInfo = null;
      ramp(music, 0, now); this.cancelClicks(now);
      this.commit({output: T, input: this.pausedAt, rate: 1, active: false});
      this.onchange('pause');
    } else if (w.kind === 'jump') {
      const x = clamp(w.input, 0, this.duration);
      if (!this.playing && !w.play) { this.pausedAt = x; this.onchange('seek'); return; }
      if (this.playing) ramp(music, 0, now);
      this.cancelClicks(now); this.countInfo = null;
      const start = w.countIn ? this.countIn(T, x) : T;
      this.commit({output: start, input: x, rate: this.rateAt(x), active: true, jump: true});
      ramp(music, 1, start);
      this.clickedTo = start;
      if (!this.playing) this.playing = true;
      this.onchange('play');
    } else if (this.playing) {             // 'continue': keep the song position continuous at T
      const x = this.position(T);
      this.cancelClicks(T);
      this.commit({output: T, input: x, rate: this.rateAt(x), active: true});
    }
  }
  // The next speed-region edge, loop end or song end after segment s, with the context time it is reached.
  nextEvent(s) {
    if (!s.active) return null;
    const x = s.input, eps = 1e-6, L = this.loop;
    let at = this.duration, kind = 'end';
    if (L.on && L.b > L.a && x < L.b - eps && L.b <= at) { at = L.b; kind = 'wrap'; }
    for (const r of this.regions) for (const b of [r.start, r.end]) if (b > x + eps && b < at - eps) { at = b; kind = 'rate'; }
    return {kind, at, t: s.output + (at - x) / s.rate};
  }
  planNext(now) {
    const s = this.segs[this.segs.length - 1];
    let ev = this.nextEvent(s);
    if (ev?.kind === 'rate') {   // two changes closer than the lead cannot both be scheduled: skip the speed edge
      const after = this.nextEvent({output: ev.t, input: ev.at, rate: this.rateAt(ev.at), active: true});
      if (after && after.t - ev.t < this.lead) ev = {...after, t: s.output + (after.at - s.input) / s.rate};
    }
    if (!ev || ev.t - now > this.lead + PLAN_AHEAD) return;
    const T = Math.max(ev.t, now + this.lead);   // later than planned only if the timer was starved or two
    const late = T - ev.t;                        // changes come closer together than the lead
    if (ev.kind === 'end') {
      this.commit({output: T, input: this.duration, rate: 1, active: false});
      this.endAt = T;
    } else if (ev.kind === 'wrap') {
      const before = {passes: this.trainer.passes, speed: this.speed}, stepped = this.trainerStep();
      const seg = {output: T, input: this.loop.a, rate: this.rateAt(this.loop.a), active: true, kind: 'wrap', late,
        undo: stepped ? {...before, stepped: this.speed} : null};
      this.commit(seg);
      // tell the page when the new pass is heard, not now (the wrap is planned about 0.2 s ahead)
      if (stepped) setTimeout(() => { if (!seg.dropped) this.onchange('trainer'); },
        Math.max(0, (T - this.ctx.currentTime + (this.ctx.outputLatency || 0)) * 1000));
    } else {
      const input = T > ev.t ? s.input + (T - s.output) * s.rate : ev.at;
      this.commit({output: T, input, rate: this.rateAt(ev.at), active: true, kind: 'rate', late});
    }
  }
  trainerStep() {
    const tr = this.trainer;
    if (!tr.on) return false;
    tr.passes++;
    if (tr.passes % tr.every === 0 && this.speed < tr.target - 1e-9)
      this.speed = Math.min(tr.target, Math.round((this.speed + tr.step) * 100) / 100);
    return true;
  }
  // A planned wrap that is replaced before it plays (an edit, a seek) takes its pass and speed step back with it;
  // a speed the user set in between stays.
  undoTrainerStep(seg) {
    const u = seg.undo;
    if (!u || !this.trainer.on) return;
    this.trainer.passes = u.passes;
    if (this.speed === u.stepped) this.speed = u.speed;
  }
  commit(seg) {
    seg.semis = this.semis;
    this.segs.push(seg);
    while (this.segs.length > 2 && this.segs[1].output < this.ctx.currentTime - 3) this.segs.shift();
    if (this.log.length < 20000) this.log.push(seg);
    const msg = {output: seg.output, input: seg.input, rate: seg.rate, semitones: seg.semis, active: seg.active};
    for (const node of this.e.nodes) node.schedule(msg);
  }
  countIn(T, x) {
    const n = this.beatsPerBar || 4, rate = this.rateAt(x), step = this.beatInterval(x) / rate;
    for (let k = 0; k < n; k++) this.click(T + k * step, k === 0);
    this.countInfo = {from: T, step, n};                // for the "count-in 1 2 3 4" readout
    // start the music so that its next beat comes one beat after the last count-in click (x may be between beats)
    const b = this.beats[lowerBound(this.beats, x - 1e-3)], lag = b === undefined ? 0 : (b - x) / rate;
    return T + n * step - (lag > 0 && lag < step - 1e-3 ? lag : 0);
  }
  scheduleClicks(now) {
    if (!this.metroOn || !this.beats.length) return;
    const last = this.segs[this.segs.length - 1], next = this.nextEvent(last);
    const to = Math.min(now + CLICK_AHEAD, next ? next.t : Infinity), from = Math.max(this.clickedTo, now);
    if (to <= from) return;
    this.segs.forEach((s, i) => {
      if (!s.active) return;
      const a = Math.max(from, s.output), b = Math.min(to, this.segs[i + 1]?.output ?? Infinity);
      if (b <= a) return;
      const xa = s.input + (a - s.output) * s.rate, xb = s.input + (b - s.output) * s.rate;
      for (let k = lowerBound(this.beats, xa); k < this.beats.length && this.beats[k] < xb; k++)
        this.click(s.output + (this.beats[k] - s.input) / s.rate, this.isDown[k]);
    });
    this.clickedTo = to;
  }
  click(t, accent) {
    const src = new AudioBufferSourceNode(this.ctx, {buffer: accent ? this.clickHi : this.clickLo});
    src.connect(this.e.metro); src.start(t);
    const c = {t, src, accent};
    this.clicks.push(c);
    src.onended = () => { const i = this.clicks.indexOf(c); if (i >= 0) this.clicks.splice(i, 1); };
    if (this.onclick) this.onclick(c);
  }
  cancelClicks(t) {
    this.clicks = this.clicks.filter(c => { if (c.t < t) return true; c.src.stop(); return false; });
    this.clickedTo = Math.min(this.clickedTo, t);
  }
}
function ramp(param, value, t) { param.cancelScheduledValues(t); param.setTargetAtTime(value, t, 0.004); }

// wavesurfer.js expects an <audio>-like object; this one forwards to the transport (the same idea as
// wavesurfer's own BSD-3 WebAudioPlayer). It only needs paused, currentTime, duration and a few events.
class EngineMedia extends EventTarget {
  constructor(tr, heard) {
    super(); this.tr = tr; this.heard = heard;
    Object.assign(this, {src: '', currentSrc: '', volume: 1, muted: false, playbackRate: 1, seeking: false, ended: false, error: null});
  }
  get paused() { return !this.tr.playing; }
  get duration() { return this.tr.duration; }
  get currentTime() { return this.tr.position(this.heard()); }
  set currentTime(x) { this.tr.seek(x); }
  play() { return this.tr.play(); }
  pause() { this.tr.pause(); }
  load() {} canPlayType() { return ''; } removeAttribute() {}
  emit(type) { this.dispatchEvent(new Event(type)); }
}

// ---------- page ---------------------------------------------------------------------------------------------
const params = new URLSearchParams(location.search);
const songs = CFG.songs;

const LAST_KEY = 'openmusic-player-last';      // no song's settings key ('openmusic-player:<name>') can be this
function pickSong() {
  const sel = $('song');
  sel.add(new Option('— ' + T.song + ' —', ''));
  for (const s of songs) sel.add(new Option(s.name, s.name));
  sel.onchange = () => { if (sel.value) location.search = '?song=' + encodeURIComponent(sel.value); };
  // ?song=, else the song opened last time, else the only song; never a silent guess (it could be a 7-minute
  // song that takes 1.5 GB to decode)
  const asked = params.get('song'), last = store.get(LAST_KEY);
  if (asked && !songs.some(s => s.name === asked)) { setStatus(fill(T.song_missing, {name: asked}), true); return null; }
  const song = songs.find(s => s.name === (asked ?? last)) || (songs.length === 1 ? songs[0] : null);
  if (!song) { if (songs.length) setStatus(T.pick_song); return null; }
  sel.value = song.name; store.set(LAST_KEY, song.name);
  for (const a of document.querySelectorAll('header .lang a')) a.search = '?song=' + encodeURIComponent(song.name);
  return song;
}

function labelStatic() {
  const t = (id, s) => { const el = $(id); if (el) el.textContent = s; };
  const tip = (id, s) => { const el = $(id); if (el) el.title = s; };
  const name = (id, s) => $(id)?.setAttribute('aria-label', s);
  t('l-pos', T.position); t('l-key', T.key); t('l-chord', T.chord);
  t('l-drag', T.drag); t('l-drag-loop', T.drag_loop); t('l-drag-speed', T.drag_speed); t('l-snap', T.snap);
  t('l-zoom', T.zoom); t('l-notes', T.notes_of); t('l-span', T.span);
  tip('play', T.play_title); t('back', T.back); tip('back', T.back_title); t('l-loop', T.loop); tip('loop-box', T.loop_title);
  t('loop-clear', T.loop_clear); tip('loop-clear', T.loop_clear_title);
  t('set-a', T.set_a); tip('set-a', T.set_a_title); t('set-b', T.set_b); tip('set-b', T.set_b_title);
  t('l-countin', T.countin); tip('countin-box', T.countin_title); t('l-metro', T.metro); tip('metro-box', T.metro_title);
  t('l-speed', T.speed); t('speed-reset', T.speed_reset); t('l-pitch', T.pitch); t('l-semi', T.semitones);
  t('l-cents', T.cents); t('pitch-reset', T.pitch_reset); tip('speed', T.speed_title); tip('pitch', T.pitch_title);
  t('l-trainer', T.trainer); t('l-regions', T.regions); t('regions-hint', T.regions_hint);
  t('l-mixer', T.mixer); t('l-master', T.master); t('reset', T.reset); tip('reset', T.reset_title); tip('limiter', T.limiter_title);
  t('reset-song', T.reset_song); t('reset-song-2', T.reset_song); t('credits', T.credits);
  name('back', T.back_title); tip('metro-vol', T.metro_vol_title); tip('master', T.fader_title);
  name('song', T.song); name('metro-vol', T.metro_vol); name('master', T.master); name('speed-n', T.speed + ' (%)');
  name('pitch-n', T.pitch + ' (' + T.semitones + ')'); name('cents', T.pitch + ' (' + T.cents + ')');
}

async function main() {
  if (location.protocol === 'file:') return;
  window.playerLib = {createEngine, loadStems, Transport};
  labelStatic();
  const song = pickSong();
  if (!songs.length) { setStatus(T.no_songs); return; }
  if (!song) return;
  document.title = song.name + ' · ' + document.title;
  const ctx = new AudioContext({sampleRate: 44100, latencyHint: 'playback'});
  const analysisP = song.analysis ? fetch(song.base + song.analysis).then(r => r.ok ? r.json() : null).catch(() => null) : null;
  const engine = await createEngine(ctx, song.stems.length, params.get('engine') || CFG.engine);
  const loaded = await loadStems(song, engine, i => setStatus(fill(T.loading, {i: i + 1, n: song.stems.length, stem: T.stems[song.stems[i].name]})));
  const analysis = await analysisP;
  const mb = Math.round(loaded.duration * ctx.sampleRate * 8 * song.stems.length / 1e6);
  setStatus(fill(T.loaded, {n: song.stems.length, dur: fmtTime(loaded.duration).replace(/\.\d$/, ''), mb}));
  const rhythm = readRhythm(analysis);
  const tr = new Transport(engine, loaded.duration, rhythm);
  const ui = buildUI(song, analysis, rhythm, loaded, engine, tr);
  window.player = {ctx, engine, tr, song, analysis, rhythm, ui, loaded};   // for tests and the console
  document.documentElement.dataset.ready = '1';
}

function readRhythm(a) {
  const tempo = a?.tempo || {};
  const beats = Float64Array.from(tempo.beats || []), downbeats = Float64Array.from(tempo.downbeats || []);
  const isDown = Array.from(beats, b => { const k = lowerBound(downbeats, b - 0.05); return k < downbeats.length && Math.abs(downbeats[k] - b) < 0.05; });
  return {beats, downbeats, isDown, bpm: tempo.bpm || 0, beatsPerBar: tempo.beats_per_bar || null};
}

function buildUI(song, analysis, rhythm, loaded, engine, tr) {
  const ctx = engine.ctx, key = 'openmusic-player:' + song.name;
  const saved = store.get(key) || {};
  const chords = (analysis?.chords?.segments || []).filter(c => c.end > c.start);
  const chordStarts = chords.map(c => c.start);
  const {beats, downbeats} = rhythm;
  const hasGrid = beats.length > 1;
  const heard = () => {
    const ts = ctx.getOutputTimestamp?.();
    if (ts && ts.contextTime > 0 && ts.performanceTime > 0) return ts.contextTime + Math.max(0, performance.now() - ts.performanceTime) / 1000;
    return ctx.currentTime - (ctx.baseLatency || 0);
  };
  for (const id of ['info', 'timeline', 'transport', 'speedpanel', 'regionpanel', 'mixer']) $(id).hidden = false;

  // ---- state that is remembered per song (in this browser only)
  const stems = song.stems.map(s => ({...s, db: 0, mute: false, solo: false, ...(saved.stems?.[s.name] || {})}));
  let masterDb = saved.masterDb ?? 0, dragMode = 'loop', snapMode = saved.snap ?? (downbeats.length > 1 ? 'bar' : hasGrid ? 'beat' : 'off');
  let rollStem = saved.roll ?? (song.notes.guitar ? 'guitar' : Object.keys(song.notes)[0] ?? null);
  tr.speed = saved.speed ?? 1; tr.semis = saved.semis ?? 0;
  tr.regions = (saved.regions || []).filter(r => r.end <= loaded.duration + 0.01);
  if (saved.loop && saved.loop.b <= loaded.duration + 0.01) tr.loop = {...saved.loop};
  if (tr.loop.on && tr.loop.b > tr.loop.a) tr.pausedAt = tr.loop.a;     // a remembered loop starts at A
  tr.metroOn = !!saved.metro && hasGrid; tr.countInOn = !!saved.countIn && hasGrid;
  Object.assign(tr.trainer, saved.trainer || {}, {on: false, passes: 0, before: null});
  let dirty = true, rollRange = null;     // dirty: the detail view below the waveform needs a redraw
  let regionNow = null;                    // the speed region under the playhead (for the hint)
  let saveTimer = null;
  const snapshot = () => ({
    stems: Object.fromEntries(stems.map(s => [s.name, {db: s.db, mute: s.mute, solo: s.solo}])), masterDb,
    speed: tr.trainer.on ? tr.trainer.before : tr.speed, semis: tr.semis, regions: tr.regions, loop: tr.loop, metro: tr.metroOn, countIn: tr.countInOn,
    trainer: {start: tr.trainer.start, step: tr.trainer.step, every: tr.trainer.every, target: tr.trainer.target},
    snap: snapMode, roll: rollStem, metroDb: +$('metro-vol').value, span: +$('span').value});
  const writeNow = () => { clearTimeout(saveTimer); saveTimer = null; store.set(key, snapshot()); };
  const save = () => { clearTimeout(saveTimer); saveTimer = setTimeout(writeNow, 300); };
  addEventListener('pagehide', () => { if (saveTimer) writeNow(); });    // a change just before leaving is kept

  // ---- grid helpers (bars and beats from analysis.json)
  const barAt = x => lowerBound(downbeats, x + 1e-6);       // 1-based bar number of song time x (0 = pickup)
  const snap = x => {
    let grid = snapMode === 'bar' ? downbeats : snapMode === 'beat' ? beats : null;
    if (!grid || grid.length < 2) return x;
    const reach = (grid[grid.length - 1] - grid[0]) / (grid.length - 1) / 2;   // half a typical bar or beat
    grid = [0, ...grid, loaded.duration];                    // the pickup and the outro can be looped too
    const k = clamp(lowerBound(grid, x), 1, grid.length - 1);
    const g = x - grid[k - 1] < grid[k] - x ? grid[k - 1] : grid[k];
    return Math.abs(g - x) <= reach ? g : x;                 // nothing near (an intro or outro without beats): keep x
  };
  const barSpan = (a, b) => {
    if (downbeats.length < 2) return '';
    const p = barAt(a), q = barAt(b - 0.01);          // bar 0 is the pickup
    if (q < 1) return ' · ' + T.pickup;
    return ' · ' + (p < 1 ? fill(T.bars_pickup, {b: q}) : fill(p === q ? T.bar_one : T.bars, {a: p, b: q}));
  };

  // ---- waveform, time ruler and regions (wavesurfer.js)
  const media = new EngineMedia(tr, heard);
  const peaks = analysis?.peaks?.data?.length ? analysis.peaks.data : loaded.peaks;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const regs = RegionsPlugin.create();
  const ws = WaveSurfer.create({
    container: $('wave'), height: 92, media, peaks: [peaks], duration: loaded.duration, normalize: true,
    waveColor: css('--wave'), progressColor: css('--accent'), cursorColor: css('--accent'), cursorWidth: 2,
    barWidth: 2, barGap: 1, barRadius: 1, dragToSeek: false,
    plugins: [TimelinePlugin.create({height: 16, style: {fontSize: '10px', color: css('--muted')}}), regs],
  });
  let syncing = false;
  // A region's label with a small ✕ that removes it (the waveform lives in a shadow root: inline styles).
  const regionLabel = (text, title, remove) => {
    const el = document.createElement('span'); el.textContent = text + ' ';
    Object.assign(el.style, {display: 'inline-block', whiteSpace: 'nowrap'});
    const x = document.createElement('button'); x.textContent = '✕'; x.title = title; x.setAttribute('aria-label', title);
    Object.assign(x.style, {font: 'inherit', fontSize: '11px', lineHeight: '15px', padding: '0 4px', border: '0',
      borderRadius: '4px', background: 'rgba(127,127,127,.28)', color: 'inherit', cursor: 'pointer', pointerEvents: 'auto',
      position: 'relative', zIndex: '3'});
    x.addEventListener('pointerdown', e => e.stopPropagation());     // not a drag or a seek
    x.addEventListener('click', e => { e.stopPropagation(); remove(); });
    el.append(x);
    return el;
  };
  const speedColor = r => r < 0.995 ? css('--slow') : r > 1.005 ? css('--fast') : css('--loop-off');
  const swatchColor = r => r < 0.995 ? css('--slow-solid') : r > 1.005 ? css('--fast-solid') : css('--muted');
  const hasLoop = () => tr.loop.b > tr.loop.a;
  function drawRegions() {
    syncing = true;
    regs.clearRegions();
    // Each drag mode edits its own kind of region. The others, and a loop that is switched off, let the mouse
    // through, so a new loop can be drawn across speed regions (or over the old loop) and a speed region inside a loop.
    const add = (opts, live) => {
      const r = regs.addRegion({...opts, drag: live, resize: live});
      if (!live && r.element) r.element.style.pointerEvents = 'none';
    };
    tr.regions.forEach((r, i) => add({id: 'speed-' + i, start: r.start, end: r.end, color: speedColor(r.speed),
      content: regionLabel(pct(r.speed), T.delete, () => { tr.regions.splice(i, 1); afterRegionEdit(); })}, dragMode === 'speed'));
    if (hasLoop()) add({id: 'loop', start: tr.loop.a, end: tr.loop.b, color: tr.loop.on ? css('--loop') : css('--loop-off'),
      content: regionLabel('A–B' + (tr.loop.on ? '' : T.loop_off), T.loop_clear_title, clearLoop)}, dragMode === 'loop' && tr.loop.on);
    $('loop-clear').disabled = !hasLoop();
    syncing = false;
    dirty = true; regionNow = null;          // the detail view and the speed-region hint follow
  }
  function setSpeedRegion(a, b, speed, skip = null) {     // the new region wins where it overlaps others
    const keep = [];
    for (const r of tr.regions) {
      if (r === skip) continue;
      if (r.end <= a || r.start >= b) { keep.push(r); continue; }
      if (r.start < a - 0.05) keep.push({...r, end: a});
      if (r.end > b + 0.05) keep.push({...r, start: b});
    }
    keep.push({start: a, end: b, speed});
    tr.regions = keep.sort((p, q) => p.start - q.start);
  }
  const minLen = () => snapMode === 'bar' && downbeats.length > 1 ? 0.5 : 0.2;
  function snapped(r) {
    let a = snap(r.start), b = snap(r.end);
    if (b - a < minLen()) b = Math.min(loaded.duration, Math.max(r.end, a + minLen()));
    return [a, b];
  }
  const intoLoop = () => { const x = tr.position(); if (tr.loop.on && (x < tr.loop.a || x >= tr.loop.b)) tr.seek(tr.loop.a); };
  const pxTime = px => px * loaded.duration / (ws.getWrapper().getBoundingClientRect().width || 1);
  const timeAt = e => { const w = ws.getWrapper().getBoundingClientRect(); return clamp((e.clientX - w.left) / w.width * loaded.duration, 0, loaded.duration); };
  let downAt = null, upAt = null;         // song times under the last press and release on the waveform
  ws.getWrapper().addEventListener('pointerdown', e => { downAt = timeAt(e); upAt = null; }, true);
  document.addEventListener('pointerup', e => { if (downAt != null) upAt = timeAt(e); }, true);
  // while the mouse is down the zoomed waveform must not scroll under it, or a drawn loop ends short of the mouse
  $('wave').addEventListener('pointerdown', () => { if (+$('zoom').value > 1) ws.setOptions({autoScroll: false}); }, true);
  document.addEventListener('pointerup', () => { if (ws.options.autoScroll === false) setTimeout(() => ws.setOptions({autoScroll: true})); }, true);
  regs.on('region-created', r => {
    if (syncing) return;
    setTimeout(() => {
      // the region runs from the press to the release: wavesurfer's own geometry is 5 px off (it starts a drawn
      // region 5 px wide) and worse when a drag turns back past its start
      const [p, q] = downAt != null && upAt != null ? [Math.min(downAt, upAt), Math.max(downAt, upAt)] : [r.start, r.end];
      if (q - p < pxTime(12)) { r.remove(); tr.seek(downAt ?? r.start); return; }   // a shaky click: seek, like a click
      const [a, b] = snapped({start: p, end: q});
      if (dragMode === 'loop') { tr.loop = {on: true, a, b}; loopBox.checked = true; updateTrainerText(); }
      else setSpeedRegion(a, b, clamp(Math.round(tr.speed * 0.8 * 20) / 20, 0.5, 1.5));   // 80 % of the speed now
      afterRegionEdit();
      if (dragMode === 'loop') intoLoop();      // a new loop plays at once, not after the song reaches it
      else showRegionRow(a);
    });
  });
  regs.on('region-updated', r => {
    setTimeout(() => {
      // a live region moved (not resized) by a few px was a shaky click: put it back and seek, as a click does
      const old = r.id === 'loop' ? {start: tr.loop.a, end: tr.loop.b} : tr.regions[+r.id.split('-')[1]];
      const ds = old ? r.start - old.start : 0, de = old ? r.end - old.end : 0;
      if (old && Math.abs(ds - de) < 1e-6 && Math.abs(ds) < pxTime(6)) { drawRegions(); if (downAt != null) tr.seek(downAt); return; }
      const [a, b] = snapped(r);
      if (r.id === 'loop') tr.loop = {...tr.loop, a, b};
      else { const old = tr.regions[+r.id.split('-')[1]]; if (old) setSpeedRegion(a, b, old.speed, old); }
      afterRegionEdit();
      if (r.id === 'loop') intoLoop();
    });
  });
  regs.on('region-double-clicked', r => { if (r.id === 'loop') clearLoop(); });
  let disableDrag = null;
  function setDragMode(m) {
    dragMode = m;
    if (disableDrag) disableDrag();
    // 10 px before a press counts as a drag (wavesurfer's default 3 px turned shaky clicks into loops)
    disableDrag = regs.enableDragSelection({color: m === 'loop' ? css('--loop') : css('--slow')}, 10);
  }
  // the browser may bring back form values after Back; the page state wins
  document.querySelectorAll('input[name=drag]').forEach(el => { el.checked = el.value === 'loop'; el.onchange = () => { setDragMode(el.value); drawRegions(); }; });
  setDragMode('loop');
  $('zoom').value = 1;
  const snapSel = $('snap');
  for (const [v, label] of [['bar', T.snap_bar], ['beat', T.snap_beat], ['off', T.snap_off]])
    if (v === 'off' || (v === 'bar' ? downbeats.length > 1 : hasGrid)) snapSel.add(new Option(label, v));
  snapSel.value = snapMode; if (snapSel.value !== snapMode) snapMode = snapSel.value = 'off';
  snapSel.onchange = () => { snapMode = snapSel.value; save(); };
  $('snap-box').hidden = !hasGrid;
  $('zoom').oninput = () => { const z = +$('zoom').value; ws.zoom(z === 1 ? 0 : z * $('wave').clientWidth / loaded.duration); };

  // ---- speed regions list
  function afterRegionEdit() { drawRegions(); renderRegionList(); tr.changed(); save(); }
  function renderRegionList() {
    const list = $('regionlist');
    list.replaceChildren();
    if (!tr.regions.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = T.regions_empty; list.append(p); return; }
    tr.regions.forEach((r, i) => {
      const row = document.createElement('div'); row.className = 'item';
      const sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = swatchColor(r.speed);
      const txt = document.createElement('span'); txt.textContent = fill(T.region_row, {a: fmtTime(r.start), b: fmtTime(r.end)}) + barSpan(r.start, r.end);
      const num = document.createElement('input'); num.type = 'number'; num.min = 50; num.max = 150; num.step = 5; num.value = Math.round(r.speed * 100);
      num.setAttribute('aria-label', T.region_speed);
      num.onchange = () => { r.speed = clamp(+num.value || 100, 50, 150) / 100; num.value = Math.round(r.speed * 100); sw.style.background = swatchColor(r.speed); drawRegions(); tr.changed(); save(); };
      const go = document.createElement('button'); go.textContent = T.play_here; go.title = T.play_here_title;
      go.setAttribute('aria-label', T.play_here_title); go.onclick = () => { tr.seek(r.start); if (!tr.playing) tr.play(); };
      const del = document.createElement('button'); del.textContent = '×'; del.title = T.delete; del.setAttribute('aria-label', T.delete);
      del.onclick = () => { tr.regions.splice(i, 1); afterRegionEdit(); };
      row.append(sw, txt, num, document.createTextNode('%'), go, del);
      list.append(row);
    });
  }
  function showRegionRow(a) {   // a new speed region: flash its row and say where its speed is set; no scrolling,
    const i = tr.regions.findIndex(r => Math.abs(r.start - a) < 1e-6), r = tr.regions[i];   // so the waveform
    const row = $('regionlist').children[i];                                               // stays in view
    if (!row) return;
    row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash');
    setStatus(fill(T.region_new, {a: fmtTime(r.start), b: fmtTime(r.end), pct: pct(r.speed)}));
  }

  // ---- transport controls
  const playBtn = $('play'), loopBox = $('loop'), trainerBox = $('trainer');
  trainerBox.checked = false;            // the trainer never comes back on by itself (not even after Back)
  const showPlay = () => { playBtn.textContent = tr.playing ? T.pause : T.play; playBtn.classList.toggle('on', tr.playing); };
  playBtn.onclick = () => tr.playing ? tr.pause() : tr.play();
  $('back').onclick = () => tr.seek(tr.loop.on ? tr.loop.a : 0);
  loopBox.checked = tr.loop.on;
  // n bars on (n < 0: back) from the bar line at or after x; 2 s a bar when there are no bars
  const barsFrom = (x, n) => {
    if (downbeats.length < 2) return clamp(x + 2 * n, 0, loaded.duration);
    const k = lowerBound(downbeats, x - 1e-3) + n;
    return k < 0 ? 0 : k < downbeats.length ? downbeats[k] : loaded.duration;
  };
  function setLoop(on) {
    if (on && !hasLoop()) {      // no loop drawn yet: take two bars from here (or 4 s)
      const x = tr.position(), k = Math.max(0, barAt(x) - 1), a = downbeats.length > 1 ? downbeats[k] : x;
      tr.loop = {a, b: Math.min(loaded.duration, downbeats.length > k + 2 ? downbeats[k + 2] : a + 4)};
    }
    if (!on) stopTrainer();
    tr.loop.on = on; loopBox.checked = on;
    const x = tr.position();
    if (on && (x < tr.loop.a || x >= tr.loop.b)) tr.seek(tr.loop.a); else tr.changed();
    drawRegions(); updateTrainerText(); save();
  }
  loopBox.onchange = () => setLoop(loopBox.checked);
  function setLoopEdge(edge) {        // Set A / Set B (keys A, B): that end of the loop goes to the playhead
    const x = snap(tr.position(heard())), min = minLen();
    let {a, b} = hasLoop() ? tr.loop : {a: x, b: x};
    if (edge === 'a') { a = Math.min(x, loaded.duration - min); if (b < a + min) b = Math.max(a + min, barsFrom(a, 2)); }
    else { b = Math.max(x, min); if (b < a + min) a = Math.max(0, Math.min(b - min, barsFrom(b, -2))); }
    b = Math.min(b, loaded.duration);
    if (!(b > a)) return;
    tr.loop = {on: true, a, b}; loopBox.checked = true;
    tr.changed(); drawRegions(); updateTrainerText(); save();
    if (edge === 'b') intoLoop();       // B pressed at the end of the phrase: back to A
  }
  $('set-a').onclick = () => setLoopEdge('a'); $('set-b').onclick = () => setLoopEdge('b');
  function clearLoop() {       // ✕ button, the ✕ on the waveform, a double-click on it, Esc: the loop is gone
    if (!hasLoop()) return;
    stopTrainer();
    tr.loop = {on: false, a: 0, b: 0}; loopBox.checked = false;
    tr.changed(); drawRegions(); updateTrainerText(); save();
  }
  $('loop-clear').onclick = clearLoop;
  const loopInfo = x => {
    if (!hasLoop()) return T.loop_none;
    const {a, b, on} = tr.loop;
    return fmtTime(a) + '–' + fmtTime(b) + barSpan(a, b) + (!on ? T.loop_off : x < a - 0.05 || x > b + 0.05 ? T.loop_outside : '');
  };
  $('countin').checked = tr.countInOn; $('countin').onchange = () => { tr.countInOn = $('countin').checked; save(); };
  $('metro').checked = tr.metroOn;
  $('metro').onchange = () => { tr.metroOn = $('metro').checked; if (!tr.metroOn) tr.cancelClicks(ctx.currentTime); else tr.tick(); save(); };
  $('metro-vol').value = saved.metroDb ?? -6;
  const setMetroVol = () => engine.metro.gain.setTargetAtTime(dbToGain(+$('metro-vol').value), ctx.currentTime, 0.01);
  $('metro-vol').oninput = () => { setMetroVol(); save(); }; setMetroVol();
  $('metro-vol').ondblclick = () => { $('metro-vol').value = -6; setMetroVol(); save(); };
  $('countin-box').hidden = $('metro-box').hidden = !hasGrid;

  // ---- speed, pitch, trainer
  const speedR = $('speed'), speedN = $('speed-n');
  function setSpeed(r, fromTrainer = false) {
    tr.speed = clamp(Math.round(r * 100) / 100, 0.5, 1.5);
    speedR.value = speedN.value = Math.round(tr.speed * 100);
    if (!fromTrainer) tr.changed();
    if (tr.trainer.on) updateTrainerText();
    save();
  }
  speedR.oninput = () => setSpeed(speedR.value / 100);
  speedN.onchange = () => setSpeed((+speedN.value || 100) / 100);
  $('speed-reset').onclick = speedR.ondblclick = () => setSpeed(1);
  const pitchR = $('pitch'), pitchN = $('pitch-n'), centsN = $('cents');
  const semis0 = Math.round(tr.semis);
  pitchR.value = pitchN.value = semis0; centsN.value = Math.round((tr.semis - semis0) * 100);
  function setPitch() {
    const st = clamp(Math.round(+pitchN.value || 0), -12, 12), c = clamp(Math.round(+centsN.value || 0), -50, 50);
    pitchR.value = pitchN.value = st; centsN.value = c;
    tr.semis = st + c / 100; tr.changed(); updateKey(); save(); dirty = true;
  }
  pitchR.oninput = () => { pitchN.value = pitchR.value; setPitch(); };
  pitchN.onchange = centsN.onchange = setPitch;
  $('pitch-reset').onclick = pitchR.ondblclick = () => { pitchN.value = 0; centsN.value = 0; setPitch(); };
  function numberInput(field, min, max, step) {
    const el = document.createElement('input'); el.type = 'number'; el.min = min; el.max = max; el.step = step;
    const scale = field === 'every' ? 1 : 100;
    el.value = Math.round(tr.trainer[field] * scale);
    el.setAttribute('aria-label', T.trainer_fields[field]);
    el.onchange = () => { tr.trainer[field] = clamp(+el.value || min, min, max) / scale; el.value = Math.round(tr.trainer[field] * scale); save(); updateTrainerText(); };
    return el;
  }
  function updateTrainerText(problem = '') {
    const p = $('trainer-text');
    if (!p.dataset.built) {
      p.dataset.built = '1';
      const inputs = {every: numberInput('every', 1, 8, 1), step: numberInput('step', 1, 20, 1), start: numberInput('start', 50, 150, 5), target: numberInput('target', 50, 150, 5)};
      for (const part of T.trainer_text.split(/(\{\w+\})/)) {
        const m = part.match(/^\{(\w+)\}$/);
        p.append(m ? inputs[m[1]] : document.createTextNode(part));
      }
      for (const id of ['trainer-need', 'trainer-status']) {
        const el = document.createElement('span'); el.id = id; el.style.display = 'block'; p.append(el);
      }
      $('trainer-need').textContent = T.trainer_need_loop;
      $('trainer-status').style.fontWeight = '600';
    }
    $('trainer-need').hidden = tr.loop.on;
    const t = tr.trainer, st = $('trainer-status');
    st.textContent = problem || (!t.on ? '' : tr.speed >= t.target - 1e-9 ? fill(T.trainer_done, {target: pct(t.target)})
      : fill(T.trainer_pass, {n: t.passes + 1, now: pct(tr.speed), target: pct(t.target)}));
    st.hidden = !st.textContent;
  }
  function stopTrainer() {           // back to the speed from before the trainer
    if (!tr.trainer.on) return;
    tr.trainer.on = trainerBox.checked = false;
    if (tr.trainer.before != null) setSpeed(tr.trainer.before);
    tr.trainer.before = null;
    updateTrainerText();
  }
  trainerBox.onchange = () => {
    if (!trainerBox.checked) return stopTrainer();
    if (tr.trainer.start >= tr.trainer.target) { trainerBox.checked = false; return updateTrainerText(T.trainer_bad); }
    tr.trainer.before = tr.speed; tr.trainer.passes = 0;
    if (!tr.loop.on) setLoop(true);
    tr.trainer.on = true;
    setSpeed(tr.trainer.start);
    updateTrainerText();
  };
  updateTrainerText();
  tr.onchange = what => {
    if (what === 'trainer') setSpeed(tr.speed, true);       // a pass of the speed trainer is heard
    if (what === 'trainer' || what === 'play') updateTrainerText();
    showPlay();
    if (what === 'pause' || what === 'play' || what === 'seek') media.emit(what === 'seek' ? 'timeupdate' : what);
  };

  // ---- mixer
  const box = $('stems');
  const strips = [];
  function applyGains() {
    const anySolo = stems.some(s => s.solo), t = ctx.currentTime;
    engine.setAudible(stems.map(s => !s.mute && (!anySolo || s.solo) && s.db > MIN_DB));
    stems.forEach((s, i) => {
      const on = !s.mute && (!anySolo || s.solo);
      engine.gains[i].gain.setTargetAtTime(on ? dbToGain(s.db) : 0, t, 0.012);
      s.row.classList.toggle('silent', !on);
      s.row.querySelector('.m').classList.toggle('on', s.mute);
      s.row.querySelector('.s').classList.toggle('on', s.solo);
      s.row.querySelector('.m').setAttribute('aria-pressed', s.mute);
      s.row.querySelector('.s').setAttribute('aria-pressed', s.solo);
      s.row.querySelector('.db').textContent = fmtDb(s.db);
      s.row.querySelector('.fader').value = Math.max(MIN_DB, s.db);
    });
    engine.out.gain.setTargetAtTime(dbToGain(masterDb), t, 0.012);
    $('master').value = masterDb; $('master-db').textContent = fmtDb(masterDb);
    save();
  }
  stems.forEach((s, i) => {
    const row = document.createElement('div'); row.className = 'stem'; row.style.setProperty('--c', STEM_COLORS[s.name]);
    row.innerHTML = `<span class="name"></span><button class="ms m"></button><button class="ms s solo"></button>
      <input type="range" class="fader" min="${MIN_DB}" max="${MAX_DB}" step="0.5"><span class="db"></span>
      <div class="strip"><canvas></canvas><i></i></div><span class="midi"></span>`;
    row.querySelector('.name').textContent = T.stems[s.name] || s.name;
    const m = row.querySelector('.m'), so = row.querySelector('.s'), f = row.querySelector('.fader');
    const label = T.stems[s.name] || s.name;
    m.textContent = 'M'; m.title = T.mute; so.textContent = 'S'; so.title = T.solo;
    m.setAttribute('aria-label', label + ' ' + T.mute); so.setAttribute('aria-label', label + ' ' + T.solo);
    m.onclick = () => { s.mute = !s.mute; applyGains(); };
    so.onclick = () => { s.solo = !s.solo; applyGains(); };
    f.oninput = () => { s.db = +f.value; applyGains(); };
    f.ondblclick = () => { s.db = 0; applyGains(); }; f.title = T.fader_title;
    f.setAttribute('aria-label', (T.stems[s.name] || s.name) + ' dB');
    if (song.midi[s.name]) {
      const a = document.createElement('a'); a.href = song.base + song.midi[s.name]; a.download = song.name + ' - ' + s.name + '.mid';
      a.textContent = T.midi; a.title = T.midi_title; row.querySelector('.midi').append(a);
    }
    const strip = row.querySelector('.strip');
    if (loaded.strips[i].reduce((m, v) => Math.max(m, v), 0) < 10 ** (-50 / 20)) {   // below -50 dBFS throughout
      const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = T.almost_silent;
      tag.title = T.almost_silent_title; strip.append(tag);
    }
    strip.onclick = e => { const r = strip.getBoundingClientRect(); tr.seek((e.clientX - r.left) / r.width * loaded.duration); };
    strips.push({canvas: strip.querySelector('canvas'), line: strip.querySelector('i'), data: loaded.strips[i], color: STEM_COLORS[s.name]});
    s.row = row; box.append(row);
  });
  $('master').oninput = () => { masterDb = +$('master').value; applyGains(); };
  $('master').ondblclick = () => { masterDb = 0; applyGains(); };
  $('reset').onclick = () => { stems.forEach(s => Object.assign(s, {db: 0, mute: false, solo: false})); masterDb = 0; applyGains(); };
  applyGains();
  function drawStrips() {
    const peak = Math.max(1e-6, ...strips.map(s => s.data.reduce((m, v) => Math.max(m, v), 0)));
    for (const s of strips) {
      const c = s.canvas, w = c.clientWidth, h = c.clientHeight, dpr = devicePixelRatio || 1;
      if (!w) continue;
      c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
      const g = c.getContext('2d'); g.scale(dpr, dpr); g.fillStyle = s.color;
      const per = s.data.length / w;
      for (let x = 0; x < w; x++) {
        let v = 0; for (let j = Math.floor(x * per); j < Math.floor((x + 1) * per); j++) v = Math.max(v, s.data[j]);
        const y = Math.sqrt(v / peak) * h; g.fillRect(x, h - y, 1, y);
      }
    }
  }

  // ---- tempo, key and chord facts
  const tempo = analysis?.tempo, keyInfo = analysis?.key;
  $('fact-tempo').hidden = !tempo?.bpm; $('fact-key').hidden = !keyInfo?.tonic; $('fact-chord').hidden = !chords.length;
  $('no-analysis').hidden = !!analysis; $('no-analysis').textContent = T.no_analysis;
  if (tempo?.bpm) {
    const alts = (tempo.bpm_alternatives || []).map(b => Math.round(b)).join(' / ');
    $('f-bpm').dataset.base = [tempo.beats_per_bar ? fill(T.meter, {n: tempo.beats_per_bar}) : '', tempo.stable === false ? T.unstable : '', alts ? fill(T.bpm_alt, {alts}) : ''].filter(Boolean).join(' · ');
  }
  function keyLabel(tonic, mode) { return tonic + (mode === 'minor' ? T.minor : T.major); }
  // the key heard after the pitch shift, for spelling transposed chords
  const heardKey = () => keyInfo?.tonic ? {pc: ((pcOf(keyInfo.tonic) + Math.round(tr.semis)) % 12 + 12) % 12, mode: keyInfo.mode} : null;
  function updateKey() {
    if (!keyInfo?.tonic) return;
    const k = Math.round(tr.semis), conf = Math.round((keyInfo.confidence ?? 0) * 100);
    const orig = CFG.lang === 'zh' ? keyInfo.label_zh || keyLabel(keyInfo.tonic, keyInfo.mode) : keyInfo.label_en || keyLabel(keyInfo.tonic, keyInfo.mode);
    $('f-key').textContent = k ? keyLabel(spellKey(pcOf(keyInfo.tonic) + k, keyInfo.mode), keyInfo.mode) : orig;   // the key you hear
    const altName = a => {          // analysis.json alternatives carry labels only: "G major" / "G 大调"
      const m = /^([A-G][#b]?) (major|minor)$/.exec(a.label_en || '');
      return k && m ? keyLabel(spellKey(pcOf(m[1]) + k, m[2]), m[2]) : CFG.lang === 'zh' ? a.label_zh : a.label_en;
    };
    const alts = (keyInfo.alternatives || []).slice(0, 2).map(altName).filter(Boolean).join(' / ');
    const notes = k ? [fill(T.key_orig, {key: orig})] : [];
    notes.push(conf < 50 && alts ? fill(T.key_low, {conf, alts}) : fill(T.key_conf, {conf}));
    $('f-key-note').textContent = notes.join(' · ');
  }
  updateKey();

  // ---- detail view: beat grid, chord lane and piano roll around the playhead (for transcribing by ear)
  const cv = $('detail'), rollSel = $('roll');
  const noteCache = {};
  let notes = null;
  const drumName = p => T.kit[p] || notes?.kit?.[p] || String(p);
  const rollStems = song.stems.filter(s => song.notes[s.name]);
  for (const s of rollStems) rollSel.add(new Option(T.stems[s.name] || s.name, s.name));
  rollSel.add(new Option(T.notes_none, ''));
  if (rollStem !== '' && !rollStems.some(s => s.name === rollStem)) rollStem = (song.notes.guitar ? 'guitar' : rollStems[0]?.name) ?? '';
  rollSel.value = rollStem;
  $('roll-box').hidden = !rollStems.length;
  $('roll-gp').replaceChildren();
  if (!$('home').hidden && Object.keys(song.midi).length) {
    // opened from the workbench: choose the audio track, and the workbench builds the file on the spot
    const b = document.createElement('button'); b.textContent = T.gp_choose; b.title = T.gp_choose_title;
    b.onclick = openGpDialog; $('roll-gp').append(b);
  } else if (song.gp) {            // a plain file server: the file the pipeline wrote (vocals + guitar + other)
    const a = document.createElement('a'); a.href = song.base + song.gp; a.download = song.name + '.gp';
    a.textContent = '⬇ ' + T.gp; a.title = T.gp_title; $('roll-gp').append(a);
  }
  // ---- Guitar Pro download with a chosen audio track (POST /api/gp on the workbench; nothing is stored)
  const GP_KEY = 'openmusic-gp-audio', GP_DEFAULT = {audio: 'stems', stems: ['vocals', 'guitar', 'other']};
  function openGpDialog() {
    const d = $('gp-dialog'), pick = {...GP_DEFAULT, ...(store.get(GP_KEY) || {})};
    $('gp-h').textContent = T.gp_h; $('gp-hint').textContent = T.gp_hint; $('gp-l-stems').textContent = T.gp_stems;
    $('gp-l-original').textContent = T.gp_original; $('gp-l-none').textContent = T.gp_none;
    $('gp-go').textContent = T.gp_go; $('gp-cancel').textContent = T.gp_cancel; $('gp-status').textContent = '';
    $('gp-stems').replaceChildren(...song.stems.map(s => {
      const l = document.createElement('label'), c = document.createElement('input');
      c.type = 'checkbox'; c.value = s.name; c.checked = pick.stems.includes(s.name);
      c.onchange = () => { d.querySelector('input[value=stems]').checked = true; };
      l.append(c, ' ' + (T.stems[s.name] || s.name)); return l;
    }));
    for (const r of d.querySelectorAll('input[name=gp-audio]')) r.checked = r.value === pick.audio;
    $('gp-go').disabled = false;
    d.showModal();
  }
  $('gp-cancel').onclick = () => $('gp-dialog').close();
  $('gp-go').onclick = async () => {
    const d = $('gp-dialog'), audio = d.querySelector('input[name=gp-audio]:checked')?.value || 'stems';
    const chosen = [...$('gp-stems').querySelectorAll('input:checked')].map(c => c.value);
    if (audio === 'stems' && !chosen.length) { $('gp-status').textContent = T.gp_pick; return; }
    store.set(GP_KEY, {audio, stems: chosen.length ? chosen : GP_DEFAULT.stems});
    const label = audio === 'original' ? T.gp_label_original : audio === 'none' ? T.gp_label_none
      : chosen.map(n => T.stems[n] || n).join(' ');
    const name = song.name + ' (' + label.replace(/[\\/:*?"<>|]/g, '-') + ').gp';
    $('gp-status').textContent = T.gp_busy; $('gp-go').disabled = true;
    try {
      const root = location.pathname.replace(/[^/]*$/, '').replace(/^\//, '').replace(/\/$/, '');
      const r = await fetch('/api/gp', {method: 'POST', headers: {'X-OpenMusic': '1', 'Content-Type': 'application/json'},
        body: JSON.stringify({root: decodeURIComponent(root), song: song.name, audio, stems: chosen})});
      if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || e.error || r.status); }
      const url = URL.createObjectURL(await r.blob()), a = document.createElement('a');
      a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      $('gp-status').textContent = fill(T.gp_done, {name});
      setTimeout(() => d.close(), 1200);
    } catch (e) {
      $('gp-status').textContent = fill(T.gp_err, {msg: e.message || String(e)});
    } finally { $('gp-go').disabled = false; }
  };
  async function loadNotes() {
    rollStem = rollSel.value; save();
    $('roll-midi').replaceChildren();
    if (!rollStem) { notes = null; layoutDetail(); return; }
    noteCache[rollStem] ??= fetch(song.base + song.notes[rollStem]).then(r => r.json()).catch(() => null);
    notes = await noteCache[rollStem];
    rollRange = null;
    if (notes) {
      notes.notes.sort((p, q) => p[0] - q[0]);
      notes.starts = notes.notes.map(n => n[0]);
      notes.maxLen = notes.notes.reduce((m, n) => Math.max(m, n[1] - n[0]), 0);
      if (notes.kit) {
        const played = new Set(notes.notes.map(n => n[2]));
        notes.rows = [...DRUM_ROWS.filter(p => played.has(p)), ...[...played].filter(p => !DRUM_ROWS.includes(p)).sort((p, q) => q - p)];
      }
    }
    cv.dataset.lane = notes?.kit ? notes.rows.join(' ') : notes ? 'pitch' : '';   // which view is drawn (for tests)
    if (song.midi[rollStem]) {
      const a = document.createElement('a'); a.href = song.base + song.midi[rollStem]; a.download = song.name + ' - ' + rollStem + '.mid';
      a.textContent = '⬇ ' + (T.stems[rollStem] || rollStem) + ' ' + T.midi; a.title = T.midi_title; $('roll-midi').append(a);
    }
    layoutDetail();
  }
  rollSel.onchange = loadNotes;
  $('span').value = saved.span ?? 10;
  const showSpan = () => { $('span-v').textContent = fill(T.seconds, {n: $('span').value}); };
  $('span').oninput = () => { dirty = true; showSpan(); save(); };
  showSpan();
  $('no-notes').hidden = rollStems.length > 0; $('no-notes').textContent = T.no_notes;
  function layoutDetail() {
    const any = hasGrid || chords.length || notes;
    cv.hidden = !any; $('detail-tools').hidden = !any;
    cv.classList.toggle('short', !notes);
    dirty = true;
  }
  const colors = {};
  const readColors = () => { for (const n of ['text', 'muted', 'line', 'accent', 'on-accent', 'accent-soft', 'panel', 'panel2', 'loop', 'loop-off', 'slow', 'fast']) colors[n] = css('--' + n); };
  readColors();
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
    readColors(); dirty = true;
    ws.setOptions({waveColor: css('--wave'), progressColor: css('--accent'), cursorColor: css('--accent')});
    setDragMode(dragMode); drawRegions(); renderRegionList();
  });
  function drawDetail(x) {
    if (cv.hidden) return;
    const dpr = devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
    if (!W) return;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
    const span = +$('span').value, t0 = x - span * 0.25, t1 = t0 + span, X = t => (t - t0) / span * W;
    const RULER = 18, CH = chords.length ? 26 : 0, top = RULER + CH + 4, k = Math.round(tr.semis);
    g.font = '11px system-ui, sans-serif'; g.textBaseline = 'middle';
    if (tr.loop.b > tr.loop.a) { g.fillStyle = tr.loop.on ? colors.loop : 'transparent'; g.fillRect(X(tr.loop.a), 0, X(tr.loop.b) - X(tr.loop.a), H); }
    for (const r of tr.regions) if (r.end > t0 && r.start < t1) { g.fillStyle = r.speed < 0.995 ? colors.slow : r.speed > 1.005 ? colors.fast : colors['loop-off']; g.fillRect(X(r.start), 0, X(r.end) - X(r.start), 4); }
    for (let i = lowerBound(beats, t0); i < beats.length && beats[i] < t1; i++) {
      const px = Math.round(X(beats[i])) + 0.5, down = rhythm.isDown[i];
      g.strokeStyle = down ? colors.muted : colors.line; g.lineWidth = down ? 1.5 : 1;
      g.beginPath(); g.moveTo(px, down ? 2 : RULER - 6); g.lineTo(px, H); g.stroke();
      if (down) { g.fillStyle = colors.muted; g.fillText(String(barAt(beats[i])), px + 3, 9); }
    }
    let ci = Math.max(0, lowerBound(chordStarts, t0) - 1);
    for (; ci < chords.length && chords[ci].start < t1; ci++) {
      const c = chords[ci]; if (c.end < t0 || c.label === 'N') continue;
      const a = Math.max(0, X(c.start)), b = Math.min(W, X(c.end)), cur = c.start <= x && x < c.end;
      g.fillStyle = cur ? colors.accent : colors.panel; g.fillRect(a + 1, RULER, Math.max(0, b - a - 2), CH - 2);
      g.fillStyle = cur ? colors['on-accent'] : colors.text; g.save(); g.beginPath(); g.rect(a + 1, RULER, Math.max(0, b - a - 2), CH); g.clip();
      g.font = '600 13px system-ui, sans-serif'; g.fillText(transposeChord(c.label, k, heardKey()), Math.max(a, 0) + 5, RULER + CH / 2 - 1); g.restore();
    }
    if (notes?.notes?.length && notes.kit) {
      // drum lane: one row per kit piece that is played, cymbals on top and kick at the bottom as in drum
      // notation; darker = louder
      const rows = notes.rows, rowH = (H - top) / rows.length, KEYS = 64, Y = i => top + i * rowH;
      rows.forEach((p, i) => { if (i % 2) { g.fillStyle = colors['accent-soft']; g.globalAlpha = 0.35; g.fillRect(KEYS, Y(i), W - KEYS, rowH); g.globalAlpha = 1; } });
      const sounding = [];
      for (let i = lowerBound(notes.starts, t0 - notes.maxLen); i < notes.notes.length && notes.notes[i][0] < t1; i++) {
        const [s, e, p, v] = notes.notes[i], row = rows.indexOf(p); if (e < t0 || row < 0) continue;
        const a = Math.max(KEYS, X(s)), on = s <= x && x < e;
        if (on) sounding.push(p);
        g.fillStyle = on ? colors.accent : STEM_COLORS.drums; g.globalAlpha = on ? 1 : 0.3 + 0.7 * v / 127;
        g.fillRect(a, Y(row) + 1, Math.max(3, X(e) - a), Math.max(2, rowH - 2)); g.globalAlpha = 1;
      }
      g.fillStyle = colors.panel2; g.fillRect(0, top, KEYS, H - top);
      g.fillStyle = colors.muted; g.font = `${clamp(rowH - 1, 8, 11)}px system-ui, sans-serif`;
      rows.forEach((p, i) => g.fillText(drumName(p), 3, Y(i) + rowH / 2));
      if (sounding.length) {
        const txt = T.now_notes + rows.filter(p => sounding.includes(p)).map(drumName).join(' ');
        g.font = '600 12px system-ui, sans-serif'; const w = g.measureText(txt).width;
        g.fillStyle = colors.panel; g.fillRect(W - w - 14, top + 2, w + 10, 18); g.fillStyle = colors.text; g.fillText(txt, W - w - 9, top + 11);
      }
    } else if (notes?.notes?.length) {
      // fit the pitch axis to the notes in view (at least 15 semitones), so rows are tall enough to label;
      // grow at once, shrink only when much too wide, so the axis does not jump while playing; with no notes in
      // view keep it as it is (padding it again on every frame made it grow without end in a silent passage)
      let lo = 127, hi = 0;
      for (let i = lowerBound(notes.starts, t0 - notes.maxLen); i < notes.notes.length && notes.starts[i] < t1; i++)
        if (notes.notes[i][1] > t0) { lo = Math.min(lo, notes.notes[i][2]); hi = Math.max(hi, notes.notes[i][2]); }
      if (lo > hi && rollRange) [lo, hi] = rollRange;
      else {
        if (lo > hi) [lo, hi] = notes.pitch_range || [48, 72];
        const pad = Math.max(1, Math.ceil((15 - (hi - lo)) / 2));
        lo = Math.max(0, lo - pad); hi = Math.min(127, hi + pad);
        if (rollRange && lo >= rollRange[0] && hi <= rollRange[1] && (rollRange[1] - rollRange[0]) - (hi - lo) < 8) [lo, hi] = rollRange;
      }
      rollRange = [lo, hi];
      cv.dataset.range = lo + '-' + hi;                // for tests
      const rowH = (H - top) / (hi - lo + 1), KEYS = 34, Y = p => top + (hi - p) * rowH;
      for (let p = lo; p <= hi; p++) if ([1, 3, 6, 8, 10].includes(((p + k) % 12 + 12) % 12)) { g.fillStyle = colors['accent-soft']; g.globalAlpha = 0.35; g.fillRect(KEYS, Y(p), W - KEYS, rowH); g.globalAlpha = 1; }
      const sounding = [];
      const first = lowerBound(notes.starts, t0 - notes.maxLen);
      for (let i = first; i < notes.notes.length && notes.notes[i][0] < t1; i++) {
        const [s, e, p] = notes.notes[i]; if (e < t0) continue;
        const a = Math.max(KEYS, X(s)), b = X(e), on = s <= x && x < e;
        if (on) sounding.push(p + k);
        g.fillStyle = on ? colors.accent : STEM_COLORS[rollStem] || colors.accent; g.globalAlpha = on ? 1 : 0.75;
        g.fillRect(a, Y(p) + 1, Math.max(2, b - a), Math.max(2, rowH - 2)); g.globalAlpha = 1;
        if (rowH >= 6.5 && b - a > 20) {
          g.fillStyle = on ? colors['on-accent'] : '#111'; g.font = `${clamp(rowH + 1, 8, 11)}px system-ui, sans-serif`;
          g.fillText(noteName(p + k), a + 3, Y(p) + rowH / 2);
        }
      }
      g.fillStyle = colors.panel2; g.fillRect(0, top, KEYS, H - top);
      g.fillStyle = colors.muted; g.font = `${clamp(rowH - 1, 8, 11)}px system-ui, sans-serif`;
      for (let p = lo; p <= hi; p++) if (rowH >= 9 || [0, 4, 7].includes(((p + k) % 12 + 12) % 12)) g.fillText(noteName(p + k), 3, Y(p) + rowH / 2);
      if (sounding.length) {
        const txt = T.now_notes + [...new Set(sounding)].sort((p, q) => p - q).map(noteName).join(' ');
        g.font = '600 12px system-ui, sans-serif'; const w = g.measureText(txt).width;
        g.fillStyle = colors.panel; g.fillRect(W - w - 14, top + 2, w + 10, 18); g.fillStyle = colors.text; g.fillText(txt, W - w - 9, top + 11);
      }
    }
    const px = Math.round(X(x)) + 0.5;
    g.strokeStyle = colors.accent; g.lineWidth = 2; g.beginPath(); g.moveTo(px, 0); g.lineTo(px, H); g.stroke();
  }
  cv.onclick = e => { const r = cv.getBoundingClientRect(), span = +$('span').value, x = tr.position(heard()); tr.seek(x - span * 0.25 + (e.clientX - r.left) / r.width * span); };

  // ---- what is not at its default: the chips in the transport bar and the "settings are back" notice
  const fmtSemis = v => { v = Math.round(v * 100) / 100; return (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v); };
  function changedSettings(all) {
    const what = [], names = list => list.map(s => T.stems[s.name] || s.name).join(T.list_sep);
    if (Math.abs(tr.speed - 1) > 0.004) what.push(fill(T.r_speed, {pct: pct(tr.speed)}));
    if (Math.abs(tr.semis) > 0.004) what.push(fill(T.r_pitch, {k: fmtSemis(tr.semis)}));
    const solo = stems.filter(s => s.solo), mute = stems.filter(s => s.mute);
    if (solo.length) what.push(fill(T.r_solo, {s: names(solo)}));
    if (!all) {
      if (regionNow >= 0 && tr.regions[regionNow]) what.push(fill(T.r_here, {pct: pct(tr.regions[regionNow].speed)}));
      return what.join(' · ');
    }
    if (mute.length) what.push(fill(T.r_mute, {s: names(mute)}));
    if (stems.some(s => s.db !== 0) || masterDb !== 0) what.push(T.r_gain);
    if (tr.loop.on && hasLoop()) {
      const bars = barSpan(tr.loop.a, tr.loop.b).replace(/^ · /, '');
      what.push(T.r_loop + ' ' + fmtTime(tr.loop.a) + '–' + fmtTime(tr.loop.b) + (bars ? fill(T.r_loop_bars, {bars}) : ''));
    }
    if (tr.regions.length) what.push(fill(T.r_regions, {n: tr.regions.length}));
    if (tr.metroOn) what.push(T.metro);
    if (tr.countInOn) what.push(T.countin);
    return what.join(' · ');
  }
  function resetSong() {
    if (!confirm(T.reset_song_confirm)) return;
    stopTrainer();
    tr.loop = {on: false, a: 0, b: 0}; loopBox.checked = false; tr.regions = [];
    stems.forEach(s => Object.assign(s, {db: 0, mute: false, solo: false})); masterDb = 0; applyGains();
    pitchN.value = 0; centsN.value = 0; setPitch(); setSpeed(1);
    tr.metroOn = $('metro').checked = false; tr.cancelClicks(ctx.currentTime);
    tr.countInOn = $('countin').checked = false;
    afterRegionEdit(); updateTrainerText();
    $('restored').hidden = true;
  }
  $('reset-song').onclick = $('reset-song-2').onclick = resetSong;
  const restored = changedSettings(true);
  $('restored').hidden = !restored;
  $('restored-text').textContent = fill(T.restored, {what: restored});
  const K = T.key_list;
  $('shortcuts').textContent = T.keys + [K.space, K.l, K.ab, K.esc, K.speed, downbeats.length > 1 ? K.bars : K.secs,
    hasGrid ? K.m : '', K.home].filter(Boolean).join(' · ');

  // ---- the frame loop: playhead text, chord, BPM now, loop and speed-region readouts, limiter, detail view
  const setText = (id, s) => { const el = $(id); if (el.textContent !== s) el.textContent = s; };
  let lastX = -1, lastW = 0, lastStripW = 0, limUntil = 0;
  const k0 = downbeats.length > 1 ? lowerBound(beats, downbeats[0] - 1e-3) : 0, bpb = rhythm.beatsPerBar || 4;
  function frame() {
    const now = heard(), x = tr.position(now), ci = tr.countInfo;
    setText('f-pos', fmtTime(x));
    if (ci && tr.playing && now >= ci.from - 0.02 && now < ci.from + ci.n * ci.step) {
      setText('f-bar', fill(T.countin_now, {n: clamp(Math.floor((now - ci.from) / ci.step) + 1, 1, ci.n)}));
    } else if (downbeats.length > 1 || hasGrid) {
      // before the first beat: nothing; pickup beats count as the last beats of bar 0 (a 2-beat pickup in 4/4 is 3, 4)
      const bar = barAt(x), bi = lowerBound(beats, x + 1e-6);
      setText('f-bar', !bi ? '' : downbeats.length < 2 ? fill(T.beat_only, {beat: bi})
        : bar > 0 ? fill(T.bar_beat, {bar, beat: Math.max(1, bi - lowerBound(beats, downbeats[bar - 1] - 1e-3))})
        : fill(T.pickup_beat, {beat: Math.max(1, bpb - (k0 - bi))}));
    }
    const rate = tr.rateAt(x);
    if (tempo?.bpm) {
      setText('f-bpm', String(Math.round(tempo.bpm * 10) / 10));
      setText('f-bpm-note', [Math.abs(rate - 1) > 0.004 ? fill(T.bpm_now, {bpm: Math.round(tempo.bpm * rate), pct: pct(rate)}) : '', $('f-bpm').dataset.base].filter(Boolean).join(' · '));
    }
    if (chords.length) {
      const ci = lowerBound(chordStarts, x + 1e-6) - 1, c = chords[ci], k = Math.round(tr.semis);
      const cur = c && x < c.end ? c.label : 'N';
      const nxt = chords.slice(Math.max(0, ci + 1)).find(d => d.label !== 'N' && d.label !== cur);
      setText('f-chord', cur === 'N' ? '–' : transposeChord(cur, k, heardKey()));
      setText('f-next', (nxt ? fill(T.chord_next, {c: transposeChord(nxt.label, k, heardKey())}) : '') + (k ? fill(T.transposed, {k: (k > 0 ? '+' : '−') + Math.abs(k)}) : ''));
    }
    for (const [id, text] of [['loop-info', loopInfo(x)], ['chips', changedSettings(false)]]) {
      const el = $(id); if (el.textContent !== text) { el.textContent = text; el.title = text; }   // cut off: hover
    }
    // inside a speed region the overall speed does not apply: say so, and light up the region's row
    const L = tr.loop, trainerRules = tr.trainer.on && L.on && x >= L.a && x < L.b;
    const ri = trainerRules ? -1 : tr.regions.findIndex(r => x >= r.start && x < r.end);
    if (ri !== regionNow) {
      regionNow = ri;
      const r = tr.regions[ri], note = $('speed-note');
      note.textContent = r ? fill(T.speed_in_region, {a: fmtTime(r.start), b: fmtTime(r.end), pct: pct(r.speed)}) : '';   // its space is kept
      [...$('regionlist').children].forEach((row, i) => row.classList.toggle('now', i === ri));
    }
    const red = engine.limiter.reduction, lim = $('limiter');
    if (tr.playing && red < -0.5) { limUntil = now + 1; setText('limiter', fill(T.limiting, {db: Math.abs(red).toFixed(1)})); }
    const limiting = tr.playing && now < limUntil;           // held for 1 s, so the chip does not blink
    lim.classList.toggle('active', limiting); lim.hidden = !limiting;
    // the "settings are back" notice goes once anything differs from what came back
    if (!$('restored').hidden && changedSettings(true) !== restored) $('restored').hidden = true;
    for (const s of strips) s.line.style.left = (x / loaded.duration * 100) + '%';
    if (box.clientWidth !== lastStripW) { lastStripW = box.clientWidth; drawStrips(); }
    if (cv.clientWidth !== lastW) { lastW = cv.clientWidth; dirty = true; }
    if (dirty || x !== lastX) { drawDetail(x); dirty = false; lastX = x; }
    requestAnimationFrame(frame);
  }

  // ---- keyboard shortcuts. A control used with the mouse gives the focus back at once, so Space always means
  // play/pause; one reached with Tab keeps it (a slider keeps its arrow keys) until Esc.
  let pressed = null, lastInput = 'key';
  document.addEventListener('pointerdown', e => { pressed = e.target; lastInput = 'pointer'; }, true);
  document.addEventListener('pointerup', () => {
    const el = pressed; pressed = null;
    if (el?.matches?.('input[type=range]')) setTimeout(() => el.blur());
  }, true);
  addEventListener('blur', () => { pressed = null; });
  document.addEventListener('keydown', () => { lastInput = 'key'; }, true);
  document.addEventListener('click', e => { if (e.detail > 0) (e.composedPath?.()[0] || e.target).closest?.('button, a')?.blur(); });
  document.addEventListener('change', e => {
    if (lastInput === 'pointer' && e.target.matches('input[type=checkbox], input[type=radio], select')) e.target.blur();
  });
  document.addEventListener('focusin', e => { if (e.target.matches?.('input[type=number]')) e.target.dataset.was = e.target.value; });
  document.addEventListener('keydown', e => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.querySelector('dialog[open]')) return;          // the Guitar Pro dialog has the keys
    if (pressed && $('wave').contains(pressed)) return;          // the mouse is still down on the waveform
    const el = e.composedPath?.()[0] || e.target;                // also inside the waveform's shadow root
    if (el.matches?.('select, textarea, input:not([type=range]):not([type=checkbox]):not([type=radio])')) {
      if (e.key === 'Escape' && el.dataset.was != null) el.value = el.dataset.was;    // Esc cancels the typing
      if (e.key === 'Enter' || e.key === 'Escape') { el.blur(); return; }
      if (!(el.matches('input[type=number]') && e.key === ' ')) return;
      el.blur();          // a number box takes no spaces: keep the value, then Space plays or pauses below
    }
    if (e.key === 'Escape' && el.matches?.('input, button, a')) { el.blur(); return; }
    if (el.matches?.('input[type=range]') && /^(Arrow|Page|Home|End)/.test(e.key)) return;   // the slider's own keys
    if (el.matches?.('input[type=radio]') && (e.key === ' ' || e.key.startsWith('Arrow'))) return;
    if (el.matches?.('input[type=checkbox]') && e.key === ' ') return;
    if (el.matches?.('button') && (e.key === ' ' || e.key === 'Enter') || el.matches?.('a') && e.key === 'Enter') return;
    const x = tr.position(heard());
    const prevBar = () => { const k = lowerBound(downbeats, x - 0.3) - 1; return k >= 0 ? downbeats[k] : 0; };
    const nextBar = () => { const k = lowerBound(downbeats, x + 0.05); return k < downbeats.length ? downbeats[k] : x + 2; };
    const map = {
      ' ': () => tr.playing ? tr.pause() : tr.play(),
      l: () => setLoop(!tr.loop.on), L: () => setLoop(!tr.loop.on),
      a: () => setLoopEdge('a'), A: () => setLoopEdge('a'), b: () => setLoopEdge('b'), B: () => setLoopEdge('b'),
      '[': () => setSpeed(tr.speed - 0.05), ']': () => setSpeed(tr.speed + 0.05),
      ArrowLeft: () => tr.seek(downbeats.length > 1 ? prevBar() : x - 2),
      ArrowRight: () => tr.seek(downbeats.length > 1 ? nextBar() : x + 2),
      m: () => { if (hasGrid) { $('metro').checked = !$('metro').checked; $('metro').onchange(); } },
      M: () => map.m(),
      Home: () => tr.seek(tr.loop.on ? tr.loop.a : 0),
      Escape: clearLoop, Delete: clearLoop, Backspace: clearLoop,
    };
    if (map[e.key]) { e.preventDefault(); map[e.key](); }
  });

  setSpeed(tr.speed, true);
  drawRegions(); renderRegionList(); showPlay();
  ws.on('ready', () => media.emit('timeupdate'));   // show the cursor at a remembered loop's A
  loadNotes();
  requestAnimationFrame(frame);
  return {ws, regs, stems, applyGains, setSpeed, setLoop, clearLoop, setLoopEdge, setPitch, setSpeedRegion, afterRegionEdit,
    drawRegions, resetSong};
}

main().catch(err => { setStatus(fill(T.load_error, {msg: err?.message || String(err)}), true); console.warn(err); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
