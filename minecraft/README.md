# OpenMusic → Minecraft 红石音乐（.nbs）

**中文** | [English](#english)

就像 OpenMusic 能导出 Guitar Pro 谱一样，它也能导出 **Minecraft 红石音乐文件 `.nbs`**：把扒谱得到的 MIDI，或者 Guitar Pro 谱，转成开源红石音乐编辑器 [Open Note Block Studio](https://github.com/OpenNBS/NoteBlockStudio)（下面简称 NBS）的 `.nbs` 格式。之后在 NBS 里试听、继续编辑，再由 NBS 导出成能放进 Minecraft 世界的红石结构：一条走道，两边是看得见的中继器、红石线和音符盒，按下按钮就开始演奏。

OpenMusic 负责生成 `.nbs`，导出结构交给 NBS。目前只适配了 NBS，而 NBS 只能导出 Java 版的结构，所以红石音乐暂时只支持 **Java 版**；基岩版见下面「[基岩版](#基岩版)」。

推荐的流程：OpenMusic 扒谱 → 下载 Guitar Pro 谱 → 在 Guitar Pro 8 里把贝斯和鼓修准 → 把修好的谱转成 `.nbs`。网上下载的 `.gp` / `.gpx` / `.gp5` 谱，以及任何 MIDI 文件，也能直接转。

## 1. 生成 .nbs

- **工作台**：页面最下面的「Minecraft 音符盒」卡片。把 Guitar Pro 谱或 MIDI 文件拖进去，勾选音轨（默认贝斯和鼓），点「生成 .nbs」就会下载。文件不会存进曲库。练习播放器里没有这个功能，它只管练琴。
- **命令行**（在项目文件夹里）：

  ```bash
  .venv/bin/python minecraft/export_nbs.py "songs/电动少女(Live).gp"
  ```

  - `--list`：只列出音轨，不写文件。
  - `--tracks bass,drums,3`：选音轨，可以写类型、`--list` 里的编号或名字。
  - `--instrument "Electric Bass=didgeridoo"`：给一条要转的音高轨换乐器；对不上音轨时会报错。
  - `--ticks-per-beat 6`：换节奏网格。
  - `--velocities`：保留谱子里的强弱。
  - `--out`：指定输出位置。不写就放进 `minecraft/output_nbs/`，用输入的文件名或歌曲文件夹名命名（这个文件夹不会提交到 git）。

  要转 OpenMusic 扒出来的 MIDI，就直接给歌曲文件夹，比如 `outputs/roformer_sw_full/<歌名>`：这首歌各条分轨的 MIDI 会一起转（默认贝斯和鼓，`--tracks all` 全部）。

转换时会做这些事：

- **速度**：红石最小一步是 0.1 秒，所以歌曲固定按每秒 10 刻播放，每拍分成整数个刻：每拍 4 刻 = 150 BPM（16 分音符准确），6 刻 = 100 BPM（8 分音符和三连音准确），8 刻 = 75 BPM，以此类推。程序会挑不打乱节奏、又最接近原速的那一档，并报告速度变了多少（例如《电动少女》122 BPM → 150 BPM，快 23%）。整首歌只有一个速度。扫弦的几根弦放在同一刻：Guitar Pro 把一次扫弦的各根弦依次错开一点（多数只有几十毫秒，.gp5 里最慢的扫弦有一个八分音符那么长），照原样对到红石刻上，后面几根弦可能晚一刻甚至更多。琶音（arpeggio）保留这种错开，但和别的音一样对到最近的刻上，很快的琶音听起来像和弦。
- **音高**：每种音符盒乐器只有两个八度，超出的音按八度移进来，所以 Note Block Studio 导出时不会丢音。
- **音色**：
  - 贝斯用低音提琴（音符盒下面放木头）。
  - 鼓按 Note Block Studio 3.12 的鼓映射，每件鼓的音高不同：
    - 底鼓和嗵鼓用 Bass Drum（石头）；
    - 闭镲和踩镲用 Click（玻璃）；
    - 开镲、军鼓和其他镲片都用 Snare Drum（沙子），所以开镲听起来像高一点的军鼓；
    - 叮叮镲的镲帽用 Bell（金块），牛铃用 Cow Bell（灵魂沙）。
  - 其他轨先按 MIDI 音色选乐器（吉他 → Guitar 羊毛，人声 → Flute 黏土，钢琴 → Piano）。如果另一种乐器的两个八度能多装下 15% 以上的音，就换成那个，比如多数人声的音域更适合 Piano。
  - 闷音（x）和死音拍弦（dead slap）没有音高，响成「哒」的一声：用鼓映射里军鼓边击（side stick）的声音，即 Click（玻璃）第 41 键，比闭镲低。一下闷音扫弦只响一声，放在这条音轨自己的一层（层名以 `: Dead notes` 结尾）。
- **强弱**：音符盒没有力度，导出结构时也不管力度，所以所有音都写成最大力度。这样在 Note Block Studio 里听到的，就是游戏里的效果。
- **高度**：同一刻超过 4 个音时，Note Block Studio 会往上叠一层。军鼓下面的沙子没有东西托着会掉下来，所以用沙子的层都排在最后，它们总在最底层。
- **名字**：Note Block Studio 每个字符只存一个字节，所以 `.nbs` 里的名字都是 ASCII：中文歌名写成 "OpenMusic song"，中文音轨名写成「类型 + `--list` 编号」，带重音的字母去掉重音。文件名保持原样。

## 2. 安装 Note Block Studio

- **Windows**：正式版 [3.11.0](https://github.com/OpenNBS/NoteBlockStudio/releases/latest)。
- **Mac**：正式版只有 Windows 版。用 [3.12.0-rc.1 预览版](https://github.com/OpenNBS/NoteBlockStudio/releases/tag/v3.12.0-rc.1) 里的 `4.Minecraft.Note.Block.Studio.dmg`（需要 macOS 12 以上），或者通过它 GitHub 首页上的 TestFlight 链接安装。

注意：这些都是 Note Block Studio 这个编曲软件的安装包（它的全名叫 "Minecraft Note Block Studio"，文件名前的 1–4 只是序号），不是 Minecraft 游戏本身。Note Block Studio 单独就能打开、试听和导出；Minecraft Java 版要另外用官方启动器安装（见第 4 步）。

## 3. 在 Note Block Studio 里试听、编辑、导出结构

1. 打开 `.nbs`，播放试听，可以直接改音。
2. 导出：
   - 3.12：「文件 → 导出 → 结构」（英文界面 File → Export → Schematic），格式选「.nbt (结构方块)」。
   - 3.11：Export as schematic，版本选「1.13+」。
3. 速度保持「10 红石刻 / 秒」（10 ticks / second），布局选「普通过道」（Simple walkway）。「包括矿车轨道」（Include minecart track）可选。
4. 文件名只用小写字母、数字、`_`、`-`、`.`（Minecraft 的要求），比如 `dian_dong_shao_nv.nbt`。

## 4. 放进 Minecraft（Java 版）

> 这一段还没在游戏里实测过。

1. 用官方启动器装 Java 版，它自带 Java，不用另外装。
2. 新建世界：
   - 游戏模式「创造」，难度「和平」；「允许命令」保持打开（选了创造就会自动打开）。
   - 世界类型选「超平坦」→「自定义」→ 预设「红石俱备」。
3. 把 `.nbt` 放进存档的 `generated/minecraft/structure/` 文件夹，没有就自己建。
   - 注意：26.1 以后的版本是单数 `structure`，1.21.x 是复数 `structures`。
   - Mac 的存档在 `~/Library/Application Support/minecraft/saves/<世界名>/`，游戏里「编辑世界 → 打开世界文件夹」也能找到。
4. 进入世界，站在想放的位置，输入 `/place template minecraft:dian_dong_shao_nv ~ ~ ~`（换成你的文件名）。
   - 如果是在世界开着的时候放进或换过这个 `.nbt`（包括重新导出的同名文件），先输入 `/reload`。
   - 原因：游戏会记住查过的每个结构名，找到的、没找到的都记，直到 `/reload` 或重新进入世界。
5. 走到结构起点的按钮那里按下去，沿着走道慢慢跟着走：音符盒在 48 格以内才听得见，离得太远的区块也不会运行红石。

可选：`/gamerule advance_time false`（时间停住）、`/gamerule advance_weather false`、`/gamerule spawn_mobs false`。

## 基岩版

还没做。NBS 只能导出 Java 版的结构，它的维护者也已经表示不打算支持基岩版（[#587](https://github.com/OpenNBS/NoteBlockStudio/issues/587)：基岩版对结构大小、导入时的区块加载和同时发声数（32 个）都有限制）；截至 2026 年 11 月，NBS 的各个 fork 也都不能导出基岩版。我们计划之后在 OpenMusic 里加一个小步骤：把 NBS 导出的 Java 版结构（`.nbt`）转成基岩版的 `.mcstructure`，保留每个音符盒的音高和每个中继器的延时，并在基岩版里实测。

## 限制

- 整首歌只有一个速度，而且只能是 150、100、75 BPM 这样的几档（每拍整数个红石刻）。
- 网格是每拍 4 刻时，三连音和 32 分音符会被移到最近的 16 分音符上；所以装饰音、快速颤音（trill）、回音和波音的音会挤到一起，像和弦一样同时响。
- 音符盒没有力度：强弱音一样响。
- 音符盒只能敲出固定的音高，不能弯音。滑音的目标音会再敲一下（连音滑奏也一样，虽然吉他上那个音不重新拨弦），滑的过程听不到；滑入、滑出只响按的那个品的音。推弦、颤音杆和揉弦也听不到：推弦的音只响按的那个品的音高，预推弦也是。MIDI 文件里的弯音（pitch bend）一律忽略，用弯音写的滑音只响第一个音。
- 反复记号每处最多演奏 8 遍。
- 吉他扒谱太密，默认不转；修好的吉他谱可以用 `--tracks` 加进来。

## 测试

```bash
.venv/bin/python minecraft/tests/test_export_nbs.py
```

加上 `--gp <文件>` 或 `--songs outputs/<曲库>`，还会转换真实的谱子和整个曲库来检查。

## 许可

- 代码是 AGPL-3.0，和 OpenMusic 一样。
- `.nbs` 用 [pynbs](https://github.com/OpenNBS/pynbs)（MIT）写。
- 乐器和鼓的映射取自 Open Note Block Studio（MIT，© 2024–2026 OpenNBS），它的 3.12 映射来自 u3002 的 [General MIDI ⇄ Minecraft Note Block Mapping](https://gist.github.com/u3002/cf4daa83bc82b5917fc86fb23815578a)（MIT，© u3002）。许可全文见 `export_nbs.py` 里映射表上方的注释。
- Guitar Pro 文件由项目里内置的 alphaTab（MPL-2.0）读取。
- Minecraft 是 Mojang 的商标。本工具与 Mojang、Microsoft 无关。

---

<a id="english"></a>

# OpenMusic → Minecraft redstone music (.nbs)

[中文](#openmusic--minecraft-红石音乐nbs) | **English**

Just as OpenMusic exports Guitar Pro scores, it exports **Minecraft redstone music files (`.nbs`)**: the MIDI of a transcription, or a Guitar Pro score, becomes a `.nbs` song for the open-source redstone music editor [Open Note Block Studio](https://github.com/OpenNBS/NoteBlockStudio) (NBS below). In NBS you listen, edit further, and export a redstone build to place in a Minecraft world: a walkway with visible repeaters, redstone and note blocks on both sides that plays when you press the button.

OpenMusic makes the `.nbs`; NBS exports the build. Only NBS is supported for now, and NBS exports Java Edition builds only, so redstone music works in **Java Edition** for now; for Bedrock see "[Bedrock Edition](#bedrock-edition)" below.

Suggested route: transcribe in OpenMusic → download the Guitar Pro file → fix the bass and drums in Guitar Pro 8 → convert the fixed score to `.nbs`. Scores downloaded elsewhere (`.gp` / `.gpx` / `.gp5`) and any MIDI file work too.

## 1. Make the .nbs

- **Workbench**: the "Minecraft note blocks" card at the bottom of the page. Drop a Guitar Pro score or a MIDI file on it, tick the tracks (bass and drums by default) and click "Build .nbs" to download it. Nothing is stored in the library. The practice player has no such button: it is for practising.
- **Command line** (in the project folder):

  ```bash
  .venv/bin/python minecraft/export_nbs.py "songs/电动少女(Live).gp"
  ```

  - `--list`: list the tracks and write nothing.
  - `--tracks bass,drums,3`: pick tracks by kind, `--list` number or name.
  - `--instrument "Electric Bass=didgeridoo"`: change a converted pitched track's instrument; a track it cannot match is an error.
  - `--ticks-per-beat 6`: change the rhythm grid.
  - `--velocities`: keep the score's dynamics.
  - `--out`: say where to write. Without it the file goes to `minecraft/output_nbs/`, named after the input file or song folder (the folder is not committed to git).

  For the MIDI OpenMusic transcribed, give the song folder, such as `outputs/roformer_sw_full/<song>`: the MIDI of the song's stems is converted together (bass and drums by default, `--tracks all` for all).

What the conversion does:

- **Tempo**: redstone moves in steps of 0.1 s, so the song always plays at 10 ticks per second and every beat gets a whole number of ticks: 4 ticks = 150 BPM (exact 16ths), 6 = 100 BPM (exact 8ths and triplets), 8 = 75 BPM, and so on. It picks the one nearest the original tempo that keeps the rhythm, and reports the change (电动少女: 122 BPM → 150 BPM, 23% faster). The whole song has one tempo. A strum's strings go on one tick: Guitar Pro starts each string a little after the one before (mostly a few hundredths of a second; the slowest .gp5 stroke takes an 8th note), so placed as played on the tick grid, its last strings could land a tick or more late. An arpeggio keeps its spread, rounded to the nearest tick like any note, so a fast one sounds like a chord.
- **Pitch**: each note block instrument covers two octaves; notes outside are moved by octaves into range, so Note Block Studio's export drops none.
- **Sound**:
  - The bass plays on the Double Bass (wood under the note block).
  - The drums follow Note Block Studio 3.12's drum map, each kit piece at its own pitch:
    - kick and toms on the Bass Drum (stone);
    - closed and pedal hi-hats on the Click (glass);
    - the open hi-hat, snare and other cymbals on the Snare Drum (sand), so an open hat sounds like a higher snare;
    - the ride bell on the Bell (gold block), the cowbell on the Cow Bell (soul sand).
  - Other tracks start from the instrument of their MIDI program (guitar → Guitar, wool; voice → Flute, clay; piano → Piano). They switch to another instrument when its two octaves hold at least 15% more of their notes; most vocal melodies fit the Piano better, for example.
  - Dead notes (x) and dead slaps have no pitch, so they play as a click: the drum map's side stick, the Click (glass) at key 41, lower than the closed hi-hat. A dead strum is one click, on a layer of the track's own (its name ends in `: Dead notes`).
- **Dynamics**: note blocks have no loudness and the structure export ignores velocity, so every note is written at full velocity. Note Block Studio then plays the song as it will sound in the game.
- **Height**: with more than 4 notes at once, Note Block Studio stacks another level. The Snare Drum's sand falls when nothing holds it up, so the sand layers come last and always fill the lowest level.
- **Names**: Note Block Studio keeps one byte per character, so names in the `.nbs` are ASCII: a Chinese title becomes "OpenMusic song", a Chinese track name its kind and `--list` number, accents are dropped. The file name stays as it is.

## 2. Install Note Block Studio

- **Windows**: the stable [3.11.0](https://github.com/OpenNBS/NoteBlockStudio/releases/latest).
- **Mac**: the stable release is Windows-only. Use `4.Minecraft.Note.Block.Studio.dmg` from the [3.12.0-rc.1 prerelease](https://github.com/OpenNBS/NoteBlockStudio/releases/tag/v3.12.0-rc.1) (macOS 12 or later), or the TestFlight linked from its GitHub page.

Note: these are installers of the Note Block Studio editor (its full name is "Minecraft Note Block Studio"; the 1–4 in front of the file names only number them), not of the game. Note Block Studio opens, plays and exports songs on its own; Minecraft Java Edition is installed separately with the official launcher (step 4).

## 3. Listen, edit and export the structure in Note Block Studio

1. Open the `.nbs`, play it, fix notes if you like.
2. Export:
   - 3.12: File → Export → Schematic, format ".nbt (Structure Block)".
   - 3.11: Export as schematic, version "1.13+".
3. Keep "10 ticks / second" and the "Simple walkway" layout. "Include minecart track" is optional.
4. Name the file with lowercase letters, digits, `_`, `-` and `.` only (Minecraft's rule), e.g. `dian_dong_shao_nv.nbt`.

## 4. Place it in Minecraft (Java Edition)

> Not tried in the game yet.

1. Install Java Edition with the official launcher; it brings its own Java.
2. Create a world:
   - Creative and Peaceful; keep "Allow Commands" on (choosing Creative turns it on).
   - World Type "Superflat" → "Customize" → preset "Redstone Ready".
3. Put the `.nbt` in the world's `generated/minecraft/structure/` folder; create it if missing.
   - Since 26.1 the folder is `structure`; 1.21.x used `structures`.
   - On a Mac, saves are in `~/Library/Application Support/minecraft/saves/<world>/`; in the game, Edit World → "Open World Folder" gets there too.
4. In the world, stand where it should go and run `/place template minecraft:dian_dong_shao_nv ~ ~ ~` (your file's name).
   - If you added or replaced the `.nbt` while the world was open (a re-export under the same name included), run `/reload` first.
   - Why: the game remembers every structure name it has looked up, found or not, until `/reload` or until you re-enter the world.
5. Press the button at the start of the structure and walk along with the music: note blocks are heard within 48 blocks, and redstone in chunks far from you stops.

Optional: `/gamerule advance_time false`, `/gamerule advance_weather false`, `/gamerule spawn_mobs false`.

## Bedrock Edition

Not done yet. NBS exports Java Edition builds only, and its maintainers have said they will not add Bedrock export ([#587](https://github.com/OpenNBS/NoteBlockStudio/issues/587): Bedrock limits a structure's size, needs every chunk it covers loaded during import, and plays at most 32 sounds at once); as of November 2026, no fork of NBS exports to Bedrock either. We plan a small step in OpenMusic that turns the Java structure NBS exports (`.nbt`) into a Bedrock `.mcstructure`, keeping every note block's pitch and every repeater's delay, tested in Bedrock.

## Limits

- One tempo per song, and only steps such as 150, 100 or 75 BPM (a whole number of redstone ticks per beat).
- On 4 ticks per beat, triplets and 32nds move to the nearest 16th, so the notes of grace notes, fast trills, turns and mordents bunch up and sound together like a chord.
- Note blocks have no loudness: soft and loud notes sound the same.
- A note block strikes a fixed pitch and cannot bend. A slide's target note is struck again (a legato slide's too, although the guitar does not pick it), and the slide itself is not heard; a slide into or out of a note plays just its fret. Neither are bends, the whammy bar or vibrato: a bent note keeps the pitch of its fret, a pre-bend too. Pitch bends in a MIDI file are ignored, so a slide written as a pitch bend plays only its first note.
- Each repeat plays at most 8 times.
- Guitar transcriptions are too dense, so they are left out by default; add a fixed guitar part with `--tracks`.

## Tests

```bash
.venv/bin/python minecraft/tests/test_export_nbs.py
```

Add `--gp <file>` or `--songs outputs/<library>` to also convert real scores or a whole library.

## Licences

- The code is AGPL-3.0, like the rest of OpenMusic.
- The `.nbs` is written with [pynbs](https://github.com/OpenNBS/pynbs) (MIT).
- The instrument and drum maps come from Open Note Block Studio (MIT, © 2024–2026 OpenNBS), whose 3.12 maps are u3002's [General MIDI ⇄ Minecraft Note Block Mapping](https://gist.github.com/u3002/cf4daa83bc82b5917fc86fb23815578a) (MIT, © u3002). The full licence text is in the comment above the maps in `export_nbs.py`.
- Guitar Pro files are read by the vendored alphaTab (MPL-2.0).
- Minecraft is a trademark of Mojang. This tool is not affiliated with Mojang or Microsoft.
