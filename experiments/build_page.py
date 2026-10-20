"""Build the listening pages: compare.html (Chinese, the default) and compare.en.html (English).

Reads results/multi_*_test.json and results/*_test.json (scores), results/method_info.json (optional
Chinese names and notes), <root>/data/song_excerpts/*.{wav,flac} and <root>/outputs/<method>/<song>/<stem>.{wav,flac}
(audio), and draws a spectrogram per stem once for both pages. <root> is the project folder by default;
--root points it at a copied bundle instead (e.g. results made on another machine).
Serve the folder (python -m http.server) and open /compare.html.

    python experiments/build_page.py
    python experiments/build_page.py --root outputs/_share/OpenMusic_compare
"""
import argparse
import html
import json
from pathlib import Path
from urllib.parse import quote

import librosa
import librosa.display
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from common import RESULTS, ROOT, load  # noqa: E402

STEM_ORDER = ("vocals", "drums", "bass", "guitar", "piano", "other")
AUDIO_EXT = (".flac", ".wav")  # newer runs write FLAC; prefer it over leftover WAVs
PAGES = {"zh": "compare.html", "en": "compare.en.html"}

TEXT = {
    "zh": {
        "stems": {"vocals": "人声", "drums": "鼓", "bass": "贝斯", "guitar": "吉他", "piano": "钢琴/键盘", "other": "其他"},
        "other_with_bass": "（含贝斯）",
        "switch": "<b>中文</b> | <a href='compare.en.html'>English</a>",
        "title": "分轨方法对比",
        "intro": "分数是 50 段 7 秒标准测试片段（MUSDB18，带真正的分轨答案）上的中位数 SDR，单位 dB，越高越好。"
                 "大约 0 dB 算“有一点效果”，5 dB 以上听感已经明显分开，9 dB 左右是 Demucs 这个级别。",
        "h_multi": "6 轨 AI 模型",
        "foot": "* MUSDB18 没有单独的吉他、钢琴答案，所以“其他*”= 吉他 + 钢琴 + 其他 合在一起打分。吉他分得好不好只能靠耳朵听。",
        "h_three": "3 轨：传统信号处理 vs AI",
        "method": "方法", "notes": "说明",
        "heads_multi": ("人声", "鼓", "贝斯", "其他*", "平均"),
        "heads_three": ("人声", "鼓", "其他（含贝斯）", "平均"),
        "excerpt": "从第 {start} 秒开始的 60 秒片段（整首 {dur} 秒）。勾选框决定“同步播放”时哪些轨道出声，可以用来试“去掉吉他”“只听贝斯+鼓”。",
        "original": "原曲片段",
        "g_multi": "6 轨 AI 模型（含贝斯、吉他）",
        "g_three": "3 轨：传统信号处理 vs AI",
        "play": "▶ 勾选的轨道同步播放", "pause": "❚❚ 暂停",
    },
    "en": {
        "stems": {"vocals": "Vocals", "drums": "Drums", "bass": "Bass", "guitar": "Guitar", "piano": "Piano/keys", "other": "Other"},
        "other_with_bass": " (incl. bass)",
        "switch": "<a href='compare.html'>中文</a> | <b>English</b>",
        "title": "Stem separation comparison",
        "intro": "Scores are median SDR in dB over 50 seven-second test clips (MUSDB18, which comes with the true stems); "
                 "higher is better. About 0 dB means a slight effect, above 5 dB the stems sound clearly separated, "
                 "and around 9 dB is Demucs level.",
        "h_multi": "6-stem AI models",
        "foot": "* MUSDB18 has no separate guitar or piano stems, so “Other*” scores guitar + piano + other together. "
                "Guitar quality can only be judged by ear.",
        "h_three": "3 stems: classical signal processing vs AI",
        "method": "Method", "notes": "Notes",
        "heads_multi": ("Vocals", "Drums", "Bass", "Other*", "Mean"),
        "heads_three": ("Vocals", "Drums", "Other (incl. bass)", "Mean"),
        "excerpt": "60 s excerpt starting at {start} s (full song {dur} s). The checkboxes choose which stems sound "
                   "during synced playback, e.g. to try “no guitar” or “just bass + drums”.",
        "original": "Original excerpt",
        "g_multi": "6-stem AI models (with bass and guitar)",
        "g_three": "3 stems: classical signal processing vs AI",
        "play": "▶ Play checked stems in sync", "pause": "❚❚ Pause",
    },
}

