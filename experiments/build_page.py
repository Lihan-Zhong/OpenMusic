"""Build compare.html: a local page to listen to every method's stems for the song excerpt, side by side.

Reads results/multi_*_test.json and results/*_test.json (scores), results/method_info.json (optional
Chinese names and notes), outputs/<method>/<song>/<stem>.wav (audio), and draws a spectrogram per stem.
Serve the project folder (python -m http.server) and open /compare.html.
"""
import html
import json
from urllib.parse import quote

import librosa
import librosa.display
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from common import EXCERPTS, OUTPUTS, RESULTS, ROOT, load  # noqa: E402

STEM_ORDER = ("vocals", "drums", "bass", "guitar", "piano", "other")
STEM_ZH = {"vocals": "人声", "drums": "鼓", "bass": "贝斯", "guitar": "吉他", "piano": "钢琴/键盘", "other": "其他"}
FIXED = {
    "mixture": ("什么都不做（底线）", "每一轨都直接用原曲。任何方法都应该比它高。"),
    "oracle_irm": ("理论上限（作弊）", "偷看了标准答案的频谱遮罩：任何“按频率格子分配”的方法最多能做到这么好。"),
    "demucs_ai": ("Demucs 4 轨（AI）", "Meta 开源的神经网络模型，用几千首分轨训练过。贝斯并入“其他”。"),
    "demucs4": ("Demucs 4 轨（AI）", "人声 / 鼓 / 贝斯 / 其他。"),
    "demucs6": ("Demucs 6 轨（AI）", "在 4 轨基础上多分出吉他和钢琴。作者自评：吉他还行，钢琴串音和杂音很多。"),
    "roformer_sw": ("BS-RoFormer-SW 6 轨（AI）", "社区训练的 6 轨模型（约 700 MB），据公开评测吉他、钢琴明显好于 Demucs 6 轨。"),
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


def rel(path):
    return quote(path.relative_to(ROOT).as_posix())


def read_scores(pattern, prefix=""):
    scores = {}
    for f in sorted(RESULTS.glob(pattern)):
        d = json.loads(f.read_text(encoding="utf-8"))
        if prefix or not f.name.startswith("multi_"):
            scores[d["method"]] = d["median_sdr"]
    return scores


def main():
    info = json.loads((RESULTS / "method_info.json").read_text(encoding="utf-8")) if (RESULTS / "method_info.json").exists() else {}

    def label(m):
        return FIXED.get(m, (info.get(m, {}).get("title_zh", m), info.get(m, {}).get("idea_zh", "")))

    def table(scores, cols, heads):
        rows = []
        for m in sorted(scores, key=lambda m: -scores[m]["mean"]):
            zh, note = label(m)
            rows.append(f"<tr><td><b>{html.escape(zh)}</b><br><code>{m}</code></td>"
                        + "".join(f"<td class=num>{scores[m][c]:.1f}</td>" for c in cols)
                        + f"<td class=note>{html.escape(note)}</td></tr>")
        return ("<div class=tablewrap><table><tr><th>方法</th>" + "".join(f"<th>{h}</th>" for h in heads)
                + "<th>说明</th></tr>" + "".join(rows) + "</table></div>")

    multi = read_scores("multi_*_test.json", prefix="multi_")
    three = read_scores("*_test.json")
    all_scores = {**{m: s["mean"] for m, s in three.items()}, **{m: s["mean"] for m, s in multi.items()}}

    excerpts = json.loads((EXCERPTS / "excerpts.json").read_text(encoding="utf-8"))
    blocks = []
    for song in sorted(p.stem for p in EXCERPTS.glob("*.wav")):
        mix_path = EXCERPTS / f"{song}.wav"
        mix = load(mix_path)
        ref = np.abs(librosa.stft(mix.mean(axis=0), n_fft=2048, hop_length=512)).max()
        img_dir = OUTPUTS / "_spectrograms" / song
        img_dir.mkdir(parents=True, exist_ok=True)
        spectrogram_png(mix, img_dir / "mixture.png", ref)

        def card(m, title_extra=""):
            folder = OUTPUTS / m / song
            stems = [s for s in STEM_ORDER if (folder / f"{s}.wav").exists()]
            has_bass = "bass" in stems
            cells = []
            for s in stems:
                png = img_dir / f"{m}_{s}.png"
                spectrogram_png(load(folder / f"{s}.wav"), png, ref)
                name = STEM_ZH[s] + ("（含贝斯）" if s == "other" and not has_bass else "")
                cells.append(f"<div class=stem><label class=stemname><input type=checkbox checked> {name}</label>"
                             f"<img src='{rel(png)}' alt='{s} spectrogram'>"
                             f"<audio controls preload=none src='{rel(folder / f'{s}.wav')}'></audio></div>")
            zh, _ = label(m)
            score = f"<span class=score>{all_scores[m]:.1f} dB</span>" if m in all_scores else ""
            return (f"<section class=card><h3>{html.escape(zh)} {score}{title_extra}"
                    f"<button class=sync>▶ 勾选的轨道同步播放</button></h3><div class=stems>{''.join(cells)}</div></section>")

        meta = excerpts.get(song, {})
        multi_methods = [m for m in ("roformer_sw", "demucs6") if (OUTPUTS / m / song).is_dir()]
        three_methods = sorted((m for m in three if (OUTPUTS / m / song).is_dir()), key=lambda m: -three[m]["mean"])
        blocks.append(
            f"<h2>{html.escape(song)}</h2><p class=muted>从第 {meta.get('start_s', 0)} 秒开始的 60 秒片段"
            f"（整首 {meta.get('duration_s', 0):.0f} 秒）。勾选框决定“同步播放”时哪些轨道出声，可以用来试“去掉吉他”“只听贝斯+鼓”。</p>"
            f"<section class=card><h3>原曲片段</h3><div class=stems><div class=stem>"
            f"<img src='{rel(img_dir / 'mixture.png')}' alt='mixture spectrogram'>"
            f"<audio controls preload=none src='{rel(mix_path)}'></audio></div></div></section>"
            + "<h3 class=group>6 轨 AI 模型（含贝斯、吉他）</h3>" + "".join(card(m) for m in multi_methods)
            + "<h3 class=group>3 轨：传统信号处理 vs AI</h3>" + "".join(card(m) for m in three_methods))

    page = (TEMPLATE
            .replace("{{MULTI}}", table(multi, ("vocals", "drums", "bass", "other4", "mean"),
                                        ("人声", "鼓", "贝斯", "其他*", "平均")) if multi else "")
            .replace("{{THREE}}", table(three, ("vocals", "drums", "other", "mean"), ("人声", "鼓", "其他（含贝斯）", "平均")))
            .replace("{{SONGS}}", "".join(blocks)))
    (ROOT / "compare.html").write_text(page, encoding="utf-8")
    print(f"wrote {ROOT / 'compare.html'}")


TEMPLATE = """<!doctype html>
<html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>分轨方法对比</title>
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
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.stems{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.stem img{width:100%;border-radius:6px;display:block;margin:4px 0}.stem audio{width:100%}.stemname{font-weight:600;font-size:13px;cursor:pointer}
.score{color:var(--accent);font-variant-numeric:tabular-nums}
button.sync{margin-left:auto;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:6px;padding:3px 10px;cursor:pointer;font:inherit;font-size:13px}
</style></head><body><main>
<h1>分轨方法对比</h1>
<p class=muted>分数是 50 段 7 秒标准测试片段（MUSDB18，带真正的分轨答案）上的中位数 SDR，单位 dB，越高越好。大约 0 dB 算“有一点效果”，5 dB 以上听感已经明显分开，9 dB 左右是 Demucs 这个级别。</p>
<h2>6 轨 AI 模型</h2>
{{MULTI}}
<p class=foot>* MUSDB18 没有单独的吉他、钢琴答案，所以“其他*”= 吉他 + 钢琴 + 其他 合在一起打分。吉他分得好不好只能靠耳朵听。</p>
<h2>3 轨：传统信号处理 vs AI</h2>
{{THREE}}
{{SONGS}}
</main>
<script>
const reset=()=>{document.querySelectorAll('audio').forEach(a=>a.pause());document.querySelectorAll('button.sync').forEach(b=>b.textContent='▶ 勾选的轨道同步播放')};
document.querySelectorAll('button.sync').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const stems=[...btn.closest('.card').querySelectorAll('.stem')];
    const els=stems.map(s=>s.querySelector('audio'));
    const playing=!els[0].paused; reset(); if(playing) return;
    const t=els[0].currentTime;
    stems.forEach(s=>{const a=s.querySelector('audio');const box=s.querySelector('input');a.muted=box?!box.checked:false;a.currentTime=t;a.play()});
    btn.textContent='❚❚ 暂停';
  });
});
document.querySelectorAll('.stem input').forEach(box=>box.addEventListener('change',()=>{box.closest('.stem').querySelector('audio').muted=!box.checked}));
</script></body></html>"""

if __name__ == "__main__":
    main()
