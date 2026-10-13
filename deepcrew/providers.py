from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .host import ProviderError, is_windows, replace_file, retry_busy, split_list, write_private
from .tariff import DAYS, check_tariff, price_share, tariff_now

NAME = os.environ.get("DEEPCREW_NAME") or "deepcrew"
CONFIG_DIR = Path(os.environ.get("DEEPCREW_CONFIG_DIR") or Path.home() / ".config" / NAME).expanduser()
ENV_FILE = CONFIG_DIR / ".env"
SECURITY = "/usr/bin/security"
BALANCE_TIMEOUT_S = 10.0
SUBPROCESS_TEXT = {"encoding": "utf-8", "errors": "replace"}
REQUIRED = ("base_url", "models", "efforts", "fast_model", "auto_compact_window", "timeout_ms", "key_sources", "key_pattern")

BUILTIN: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "models": ["deepseek-flash[1m]", "deepseek-v4-pro[1m]"],
        "efforts": ["low", "medium", "high", "max"],
        "fast_model": "deepseek-flash",
        "auto_compact_window": 786432,
        "timeout_ms": 600000,
        "max_output_tokens": 128000,
        "thinking_off_body": True,
        "documents": False,
        "key_sources": ["env:DEEPSEEK_API_KEY", "keychain:deepcrew-deepseek", "dotenv:DEEPSEEK_API_KEY"],
        "key_pattern": r"sk-[A-Za-z0-9]{16,128}",
        "balance_url": "https://api.deepseek.com/user/balance",
        "prefix_cache": True,
        "prices": {"deepseek-flash": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
                   "deepseek-v4-pro": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96}},
        "tariff": {"peak_utc": ["01:00-04:00", "06:00-10:00"], "peak_days": ["mon", "tue", "wed", "thu", "fri"], "off_peak": 0.5,
                   "holidays": []},
        "vision": ["deepseek-flash"],
    },
}

