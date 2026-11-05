/**
 * URL and body rewrites for the /openmusic proxy.  Kept in their own module so the smoke test can exercise
 * them without a Cordis context or a running workbench.
 */

/** Point the workbench/player page's root-absolute URLs at the same-origin /openmusic prefix. */
export function rewriteHtml(text) {
  return text
    .replace(/(["'`])\/api\//g, "$1/openmusic/api/")
    .replace(/(["'`])\/outputs\//g, "$1/openmusic/outputs/")
    .replace(/href=(["'])\/\?lang=/g, "href=$1/openmusic/app?lang=");
}

/** /api/state returns absolute /outputs/... links; keep the player/MIDI/Guitar Pro links inside the prefix. */
export function rewriteJson(text) {
  return text.replace(/"\/outputs\//g, '"/openmusic/outputs/');
}

/**
 * The player names its library as "outputs/<library>"; through the proxy it arrives as
 * "openmusic/outputs/<library>", while /api/gp expects the path relative to the project.
 */
export function rewriteGpBody(buffer) {
  try {
    const data = JSON.parse(buffer.toString("utf8"));
    if (typeof data.root !== "string" || !data.root.startsWith("openmusic/")) return buffer;
    data.root = data.root.slice("openmusic/".length);
    return Buffer.from(JSON.stringify(data));
  } catch {
    return buffer;
  }
}
