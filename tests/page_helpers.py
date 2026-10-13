from __future__ import annotations

import http.client
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from deepcrew import policy, ui


def running(**kw):
    ready, result = queue.Queue(), {}
    thread = threading.Thread(target=lambda: result.update(ui.serve(on_ready=ready.put, timeout_s=30, **kw)), daemon=True)
    thread.start()
    return ready.get(timeout=5), thread, result


def caller(url):
    parts = urlsplit(url)
    token = parse_qs(parts.query)["t"][0]

    def call(method, path, body=None, *, auth=token, host=f"127.0.0.1:{parts.port}", headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
        head = {"Host": host, "Content-Type": "application/json", **({"X-Deepcrew-Token": auth} if auth is not None else {}), **(headers or {})}
        conn.request(method, path, body=raw if raw is not None else json.dumps(body) if body is not None else None, headers=head)
        reply = conn.getresponse()
        data = reply.read()
        conn.close()
        return reply.status, reply, (json.loads(data) if reply.getheader("Content-Type", "").startswith("application/json") else data)

    return SimpleNamespace(url=url, token=token, port=parts.port, call=call)


class TeamPages:
    def __init__(self, board):
        self.board, self.threads = board, []

    def __call__(self, timeout_s=30):
        thread = threading.Thread(target=ui.serve_team, args=(self.board.root, self.board.cfg["team"], timeout_s), daemon=True)
        thread.start()
        self.threads.append(thread)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ui.running(self.board.dir):
            time.sleep(0.02)
        info = ui.running(self.board.dir)
        assert info, "the team's page did not come up"
        page = caller(info["url"])
        page.thread, page.info, page.board = thread, info, self.board
        return page

    def close(self):
        for thread in self.threads:
            if thread.is_alive():
                ui.stop_page(self.board.dir)
            thread.join(5)


def filed_at() -> float:
    FILED[0] = max(time.time(), FILED[0] + 0.001)
    return FILED[0]


FILED = [0.0]


def ask_perm(board, command="wc -c", member="alpha", tool="Bash", tool_input=None):
    return board.request("perms", member, {"tool": tool, "input": tool_input or {"command": command}, "tool_use_id": None, "always": False,
                                           "ts": filed_at()})["id"]


def ask_question(board, text="which database?", member="beta", **extra):
    return board.request("questions", member, {"question": text, "task": "t1", "answer": None, "ts": filed_at(), **extra})["id"]


def standin_widening(monkeypatch) -> None:
    monkeypatch.setattr(policy, "widening", _widening, raising=False)


def _widening(board, r: dict) -> dict | None:
    tool, data = r.get("tool", ""), r.get("input") or {}
    command = str(data.get("command", "")).strip()
    if tool == "Bash" and command and not policy.SHELL_META & set(command):
        return {"kind": "command", "value": command, "text": f"`{command}` runs without asking from now on"}
    if tool == "WebFetch" and (host := urlsplit(str(data.get("url", ""))).hostname):
        return {"kind": "site", "value": host, "text": f"members reach {host} without asking"}
    if tool.startswith("mcp__") and not tool.startswith("mcp__deepcrew__"):
        return {"kind": "tool", "value": tool, "text": f"members use {tool} without asking"}
    return None


def page_fields(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<(input|select)\b([^>]*)>", page):
        attrs = {k: v for k, v in re.findall(r'([\w-]+)(?:="([^"]*)")?', m.group(2))}
        if "data-key" not in attrs:
            continue
        label = page[page.rfind("<label", 0, m.start()):page.find("</label>", m.end())]
        words = " ".join(re.sub(r"<[^>]*>", " ", re.sub(r"<select\b.*?</select>", " ", label, flags=re.S)).split())
        options = re.findall(r'<option value="([^"]*)"', page[m.end():page.find("</select>", m.end())]) if m.group(1) == "select" else []
        out.append({"tag": m.group(1), "type": attrs.get("type") or ("select-one" if m.group(1) == "select" else "text"), "id": attrs.get("id", ""),
                    "key": attrs["data-key"], "placeholder": attrs.get("placeholder", ""), "options": options, "label": words})
    return out


HARNESS = r"""
"use strict";
const vm = require("node:vm");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const out = {requests: [], results: [], errors: []};
process.on("unhandledRejection", (e) => out.errors.push(String((e && e.stack) || e)));

function stub(own = {}, onSet = null) {
  const target = () => {};
  delete target.length;
  delete target.name;
  Object.assign(target, own);
  return new Proxy(target, {
    get(t, key) {
      if (key in t) return t[key];
      if (key === Symbol.iterator) return function* () {};
      if (key === Symbol.toPrimitive) return () => "";
      if (typeof key === "symbol" || key === "then" || key === "toJSON") return undefined;
      return (t[key] = stub());
    },
    set(t, key, value) { if (onSet) onSet(key, value); t[key] = value; return true; },
    apply: () => stub(),
  });
}

function classes() {
  const set = new Set();
  return {add: (...names) => names.forEach((n) => set.add(n)), remove: (...names) => names.forEach((n) => set.delete(n)),
          contains: (n) => set.has(n), toggle: (n, on = !set.has(n)) => (on ? set.add(n) : set.delete(n), on),
          replace: (a, b) => (set.delete(a) ? (set.add(b), true) : false), reset: (text) => { set.clear(); String(text).split(/\s+/).filter(Boolean).forEach((n) => set.add(n)); },
          toString: () => [...set].join(" ")};
}

const matches = (el, selector) => selector.split(",").some((part) => {
  const m = part.trim().match(/^([a-z]*)((?:\.[\w-]+)*)(:not\(:disabled\))?$/);
  return !!m && (!m[1] || el.tagName === m[1].toUpperCase()) && m[2].split(".").filter(Boolean).every((c) => el.classList.contains(c)) && !(m[3] && el.disabled);
});

let document = null;
function node(tag, own = {}) {
  const el = stub({isNode: true, tagName: tag.toUpperCase(), children: [], attributes: {}, dataset: {}, classList: classes(), textContent: "", title: "",
                   hidden: false, disabled: false, value: "", placeholder: "", isConnected: true, parentNode: null, ...own},
                  (key, value) => { if (key === "className") el.classList.reset(value); });
  const siblings = () => (el.parentNode ? el.parentNode.children : []);
  Object.defineProperty(el, "firstElementChild", {get: () => el.children[0] || null});
  Object.defineProperty(el, "nextElementSibling", {get: () => siblings()[siblings().indexOf(el) + 1] || null});
  el.setAttribute = (name, value) => { el.attributes[name] = String(value); if (name.startsWith("data-")) el.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value); };
  el.getAttribute = (name) => (name in el.attributes ? el.attributes[name] : null);
  el.removeAttribute = (name) => { delete el.attributes[name]; };
  el.remove = () => { if (el.parentNode) el.parentNode.children = el.parentNode.children.filter((kid) => kid !== el); el.parentNode = null; el.isConnected = false; };
  el.insertBefore = (kid, before) => {
    if (kid.parentNode) kid.remove();
    const at = before ? el.children.indexOf(before) : -1;
    el.children.splice(at < 0 ? el.children.length : at, 0, kid);
    kid.parentNode = el;
    kid.isConnected = true;
    return kid;
  };
  el.append = (...kids) => {
    for (const kid of kids) {
      if (kid == null || kid === "") continue;
      if (kid.isNode === true) el.insertBefore(kid, null);
      else el.textContent += String(typeof kid === "string" || typeof kid === "number" ? kid : kid.nodeValue ?? "");   // text, or a text node
    }
  };
  el.replaceChildren = (...kids) => { for (const kid of el.children) { kid.parentNode = null; kid.isConnected = false; } el.children = []; el.textContent = ""; el.append(...kids); };
  el.querySelectorAll = (selector) => el.children.flatMap((kid) => [...(matches(kid, selector) ? [kid] : []), ...kid.querySelectorAll(selector)]);
  el.querySelector = (selector) => el.querySelectorAll(selector)[0] || null;
  el.contains = (other) => other === el || el.children.some((kid) => kid.contains(other));
  el.focus = () => { document.activeElement = el; };
  return el;
}

function field(f) {
  const el = node(f.tag, {id: f.id, type: f.type, placeholder: f.placeholder, checked: false, options: f.options.map((value) => ({value}))});
  el.dataset.key = f.key;
  el.value = f.options.length ? f.options[0] : "";
  if (f.tag === "select") el.replaceChildren = (...options) => { el.options = options.map((o) => ({value: o.value})); el.value = el.options.length ? el.options[0].value : ""; };
  el.closest = () => ({childNodes: [{nodeType: 3, textContent: f.label}]});
  return el;
}

const listeners = {};
const listen = (where) => (type, fn) => { (listeners[where + " " + type] = listeners[where + " " + type] || []).push(fn); };
const fire = (where) => (event) => { for (const fn of listeners[where + " " + event.type] || []) fn(event); return true; };
const fields = input.fields.map(field);
const byId = new Map(fields.filter((f) => f.id).map((f) => [f.id, f]));
const element = (id) => { if (!byId.has(id)) byId.set(id, node("div", {id})); return byId.get(id); };
document = stub({hidden: false, activeElement: null, body: node("body"), documentElement: node("html", {dataset: {theme: "auto"}}),
  querySelector: (selector) => (/^#[\w-]+$/.test(selector) ? element(selector.slice(1)) : node("div")),
  querySelectorAll: (selector) => (selector === "[data-key]" ? fields : []), getElementById: element,
  createElement: (tag) => node(tag), createTextNode: (text) => stub({nodeValue: text}), addEventListener: listen("document"), dispatchEvent: fire("document")});

const sandbox = {
  document, location: {search: "?t=stand-in", hash: ""}, URLSearchParams, performance, console: new console.Console(process.stderr),
  fetch: async (path, options = {}) => {
    const method = options.method || "GET", body = options.body === undefined ? null : JSON.parse(options.body);
    out.requests.push({method, path, body});
    const reply = JSON.parse(JSON.stringify(input.replies[method + " " + path.split("?")[0]] ?? {}));
    const status = reply.__status__ || 200;
    delete reply.__status__;
    return {ok: status < 400, status, json: async () => reply};
  },
  matchMedia: (query) => ({matches: query.includes("reduced-motion"), addEventListener() {}}),
  localStorage: {getItem: () => null, setItem() {}}, setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: () => 0,
  getComputedStyle: () => stub(), addEventListener: listen("window"), dispatchEvent: fire("window"), innerWidth: 1024, innerHeight: 768,
  CustomEvent: class { constructor(type) { this.type = type; } }, Event: class { constructor(type) { this.type = type; } },
  Option: class { constructor(text, value) { this.text = text; this.value = value; } },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(input.script, sandbox, {filename: "ui.html"});
(async () => {
  await new Promise((resolve) => setImmediate(resolve));   // the first look at the settings is drawn
  for (const step of input.steps) {
    try {
      const value = await vm.runInContext("(async () => {\n" + step + "\n})()", sandbox);
      out.results.push(value === undefined ? null : JSON.parse(JSON.stringify(value)));
    } catch (e) {
      out.errors.push(String((e && e.stack) || e));
      out.results.push(null);
    }
    await new Promise((resolve) => setImmediate(resolve));
  }
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
})();
"""


def page_script(steps: list[str], state: dict, replies: dict | None = None) -> dict:
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("needs node to run the page's script")
    page = Path(ui.__file__).with_name("ui.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    payload = {"script": script, "fields": page_fields(page), "replies": {"GET /api/state": state, **(replies or {})}, "steps": steps}
    done = subprocess.run([exe, "-e", HARNESS], input=json.dumps(payload), capture_output=True, encoding="utf-8", timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["errors"] == [], "\n".join(out["errors"])
    return out