# (name, note) per method. Chinese names of the classical methods come from results/method_info.json.
LABELS = {
    "zh": {
        "mixture": ("什么都不做（底线）", "每一轨都直接用原曲。任何方法都应该比它高。"),
        "oracle_irm": ("理论上限（作弊）", "偷看了标准答案的频谱遮罩：任何“按频率格子分配”的方法最多能做到这么好。"),
        "demucs_ai": ("Demucs 4 轨（AI）", "Meta 开源的神经网络模型，用几千首分轨训练过。贝斯并入“其他”。"),
        "demucs4": ("Demucs 4 轨（AI）", "人声 / 鼓 / 贝斯 / 其他。"),
        "demucs6": ("Demucs 6 轨（AI）", "在 4 轨基础上多分出吉他和钢琴。作者自评：吉他还行，钢琴串音和杂音很多。"),
        "roformer_sw": ("BS-RoFormer-SW 6 轨（AI）", "社区流传的 6 轨模型（约 700 MB，训练者和协议不明），据公开评测吉他、钢琴明显好于 Demucs 6 轨。"),
    },
    "en": {
        "mixture": ("Do nothing (floor)", "Every stem is just the original mix. Any method should beat this."),
        "oracle_irm": ("Theoretical ceiling (cheats)", "A spectrogram mask that peeks at the true stems: the best any "
                       "method that assigns each time-frequency cell to a stem could do."),
        "demucs_ai": ("Demucs 4-stem (AI)", "Meta's open-source neural network, trained on thousands of multitrack "
                      "songs. Bass is folded into “other”."),
        "demucs4": ("Demucs 4-stem (AI)", "Vocals / drums / bass / other."),
        "demucs6": ("Demucs 6-stem (AI)", "Adds guitar and piano to the 4 stems. Its authors say guitar is okay and "
                    "piano has a lot of bleed and artifacts."),
        "roformer_sw": ("BS-RoFormer-SW 6-stem (AI)", "Community-shared 6-stem checkpoint (about 700 MB; trainer and licence unknown); public "
                        "benchmarks rate its guitar and piano clearly above Demucs 6-stem."),
        "stereo": ("Stereo position", "Splits by where sounds sit between left and right (centred, wide, side), "
                   "with per-band gains learned on the train clips."),
        "freq_bands": ("Frequency bands only (fixed EQ)", "One fixed EQ curve per stem, learned on the train clips "
                       "and the same at every moment."),
        "hpss": ("Two-stage harmonic/percussive (HPSS)", "Drum hits are vertical lines in a spectrogram and held "
                 "notes horizontal ones; a second pass with a longer window separates the voice."),
        "repet": ("Repetition (REPET)", "The accompaniment repeats bar after bar while the voice varies; what "
                  "doesn't repeat is taken as vocals."),
        "pitch": ("Melody-guided (pitch tracking)", "Follows the sung melody and keeps its harmonics as vocals."),
        "nmf": ("Sound building blocks (supervised NMF)", "Rebuilds the spectrogram from per-stem sound templates "
                "learned on the train clips."),
        "combo": ("Multi-expert fusion", "Five classical methods vote in each frequency band, with trust weights "
                  "learned on the train clips; the best classical result."),
    },
}


def spectrogram_png(y, path, ref):
    S = librosa.amplitude_to_db(np.abs(librosa.stft(y.mean(axis=0), n_fft=2048, hop_length=512)), ref=ref)
    fig, ax = plt.subplots(figsize=(4.2, 1.5), dpi=110)
    librosa.display.specshow(S, sr=44100, hop_length=512, y_axis="log", ax=ax, vmin=-80, vmax=0, cmap="magma")
    ax.set_ylabel("")
    ax.set_yticks([100, 1000, 10000], ["100Hz", "1k", "10k"], fontsize=6)
    fig.subplots_adjust(0.11, 0.03, 0.995, 0.97)
    fig.savefig(path)
    plt.close(fig)


def rel(path, root):
    return quote(path.relative_to(root).as_posix())


def find_audio(folder, stem):
    """The first <folder>/<stem>.wav or .flac that exists, else None."""
    return next((folder / f"{stem}{ext}" for ext in AUDIO_EXT if (folder / f"{stem}{ext}").exists()), None)