TEMPLATES = {
    "zai": r"""
# Z.ai (GLM), the international platform. From docs.z.ai, read 2026-10-11: /devpack/tool/claude (its Claude Code settings),
# /devpack/latest-model, /guides/overview/pricing, /guides/llm/glm-5.3, /guides/vlm/glm-5.3-flash and /api-reference/api-code.
# The first model is the fast one and the last the strongest; "[1m]" tells Claude Code the context window is 1M tokens.
[zai]
base_url = "https://api.z.ai/api/anthropic"
models = ["glm-5.3-flash[1m]", "glm-5.3[1m]"]
# The Anthropic endpoint takes low, high and max effort (medium counts as high).
efforts = ["low", "high", "max"]
fast_model = "glm-5.3-flash[1m]"
auto_compact_window = 1000000
# Z.ai's Claude Code guide sets API_TIMEOUT_MS to 3000000 (50 minutes).
timeout_ms = 3000000
# Without a thinking field the model thinks at its highest effort, so with thinking off the request says so.
thinking_off_body = true
# No PDF document blocks. GLM-5.3-Flash reads images (screenshots); GLM-5.3 reads text only.
documents = false
vision = ["glm-5.3-flash"]
# Z.ai caches repeated prompt prefixes without being asked.
prefix_cache = true
key_sources = ["env:ZAI_API_KEY", "keychain:deepcrew-zai", "dotenv:ZAI_API_KEY"]
# Z.ai does not document the form of its keys.
key_pattern = '\S{16,}'

# USD per 1M tokens (docs.z.ai/guides/overview/pricing).
[zai.prices."glm-5.3-flash"]
cache_hit = 0.03
cache_miss = 0.15
output = 0.50

[zai.prices."glm-5.3"]
cache_hit = 0.26
cache_miss = 1.4
output = 4.4

# Z.ai answers HTTP 429 for an empty balance as well as for its limits, and the message tells them apart (docs.z.ai/api-reference/api-code):
# code 1113 "Insufficient balance or no resource package. Please recharge.", 1309 "Your GLM Coding Plan package has expired ...",
# 1305 "The service may be temporarily overloaded, please try again later". Any other 429 counts as a rate limit.
[zai.errors]
balance = ["429:insufficient balance", "429:package has expired"]
overload = ["429:temporarily overloaded"]
""",
    "minimax": r"""
# MiniMax, the international platform. From platform.minimax.io/docs, read 2026-10-11: /m-plan/claude-code (its Claude Code settings),
# /guides/pricing-paygo, /api-reference/text-anthropic-api, /api-reference/text-chat-anthropic, /guides/rate-limits and
# /api-reference/anthropic-api-compatible-cache.
[minimax]
base_url = "https://api.minimax.io/anthropic"
# One model: MiniMax-M3 reads images and has a 1M-token window. MiniMax-M2.7 costs the same, with a 204,800-token window and no images.
models = ["MiniMax-M3[1m]"]
# MiniMax documents the effort level only for MiniMax-M3.1-Flash-Preview, a model of its subscription plans.
efforts = []
fast_model = "MiniMax-M3"
# MiniMax's Claude Code guide compacts at 512K tokens, which also keeps every request at the lower price below.
auto_compact_window = 524288
timeout_ms = 600000
# MiniMax's recommended max_tokens for MiniMax-M3 (it allows up to 524,288).
max_output_tokens = 131072
# MiniMax-M3 thinks only when the request asks for it, as Claude Code does unless thinking is off.
thinking_off_body = false
# No PDF document blocks; images are read.
documents = false
vision = ["MiniMax-M3"]
# MiniMax caches what the request marks with cache_control (for 5 minutes), as Claude Code marks it.
prefix_cache = true
key_sources = ["env:MINIMAX_API_KEY", "keychain:deepcrew-minimax", "dotenv:MINIMAX_API_KEY"]
# MiniMax does not document the form of its keys.
key_pattern = '\S{16,}'

# USD per 1M tokens up to 512K input tokens (twice these above).
[minimax.prices."MiniMax-M3"]
cache_hit = 0.06
cache_miss = 0.30
output = 1.20

# MiniMax answers HTTP 529 (overloaded_error) when the model is overloaded and 429 (rate_limit_error) at a rate limit. Its Anthropic
# API documents no error for an empty balance, so 402 keeps meaning one, as for DeepSeek.
[minimax.errors]
overload = ["529"]
rate_limit = ["429"]
""",
    "zhipu": r"""
# Zhipu (GLM) on the mainland China platform, with an account and keys of its own. From docs.bigmodel.cn, read 2026-10-11:
# /cn/coding-plan/tool/claude, /cn/guide/develop/claude/introduction, /cn/guide/start/pricing and /cn/api/api-code.
# Its prices are in CNY per 1M tokens: GLM-5.3-Flash cache hit 0.23, input 0.8, output 2.8; GLM-5.3 cache hit 2, input 8, output 28.
# deepcrew counts costs in USD only, so this entry has no prices: costs are not tracked, and a budget cannot be counted.
[zhipu]
base_url = "https://open.bigmodel.cn/api/anthropic"
models = ["glm-5.3-flash[1m]", "glm-5.3[1m]"]
# The Anthropic endpoint takes low, high and max effort (medium counts as high).
efforts = ["low", "high", "max"]
fast_model = "glm-5.3-flash[1m]"
auto_compact_window = 1000000
timeout_ms = 3000000
# Without a thinking field the model thinks at its highest effort, so with thinking off the request says so.
thinking_off_body = true
# No PDF document blocks. GLM-5.3-Flash reads images (screenshots); GLM-5.3 reads text only.
documents = false
vision = ["glm-5.3-flash"]
prefix_cache = true
key_sources = ["env:ZHIPU_API_KEY", "keychain:deepcrew-zhipu", "dotenv:ZHIPU_API_KEY"]
# Zhipu does not document the form of its keys.
key_pattern = '\S{16,}'

# Zhipu answers HTTP 429 for an empty balance as well as for its limits, with the message in Chinese (docs.bigmodel.cn/cn/api/api-code):
# code 1113 says the account is in arrears, 1309 that the GLM Coding Plan has expired, 1305 that the model has too many requests. They
# are written as TOML \u escapes, and the English wording counts too. Any other 429 counts as a rate limit.
[zhipu.errors]
balance = ["429:\u6b20\u8d39", "429:\u5957\u9910\u5df2\u5230\u671f", "429:insufficient balance", "429:package has expired"]
overload = ["429:\u8bbf\u95ee\u91cf\u8fc7\u5927", "429:temporarily overloaded"]
""",
    "minimax-cn": r"""
# MiniMax on the mainland China platform, with an account and keys of its own. From platform.minimaxi.com/docs, read 2026-10-11:
# /m-plan/claude-code and /guides/pricing-paygo. Its prices are in CNY per 1M tokens, for MiniMax-M3 up to 512K input tokens: cache
# read 0.42, input 2.10, output 8.40 (above 512K: 0.84, 4.20, 16.80). deepcrew counts costs in USD only, so this entry has no prices:
# costs are not tracked, and a budget cannot be counted.
[minimax-cn]
base_url = "https://api.minimax.cn/anthropic"
# One model: MiniMax-M3 reads images and has a 1M-token window.
models = ["MiniMax-M3[1m]"]
# MiniMax documents the effort level only for MiniMax-M3.1-Flash-Preview, a model of its subscription plans.
efforts = []
fast_model = "MiniMax-M3"
# MiniMax's Claude Code guide compacts at 512K tokens, which also keeps every request at the lower price.
auto_compact_window = 524288
timeout_ms = 600000
max_output_tokens = 131072
# MiniMax-M3 thinks only when the request asks for it, as Claude Code does unless thinking is off.
thinking_off_body = false
documents = false
vision = ["MiniMax-M3"]
prefix_cache = true
key_sources = ["env:MINIMAX_CN_API_KEY", "keychain:deepcrew-minimax-cn", "dotenv:MINIMAX_CN_API_KEY"]
# MiniMax does not document the form of its keys.
key_pattern = '\S{16,}'

# HTTP 529 (overloaded_error) when the model is overloaded, 429 (rate_limit_error) at a rate limit; 402 keeps meaning an empty balance.
[minimax-cn.errors]
overload = ["529"]
rate_limit = ["429"]
""",
}

