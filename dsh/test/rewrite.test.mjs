import test from "node:test";
import assert from "node:assert/strict";
import { rewriteHtml, rewriteJson, rewriteGpBody } from "../lib/rewrite.js";

test("rewriteHtml prefixes API, static files and the player's workbench link", () => {
  const html = [
    "<a id=home href=\"/?lang=zh\">workbench</a>",
    "<script>fetch('/api/state')</script>",
    "<script>post('/api/gp', body)</script>",
    "<a href=\"/outputs/roformer_sw_full/player.html\">player</a>",
    "<script src=\"/outputs/x/_player/wavesurfer.esm.js\"></script>",
  ].join("\n");
  const out = rewriteHtml(html);
  assert.match(out, /href="\/openmusic\/app\?lang=zh"/);
  assert.match(out, /fetch\('\/openmusic\/api\/state'\)/);
  assert.match(out, /post\('\/openmusic\/api\/gp'/);
  assert.match(out, /href="\/openmusic\/outputs\/roformer_sw_full\/player\.html"/);
  assert.match(out, /src="\/openmusic\/outputs\/x\/_player\/wavesurfer\.esm\.js"/);
  assert.doesNotMatch(out, /["'`]\/api\//);
  assert.doesNotMatch(out, /["'`]\/outputs\//);
});

test("rewriteHtml leaves relative asset imports alone", () => {
  const html = "<script type=module>import X from './_player/SignalsmithStretch.mjs'</script>";
  assert.equal(rewriteHtml(html), html);
});

test("rewriteJson prefixes song links in the workbench state", () => {
  const state = JSON.stringify({
    midi_urls: { bass: "/outputs/mp3/song/transcription/bass.mid" },
    player_url: "/outputs/mp3/player.html?song=song",
  });
  const out = rewriteJson(state);
  assert.match(out, /"\/openmusic\/outputs\/mp3\/song\/transcription\/bass\.mid"/);
  assert.match(out, /"\/openmusic\/outputs\/mp3\/player\.html\?song=song"/);
  assert.doesNotMatch(out, /"\/outputs\//);
});

test("rewriteGpBody strips only the proxy prefix from the player's root", () => {
  const prefixed = Buffer.from(JSON.stringify({ root: "openmusic/outputs/mp3", song: "x", audio: "none" }));
  assert.deepEqual(JSON.parse(rewriteGpBody(prefixed).toString()), {
    root: "outputs/mp3",
    song: "x",
    audio: "none",
  });

  const plain = Buffer.from(JSON.stringify({ root: "outputs/mp3", song: "x" }));
  assert.equal(rewriteGpBody(plain), plain, "an already-relative root is left alone");

  const notJson = Buffer.from("not json");
  assert.equal(rewriteGpBody(notJson), notJson, "a non-JSON body is forwarded untouched");
});
