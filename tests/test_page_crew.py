from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from deepcrew import ui

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")

HARNESS = r"""
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
let frames = 0;
function el(tag) {
  return {tag, attrs: {}, children: [], style: {setProperty() {}}, dataset: {}, isConnected: true, className: "", textContent: "",
    classList: {toggle() {}}, setAttribute(k, v) { this.attrs[k] = String(v); }, appendChild(c) { this.children.push(c); return c; },
    append(...kids) { for (const k of kids) this.children.push(typeof k === "string" ? {text: k} : k); },
    replaceChildren(...kids) { this.children = []; this.append(...kids); }};
}
const text = (e) => (e.text !== undefined ? e.text : e.children.length ? e.children.map(text).join("") : e.textContent || "");
globalThis.motion = {matches: input.still, addEventListener() {}};
globalThis.devicePixelRatio = input.dpr;
globalThis.document = {hidden: false, addEventListener() {}, createElementNS: (ns, tag) => el(tag), createElement: el};
globalThis.requestAnimationFrame = () => { frames += 1; return frames; };
globalThis.addEventListener = () => {};
globalThis.setTimeout = () => 0;
eval(input.engine + "\nglobalThis.PixelCrew = PixelCrew;");
const out = {};
for (const [name, opts] of Object.entries(input.scenes)) {
  const host = el("div");
  host.clientWidth = opts.width || 600;
  PixelCrew.picture(host, opts);
  const svg = host.children[0];
  out[name] = {box: svg.attrs.viewBox, width: Number(svg.attrs.width), paths: svg.children.map((p) => [p.attrs.style || "", p.attrs.d || ""])};
}
const fills = (d) => d.paths.map((p) => p.attrs.style || "").join(" ");
const diver = new PixelCrew.Diver({suit: "strongest"});
out.strongest = fills(diver);
out.idle = fills(diver.set("idle"));
out.failed = fills(diver.set("failed"));
out.stuck = fills(new PixelCrew.Diver({suit: "fast", state: "active"}).tint(1).set("ready"));
out.frames = frames;
process.stdout.write(JSON.stringify(out));
"""
SCENES = {"solo": {"size": 1}, "crowd": {"size": 8, "sandbox": True, "network": True},
          "locked": {"size": 4, "suit": "strong", "sandbox": True, "network": False}, "large": {"size": 16},
          "narrow": {"size": 8, "width": 200}}
VISOR, FLAG, LOCK, INK = "fill:#1A1918", "fill:var(--a-orange)", "fill:var(--a-gold)", "fill:var(--muted)"
PIXEL = re.compile(r"M(\d+) (\d+)h1v1h-1z")


def crew(still: bool, dpr: float = 1) -> dict:
    page = Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")
    start = page.index("const PixelCrew = (() => {")
    engine = page[start:page.index("\n})();\n", start) + 6]
    payload = {"engine": engine, "still": still, "dpr": dpr, "scenes": SCENES}
    done = subprocess.run([shutil.which("node"), "-e", HARNESS], input=json.dumps(payload), capture_output=True, encoding="utf-8",
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def pixels(scene: dict) -> dict:
    return {(int(x), int(y)): fill for fill, d in scene["paths"] for x, y in PIXEL.findall(d)}


def test_the_picture_has_a_diver_for_each_member_the_boat_and_the_sandbox_as_a_dome():
    out = crew(still=True)
    for name, members in (("solo", 1), ("crowd", 8), ("locked", 4), ("large", 16)):
        px = pixels(out[name])
        width = int(out[name]["box"].split()[2])
        top, cx = 12, width // 2
        assert list(px.values()).count(VISOR) == 48 * members
        assert list(px.values()).count(FLAG) == 5
        edge = [x for (x, y), fill in px.items() if y == top and fill == INK and x != cx]
        if name == "crowd":
            assert edge and all(abs(x - cx) > 3 for x in edge) and LOCK not in px.values()
        elif name == "locked":
            assert edge and list(px.values()).count(LOCK) == 19 and px[(cx, top)] == LOCK
        else:
            assert not edge and LOCK not in px.values()


def test_the_picture_is_drawn_in_whole_device_pixels_about_one_and_a_half_screen_pixels_to_a_pixel():
    plain, sharp = crew(still=True), crew(still=True, dpr=2)
    assert plain["crowd"]["width"] == 174 * 2 and sharp["crowd"]["width"] == 174 * 1.5
    assert plain["narrow"]["width"] == 174


def test_avatars_wear_the_suit_of_their_strength_and_state_and_redden_when_stuck():
    out = crew(still=True)
    assert "#E8B04A" in out["strongest"] and "#E07A52" not in out["strongest"]
    assert "#CFCBC1" in out["idle"] and "#C0603C" in out["failed"]
    assert "color-mix(in srgb, #E07A52" in out["stuck"] and "#C0603C" in out["stuck"]


def test_nothing_moves_when_the_system_asks_for_less_motion():
    assert crew(still=True)["frames"] == 0
    assert crew(still=False)["frames"] > 0