ERROR_KINDS = ("balance", "overload", "rate_limit")
DEFAULT_ERRORS = {"balance": ["402"], "overload": ["503", "529"], "rate_limit": ["429"]}
ERROR_RULE = re.compile(r"([1-5][0-9][0-9])(?::(.*\S.*))?", re.S)


def error_rules(p: dict) -> list[tuple[str, int, str]]:
    def parsed(kind: str, rules) -> list[tuple[str, int, str]]:
        return [(kind, int(m[1]), (m[2] or "").strip().lower()) for m in map(ERROR_RULE.fullmatch, rules) if m]
    own = [r for kind in ERROR_KINDS for r in parsed(kind, (p.get("errors") or {}).get(kind) or [])]
    return sorted(own, key=lambda r: not r[2]) + [r for kind in ERROR_KINDS for r in parsed(kind, DEFAULT_ERRORS[kind])]


def error_kind(p: dict, status, said: str) -> str | None:
    text = (said or "").lower()
    return next((kind for kind, code, words in error_rules(p) if code == status and words in text), None)


def team_tables(cfg: dict) -> list[dict]:
    return [cfg["provider"], *[p for p in cfg.get("fallback") or [] if isinstance(p, dict)]]


def _user_tables() -> dict:
    user = CONFIG_DIR / "providers.toml"
    if not user.exists():
        return {}
    try:
        return tomllib.loads(user.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise ProviderError(f"{shown(user)} is not valid TOML: {e}") from None


def load(name: str) -> dict:
    tables = {k: dict(v) for k, v in BUILTIN.items()}
    for key, table in _user_tables().items():
        if isinstance(table, dict):
            tables[key] = merged(tables.get(key, {}), table)
    if name not in tables:
        ready = sorted(set(TEMPLATES) - set(tables))
        raise ProviderError(f"unknown provider {name!r}; known: {', '.join(sorted(tables))}" + (f"; ready entries: {', '.join(ready)}" if ready else "")
                            + f" (add one in {shown(CONFIG_DIR / 'providers.toml')})")
    p = {"name": name, **tables[name]}
    missing = [k for k in REQUIRED if k not in p]
    if missing:
        raise ProviderError(f"provider {name!r} lacks {', '.join(missing)}")
    url = urlparse(p["base_url"])
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")):
        raise ProviderError(f"provider {name!r}: base_url must be https (http only for localhost)")
    if p.get("tariff"):
        try:
            check_tariff(p["tariff"])
        except ProviderError as e:
            raise ProviderError(f"provider {name!r}: {e}") from None
    errors = p.get("errors", {})
    if not isinstance(errors, dict) or set(errors) - set(ERROR_KINDS) or not all(
            isinstance(rules, list) and all(isinstance(r, str) and ERROR_RULE.fullmatch(r) for r in rules) for rules in errors.values()):
        raise ProviderError(f"provider {name!r}: errors maps {', '.join(ERROR_KINDS)} to lists of \"STATUS\" or \"STATUS:words in the message\", "
                            "such as \"429:insufficient balance\"")
    return p


def add_template(name: str) -> bool:
    if name in known():
        return False
    if name not in TEMPLATES:
        load(name)
    path = CONFIG_DIR / "providers.toml"
    before = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    text = (before.rstrip("\n") + "\n\n" if before.strip() else "") + TEMPLATES[name].strip("\n") + "\n"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProviderError(f"{name}'s entry cannot be added to {shown(path)} ({e}): add it by hand") from None
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = path.with_name(f".providers.toml.{os.getpid()}.tmp")
    write_private(tmp, text)
    replace_file(tmp, path)
    return True


def merged(base: dict, extra: dict) -> dict:
    return {**base, **{k: merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v for k, v in extra.items()}}


def known() -> list[str]:
    return sorted(set(BUILTIN) | {k for k, v in _user_tables().items() if isinstance(v, dict)})


def _env_lines() -> list[str]:
    return ENV_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines()


def env_file_get(var: str) -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in _env_lines():
        k, sep, v = line.strip().partition("=")
        if sep and k.strip() == var:
            return v.strip().strip("'\"") or None
    return None


def env_file_has(var: str) -> bool:
    return ENV_FILE.exists() and any(line.strip().partition("=")[0].strip() == var and "=" in line for line in _env_lines())


def env_file_set(values: dict[str, str]) -> Path:
    for var, value in values.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", var) or "\n" in value or "\r" in value:
            raise ProviderError(f"cannot store {var!r} in {shown(ENV_FILE)}")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    lines = _env_lines() if ENV_FILE.exists() else [
        "# deepcrew configuration. Read only by deepcrew; keep it out of every repository."]
    todo = dict(values)
    out = []
    for line in lines:
        k = line.partition("=")[0].strip()
        out.append(f"{k}={todo.pop(k)}" if k in todo else line)
    out += [f"{k}={v}" for k, v in todo.items()]
    tmp = ENV_FILE.with_name(f".env.{os.getpid()}.tmp")
    write_private(tmp, "\n".join(out) + "\n")
    replace_file(tmp, ENV_FILE)
    return ENV_FILE


def setting(var: str) -> str | None:
    return os.environ.get(var) or env_file_get(var)


def choices() -> dict[str, str | None]:
    return {"provider": setting("DEEPCREW_PROVIDER"), "model": setting("DEEPCREW_MODEL"), "effort": setting("DEEPCREW_EFFORT"),
            "thinking": setting("DEEPCREW_THINKING")}


def _keychain_get(service: str) -> str | None:
    if sys.platform != "darwin":
        return None
    r = subprocess.run([SECURITY, "find-generic-password", "-s", service, "-a", getpass.getuser(), "-w"],
                       capture_output=True, text=True, **SUBPROCESS_TEXT, check=False)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _file_get(path: str) -> str | None:
    p = Path(path).expanduser()
    return p.read_text(encoding="utf-8-sig", errors="replace").strip() or None if p.is_file() else None


def resolve_key(p: dict) -> tuple[str, str] | None:
    for source in p["key_sources"]:
        kind, _, arg = source.partition(":")
        value = {"env": lambda a: os.environ.get(a), "keychain": _keychain_get, "dotenv": env_file_get, "file": _file_get}.get(
            kind, lambda a: None)(arg)
        if value:
            return source, value
    return None


def key_source(p: dict) -> str | None:
    found = resolve_key(p)
    return found[0] if found else None


def valid_key(p: dict, key: str) -> bool:
    return bool(re.fullmatch(p["key_pattern"], key))


def _source_arg(p: dict, kind: str) -> str | None:
    return next((s.partition(":")[2] for s in p["key_sources"] if s.startswith(kind + ":")), None)


def store_keychain(p: dict, key: str) -> str:
    service = _source_arg(p, "keychain")
    if sys.platform != "darwin" or not service:
        raise ProviderError("no Keychain source for this provider on this system")
    if not valid_key(p, key):
        raise ProviderError("that does not look like a key for this provider; nothing was stored")
    subprocess.run([SECURITY, "-i"], input=f"add-generic-password -U -a {getpass.getuser()} -s {service} -w {key}\n",
                   capture_output=True, text=True, **SUBPROCESS_TEXT, check=False)
    if _keychain_get(service) != key:
        raise ProviderError("the Keychain did not accept the key")
    return f"keychain:{service}"


def store_env_file(p: dict, key: str) -> str:
    var = _source_arg(p, "dotenv")
    if not var:
        raise ProviderError(f"provider {p['name']!r} has no dotenv key source")
    if not valid_key(p, key):
        raise ProviderError("that does not look like a key for this provider; nothing was stored")
    env_file_set({var: key})
    return f"dotenv:{var}"


def clear_keys(p: dict) -> list[str]:
    removed = []
    service = _source_arg(p, "keychain")
    if sys.platform == "darwin" and service:
        r = subprocess.run([SECURITY, "delete-generic-password", "-s", service, "-a", getpass.getuser()],
                           capture_output=True, text=True, **SUBPROCESS_TEXT, check=False)
        if r.returncode == 0:
            removed.append(f"keychain:{service}")
    var = _source_arg(p, "dotenv")
    if var and env_file_get(var):
        env_file_set({var: ""})
        removed.append(f"dotenv:{var}")
    return removed


def env_template(p: dict) -> Path:
    var = _source_arg(p, "dotenv")
    if var and env_file_get(var) is None:
        env_file_set({var: ""})
    elif not ENV_FILE.exists():
        env_file_set({})
    return ENV_FILE


def ask_tty(p: dict) -> str:
    key = getpass.getpass(f"{p['name']} API key (input hidden): ").strip()
    return store_keychain(p, key) if sys.platform == "darwin" and _source_arg(p, "keychain") else store_env_file(p, key)


def windows_spellings(path: str) -> set[str]:
    slashed = path.replace("\\", "/")
    out = {path, slashed, path.replace("\\", "\\\\")}
    drive = re.match(r"([A-Za-z]):/(.*)", slashed)
    return out | ({f"/{drive.group(1).lower()}/{drive.group(2)}"} if drive else set())


def shown(value):
    if isinstance(value, dict):
        return {k: shown(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shown(v) for v in value]
    if not isinstance(value, str | Path):
        return value
    text, home = str(value), Path.home()
    if home.parent == home:
        return text
    for spelling in dict.fromkeys((str(home), str(home).replace("\\", "/"), str(home).replace("\\", "\\\\"))):
        text = re.sub(r"(?<![\w./\\-])" + re.escape(spelling) + r"(?=[/\\]|$|[^\w.-])", "~", text, flags=re.IGNORECASE if is_windows() else 0)
    if is_windows():
        text = re.sub(r"(?<![\w.-])~(?:[/\\][^\s/\\\"'<>|;,()]*)+", lambda m: re.sub(r"\\+", "/", m.group(0)), text)
    return text


def spellings(path) -> set[str]:
    home, out = str(Path.home()), {str(path), os.path.realpath(path)}
    for p in list(out):
        if p.startswith(home):
            out |= {p.replace(home, alias, 1) for alias in ("~", "$HOME", "${HOME}")}
    return set().union(*map(windows_spellings, out)) if is_windows() else out


def secret_locations(p: dict) -> list[str]:
    places = set(spellings(CONFIG_DIR))
    for s in p["key_sources"]:
        kind, _, arg = s.partition(":")
        places |= spellings(arg) if kind == "file" else {arg} if kind == "keychain" else set()
    return sorted(places | {"deepcrew.member", "find-generic-password", *(["_run.py"] if is_windows() else [])})


def balance(p: dict, key: str) -> dict:
    url = p.get("balance_url")
    if not url:
        return {"ok": True, "detail": "provider has no balance endpoint"}
    if urlparse(url).scheme != "https":
        return {"ok": False, "status": None, "detail": "balance_url must be https"}
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT_S) as resp:
            raw = resp.read() or b"{}"
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("not an object")
    except ValueError:
        return {"ok": False, "status": 200, "detail": "the balance endpoint did not return a JSON object"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "detail": "key rejected" if e.code in (401, 403) else f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "status": None, "detail": f"unreachable: {getattr(e, 'reason', e)}"}
    infos = body.get("balance_infos") or []
    out = {"ok": bool(body.get("is_available", True)), "status": 200, "balances": {i.get("currency", "?"): i.get("total_balance") for i in infos}}
    if not out["ok"]:
        out["detail"] = "the balance is not enough for API calls"
    return out


QUESTIONS_SCHEMA = "deepcrew.questions.v1"


def can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