def read_scores(pattern, prefix=""):
    scores = {}
    for f in sorted(RESULTS.glob(pattern)):
        d = json.loads(f.read_text(encoding="utf-8"))
        if prefix or not f.name.startswith("multi_"):
            scores[d["method"]] = d["median_sdr"]
    return scores


def collect_songs(three, root):
    """Draw every spectrogram once; return per-song data that both languages render from."""
    excerpt_dir, outputs = root / "data" / "song_excerpts", root / "outputs"
    meta_path = excerpt_dir / "excerpts.json"
    excerpts = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    songs = []
    for song in sorted({p.stem for p in excerpt_dir.iterdir() if p.suffix.lower() in AUDIO_EXT}):
        mix_path = find_audio(excerpt_dir, song)
        mix = load(mix_path)
        ref = np.abs(librosa.stft(mix.mean(axis=0), n_fft=2048, hop_length=512)).max()
        img_dir = outputs / "_spectrograms" / song
        img_dir.mkdir(parents=True, exist_ok=True)
        spectrogram_png(mix, img_dir / "mixture.png", ref)

        def method_stems(m):
            stems = []
            for s in STEM_ORDER:
                audio = find_audio(outputs / m / song, s)
                if audio:
                    png = img_dir / f"{m}_{s}.png"
                    spectrogram_png(load(audio), png, ref)
                    stems.append((s, audio, png))
            return stems

        multi_methods = [m for m in ("roformer_sw", "demucs6") if (outputs / m / song).is_dir()]
        three_methods = sorted((m for m in three if (outputs / m / song).is_dir()), key=lambda m: -three[m]["mean"])
        songs.append({"name": song, "meta": excerpts.get(song, {}), "mix": mix_path, "mix_png": img_dir / "mixture.png",
                      "multi": [(m, method_stems(m)) for m in multi_methods],
                      "three": [(m, method_stems(m)) for m in three_methods]})
    return songs


def render(lang, multi, three, songs, info, root):
    t = TEXT[lang]

    def label(m):
        if m in LABELS[lang]:
            return LABELS[lang][m]
        return info.get(m, {}).get("title_zh", m), info.get(m, {}).get("idea_zh", "")

    def table(scores, cols, heads):
        rows = []
        for m in sorted(scores, key=lambda m: -scores[m]["mean"]):
            name, note = label(m)
            rows.append(f"<tr><td><b>{html.escape(name)}</b><br><code>{m}</code></td>"
                        + "".join(f"<td class=num>{scores[m][c]:.1f}</td>" for c in cols)
                        + f"<td class=note>{html.escape(note)}</td></tr>")
        return (f"<div class=tablewrap><table><tr><th>{t['method']}</th>" + "".join(f"<th>{h}</th>" for h in heads)
                + f"<th>{t['notes']}</th></tr>" + "".join(rows) + "</table></div>")

    all_scores = {**{m: s["mean"] for m, s in three.items()}, **{m: s["mean"] for m, s in multi.items()}}

    def card(m, stems):
        has_bass = any(s == "bass" for s, _, _ in stems)
        cells = []
        for s, wav, png in stems:
            name = t["stems"][s] + (t["other_with_bass"] if s == "other" and not has_bass else "")
            cells.append(f"<div class=stem><label class=stemname><input type=checkbox checked> {name}</label>"
                         f"<img src='{rel(png, root)}' alt='{s} spectrogram'>"
                         f"<audio controls preload=none src='{rel(wav, root)}'></audio></div>")
        score = f"<span class=score>{all_scores[m]:.1f} dB</span>" if m in all_scores else ""
        return (f"<section class=card><h3>{html.escape(label(m)[0])} {score}"
                f"<button class=sync>{t['play']}</button></h3><div class=stems>{''.join(cells)}</div></section>")

    blocks = []
    for song in songs:
        meta = song["meta"]
        excerpt = t["excerpt"].format(start=meta.get("start_s", 0), dur=f"{meta.get('duration_s', 0):.0f}")
        blocks.append(
            f"<h2>{html.escape(song['name'])}</h2><p class=muted>{excerpt}</p>"
            f"<section class=card><h3>{t['original']}</h3><div class=stems><div class=stem>"
            f"<img src='{rel(song['mix_png'], root)}' alt='mixture spectrogram'>"
            f"<audio controls preload=none src='{rel(song['mix'], root)}'></audio></div></div></section>"
            + f"<h3 class=group>{t['g_multi']}</h3>" + "".join(card(m, st) for m, st in song["multi"])
            + f"<h3 class=group>{t['g_three']}</h3>" + "".join(card(m, st) for m, st in song["three"]))

    fill = {
        "LANG": lang, "TITLE": t["title"], "SWITCH": t["switch"], "INTRO": t["intro"], "H_MULTI": t["h_multi"],
        "FOOT": t["foot"], "H_THREE": t["h_three"], "PLAY": t["play"], "PAUSE": t["pause"],
        "MULTI": table(multi, ("vocals", "drums", "bass", "other4", "mean"), t["heads_multi"]) if multi else "",
        "THREE": table(three, ("vocals", "drums", "other", "mean"), t["heads_three"]),
        "SONGS": "".join(blocks),
    }
    page = TEMPLATE
    for key, value in fill.items():
        page = page.replace("{{" + key + "}}", value)
    return page


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="folder holding data/song_excerpts and outputs (default: the project)")
    root = parser.parse_args().root.resolve()

    info_path = RESULTS / "method_info.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    multi = read_scores("multi_*_test.json", prefix="multi_")
    three = read_scores("*_test.json")
    songs = collect_songs(three, root)
    for lang, name in PAGES.items():
        (root / name).write_text(render(lang, multi, three, songs, info, root), encoding="utf-8")
        print(f"wrote {root / name}")


