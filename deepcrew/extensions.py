from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import sys
import threading
import types
from pathlib import Path

from . import providers

API_VERSION = 1
KEY = "DEEPCREW_EXTENSIONS"
FILE = "extensions.json"
PINS = "extensions.pins.json"
SPEC = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)?")
_loaded: dict[tuple, list] = {}
_failures: dict[str, str] = {}
_said: set[tuple[str, str]] = set()
_lock = threading.RLock()


def checked(raw: str) -> str:
    specs = providers.split_list(raw)
    for spec in specs:
        if not SPEC.fullmatch(spec):
            raise ValueError(f"{KEY}: {spec!r} is not package.module or package.module:object")
    return ",".join(specs)


def project_key(project=None) -> str:
    return str(Path(project or os.getcwd()).resolve())


def _read(name: str) -> dict:
    try:
        data = json.loads((providers.CONFIG_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(name: str, data: dict) -> None:
    providers.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(providers.CONFIG_DIR, 0o700)
    tmp = providers.CONFIG_DIR / f".{name}.{os.getpid()}.tmp"
    providers.write_private(tmp, json.dumps(data, indent=1, sort_keys=True) + "\n")
    providers.replace_file(tmp, providers.CONFIG_DIR / name)


def saved() -> dict[str, list[str]]:
    return {k: [s for s in v if isinstance(s, str)] for k, v in _read(FILE).items() if isinstance(v, list)}


def everywhere() -> list[str]:
    return providers.split_list(providers.env_file_get(KEY))


def declared(project=None) -> list[str]:
    return list(dict.fromkeys([*providers.split_list(os.environ.get(KEY)), *everywhere(), *saved().get(project_key(project), [])]))


def declare(project, raw: str, every_project: bool = False) -> list[str]:
    specs = [] if raw.strip().lower() in ("", "none") else providers.split_list(checked(raw))
    pins = {}
    for spec in specs:
        try:
            own = own_file(spec, project)
            if own and not every_project:
                pins[own.relative_to(project_key(project)).as_posix()] = hashlib.sha256(own.read_bytes()).hexdigest()
            if not (own and every_project):
                resolve(spec, project, pins)
        except Exception as e:
            raise ValueError(providers.shown(f"extension {spec} does not load ({type(e).__name__}: {e}); nothing was saved. Name a module inside "
                                             f"the project (tools.myext for tools/myext.py) or one installed for {sys.executable}")) from None
        if own and every_project:
            raise ValueError(f"extension {spec} is {own.relative_to(project_key(project)).as_posix()} of this project, whose bytes are pinned for "
                             "this project alone: declare it without --global; nothing was saved")
    if every_project:
        providers.env_file_set({KEY: ",".join(specs)})
    else:
        data, key = saved(), project_key(project)
        if specs:
            data[key] = specs
        else:
            data.pop(key, None)
        _store(FILE, data)
        _store(PINS, {**_read(PINS), key: pins} if pins else {k: v for k, v in _read(PINS).items() if k != key})
    with _lock:
        _loaded.clear()
    return specs


def _in_project(place: str, root: Path) -> bool:
    written = Path(os.path.abspath(place))
    resolved = written.resolve()
    return resolved == root or root in resolved.parents or any(p.resolve() == root for p in (written, *written.parents))


def own_file(spec: str, project=None) -> Path | None:
    names = spec.partition(":")[0].split(".")
    if names[0] == __name__.partition(".")[0]:
        return None
    root = Path(project_key(project))
    try:
        found = importlib.util.find_spec(names[0])
    except (ImportError, ValueError):
        found = None
    places = [*([found.origin] if found.has_location else []), *(found.submodule_search_locations or [])] if found else []
    if found is not None and not any(_in_project(p, root) for p in places):
        return None
    path = root.joinpath(*names[:-1], names[-1] + ".py")
    if path.is_file() and not any((root.joinpath(*names[:i]) / "__init__.py").exists() for i in range(1, len(names))):
        return path
    if found is None:
        return None
    raise ImportError(f"{'.'.join(names)} leads into the project, but not to one file in folders without __init__.py: only such a file "
                      "can be pinned")


class _Pinned(importlib.abc.Loader):
    def __init__(self, path: Path, code: bytes):
        self.path, self.code = path, code

    def exec_module(self, module) -> None:
        exec(compile(self.code, str(self.path), "exec"), module.__dict__)


def _run_pinned(name: str, path: Path, code: bytes) -> types.ModuleType:
    spec = importlib.util.spec_from_loader(name, _Pinned(path, code), origin=str(path))
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    before = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if before is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = before
    return module


def resolve(spec: str, project=None, pins: dict | None = None) -> object:
    checked(spec)
    module, _, attr = spec.partition(":")
    if own := own_file(spec, project):
        rel, code = own.relative_to(project_key(project)).as_posix(), own.read_bytes()
        pinned = (pins if pins is not None else _read(PINS).get(project_key(project), {})).get(rel)
        if pinned != hashlib.sha256(code).hexdigest():
            raise ImportError(f"{rel} changed since it was declared (or was never declared): run `config set extensions` again to trust it")
        obj = _run_pinned(module, own, code)
    else:
        obj = importlib.import_module(module)
    for part in filter(None, attr.split(".")):
        obj = getattr(obj, part)
    needs = getattr(obj, "DEEPCREW_API", API_VERSION)
    if not isinstance(needs, int) or isinstance(needs, bool) or needs > API_VERSION:
        raise ImportError(f"it needs extension API {needs!r}; this deepcrew has API {API_VERSION}")
    return obj


def loaded(project=None) -> list[tuple[str, object]]:
    key = (project_key(project), *declared(project))
    with _lock:
        if key not in _loaded:
            found = []
            for spec in key[1:]:
                try:
                    found.append((spec, resolve(spec, project)))
                except Exception as e:
                    _failures[spec] = providers.shown(f"{type(e).__name__}: {e}")
            _loaded[key] = found
        return _loaded[key]


def failures(project=None) -> dict[str, str]:
    names = {spec for spec, _ in loaded(project)}
    return {spec: _failures.get(spec, "did not load") for spec in declared(project) if spec not in names}


def label(spec: str) -> str:
    return re.split(r"[.:]", spec)[-1]


def hooks(name: str, project=None) -> list:
    return [h for _, x in loaded(project) if callable(h := getattr(x, name, None))]


def _failed(name: str, e: Exception) -> None:
    said = (name, providers.shown(f"{type(e).__name__}: {e}"))
    with _lock:
        first = said not in _said
        _said.add(said)
    if first:
        print(f"{providers.NAME}: extension hook {name} failed ({said[1]})", file=sys.stderr)


def call(name: str, *args, project=None) -> list:
    out = []
    for hook in hooks(name, project):
        try:
            out.append(hook(*args))
        except Exception as e:
            _failed(name, e)
    return out


def chain(name: str, value, *args, project=None):
    for hook in hooks(name, project):
        try:
            got = hook(*args, value)
            value = value if got is None else got
        except Exception as e:
            _failed(name, e)
    return value