TEMPLATE = """<!doctype html>
<html lang={{LANG}}><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e4e2dd;--accent:#b4451f}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#17171a;--fg:#ececef;--muted:#9a9aa2;--card:#202024;--line:#33333a;--accent:#f08a5d}}
:root[data-theme=dark]{--bg:#17171a;--fg:#ececef;--muted:#9a9aa2;--card:#202024;--line:#33333a;--accent:#f08a5d}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{margin:40px 0 4px;font-size:19px}h3{margin:0 0 10px;font-size:15px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
h3.group{margin:28px 0 4px;color:var(--muted);font-size:14px}
.muted{color:var(--muted)}table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:8px 0 4px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:13px;color:var(--muted);font-weight:600}
td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}td.note{color:var(--muted);font-size:13px}
code{font-size:12px;color:var(--muted)}.tablewrap{overflow-x:auto}.foot{font-size:13px;color:var(--muted);margin:0 0 20px}
.switch{font-size:13px;color:var(--muted);margin:0 0 12px}.switch a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.stems{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.stem img{width:100%;border-radius:6px;display:block;margin:4px 0}.stem audio{width:100%}.stemname{font-weight:600;font-size:13px;cursor:pointer}
.score{color:var(--accent);font-variant-numeric:tabular-nums}
button.sync{margin-left:auto;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:6px;padding:3px 10px;cursor:pointer;font:inherit;font-size:13px}
</style></head><body><main>
<p class=switch>{{SWITCH}}</p>
<h1>{{TITLE}}</h1>
<p class=muted>{{INTRO}}</p>
<h2>{{H_MULTI}}</h2>
{{MULTI}}
<p class=foot>{{FOOT}}</p>
<h2>{{H_THREE}}</h2>
{{THREE}}
{{SONGS}}
</main>
<script>
const PLAY='{{PLAY}}', PAUSE='{{PAUSE}}';
const reset=()=>{document.querySelectorAll('audio').forEach(a=>a.pause());document.querySelectorAll('button.sync').forEach(b=>b.textContent=PLAY)};
document.querySelectorAll('button.sync').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const stems=[...btn.closest('.card').querySelectorAll('.stem')];
    const els=stems.map(s=>s.querySelector('audio'));
    const playing=!els[0].paused; reset(); if(playing) return;
    const t=els[0].currentTime;
    stems.forEach(s=>{const a=s.querySelector('audio');const box=s.querySelector('input');a.muted=box?!box.checked:false;a.currentTime=t;a.play()});
    btn.textContent=PAUSE;
  });
});
document.querySelectorAll('.stem input').forEach(box=>box.addEventListener('change',()=>{box.closest('.stem').querySelector('audio').muted=!box.checked}));
</script></body></html>"""

if __name__ == "__main__":
    main()
