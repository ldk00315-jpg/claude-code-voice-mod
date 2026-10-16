# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Voice helper: talk to an OpenAI realtime model through `codex app-server`
(ChatGPT login, no API key), with a local browser page as the WebRTC audio endpoint
(an Edge app window on Windows; on macOS, experimental, a Chrome/Edge/Brave/Chromium app window or
the default browser).

    browser page (mic/speaker, AEC, jitter buffer)  --SDP offer/answer via 127.0.0.1-->  this helper
    this helper  --stdio JSON-RPC-->  codex app-server  --(ChatGPT auth)-->  OpenAI realtime (AVAS)
    WebRTC media flows page <-> OpenAI directly; codex keeps a sideband control socket.

stdout protocol (one JSON object per line, read by the Claude Code plugin):
  {"type":"ready","port":N,"pid":P,"url":..,"threadId":..,"token":..,"pageUrl":..}
        token: per-run secret for HTTP; pageUrl: the page with its one-time key (both stdout only)
  {"type":"realtime","state":"connecting|started|connected|media-connected|media-failed|closed|error",...}
  {"type":"voice","event":"selected|active","voice":..,"source"|"purpose":..}  chosen / in-use voice
  {"type":"transcript","role":"user|assistant","text":..}       final text of one utterance
  {"type":"speech_started"}                                       user started talking (barge-in hint)
  {"type":"handoff","payload":{...}}                              realtime model asked Codex to do work
  {"type":"codex_turn","event":"started|interrupted|completed",...}
  {"type":"codex_item","itemType":..,"text":..}                    what Codex received (e.g. <realtime_delegation>)
  {"type":"request","id":..,"text":..,"userSaid":[..]}            work handed off by the realtime model,
                                                                  for the Mod to submit to Claude Code
  {"type":"answer_delivered","id":..,"status":..,"chars":N,"via":"appendText|appendSpeech"}
  {"type":"answer_undelivered","id":..,"error":..}                realtime not running, etc.
  {"type":"ack","id":..}                                          POST /answer received
  {"type":"mcp_blocked","reason":..}       MCP isolation failed or could not be confirmed: realtime
                                            stopped and refused (before ready: the helper exits)
  {"type":"page_url","pageUrl":..}         POST /reopen without --open (stdout only)
  {"type":"heartbeat"} / {"type":"bye","reason":..}

HTTP (127.0.0.1 only). Every request must carry Host 127.0.0.1:<port> or localhost:<port>, and an
Origin, when sent, must be exactly http://127.0.0.1:<port> or http://localhost:<port>. GET / needs
?k=<one-time key> (the helper opens the browser with it; it works once, then the page strips it from the
address bar); the page it serves carries the per-run token. Every other path but /favicon.ico needs
the header X-Voice-Token: <token>. No CORS headers are ever sent. Anything refused gets 403. Bodies
are JSON objects within a size limit (413), typed fields (400); reads time out after 10 s; at most 8
requests are handled at once (503). Processes of the same OS user are out of scope: they can read
the token from the plugin's process, the page, or the browser's memory.
  GET  /?k=..         the voice page        GET /health  {"ok":true,...}
  GET  /events?since=N   recent events for the page
  GET  /voices    voice list (listVoices, static fallback) + selected/active voice
  GET  /settings  <data dir>/settings.json   POST /settings {"voice":..}  remember the voice
  POST /offer   {"sdp":offer,"voice"?,"purpose"?:"preview"} -> {"ok":true,"sdp":answer}
                starts realtime (stops a running one first); voice is fixed for the session
  POST /stop    stop realtime               POST /say {"text":..,"mode":"text|speech"}  inject text
  POST /client-state {"state":..}  page reports RTCPeerConnection state
  POST /answer  {"id":..,"answer":..,"status":"ok|aborted|error|refusal|timeout|busy"}
                Claude's answer for a request -> trimmed -> thread/realtime/appendText, so the
                realtime model speaks a 1-2 sentence summary. Only ids of requests this helper
                emitted in the current realtime session: unknown 403, earlier session 409
  POST /reopen  a new one-time page link (opens the browser with --open, else a stdout page_url line)
  POST /shutdown  exit

Data (logs, settings.json with the voice and the persona, an empty working folder for codex) is
kept outside the plugin folder: --data-dir, else $VOICE_MOD_DATA_DIR, else
%LOCALAPPDATA%\claude-code-voice-mod on Windows, ~/Library/Application Support/claude-code-voice-mod
on macOS, ${XDG_STATE_HOME:-~/.local/state}/claude-code-voice-mod on other POSIX systems.

Child processes: codex never outlives the helper. Windows: a job object with KILL_ON_JOB_CLOSE.
POSIX (macOS experimental): codex runs in its own process group (start_new_session), which the
helper terminates (SIGTERM, then SIGKILL) when it exits, and a small watchdog process (this script
with --watch-group) kills the group when the helper's pipe to it closes, even if the helper was
killed hard.
The log keeps metadata only (states, errors, ids, lengths) unless --debug-log or settings.json
"debugLog": true; log files older than 7 days, and the oldest past 20 MB, are deleted at startup.

Run:  uv run --script helper/voice_helper.py [--open]          (what the plugin does)
Self test without microphone or browser (silent aiortc peer, sends one text):
      uv run --project helper --group selftest python helper/voice_helper.py --selftest-text "..."
Handoff self test (text that should be handed off, then a fake answer is fed back):
      uv run --project helper --group selftest python helper/voice_helper.py --selftest-handoff "..." \
             [--selftest-answer "..."]
"""

import argparse
import asyncio
import ctypes
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(HERE, "voice_page.html")


APP_DIR_NAME = "claude-code-voice-mod"


def default_data_dir(platform: str | None = None, environ=None) -> str:
    """Per-user folder for logs and settings, outside the plugin folder (which Claude Code may
    watch and hot-reload, and which a plugin update may replace).

    $VOICE_MOD_DATA_DIR wins everywhere. Windows: %LOCALAPPDATA%. macOS (experimental):
    ~/Library/Application Support. Other POSIX: $XDG_STATE_HOME (if absolute), else ~/.local/state."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    env = environ.get("VOICE_MOD_DATA_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    home = os.path.expanduser("~")
    if platform == "win32":
        base = environ.get("LOCALAPPDATA") or os.path.join(home, ".local", "share")
    elif platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        xdg = environ.get("XDG_STATE_HOME") or ""
        base = xdg if os.path.isabs(xdg) else os.path.join(home, ".local", "state")
    return os.path.join(base, APP_DIR_NAME)


def set_data_dir(path: str):
    global DATA_DIR, LOG_DIR, CODEX_CWD, ANSWERS_PATH, SETTINGS_PATH
    DATA_DIR = os.path.abspath(path)
    LOG_DIR = os.path.join(DATA_DIR, "logs")
    CODEX_CWD = os.path.join(DATA_DIR, "codex-cwd")  # empty folder: nothing for Codex to look at
    ANSWERS_PATH = os.path.join(LOG_DIR, "voice-answers.jsonl")  # metadata only (no answer text)
    SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")  # {"voice": ..., "persona": {...}}


set_data_dir(default_data_dir())
HEARTBEAT_SECONDS = 5
SDP_TIMEOUT = 45
PREVIEW_MAX_SECONDS = 40  # safety net: a preview session is stopped by the helper after this

# Fallback when thread/realtime/listVoices fails (codex 0.160.0 builtin list for v1/v3).
STATIC_VOICES = ["juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove"]
STATIC_DEFAULT_VOICE = "cove"

# ---------------------------------------------------------------- persona (settings.json "persona")
#
# Every field is optional; the defaults give a neutral, friendly assistant that answers in the
# language the user speaks. See settings.example.json and the README for an example character.
DEFAULT_PERSONA = {
    "name": "",          # what the assistant calls itself ("" = no name, "the voice assistant")
    "userName": "",      # how to address the user ("" = not by name)
    "language": "",      # e.g. "Japanese", "English" ("" = the language the user speaks)
    "style": "friendly, calm and natural; short spoken sentences, never a lecture",
    "fillers": [],       # short phrases to say right after handing work off
    "instructions": "",  # extra free text appended to the realtime prompt
    "previewText": "",   # what the voice preview asks the model to say
}
PERSONA = dict(DEFAULT_PERSONA)


def load_persona(settings: dict) -> dict:
    """settings.json "persona" merged over the defaults; wrong types are ignored."""
    persona = dict(DEFAULT_PERSONA)
    raw = settings.get("persona") if isinstance(settings, dict) else None
    if isinstance(raw, dict):
        for key, default in DEFAULT_PERSONA.items():
            value = raw.get(key)
            if isinstance(default, list):
                if isinstance(value, list):
                    persona[key] = [str(v).strip() for v in value if str(v).strip()][:6]
            elif isinstance(value, str):
                persona[key] = value.strip()
    return persona


def addressee(persona: dict | None = None) -> str:
    p = persona or PERSONA
    return p["userName"] or "the user"


def language_rule(persona: dict | None = None) -> str:
    p = persona or PERSONA
    if p["language"]:
        return f"Always speak {p['language']}."
    return ("Always answer in the language the user speaks to you (for example Japanese or English), "
            "and stay in that language.")


def preview_text(persona: dict | None = None) -> str:
    p = persona or PERSONA
    if p["previewText"]:
        return p["previewText"]
    text = "Introduce yourself in one short sentence, using this voice."
    return text + (f" Speak {p['language']}." if p["language"] else "")


def build_realtime_prompt(persona: dict | None = None, label: str = "") -> str:
    """Instructions for the realtime (voice) model. They replace codex's default backend prompt
    (codex-rs/prompts/templates/realtime/backend_prompt.md), so they have to say when to delegate."""
    p = persona or PERSONA
    who = f'You are "{p["name"]}", the voice front desk' if p["name"] else "You are the voice front desk"
    lines = [
        f"{who} of a coding assistant (Claude Code) that works on the user's computer.",
        f"You talk with {addressee(p)}." if p["userName"] else None,
        language_rule(p),
        f"Speaking style: {p['style']}." if p["style"] else None,
        "Keep every reply short and conversational: this is speech, not text.",
        "",
        "## Answer yourself",
        "- Greetings, small talk, feelings, and short questions that general knowledge can answer:"
        " answer yourself in one or two sentences.",
        "",
        "## Delegate to the backend",
        "- Behind you there is a backend (the worker). Everything on the computer is done by the backend;"
        " you cannot read files or run commands yourself, and never claim you did.",
        "- Anything that needs looking at, investigating or changing something on the computer (files, folders,"
        " code, programs, logs, settings, research, searching, checking, running, creating, fixing) must be"
        " delegated to the backend instead of answered. If unsure, delegate.",
        "- When you delegate, pass the full request as one concrete sentence without leaving anything out.",
        "- Right after delegating, say only one short filler"
        + (" such as " + " / ".join(f'"{f}"' for f in p["fillers"]) if p["fillers"]
           else ' (for example "Let me check that.")')
        + " and wait. Never guess or invent the result.",
        "- If asked how it is going while the backend works, say briefly that it is still working.",
        "",
        "## When results arrive",
        "- A message starting with [RESULT] is the backend's result: summarise it in one or two spoken"
        " sentences in your speaking style and speak right away.",
        "- Do not read out code, tables, paths or long lists; give the gist (saying the details are on the"
        " screen is fine).",
        "- A message starting with [FAILED] means the work did not succeed: say so briefly and ask"
        f" {addressee(p)} to check the screen or the keyboard.",
    ]
    if p["instructions"]:
        lines += ["", "## Additional instructions", p["instructions"]]
    if label:
        lines += ["", "## Connected thread",
                  f'- This voice front desk is connected to the Claude Code thread named "{label}".'
                  " If asked which thread it is, answer with this name."]
    return "\n".join(line for line in lines if line is not None) + "\n"


# Name of the Claude Code thread this helper serves (--label), shown in the page title so
# several voice windows can be told apart, and told to the realtime model.
LABEL = ""


def realtime_prompt():
    return build_realtime_prompt(PERSONA, LABEL)


# Per-run secret (set in amain): required as X-Voice-Token on every HTTP call but GET /.
TOKEN = ""

# One-time page links. GET / needs ?k=<key> with a key minted for this run (the helper opens Edge
# with it, /reopen mints a new one); a key works once, for PAGE_KEY_SECONDS, and is never logged.
# The page it serves carries TOKEN for every later call.
PAGE_KEY_SECONDS = 300
PAGE_KEYS_MAX = 4
_page_keys: dict[str, float] = {}  # key -> minted at (monotonic)
_page_keys_lock = threading.Lock()


def mint_page_key() -> str:
    key = secrets.token_urlsafe(32)
    with _page_keys_lock:
        now = time.monotonic()
        for k, at in list(_page_keys.items()):
            if now - at > PAGE_KEY_SECONDS:
                del _page_keys[k]
        while len(_page_keys) >= PAGE_KEYS_MAX:
            del _page_keys[min(_page_keys, key=_page_keys.get)]
        _page_keys[key] = now
    return key


def use_page_key(given: str) -> bool:
    """True once for a valid key (then it is gone); compared in constant time."""
    got = given.encode("utf-8")
    with _page_keys_lock:
        now = time.monotonic()
        for k, at in list(_page_keys.items()):
            if hmac.compare_digest(got, k.encode("utf-8")):
                del _page_keys[k]
                return now - at <= PAGE_KEY_SECONDS
    return False


def page_url(port: int, key: str) -> str:
    return f"http://127.0.0.1:{port}/?k={key}"


def page_with_token(page: bytes, token: str) -> bytes:
    """The voice page with the per-run token in its <meta name="voice-token">."""
    value = html.escape(token, quote=True).encode("utf-8")
    return page.replace(b'<meta name="voice-token" content="">',
                        b'<meta name="voice-token" content="' + value + b'">', 1)


def page_with_label(page):
    """The voice page with the thread name in front of its title and heading."""
    if not LABEL:
        return page
    tag = html.escape(f"[{LABEL}] ").encode("utf-8")
    return page.replace(b"<title>Voice Mod", b"<title>" + tag + b"Voice Mod", 1).replace(
        b"<h1>Voice Mod", b"<h1>" + tag + b"Voice Mod", 1)


# Codex (the backing thread) must not do any work: Claude Code is the worker. Every turn
# a handoff starts is interrupted at once, and these instructions are a second line.
CODEX_DEVELOPER_INSTRUCTIONS = (
    "You are a passive relay. Another agent (outside Codex) does all the work. Never run commands, "
    "never read or write files, never call any tool, never browse. Reply with the single word OK and nothing else."
)
CODEX_OVERRIDES = [
    'sandbox_mode="read-only"',
    'approval_policy="never"',
    'web_search="disabled"',
    'model_reasoning_effort="low"',
    "notify=[]",  # do not run the user's turn-ended notifier
]
# Feature keys from codex-rs/features/src/lib.rs (rust-v0.160.0). "apps" = the codex_apps MCP
# server, "plugins" = plugin-provided MCP servers/skills (cua_repl etc.). realtime is not touched.
CODEX_DISABLED_FEATURES = ["hooks", "multi_agent", "apps", "plugins", "computer_use", "browser_use",
                           "in_app_browser", "image_generation", "memories", "js_repl", "goals"]
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
CODEX_CONFIG_PATH = os.path.join(CODEX_HOME, "config.toml")  # read only, never written

ANSWER_MAX_CHARS = 2000  # what is sent to the realtime model (it summarises it to 1-2 sentences)
ANSWER_SPOKEN_CHECK_SECONDS = 12  # no assistant transcript by then -> one retry via appendSpeech


# ---------------------------------------------------------------- logging / stdout

# Secret-looking strings, replaced by [REDACTED] in Claude's answers before they go to the realtime
# model (OpenAI), and in everything written to the log. Best effort: it catches common formats,
# not every secret, and may also hide harmless long ids (commit hashes, for example).
_SECRET_RULES = [
    # PEM private keys: the whole block (or up to the end of the text when it is cut off)
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "[REDACTED]"),
    # credentials in URLs and connection strings: scheme://user:password@host
    (re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)[^\s/:@\"'<>]+:[^\s/@\"'<>]+@"), r"\1[REDACTED]@"),
    # Authorization: Bearer ... / Basic ...
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9\-._~+/]{8,}=*"), r"\1\2[REDACTED]"),
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED]"),
    # well-known key formats
    (re.compile(r"\b(?:sk|rk|pk)-(?:[A-Za-z0-9]+-)*[A-Za-z0-9_\-]{16,}"), "[REDACTED]"),  # sk-..., sk-proj-...
    (re.compile(r"\b(?:sk|rk|pk|whsec)_(?:live_|test_)?[A-Za-z0-9]{16,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),  # GitHub tokens
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),  # GitLab
    (re.compile(r"\bxox[a-z]-[A-Za-z0-9\-]{10,}"), "[REDACTED]"),  # Slack
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"), "[REDACTED]"),  # AWS key ids
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), "[REDACTED]"),  # Google API keys
    (re.compile(r"\b(?:npm|hf)_[A-Za-z0-9]{30,}"), "[REDACTED]"),
    # long hex strings (32+), e.g. API secrets, hashes
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])"), "[REDACTED]"),
]

# Secret-named assignments: "name = value", "name: value", "name=value" (env), '"name": value'
# (JSON), and prose ("the password is 123456"). For a secret name every value is hidden, short
# numbers included. A name is secret when one of its parts (split at _ - . and camelCase) is a
# short secret word, or when it contains a long one. Only the names in _HARMLESS_NAMES keep a
# numeric value (token counts and the like).
_SECRET_SUBSTRINGS = ("password", "passwd", "passphrase", "passcode", "secret", "token", "apikey",
                      "accesskey", "privatekey", "credential", "authorization")
_SECRET_PARTS = {"pwd", "pin", "pins", "otp"}
_HARMLESS_NAMES = {  # compared lower case with _ - . removed; numeric values only
    "maxtokens", "maxtoken", "maxoutputtokens", "maxcompletiontokens", "maxnewtokens", "maxinputtokens",
    "numtokens", "tokens", "totaltokens", "prompttokens", "completiontokens", "inputtokens",
    "outputtokens", "cachedtokens", "reasoningtokens", "tokencount", "tokenlimit", "tokenbudget",
    "tokenusage", "tokensused", "timeout", "port",
}
# Prose values that are words about the secret, not the secret ("the password is wrong").
_PROSE_WORDS = {
    "a", "an", "the", "not", "now", "still", "also", "too", "being", "only", "same", "different",
    "wrong", "correct", "incorrect", "invalid", "valid", "expired", "missing", "required", "needed",
    "set", "unset", "empty", "blank", "stored", "saved", "hidden", "changed", "updated", "rotated",
    "revoked", "encrypted", "hashed", "known", "unknown", "in", "on", "at", "for", "from", "used",
    "ok", "fine", "weak", "strong", "long", "short", "there", "here", "it", "this", "that", "no",
    "none", "null",
}
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9_])(?P<q>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)(?P=q)"
    r"(?P<sep>\s*(?:=>|[:=：＝])\s*|\s+(?:is|was|are)\s+)"
    r"(?=(?P<val>\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;}\])]+))",  # a lookahead: "id:token=x" finds token too
    re.I)
_JA_SECRET = re.compile(
    r"(?P<lead>(?:パスワード|暗証番号|パスコード|合言葉|ＰＩＮ|PIN)(?:コード)?\s*(?:は|が|[:：=＝])\s*)"
    r"(?P<val>「[^」\n]*」|[^\s、。，,]+)")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _name_parts(name: str) -> list[str]:
    return [p.lower() for p in re.split(r"[_.\-]+", _CAMEL.sub("_", name)) if p]


def is_secret_name(name: str) -> bool:
    """Whether "name = value" holds a secret (password, pin, api_key, clientSecret, ...)."""
    parts = _name_parts(name)
    fused = "".join(parts)
    return any(p in _SECRET_PARTS for p in parts) or any(w in fused for w in _SECRET_SUBSTRINGS)


def _secret_value(m: re.Match) -> bool:
    """Whether the value after this name/separator is to be hidden."""
    name, val, sep = m.group("name"), m.group("val"), m.group("sep")
    if not is_secret_name(name):
        return False
    bare = val.strip("\"'")
    if bare.startswith("[REDACTED"):
        return False
    if "".join(_name_parts(name)) in _HARMLESS_NAMES and re.fullmatch(r"-?\d+(\.\d+)?", bare):
        return False
    if sep.strip().lower() in ("is", "was", "are") and bare.lower().rstrip(".!?") in _PROSE_WORDS:
        return False
    return True


def _redact_assignments(text: str) -> str:
    out, pos = [], 0
    for m in _ASSIGNMENT.finditer(text):
        start, end = m.start("val"), m.end("val")
        if start < pos or not _secret_value(m):
            continue  # inside a value already hidden, or not a secret
        val = m.group("val")
        quote = val[0] if val[:1] in ("\"", "'") and len(val) >= 2 and val[-1] == val[0] else ""
        if not quote and len(val.rstrip(".!?:")) > 0:
            end = start + len(val.rstrip(".!?:"))  # keep a sentence's full stop
        out += [text[pos:start], f"{quote}[REDACTED]{quote}"]
        pos = end
    return "".join(out) + text[pos:]


def _ja_secret(m: re.Match) -> str:
    val = m.group("val")
    if val.strip("「」").startswith("[REDACTED"):
        return m.group(0)
    return m.group("lead") + ("「[REDACTED]」" if val.startswith("「") else "[REDACTED]")


_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/=_\-])")


def _long_token(m: re.Match) -> str:
    """A 32+ character base64/base64url run that looks random (digits, upper and lower case, many
    different characters): a secret. Words, paths and ordinary identifiers are kept."""
    s = m.group(0)
    if (any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s)
            and len(set(s)) >= 16):
        return "[REDACTED]"
    return s


def redact_secrets(text: str) -> str:
    """Replaces secret-looking strings with [REDACTED] (best effort, see _SECRET_RULES)."""
    if not text:
        return text
    for pattern, repl in _SECRET_RULES:
        text = pattern.sub(repl, text)
    text = _redact_assignments(text)
    text = _JA_SECRET.sub(_ja_secret, text)
    return _LONG_TOKEN.sub(_long_token, text)


def short_hash(text: str) -> str:
    """First 12 hex digits of the text's SHA-256: lets logs tell answers apart without their text."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


_SECRET_PATTERNS = [
    # SDP lines (also when the SDP sits inside JSON with escaped \r\n, hence the excluded backslash)
    (re.compile(r"(a=ice-pwd:)[^\s\\]+"), r"\1[REDACTED]"),
    (re.compile(r"(a=ice-ufrag:)[^\s\\]+"), r"\1[REDACTED]"),
    (re.compile(r"(a=fingerprint:[^\s\\]+ )[^\s\\]+"), r"\1[REDACTED]"),
    (re.compile(r"(a=candidate:[^\s\\]+ \d+ [^\s\\]+ \d+ )[^\s\\]+( \d+)"), r"\1[ip]\2"),
    (re.compile(r"(c=IN IP[46] )[^\s\\]+"), r"\1[ip]"),
]


def redact(text: str) -> str:
    """What the log gets: SDP credentials and addresses out, then secret-looking strings."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return redact_secrets(text)


def _strip_sdp(obj):
    if isinstance(obj, dict):
        return {k: (f"<sdp {len(v)} bytes, see SDP line>" if k == "sdp" and isinstance(v, str) else _strip_sdp(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_sdp(v) for v in obj]
    return obj


# Conversation logging is off unless --debug-log or settings.json "debugLog": true. Off, the log
# gets metadata only: states, errors, ids, names, numbers and the length of every other text.
DEBUG_LOG = False
_META_KEYS = {"type", "state", "event", "id", "role", "status", "via", "version", "voice", "purpose", "source",
              "method", "threadId", "turnId", "itemType", "phase", "outcome", "name", "runtimeStatus", "error",
              "message", "reason", "by", "url", "log", "realtimeSessionId", "responseId", "handoff_id",
              "item_id", "code", "model", "modelProvider", "kind", "detail", "cursor", "nextCursor",
              "approvalPolicy", "sandbox", "userAgent"}
_META_LISTS = {"keys", "voices", "v1", "v2", "media", "codecs"}


def metadata(obj, key: str = ""):
    """`obj` with every text replaced by its length, except the values of _META_KEYS (states,
    ids, errors, names). What the log keeps of a message when conversation logging is off."""
    if isinstance(obj, dict):
        return {k: metadata(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        if key in _META_LISTS and all(isinstance(v, str) for v in obj):
            return obj[:50]
        return [metadata(v, key) for v in obj]
    if isinstance(obj, str):
        if key in _META_KEYS:
            return obj[:300]
        return f"<{len(obj)} chars>"
    return obj


def loggable(raw: str, limit: int = 4000) -> str:
    """Raw JSON for the log. Debug logging: SDP bodies replaced by a size note (they hold ICE
    credentials). Otherwise only its metadata (see metadata())."""
    if not DEBUG_LOG:
        try:
            return json.dumps(metadata(json.loads(raw)), ensure_ascii=False)[:limit]
        except ValueError:
            return f"<{len(raw)} chars, not JSON>"
    if '"sdp"' in raw:
        try:
            raw = json.dumps(_strip_sdp(json.loads(raw)), ensure_ascii=False)
        except ValueError:
            pass
    return raw[:limit]


def sdp_summary(sdp: str) -> str:
    lines = sdp.splitlines()
    media = [l for l in lines if l.startswith("m=")]
    codecs = [l.split(" ", 1)[1] for l in lines if l.startswith("a=rtpmap:")][:6]
    cands = sum(1 for l in lines if l.startswith("a=candidate"))
    return f"{len(sdp)} bytes, media={media}, codecs={codecs}, candidates={cands}"


LOG_MAX_AGE_DAYS = 7
LOG_MAX_TOTAL_BYTES = 20 * 1024 * 1024
ANSWERS_ROTATE_BYTES = 1024 * 1024


def prune_logs(log_dir: str, max_age_days: float = LOG_MAX_AGE_DAYS,
               max_total: int = LOG_MAX_TOTAL_BYTES, now: float | None = None) -> list[str]:
    """Retention, applied at startup: rotates voice-answers.jsonl past 1 MB, deletes the helper's
    log files older than max_age_days, then the oldest ones until all of them fit in max_total."""
    removed = []
    now = time.time() if now is None else now
    answers = os.path.join(log_dir, "voice-answers.jsonl")
    try:
        if os.path.getsize(answers) > ANSWERS_ROTATE_BYTES:
            os.replace(answers, os.path.join(log_dir, "voice-answers.1.jsonl"))
    except OSError:
        pass
    files = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return removed
    for name in names:
        if not re.fullmatch(r"voice-.+\.(log|jsonl)", name):
            continue  # only the helper's own files
        path = os.path.join(log_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        files.append([st.st_mtime, st.st_size, path])
    files.sort()
    for f in list(files):
        if now - f[0] > max_age_days * 86400:
            try:
                os.remove(f[2])
                removed.append(f[2])
                files.remove(f)
            except OSError:
                pass
    total = sum(f[1] for f in files)
    for f in list(files):
        if total <= max_total:
            break
        try:
            os.remove(f[2])
            removed.append(f[2])
            total -= f[1]
        except OSError:
            pass
    return removed


class Log:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.pruned = prune_logs(LOG_DIR)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(LOG_DIR, f"voice-{stamp}.log")
        self._f = open(self.path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, kind: str, text: str):
        line = f"{datetime.now().isoformat(timespec='milliseconds')} {kind} {redact(text)}\n"
        with self._lock:
            try:
                self._f.write(line)
                self._f.flush()
            except ValueError:
                pass

    def close(self):
        with self._lock:
            self._f.close()


LOG: Log  # set in main()
_out_lock = threading.Lock()
_events = deque(maxlen=300)  # (seq, obj) for the page
_seq = 0
_stop_cb = None  # set by main: callable(reason)


# Fields that go to stdout (the Mod) only: never to the log or the page's event buffer.
_STDOUT_ONLY = ("token", "pageUrl")


def emit(obj: dict):
    """One JSON line to stdout (+ log + page event buffer). A broken pipe means the parent is gone."""
    global _seq
    line = json.dumps(obj, ensure_ascii=False)
    public = {k: v for k, v in obj.items() if k not in _STDOUT_ONLY}
    LOG.write("OUT", json.dumps(public if DEBUG_LOG else metadata(public), ensure_ascii=False))
    with _out_lock:
        _seq += 1
        if obj.get("type") != "heartbeat":
            _events.append((_seq, public))
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            try:  # avoid a second failure when the interpreter flushes stdout at exit
                sys.stdout = open(os.devnull, "w")
            except OSError:
                pass
            if _stop_cb:
                _stop_cb("stdout closed (parent gone)")


# ---------------------------------------------------------------- settings.json (remembered voice)

def _read_settings_quietly() -> dict:
    """settings.json before the log exists (for "debugLog")."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        LOG.write("WARN", f"settings.json unreadable: {e}")
        return {}


def save_settings(updates: dict) -> dict:
    data = load_settings()
    data.update(updates)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS_PATH)
    return data


# ---------------------------------------------------------------- codex surface: user MCP servers off

_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def user_mcp_server_names(path: str | None = None) -> list[str]:
    """Names under [mcp_servers] in $CODEX_HOME/config.toml (read only, never written)."""
    path = path or CODEX_CONFIG_PATH
    try:
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:  # unreadable config: codex itself complains; config/read decides later
        LOG.write("WARN", f"could not read {path}: {e!r}")
        return []
    servers = data.get("mcp_servers")
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def toml_key(name: str) -> str:
    """A TOML key for `name`: bare when it may be, else a quoted (basic string) key."""
    return name if _SAFE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def mcp_disable_overrides(names: list[str]) -> list[str]:
    """One `-c mcp_servers={<name>={enabled=false}, ...}` that disables every configured server
    (config/src/mcp_types.rs: RawMcpServerConfig.enabled).

    codex rust-v0.160.0 splits a -c key path at every dot and knows no quoted keys there
    (config/src/overrides.rs apply_toml_override), so `mcp_servers."corp.files".enabled` would
    address the wrong table. The value, however, is parsed as TOML (utils/cli/src/
    config_override.rs), where a quoted key may hold dots. Only one override: a second
    `mcp_servers=` replaces the first inside the CLI layer, which is then merged over config.toml
    (config/src/merge.rs), so each server keeps its transport. Only servers defined in
    config.toml: an entry for an unknown name would have no transport, which codex rejects."""
    if not names:
        return []
    body = ", ".join(f"{toml_key(n)}={{enabled=false}}" for n in names)
    return ["mcp_servers={" + body + "}"]


def all_mcp_server_names(config: dict) -> list[str]:
    """Every server in the effective config that `config/read` returned (system, user, profile,
    project layers and the -c overrides), whatever its state."""
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def enabled_mcp_server_names(config: dict) -> list[str]:
    """The servers of the effective config that are not disabled."""
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(name for name, server in servers.items()
                  if not (isinstance(server, dict) and server.get("enabled") is False))


def thread_mcp_overrides(names: list[str]) -> dict:
    """thread/start `config` that disables every named server for the helper's thread, as one
    nested object: {"mcp_servers": {"corp.files": {"enabled": false}, ...}}.

    The app-server applies these keys like -c overrides (rust-v0.160.0 app-server/src/
    config_manager.rs load_with_cli_overrides: chained after the process's -c overrides into one
    CLI layer). A nested object reaches names with dots, which a dotted key cannot; and because
    it replaces the CLI layer's own `mcp_servers` table, it must name every server, the ones
    the -c override already disabled included."""
    if not names:
        return {}
    return {"mcp_servers": {n: {"enabled": False} for n in names}}


class McpIsolationError(RuntimeError):
    """No realtime session may run: an MCP server could run for the helper's codex thread, or
    that could not be confirmed."""


# MCP server states (app-server-protocol v2 McpServerConnectionStatus) that are safe: disabled.
# Anything else (notStarted can still start, starting, connected, authenticationRequired, failed,
# cancelled, or no status at all) means the server was not disabled.
MCP_SAFE_STATES = {"disabled"}


# ---------------------------------------------------------------- answers -> realtime text

_FENCE = re.compile(r"```.*?(```|$)", re.S)


def trim_answer(answer: str, limit: int = ANSWER_MAX_CHARS) -> str:
    """Make Claude's answer fit for a spoken summary: code blocks out, markdown noise down,
    cut at a sentence/line end before `limit` characters."""
    text = _FENCE.sub("(code omitted)", answer or "")
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.M)  # table rows
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = max(cut.rfind(c) for c in ("\u3002", "\n", "\uff01", "\uff1f", ". ", "! ", "? "))
    if best > limit // 2:
        cut = cut[:best + 1]
    return cut.rstrip() + "\n(truncated; the full answer is on the screen)"


def answer_message(status: str, request_text: str, answer: str) -> str:
    """The text appended to the realtime conversation for one finished request."""
    req = (request_text or "").strip().replace("\n", " ")[:200]
    who = addressee()
    if status in ("ok", "late") and (answer or "").strip():
        return (f'[RESULT] The result for the request "{req}" has arrived. Summarise it in one or two spoken '
                f"sentences, in your speaking style and the language of the conversation, and tell {who} now."
                f"\n---\n{trim_answer(answer)}")
    reasons = {
        "aborted": "the work was stopped before it finished",
        "error": "an error occurred during the work",
        "refusal": "the backend declined this request",
        "timeout": "it took too long, so we stopped waiting (the work may still be running on the screen)",
        "busy": "the backend was busy with other work and could not take it",
        "ok": "the backend returned an empty answer",
    }
    reason = reasons.get(status, f"no result came back from the backend ({status})")
    detail = trim_answer(answer, 300) if (answer or "").strip() else ""
    return (f'[FAILED] Request "{req}": {reason}. Tell {who} briefly, in the language of the conversation, '
            f"and ask them to check the screen or the keyboard." + (f"\n---\n{detail}" if detail else ""))


# ---------------------------------------------------------------- Windows job object (no orphans)

class _JobKiller:
    """Puts the codex child in a job object with KILL_ON_JOB_CLOSE: if this helper dies for any
    reason (even hard-killed), Windows closes the handle and kills codex and its children."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RB", "WB", "OB")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._k32 = k32
        h = k32.CreateJobObjectW(None, None)
        if not h:
            return
        info = EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(h)
            return
        self.handle = h

    def add(self, pid: int) -> bool:
        if not self.handle:
            return False
        hp = self._k32.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not hp:
            return False
        ok = bool(self._k32.AssignProcessToJobObject(self.handle, hp))
        self._k32.CloseHandle(hp)
        return ok


# ---------------------------------------------------------------- POSIX process group (no orphans)
#
# macOS support is experimental: implemented and unit-tested with mocks, not run on a real Mac.
# Linux shares this path but is not a supported platform.

GROUP_KILL_GRACE = 2.0  # seconds between SIGTERM and SIGKILL for codex's process group


def child_spawn_options(name: str | None = None) -> dict:
    """Extra keyword arguments for starting codex. Windows: no console window (the job object
    owns the process). POSIX: a new session, so codex and its children form one process group
    that can be signalled as a whole and does not get the terminal's Ctrl+C."""
    if (os.name if name is None else name) == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_process_group(pgid: int, grace: float = GROUP_KILL_GRACE, killpg=None, sleep=time.sleep,
                       clock=time.monotonic) -> str:
    """SIGTERM to the whole group, then SIGKILL if anything is still in it after `grace` seconds.
    Returns "gone" (nothing was there), "terminated", "killed" or "denied"."""
    killpg = killpg or getattr(os, "killpg")
    sigkill = getattr(signal, "SIGKILL", 9)
    try:
        killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"
    deadline = clock() + grace
    while clock() < deadline:
        try:
            killpg(pgid, 0)  # anyone left in the group?
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            return "denied"
        sleep(0.05)
    try:
        killpg(pgid, sigkill)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "denied"
    return "killed"


def watchdog_argv(pgid: int, python: str | None = None, script: str | None = None) -> list[str]:
    """The watchdog is this script in a separate interpreter (-I: no site, no cwd on sys.path)."""
    return [python or sys.executable, "-I", script or os.path.abspath(__file__), "--watch-group", str(pgid)]


def watch_group(pgid: int, stdin=None, parent: int | None = None, getppid=os.getppid,
                kill=kill_process_group, poll: float = 1.0) -> str:
    """Body of the watchdog process: blocks until the helper's end of the pipe on our stdin
    closes (the helper exited, however it died) or our parent changes (re-parented to launchd
    or init), then kills codex's process group. Returns what kill_process_group returned."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    parent = getppid() if parent is None else parent
    done = threading.Event()

    def read_until_eof():
        try:
            while stdin.read(1):
                pass
        except (OSError, ValueError):
            pass
        done.set()

    threading.Thread(target=read_until_eof, daemon=True).start()
    while not done.wait(poll):
        if getppid() != parent:
            break
    return kill(pgid)


class _PosixGroup:
    """POSIX counterpart of _JobKiller: codex was started with start_new_session=True, so its pid
    is its process group id. add() starts the watchdog for that group; close() terminates the
    group (every exit path of the helper goes through AppServer.close) and lets the watchdog go."""

    def __init__(self, popen=subprocess.Popen, kill=kill_process_group):
        self._popen, self._kill = popen, kill
        self.pgid: int | None = None
        self.watchdog = None

    def add(self, pid: int) -> bool:
        self.pgid = pid
        try:
            self.watchdog = self._popen(watchdog_argv(pid), stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        close_fds=True, start_new_session=True)
        except OSError as e:
            LOG.write("WARN", f"codex watchdog not started: {type(e).__name__}")
            return False
        return True

    def close(self) -> str:
        """Terminates codex's group and stops the watchdog. Blocking (up to a few seconds)."""
        result = self._kill(self.pgid) if self.pgid else "none"
        wd, self.watchdog = self.watchdog, None
        if wd is not None:
            try:
                wd.stdin.close()  # the watchdog sees EOF, finds the group gone and exits
            except Exception:
                pass
            try:
                wd.wait(GROUP_KILL_GRACE + 3)
            except Exception:
                pass
        return result


def child_guard(name: str | None = None):
    """What keeps codex from outliving the helper on this platform."""
    return _JobKiller() if (os.name if name is None else name) == "nt" else _PosixGroup()


def find_codex_exe(explicit: str | None) -> tuple[str, dict]:
    """Prefer the native codex.exe (so the job object really owns it) over the npm .cmd shim."""
    env = dict(os.environ)
    if explicit:
        return explicit, env
    shim = shutil.which("codex")
    if shim:
        pkg = os.path.join(os.path.dirname(shim), "node_modules", "@openai", "codex")
        exe = os.path.join(pkg, "node_modules", "@openai", "codex-win32-x64", "vendor",
                           "x86_64-pc-windows-msvc", "bin", "codex.exe")
        if os.path.isfile(exe):
            env["CODEX_MANAGED_PACKAGE_ROOT"] = pkg  # what the npm shim sets
            env["CODEX_MANAGED_BY_NPM"] = "1"
            return exe, env
        return shim, env
    raise SystemExit("codex not found on PATH (use --codex PATH)")


# ---------------------------------------------------------------- codex app-server JSON-RPC client

class RpcError(Exception):
    pass


class AppServer:
    def __init__(self, exe: str, env: dict, jobs: _JobKiller, interrupt_turns: bool):
        self.exe, self.env, self.jobs = exe, env, jobs
        self.interrupt_turns = interrupt_turns
        self.proc = None
        self.thread_id = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._sdp_wait: asyncio.Future | None = None
        self.realtime_active = False
        self.exited = asyncio.Event()
        self._tasks = []
        self._delta_chars = {"user": 0, "assistant": 0}
        self._audio_deltas = 0
        self.voices_raw = None  # result of thread/realtime/listVoices (None = not available)
        self.active_voice = None  # voice of the running realtime session
        self.purpose = None  # "conversation" | "preview" while realtime runs
        self._session_gen = 0  # bumped by every realtime start and stop
        self._rt_lock = asyncio.Lock()  # start_realtime / stop_realtime run one at a time
        self._spoken_tasks: set[asyncio.Task] = set()  # pending _ensure_spoken re-sends
        self.mcp_blocked: str | None = None  # why no realtime session may run (MCP isolation)
        self.cli_mcp_names: list[str] = []  # servers the -c override disabled at spawn
        # handoffs from the realtime model -> requests for Claude Code
        self.requests: dict[str, dict] = {}  # handoff id -> {"text", "at", "answered", "gen", "emitted"}
        self.recent_user: deque = deque(maxlen=4)  # last final user utterances (context for requests)
        self.assistant_seq = 0  # bumps on each final assistant transcript
        self.emit_requests = True
        self.mcp_warned: set = set()  # (name, status) already reported

    async def start(self):
        args = [self.exe, "app-server"]
        self.cli_mcp_names = user_mcp_server_names()
        mcp = mcp_disable_overrides(self.cli_mcp_names)
        LOG.write("INFO", f"MCP servers in config.toml, disabled by -c for this app-server: {self.cli_mcp_names}")
        for o in CODEX_OVERRIDES + mcp:
            args += ["-c", o]
        for f in CODEX_DISABLED_FEATURES:
            args += ["--disable", f]
        os.makedirs(CODEX_CWD, exist_ok=True)
        env = dict(self.env)
        env.setdefault("RUST_LOG", "warn")
        LOG.write("SPAWN", " ".join(args[1:]))
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=CODEX_CWD, env=env, limit=32 * 1024 * 1024,
            **child_spawn_options())
        LOG.write("SPAWN", f"codex app-server pid={self.proc.pid} job={self.jobs.add(self.proc.pid)}")
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    # -- wire
    async def _send(self, obj: dict):
        data = json.dumps(obj, ensure_ascii=False)
        if obj.get("method") in ("thread/realtime/appendText", "thread/realtime/appendSpeech"):
            text = str((obj.get("params") or {}).get("text") or "")  # an answer or typed text: no body
            LOG.write("RPC>", f"{obj['method']} id={obj.get('id')} chars={len(text)} sha256={short_hash(text)}")
        else:
            LOG.write("RPC>", loggable(data))
        self.proc.stdin.write((data + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict | None, timeout: float = 60):
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"method": method, "id": rid, "params": params if params is not None else {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None):
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def _read_stderr(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = re.sub(r"\x1b\[[0-9;]*m", "", line.decode("utf-8", "replace").rstrip())
            LOG.write("CODEX-ERR", text[:2000])

    async def _read_stdout(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    LOG.write("RPC<?", text[:2000] if DEBUG_LOG else f"<{len(text)} chars, not JSON>")
                    continue
                try:
                    await self._dispatch(msg, text)
                except Exception as e:  # never let one bad message kill the reader
                    LOG.write("ERROR", f"dispatch failed: {e!r}")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError("codex app-server exited"))
            if self._sdp_wait and not self._sdp_wait.done():
                self._sdp_wait.set_exception(RpcError("codex app-server exited"))
            self.exited.set()

    async def _dispatch(self, msg: dict, raw: str):
        method = msg.get("method")
        if method is None and "id" in msg:  # response
            LOG.write("RPC<", loggable(raw))
            fut = self._pending.get(msg["id"])
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RpcError(json.dumps(msg["error"], ensure_ascii=False)))
                else:
                    fut.set_result(msg.get("result"))
            return
        if "id" in msg:  # server -> client request
            LOG.write("SREQ<", loggable(raw))
            await self._answer_server_request(msg)
            return
        await self._on_notification(method, msg.get("params") or {}, raw)

    async def _answer_server_request(self, msg: dict):
        method, rid = msg["method"], msg["id"]
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "denied"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        elif method == "currentTime/read":
            result = {"currentTimeAt": int(time.time())}
        else:
            emit({"type": "codex_request_refused", "method": method})
            await self._send({"id": rid, "error": {"code": -32601, "message": "not supported by the voice helper"}})
            return
        emit({"type": "codex_request_refused", "method": method, "result": result})
        await self._send({"id": rid, "result": result})

    async def _on_notification(self, method: str, p: dict, raw: str):
        quiet = method in ("thread/realtime/transcript/delta", "thread/realtime/item/transcript/delta",
                           "thread/realtime/outputAudio/delta")
        if method == "thread/realtime/outputAudio/delta":
            self._audio_deltas += 1
            if self._audio_deltas in (1, 100, 1000):
                LOG.write("RPC<", f"{method} (count={self._audio_deltas}, audio via codex, not WebRTC media?)")
            return
        if not quiet:
            LOG.write("RPC<", loggable(raw))

        if method == "mcpServer/startupStatus/updated":
            self._on_mcp_status(str(p.get("name") or "?"), str(p.get("status") or "?"))
        elif method == "thread/realtime/started":
            self.realtime_active = True
            emit({"type": "realtime", "state": "started", "version": p.get("version"),
                  "realtimeSessionId": p.get("realtimeSessionId")})
        elif method == "thread/realtime/sdp":
            sdp = p.get("sdp", "")
            LOG.write("SDP<", "answer " + sdp_summary(sdp))
            if self._sdp_wait and not self._sdp_wait.done():
                self._sdp_wait.set_result(sdp)
        elif method == "thread/realtime/error":
            message = p.get("message", "")
            emit({"type": "realtime", "state": "error", "message": message})
            if self._sdp_wait and not self._sdp_wait.done():
                self._sdp_wait.set_exception(RpcError(message))
        elif method == "thread/realtime/closed":
            self.realtime_active = False
            self.active_voice = None
            self.purpose = None
            emit({"type": "realtime", "state": "closed", "reason": p.get("reason")})
            if self._sdp_wait and not self._sdp_wait.done():
                self._sdp_wait.set_exception(RpcError(f"closed: {p.get('reason')}"))
        elif method == "thread/realtime/transcript/delta":
            role = p.get("role", "?")
            self._delta_chars[role] = self._delta_chars.get(role, 0) + len(p.get("delta", ""))
        elif method == "thread/realtime/transcript/done":
            text = p.get("text", "")
            if p.get("role") == "user" and text.strip():
                self.recent_user.append(text.strip())
            elif p.get("role") == "assistant":
                self.assistant_seq += 1
            emit({"type": "transcript", "role": p.get("role"), "text": text})
        elif method == "thread/realtime/itemAdded":
            item = p.get("item") or {}
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "handoff_request":
                emit({"type": "handoff", "payload": item})
                self._on_handoff(item)
            elif kind == "input_audio_buffer.speech_started":
                emit({"type": "speech_started"})
            elif kind == "response.cancelled":
                emit({"type": "response_cancelled", "responseId": item.get("response_id")})
            else:
                emit({"type": "realtime_item", "itemType": kind,
                      "keys": sorted(item.keys()) if isinstance(item, dict) else None})
        elif method in ("thread/realtime/item/started", "thread/realtime/item/completed"):
            item = p.get("item") or {}
            if item.get("type") != "transcriptSegment":
                emit({"type": "realtime_item", "phase": method.rsplit("/", 1)[1], "itemType": item.get("type"),
                      "outcome": item.get("outcome")})
        elif method == "turn/started":
            turn = p.get("turn") or {}
            tid = turn.get("id")
            emit({"type": "codex_turn", "event": "started", "turnId": tid})
            if self.interrupt_turns and tid:
                asyncio.create_task(self._interrupt(tid))
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            emit({"type": "codex_turn", "event": "completed", "turnId": turn.get("id"),
                  "status": turn.get("status")})
        elif method == "item/started":
            item = p.get("item") or {}
            if item.get("type") in ("userMessage", "agentMessage"):
                emit({"type": "codex_item", "itemType": item.get("type"), "turnId": p.get("turnId"),
                      "text": _item_text(item)[:4000]})
            elif item.get("type") not in ("reasoning",):
                emit({"type": "codex_item", "itemType": item.get("type"), "turnId": p.get("turnId")})

    def _on_mcp_status(self, name: str, status: str):
        """Every MCP server is disabled for this app-server: one that reports a startup status
        anyway (starting, ready, failed, cancelled) was not. No realtime session may run then."""
        if (name, status) in self.mcp_warned:
            return
        self.mcp_warned.add((name, status))
        self.block_realtime(f"MCP server {name!r} reported {status!r} although every MCP server is "
                            "disabled for the voice helper")

    def block_realtime(self, reason: str):
        """MCP isolation failed: stop the realtime session (if any) and refuse new ones."""
        first = self.mcp_blocked is None
        if first:
            self.mcp_blocked = reason
        LOG.write("ERROR", f"realtime blocked: {reason}")
        emit({"type": "mcp_blocked", "reason": reason})
        if self._sdp_wait and not self._sdp_wait.done():
            self._sdp_wait.set_exception(McpIsolationError(reason))
        if first and self.thread_id:
            # not awaited: this runs inside the stdout reader, which a stop waits on
            asyncio.get_running_loop().create_task(self.stop_realtime())

    def _on_handoff(self, item: dict):
        """One handoff_request (bespoke_event_handling.rs) -> one stdout "request" line.
        v3 (Frameless Bidi) fills input_transcript with the delegation's input_text (the realtime
        model's own wording of the task) and leaves active_transcript empty, so the last final
        user utterances are attached as context."""
        hid = str(item.get("handoff_id") or item.get("item_id") or "")
        text = (item.get("input_transcript") or "").strip()
        if not text:
            entries = item.get("active_transcript") or []
            text = "\n".join(f"{e.get('role')}: {e.get('text')}" for e in entries
                             if isinstance(e, dict) and e.get("text")).strip()
        if not hid or not text:
            LOG.write("WARN", f"handoff without id or text: {loggable(json.dumps(item, ensure_ascii=False), 500)}")
            return
        if hid in self.requests:
            LOG.write("INFO", f"duplicate handoff {hid} ignored")
            return
        self.requests[hid] = {"text": text, "at": time.time(), "answered": False, "gen": self._session_gen,
                              "emitted": self.emit_requests}
        if self.emit_requests:
            emit({"type": "request", "id": hid, "text": text, "userSaid": list(self.recent_user)})

    async def deliver_answer(self, rid: str, answer: str, status: str) -> dict:
        """Hand Claude's answer to the realtime model.

        codex 0.160.0 has no client call that completes one specific handoff: with
        clientManagedHandoffs=true, handoff_out()/handoff_complete() return early
        (core/src/realtime_conversation.rs) and the client is left with the append calls.
        For v3 both thread/realtime/appendText and appendSpeech become a
        `session.context.append` on the sideband socket (methods_common.rs; appendSpeech adds
        channel "speakable"). appendText is used first (the selftest showed it makes the model
        speak); if no assistant transcript follows, one retry goes through appendSpeech."""
        # Checked and sent under the start/stop lock: a realtime session that stops or restarts
        # meanwhile cannot receive an answer of the session the request belongs to.
        async with self._rt_lock:
            why = self.answer_refusal(rid)
            if why:
                return {"ok": False, "error": why, "refused": True}
            req = self.requests[rid]
            if not self.realtime_active:
                emit({"type": "answer_undelivered", "id": rid, "error": "realtime not running"})
                return {"ok": False, "error": "realtime not running"}
            # Claude's answer goes to the realtime model (OpenAI): secret-looking strings out first.
            msg = answer_message(status, req["text"], redact_secrets(answer))
            late = req["answered"]
            req["answered"] = True
            seq = self.assistant_seq
            gen = self._session_gen
            await self.request("thread/realtime/appendText",
                               {"threadId": self.thread_id, "text": msg, "role": "user"}, timeout=20)
            emit({"type": "answer_delivered", "id": rid, "status": status, "chars": len(msg),
                  "via": "appendText", "again": late})
            task = asyncio.create_task(self._ensure_spoken(rid, msg, seq, gen))
            self._spoken_tasks.add(task)
            task.add_done_callback(self._spoken_tasks.discard)
        return {"ok": True, "chars": len(msg)}

    def answer_refusal(self, rid: str) -> str | None:
        """Why an answer for `rid` is not taken (None: it is). Only a request this helper emitted
        in the current realtime session is answered; nothing is recorded for a refused id."""
        req = self.requests.get(rid)
        if not req or not req.get("emitted"):
            return "unknown request id"
        if req.get("gen") != self._session_gen:
            return "request belongs to an earlier realtime session"
        return None

    def _spoken_still_due(self, seq: int, gen: int) -> bool:
        """Whether a re-send of an answer given in realtime session `gen` may still go out."""
        return self._session_gen == gen and self.realtime_active and self.assistant_seq <= seq

    def _cancel_spoken_tasks(self):
        """Drops the pending re-sends (the realtime session stops or a new one starts)."""
        current = asyncio.current_task()
        for task in list(self._spoken_tasks):
            if task is not current:
                task.cancel()
        self._spoken_tasks.clear()

    async def _ensure_spoken(self, rid: str, msg: str, seq: int, gen: int):
        """One re-send via appendSpeech if no assistant transcript followed the answer. Only into
        the realtime session the answer was given in (`gen`): re-checked after every wait and,
        under the start/stop lock, right before sending; a stop or a new start also cancels it."""
        for _ in range(ANSWER_SPOKEN_CHECK_SECONDS * 2):
            await asyncio.sleep(0.5)
            if not self._spoken_still_due(seq, gen):
                return
        async with self._rt_lock:
            if not self._spoken_still_due(seq, gen):
                return
            LOG.write("WARN", f"no assistant transcript {ANSWER_SPOKEN_CHECK_SECONDS}s after answer {rid}; "
                              "retrying via appendSpeech")
            try:
                await self.request("thread/realtime/appendSpeech", {"threadId": self.thread_id, "text": msg},
                                   timeout=20)
                emit({"type": "answer_delivered", "id": rid, "chars": len(msg), "via": "appendSpeech",
                      "again": True})
            except Exception as e:
                emit({"type": "answer_undelivered", "id": rid, "error": f"appendSpeech: {e}"})

    async def _interrupt(self, turn_id: str):
        try:
            await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}, timeout=15)
            emit({"type": "codex_turn", "event": "interrupted", "turnId": turn_id})
        except Exception as e:
            emit({"type": "codex_turn", "event": "interrupt_failed", "turnId": turn_id, "error": str(e)})

    # -- high level
    async def handshake(self):
        init = await self.request("initialize", {
            "clientInfo": {"name": "claude_voice_mod", "title": "Claude Voice Mod", "version": "0.4.1"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        }, timeout=60)
        await self.notify("initialized")
        LOG.write("INFO", f"initialized userAgent={(init or {}).get('userAgent')}")
        params = {
            "cwd": CODEX_CWD,
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "ephemeral": True,
            "developerInstructions": CODEX_DEVELOPER_INSTRUCTIONS,
            "serviceName": "claude_voice_mod",
        }
        overrides = await self.mcp_thread_overrides()
        if overrides:
            params["config"] = overrides
        res = await self.request("thread/start", params, timeout=90)
        self.thread_id = res["thread"]["id"]
        LOG.write("INFO", f"thread {self.thread_id} model={res.get('model')} provider={res.get('modelProvider')} "
                          f"approval={res.get('approvalPolicy')} sandbox={json.dumps(res.get('sandbox'))}")
        await self.check_mcp_servers()
        await self.list_voices()
        return res

    async def mcp_thread_overrides(self) -> dict:
        """No MCP server may run for the helper. The -c override at spawn covers the servers in
        config.toml, and --disable apps/plugins the app and plugin ones. The effective config
        (config/read: system, user, profile and project layers) must then be readable, and every
        server it names is disabled again for the thread through thread/start `config`. If it
        cannot be read, the helper refuses to start (McpIsolationError)."""
        try:
            res = await self.request("config/read", {"cwd": CODEX_CWD}, timeout=30)
        except Exception as e:
            raise McpIsolationError(f"config/read failed ({e}); cannot confirm that MCP servers are off")
        config = res.get("config") if isinstance(res, dict) else None
        if not isinstance(config, dict):
            raise McpIsolationError("config/read returned no config; cannot confirm that MCP servers are off")
        names = sorted(set(all_mcp_server_names(config)) | set(self.cli_mcp_names))
        LOG.write("INFO", f"MCP servers in the effective config: {names}; "
                          f"not yet disabled: {enabled_mcp_server_names(config)}")
        return thread_mcp_overrides(names)

    async def mcp_server_states(self) -> dict:
        """name -> runtimeStatus of every MCP server of the helper's thread (all pages)."""
        states, cursor = {}, None
        for _ in range(50):
            params = {"threadId": self.thread_id, "detail": "toolsAndAuthOnly", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            res = await self.request("mcpServerStatus/list", params, timeout=30)
            if not isinstance(res, dict) or not isinstance(res.get("data"), list):
                raise McpIsolationError("mcpServerStatus/list returned no list")
            for r in res["data"]:
                if isinstance(r, dict):
                    states[str(r.get("name"))] = r.get("runtimeStatus")
            cursor = res.get("nextCursor")
            if not cursor:
                return states
        raise McpIsolationError("mcpServerStatus/list did not end")

    async def check_mcp_servers(self):
        """Required before the helper reports ready: the status list must be readable and every
        MCP server in it disabled. Otherwise McpIsolationError (the helper refuses to start)."""
        if self.mcp_blocked:
            raise McpIsolationError(self.mcp_blocked)
        try:
            states = await self.mcp_server_states()
        except McpIsolationError:
            raise
        except Exception as e:
            raise McpIsolationError(f"mcpServerStatus/list failed ({e}); cannot confirm that MCP servers are off")
        LOG.write("INFO", f"MCP servers for the voice thread: {states}")
        running = {n: st for n, st in states.items() if st not in MCP_SAFE_STATES}
        if running:
            raise McpIsolationError(f"MCP servers not disabled for the voice thread: {running}")
        if self.mcp_blocked:  # a startup notification arrived meanwhile
            raise McpIsolationError(self.mcp_blocked)

    async def list_voices(self) -> bool:
        try:
            res = await self.request("thread/realtime/listVoices", {}, timeout=20)
            LOG.write("INFO", f"voices {json.dumps(res, ensure_ascii=False)}")
            if isinstance(res, dict) and isinstance(res.get("voices"), dict):
                self.voices_raw = res["voices"]
                return True
        except Exception as e:
            LOG.write("WARN", f"listVoices failed: {e}")
        return False

    def voice_options(self, version: str | None) -> tuple[list, str, bool]:
        """(voices, default, from_codex) for a realtime version. v1 and v3 share the v1 list."""
        raw = self.voices_raw or {}
        key, dkey = ("v2", "defaultV2") if version == "v2" else ("v1", "defaultV1")
        voices = raw.get(key)
        if isinstance(voices, list) and voices and all(isinstance(v, str) for v in voices):
            default = raw.get(dkey) if raw.get(dkey) in voices else voices[-1]
            return list(voices), default, True
        return list(STATIC_VOICES), STATIC_DEFAULT_VOICE, False

    async def start_realtime(self, offer_sdp: str, version: str | None, voice: str | None,
                             purpose: str = "conversation") -> str:
        # The voice is part of the session config sent at start; codex 0.160.0 has no RPC to
        # change it mid-session, so a different voice always means a new realtime session.
        # One start or stop at a time (several HTTP threads may ask at once).
        async with self._rt_lock:
            if self.mcp_blocked:
                raise McpIsolationError(self.mcp_blocked)
            if self.realtime_active:
                await self._stop_realtime_locked()
            LOG.write("SDP>", "offer " + sdp_summary(offer_sdp))
            loop = asyncio.get_running_loop()
            self._sdp_wait = loop.create_future()
            params = {
                "threadId": self.thread_id,
                "outputModality": "audio",
                "transport": {"type": "webrtc", "sdp": offer_sdp},
                "prompt": realtime_prompt(),
                "includeStartupContext": False,
                "clientManagedHandoffs": True,
                # what the backing Codex model is told when realtime starts (default: realtime_start.md)
                "realtimeStartInstructions": CODEX_DEVELOPER_INSTRUCTIONS,
            }
            if version:
                params["version"] = version
            if voice:
                params["voice"] = voice
            emit({"type": "realtime", "state": "connecting", "version": version or "default", "voice": voice,
                  "purpose": purpose})
            self._session_gen += 1
            gen = self._session_gen
            self._cancel_spoken_tasks()  # nothing of an earlier session is re-sent into this one
            try:
                await self.request("thread/realtime/start", params, timeout=30)
                answer = await asyncio.wait_for(self._sdp_wait, SDP_TIMEOUT)
            except BaseException:
                await self._stop_realtime_locked()
                raise
            if self.mcp_blocked:
                await self._stop_realtime_locked()
                raise McpIsolationError(self.mcp_blocked)
            self.realtime_active = True
            self.active_voice = voice or self.voice_options(version)[1]
            self.purpose = purpose
            emit({"type": "realtime", "state": "connected", "by": "codex (SDP answer returned)"})
            emit({"type": "voice", "event": "active", "voice": self.active_voice, "purpose": purpose})
            if purpose == "preview":
                asyncio.create_task(self._preview_guard(gen))
            return answer

    async def _preview_guard(self, gen: int):
        """Stops a preview session if the page did not (page closed, reply never came, ...)."""
        await asyncio.sleep(PREVIEW_MAX_SECONDS)
        async with self._rt_lock:
            if self._session_gen == gen and self.realtime_active and self.purpose == "preview":
                LOG.write("INFO", f"preview still running after {PREVIEW_MAX_SECONDS}s; stopping it")
                await self._stop_realtime_locked()

    async def stop_realtime(self):
        async with self._rt_lock:
            await self._stop_realtime_locked()

    async def _stop_realtime_locked(self):
        if not self.thread_id:
            return
        self._session_gen += 1  # answers for requests of the stopped session are refused
        self._cancel_spoken_tasks()  # and their pending re-sends are dropped
        try:
            await self.request("thread/realtime/stop", {"threadId": self.thread_id}, timeout=10)
        except Exception as e:
            LOG.write("WARN", f"realtime stop: {e}")
        self.realtime_active = False
        self.active_voice = None
        self.purpose = None

    async def say(self, text: str, mode: str):
        if mode == "speech":
            await self.request("thread/realtime/appendSpeech", {"threadId": self.thread_id, "text": text})
        else:
            await self.request("thread/realtime/appendText",
                               {"threadId": self.thread_id, "text": text, "role": "user"})

    async def close(self):
        if not self.proc:
            return
        if self.realtime_active:
            await self.stop_realtime()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except asyncio.TimeoutError:
            LOG.write("INFO", "codex did not exit on stdin close; killing")
            if not isinstance(self.jobs, _PosixGroup):  # Windows: the job object owns the rest
                try:
                    self.proc.kill()
                    await asyncio.wait_for(self.proc.wait(), 3)
                except Exception:
                    pass
        if isinstance(self.jobs, _PosixGroup):
            # The whole group (codex and anything it started): SIGTERM, then SIGKILL. Also run
            # when codex itself exited, so no child of it is left behind.
            result = await asyncio.to_thread(self.jobs.close)
            LOG.write("INFO", f"codex process group: {result}")
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                pass
        LOG.write("INFO", f"codex app-server exited code={self.proc.returncode} "
                          f"transcriptDeltaChars={self._delta_chars} audioDeltas={self._audio_deltas}")


def _item_text(item: dict) -> str:
    if isinstance(item.get("text"), str):
        return item["text"]
    parts = []
    for c in item.get("content") or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "\n".join(parts)


# ---------------------------------------------------------------- HTTP server for the page and the Mod

class Bridge:
    """Lets the HTTP threads run coroutines on the asyncio loop."""

    def __init__(self, loop, app: AppServer, args):
        self.loop, self.app, self.args = loop, app, args
        self.last_activity = time.monotonic()
        # Voice for the next session: --voice > settings.json > codex default.
        voices, default, _ = app.voice_options(args.version)
        saved = load_settings().get("voice")
        if args.voice:
            self.voice, self.voice_source = args.voice, "cli"
            if args.voice not in voices:
                LOG.write("WARN", f"--voice {args.voice!r} is not in the voice list {voices}")
        elif isinstance(saved, str) and saved in voices:
            self.voice, self.voice_source = saved, "settings"
        else:
            if saved:
                LOG.write("WARN", f"settings.json voice {saved!r} is not in the voice list; using default")
            self.voice, self.voice_source = default, "default"

    def run(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def voices_info(self) -> dict:
        voices, default, from_codex = self.app.voice_options(self.args.version)
        return {"ok": True, "voices": voices, "default": default, "fromCodex": from_codex,
                "version": self.args.version, "selected": self.voice, "source": self.voice_source,
                "cliVoice": self.args.voice, "active": self.app.active_voice, "purpose": self.app.purpose,
                "previewText": preview_text(), "settingsPath": SETTINGS_PATH}


# HTTP limits: request bodies (bytes) per path, the socket read timeout, concurrent handlers.
BODY_LIMITS = {"/offer": 256 * 1024, "/answer": 128 * 1024}
BODY_LIMIT_DEFAULT = 16 * 1024
BODY_LIMIT_MAX = 1024 * 1024
HTTP_READ_TIMEOUT = 10
HTTP_MAX_HANDLERS = 8
ANSWER_STATUSES = re.compile(r"^[a-z_]{1,32}$")


class HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class LimitedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most HTTP_MAX_HANDLERS requests handled at once; one more
    gets 503 at once (nothing read)."""
    daemon_threads = True

    def __init__(self, address, handler, max_handlers: int = HTTP_MAX_HANDLERS):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_handlers)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Retry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        LOG.write("HTTP", f"connection error: {sys.exc_info()[1]!r}")  # a client gone, a timeout


def _field(data: dict, key: str, kind=str, required: bool = False, max_len: int = 0, default=None):
    """One typed field of a JSON body, or HttpError(400)."""
    value = data.get(key)
    if value is None:
        if required:
            raise HttpError(400, f"{key} is required")
        return default
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise HttpError(400, f"{key} must be {kind.__name__}")
    if max_len and isinstance(value, str) and len(value) > max_len:
        raise HttpError(400, f"{key} is too long")
    return value


_PAGE_KEY_IN_PATH = re.compile(r"([?&]k=)[^&\s\"]*")


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        timeout = HTTP_READ_TIMEOUT  # socket timeout for the request line, headers and body

        def _reply(self, code, obj=None, body=None, ctype="application/json; charset=utf-8"):
            if body is None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # No Access-Control-* headers, ever: other origins must not read any response.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, path: str) -> dict:
            """The request body as a JSON object, within the size limit for `path`."""
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                if self.headers.get("Transfer-Encoding"):
                    raise HttpError(411, "Content-Length is required")
                return {}
            if not raw_length.strip().isdigit():
                raise HttpError(400, "bad Content-Length")
            length = int(raw_length)
            limit = min(BODY_LIMITS.get(path, BODY_LIMIT_DEFAULT), BODY_LIMIT_MAX)
            if length > limit:
                raise HttpError(413, f"body over {limit} bytes")
            self._body_read = True
            raw = self.rfile.read(length) if length else b""
            if len(raw) != length:
                raise HttpError(400, "body shorter than Content-Length")
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError):
                raise HttpError(400, "body is not JSON")
            if not isinstance(data, dict):
                raise HttpError(400, "body must be a JSON object")
            return data

        def _discard_body(self):
            """Reads and drops a body that was not read (up to BODY_LIMIT_MAX), so the client
            gets the error reply rather than a connection reset; the connection is closed."""
            self.close_connection = True
            if getattr(self, "_body_read", False):
                return
            self._body_read = True
            raw_length = (self.headers.get("Content-Length") or "").strip()
            left = int(raw_length) if raw_length.isdigit() else 0
            if left > BODY_LIMIT_MAX:
                return
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:
                pass

        def _refusal(self, open_path: bool) -> str | None:
            """Why this request is refused (403), or None. Host must be this server (no DNS
            rebinding), an Origin must be this server's own, and every path but the page itself
            needs the per-run token (the page needs a one-time key instead)."""
            port = self.server.server_address[1]
            own = (f"127.0.0.1:{port}", f"localhost:{port}")
            if (self.headers.get("Host") or "").lower() not in own:
                return "host not allowed"
            origin = self.headers.get("Origin")
            if origin is not None and origin.lower() not in tuple(f"http://{h}" for h in own):
                return "origin not allowed"
            if open_path:
                return None
            got = (self.headers.get("X-Voice-Token") or "").encode("utf-8")
            if not TOKEN or not hmac.compare_digest(got, TOKEN.encode("utf-8")):
                return "missing or wrong X-Voice-Token"
            return None

        def do_GET(self):
            url = urlparse(self.path)
            why = self._refusal(open_path=url.path in ("/", "/favicon.ico"))
            if not why and url.path == "/":
                key = (parse_qs(url.query).get("k") or [""])[0]
                if not key or not use_page_key(key):
                    why = "page link missing, used or expired"
            if why:
                LOG.write("HTTP", f"403 GET {url.path}: {why}")
                if url.path == "/":
                    self._reply(403, body=PAGE_REFUSED, ctype="text/html; charset=utf-8")
                else:
                    self._reply(403, {"ok": False, "error": why})
                return
            bridge.last_activity = time.monotonic()
            if url.path == "/":
                with open(PAGE_PATH, "rb") as f:
                    page = page_with_token(page_with_label(f.read()), TOKEN)
                self._reply(200, body=page, ctype="text/html; charset=utf-8")
            elif url.path == "/favicon.ico":
                self._reply(204, body=b"", ctype="image/x-icon")
            elif url.path == "/health":
                self._reply(200, {"ok": True, "threadId": bridge.app.thread_id, "label": LABEL,
                                  "realtime": bridge.app.realtime_active, "voice": bridge.voice,
                                  "activeVoice": bridge.app.active_voice, "purpose": bridge.app.purpose,
                                  "blocked": bridge.app.mcp_blocked, "debugLog": DEBUG_LOG})
            elif url.path == "/voices":
                if bridge.app.voices_raw is None:  # handshake call failed: try once more, else static list
                    try:
                        bridge.run(bridge.app.list_voices(), 25)
                    except Exception as e:
                        LOG.write("WARN", f"/voices retry: {e!r}")
                self._reply(200, bridge.voices_info())
            elif url.path == "/settings":
                self._reply(200, {"ok": True, "path": SETTINGS_PATH, "settings": load_settings()})
            elif url.path == "/events":
                try:
                    since = int((parse_qs(url.query).get("since") or ["0"])[0])
                except ValueError:
                    self._reply(400, {"ok": False, "error": "since must be a number"})
                    return
                with _out_lock:
                    evs = [{"seq": s, **o} for s, o in _events if s > since]
                    last = _seq
                self._reply(200, {"events": evs, "last": last})
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            why = self._refusal(open_path=False)
            if why:
                LOG.write("HTTP", f"403 POST {path}: {why}")
                self._discard_body()
                self._reply(403, {"ok": False, "error": why})
                return
            bridge.last_activity = time.monotonic()
            try:
                data = self._json(path)
                self._post(path, data)
            except HttpError as e:
                LOG.write("HTTP", f"{e.code} POST {path}: {e}")
                self._discard_body()
                self._reply(e.code, {"ok": False, "error": str(e)})
            except McpIsolationError as e:
                self._reply(409, {"ok": False, "error": f"blocked: {e}", "blocked": True})
            except Exception as e:
                LOG.write("ERROR", f"{path}: {e!r}")
                self._reply(500, {"ok": False, "error": str(e) or type(e).__name__})

        def _post(self, path: str, data: dict):
            if path == "/offer":
                sdp = _field(data, "sdp", str, required=True, max_len=BODY_LIMITS["/offer"])
                purpose = "preview" if _field(data, "purpose", str, max_len=20) == "preview" else "conversation"
                voice = _field(data, "voice", str, max_len=64) or bridge.voice
                version = _field(data, "version", str, max_len=8) or bridge.args.version
                answer = bridge.run(bridge.app.start_realtime(sdp, version, voice, purpose), SDP_TIMEOUT + 40)
                self._reply(200, {"ok": True, "sdp": answer, "voice": bridge.app.active_voice,
                                  "purpose": purpose})
            elif path == "/settings":
                voice = _field(data, "voice", str, required=True, max_len=64)
                voices = bridge.app.voice_options(bridge.args.version)[0]
                if voice not in voices:
                    self._reply(400, {"ok": False, "error": f"unknown voice: {voice!r}", "voices": voices})
                    return
                saved = save_settings({"voice": voice})
                bridge.voice, bridge.voice_source = voice, "settings"
                LOG.write("INFO", f"settings saved: voice={voice!r} keys={sorted(saved)}")
                emit({"type": "voice", "event": "selected", "voice": voice, "source": "settings"})
                self._reply(200, {"ok": True, "settings": saved, **bridge.voices_info()})
            elif path == "/label":
                global LABEL
                label = _field(data, "label", str, required=True, max_len=200).strip()
                if not label:
                    self._reply(400, {"ok": False, "error": "label is empty"})
                    return
                LABEL = label[:60]
                LOG.write("INFO", f"label set ({len(LABEL)} chars)")
                emit({"type": "label", "label": LABEL})
                # The realtime prompt is fixed for a running session: the new name reaches
                # the voice model at the next Start.
                self._reply(200, {"ok": True, "label": LABEL, "appliesToVoice": "next start"
                                  if bridge.app.realtime_active else "now"})
            elif path == "/stop":
                bridge.run(bridge.app.stop_realtime(), 15)
                self._reply(200, {"ok": True})
            elif path == "/say":
                text = _field(data, "text", str, required=True, max_len=4000)
                mode = _field(data, "mode", str, max_len=10, default="text")
                if mode not in ("text", "speech"):
                    raise HttpError(400, "mode must be text or speech")
                if not bridge.app.realtime_active:
                    raise HttpError(409, "realtime not running")
                bridge.run(bridge.app.say(text, mode), 20)
                self._reply(200, {"ok": True})
            elif path == "/client-state":
                state = _field(data, "state", str, required=True, max_len=40)
                if state in ("connected", "failed", "disconnected", "closed"):
                    mapped = {"connected": "media-connected", "failed": "media-failed"}.get(state, "media-" + state)
                    emit({"type": "realtime", "state": mapped, "by": "page"})
                else:
                    LOG.write("PAGE", loggable(json.dumps(data, ensure_ascii=False), 2000))
                self._reply(200, {"ok": True})
            elif path == "/answer":
                rid = _field(data, "id", str, required=True, max_len=200)
                answer = _field(data, "answer", str, max_len=20000, default="")
                status = _field(data, "status", str, max_len=32, default="ok")
                if not ANSWER_STATUSES.match(status):
                    raise HttpError(400, "bad status")
                turn_id = _field(data, "turnId", str, max_len=200)
                refusal = bridge.run(_answer_refusal(bridge.app, rid), 10)
                if refusal:
                    # Only requests this helper emitted in the current realtime session: nothing
                    # is recorded for any other id.
                    LOG.write("HTTP", f"answer refused: {refusal}")
                    self._reply(403 if refusal == "unknown request id" else 409, {"ok": False, "error": refusal})
                    return
                os.makedirs(LOG_DIR, exist_ok=True)
                with open(ANSWERS_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"receivedAt": datetime.now().isoformat(), "id": rid, "status": status,
                                        "chars": len(answer), "sha256": short_hash(answer),
                                        "turnId": turn_id}, ensure_ascii=False) + "\n")
                emit({"type": "ack", "id": rid, "status": status})
                res = bridge.run(bridge.app.deliver_answer(rid, answer, status), 30)
                self._reply(200 if res.get("ok") else 409, res)
            elif path == "/reopen":
                key = mint_page_key()
                url = page_url(self.server.server_address[1], key)
                opened = bool(bridge.args.open)
                if opened:
                    open_page(url)
                else:
                    emit({"type": "page_url", "pageUrl": url})  # stdout only (never logged)
                self._reply(200, {"ok": True, "opened": opened})
            elif path == "/shutdown":
                self._reply(200, {"ok": True})
                bridge.loop.call_soon_threadsafe(_stop_cb, "shutdown requested")
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def log_message(self, fmt, *args):
            line = fmt % args
            if "/events" not in line:
                LOG.write("HTTP", _PAGE_KEY_IN_PATH.sub(r"\1[hidden]", line))

    return Handler


async def _answer_refusal(app: "AppServer", rid: str) -> str | None:
    return app.answer_refusal(rid)


PAGE_REFUSED = (
    "<!doctype html><meta charset=utf-8><title>Voice Mod</title>"
    "<body style='font:15px system-ui,sans-serif;margin:2em'>"
    "<h1>Voice Mod: this link is used up</h1><p>The voice page opens only through the one-time link "
    "the helper opens the browser with. Run <code>/talk page</code> in Claude Code to open it again.</p>"
).encode("utf-8")

# macOS (experimental): Chromium-based browsers that take --app=<url> (an app-style window), in
# order of preference. Without one, the default browser opens the page in a normal tab (Safari
# works for WebRTC but has no app mode).
MAC_APP_BROWSERS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
# Other POSIX systems (not supported, best effort): executables that take --app=<url>.
POSIX_APP_BROWSERS = ("google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable",
                      "brave-browser", "chromium", "chromium-browser")


def page_command(url: str, platform: str | None = None, isdir=None, which=None,
                 home: str | None = None) -> list[str]:
    """argv that opens the voice page (never run through a shell). The URL carries the one-time
    key: it is never logged."""
    platform = sys.platform if platform is None else platform
    isdir, which = isdir or os.path.isdir, which or shutil.which
    if platform == "win32":
        return ["cmd", "/c", "start", "", "msedge", f"--app={url}"]
    if platform == "darwin":
        home = os.path.expanduser("~") if home is None else home
        for app in MAC_APP_BROWSERS:
            for root in ("/Applications", os.path.join(home, "Applications")):
                path = f"{root}/{app}.app"
                if isdir(path):
                    return ["open", "-na", path, "--args", f"--app={url}"]
        return ["open", url]
    for name in POSIX_APP_BROWSERS:
        exe = which(name)
        if exe:
            return [exe, f"--app={url}"]
    return ["xdg-open", url]


def open_page(url: str, platform: str | None = None, popen=subprocess.Popen):
    """Opens the voice page: an Edge app window on Windows; on macOS (experimental) an app window
    of Chrome, Edge, Brave or Chromium if one is installed, else the default browser."""
    platform = sys.platform if platform is None else platform
    argv = page_command(url, platform)
    if platform == "win32":
        popen(argv, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    # Own session (not killed with the helper), and nothing on our stdout: it is the JSON protocol.
    try:
        popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True, start_new_session=True)
    except OSError as e:
        LOG.write("ERROR", f"could not open the voice page with {os.path.basename(argv[0])}: "
                           f"{type(e).__name__} errno={getattr(e, 'errno', None)}")


# ---------------------------------------------------------------- headless self test (no mic, no browser)

async def selftest(app: AppServer, args, voice: str | None):
    """Silent aiortc peer: proves SDP answer + media connection + one text turn. Sends no microphone audio."""
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack  # yields silence

    pc = RTCPeerConnection()
    pc.addTrack(AudioStreamTrack())
    dc = pc.createDataChannel("oai-events")
    dc_types: dict[str, int] = {}
    frames = {"n": 0}
    connected = asyncio.Event()

    @dc.on("message")
    def _on_dc(msg):
        try:
            t = json.loads(msg).get("type", "?")
        except Exception:
            t = "non-json"
        dc_types[t] = dc_types.get(t, 0) + 1

    @pc.on("connectionstatechange")
    async def _on_state():
        LOG.write("SELFTEST", f"pc.connectionState={pc.connectionState}")
        if pc.connectionState == "connected":
            connected.set()
            emit({"type": "realtime", "state": "media-connected", "by": "selftest"})

    @pc.on("track")
    def _on_track(track):
        async def drain():
            try:
                while True:
                    await track.recv()
                    frames["n"] += 1
            except Exception:
                pass
        asyncio.create_task(drain())

    await pc.setLocalDescription(await pc.createOffer())
    answer = await app.start_realtime(pc.localDescription.sdp, args.version, voice)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
    try:
        await asyncio.wait_for(connected.wait(), 20)
    except asyncio.TimeoutError:
        LOG.write("SELFTEST", "media did not connect within 20s")
    await asyncio.sleep(args.selftest_delay)
    got = asyncio.Event()
    seen_before = _seq

    def assistant_spoke():
        with _out_lock:
            return any(s > seen_before and o.get("type") == "transcript" and o.get("role") == "assistant"
                       for s, o in _events)

    for mode in ("text", "speech"):
        LOG.write("SELFTEST", f"append{mode} -> {args.selftest_text!r}" if DEBUG_LOG else f"append{mode}")
        try:
            await app.say(args.selftest_text, mode)
        except Exception as e:
            LOG.write("SELFTEST", f"append {mode} failed: {e}")
            continue
        for _ in range(int(args.selftest_wait * 2)):
            await asyncio.sleep(0.5)
            if assistant_spoke():
                got.set()
                break
        if got.is_set():
            break
    await asyncio.sleep(1)
    LOG.write("SELFTEST", f"inbound audio frames={frames['n']} datachannel event types={dc_types}")
    emit({"type": "selftest", "assistantTranscript": got.is_set(), "inboundAudioFrames": frames["n"],
          "dataChannelEventTypes": dc_types})
    await app.stop_realtime()
    await pc.close()


def _WavThenSilence(path: str):
    """aiortc audio track: silence, then (after play()) a 16-bit mono wav, then silence again."""
    import fractions
    import wave
    from aiortc.mediastreams import MediaStreamTrack
    from av import AudioFrame

    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise SystemExit("--selftest-audio needs a 16-bit mono wav")
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())

    class Track(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.pos = None
            self._start = None
            self._ts = 0

        def play(self):
            self.pos = 0

        async def recv(self):
            samples = rate // 50  # 20 ms
            if self._start is None:
                self._start = time.time()
            else:
                self._ts += samples
                await asyncio.sleep(max(0, self._start + self._ts / rate - time.time()))
            n = samples * 2
            chunk = b""
            if self.pos is not None and self.pos < len(pcm):
                chunk = pcm[self.pos:self.pos + n]
                self.pos += n
            chunk = chunk.ljust(n, b"\0")
            frame = AudioFrame(format="s16", layout="mono", samples=samples)
            frame.planes[0].update(chunk)
            frame.pts = self._ts
            frame.sample_rate = rate
            frame.time_base = fractions.Fraction(1, rate)
            return frame

    return Track()


async def selftest_handoff(app: AppServer, args, voice: str | None):
    """Headless handoff check: send a text that should be handed off, wait for the handoff
    (stdout "request"), then feed a fake Claude answer back like the Mod would, and wait for the
    realtime model's spoken summary (assistant transcript). No microphone, no browser."""
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import AudioStreamTrack

    pc = RTCPeerConnection()
    track = _WavThenSilence(args.selftest_audio) if args.selftest_audio else AudioStreamTrack()
    pc.addTrack(track)
    dc = pc.createDataChannel("oai-events")
    connected = asyncio.Event()
    dc_types: dict[str, int] = {}

    @dc.on("message")
    def _on_dc(msg):
        try:
            obj = json.loads(msg)
            t = obj.get("type", "?")
        except Exception:
            obj, t = None, "non-json"
        dc_types[t] = dc_types.get(t, 0) + 1
        if "delegation" in t or t in ("error", "session.context.appended"):
            LOG.write("SELFTEST", f"datachannel {t}: {loggable(json.dumps(obj, ensure_ascii=False), 1500)}")

    @pc.on("connectionstatechange")
    async def _on_state():
        if pc.connectionState == "connected":
            connected.set()

    @pc.on("track")
    def _on_track(track):
        async def drain():
            try:
                while True:
                    await track.recv()
            except Exception:
                pass
        asyncio.create_task(drain())

    def events_after(seq, kind, role=None):
        with _out_lock:
            return [o for s, o in _events if s > seq and o.get("type") == kind
                    and (role is None or o.get("role") == role)]

    await pc.setLocalDescription(await pc.createOffer())
    answer = await app.start_realtime(pc.localDescription.sdp, args.version, voice)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
    result = {"type": "selftest", "mode": "handoff"}
    try:
        await asyncio.wait_for(connected.wait(), 20)
        await asyncio.sleep(args.selftest_delay)
        seq0 = _seq
        if args.selftest_audio:
            # A real spoken request (a local TTS wav): delegation is driven by user audio turns;
            # text appended with appendText is session context and did not delegate (live test).
            LOG.write("SELFTEST", f"playing {args.selftest_audio} as the microphone")
            track.play()
        else:
            LOG.write("SELFTEST", f"appendText (user) -> {args.selftest_handoff!r}" if DEBUG_LOG else "appendText (user)")
            await app.say(args.selftest_handoff, "text")
        req = None
        for _ in range(int(args.selftest_wait * 2)):
            await asyncio.sleep(0.5)
            reqs = events_after(seq0, "request")
            if reqs:
                req = reqs[0]
                break
        result["request"] = req
        if req:
            await asyncio.sleep(4)  # let the filler finish, as a real Claude turn would take a while
            result["filler"] = [o.get("text") for o in events_after(seq0, "transcript", "assistant")]
            seq1 = _seq
            fake = args.selftest_answer
            LOG.write("SELFTEST", f"fake answer for {req['id']} ({len(fake)} chars)")
            await app.deliver_answer(req["id"], fake, "ok")
            # A filler that was already being spoken can finish right after the append (seen live:
            # its transcript/done arrived 0.3 s later), so collect until 6 s of quiet instead of
            # stopping at the first assistant transcript.
            spoken, last_change, count = [], time.monotonic(), 0
            deadline = time.monotonic() + args.selftest_wait + ANSWER_SPOKEN_CHECK_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                spoken = events_after(seq1, "transcript", "assistant")
                if len(spoken) != count:
                    count, last_change = len(spoken), time.monotonic()
                elif count >= 1 and time.monotonic() - last_change > 6:
                    break
            result["summary"] = [o.get("text") for o in spoken]
            result["delivered"] = [{k: o.get(k) for k in ("via", "chars")}
                                   for o in events_after(seq1, "answer_delivered")]
        else:
            result["assistant"] = [o.get("text") for o in events_after(seq0, "transcript", "assistant")]
        result["codexTurns"] = [o.get("event") for o in events_after(seq0, "codex_turn")]
        result["dataChannelEventTypes"] = dc_types
    except asyncio.TimeoutError:
        result["error"] = "media did not connect"
    emit(result)
    await app.stop_realtime()
    await pc.close()


# ---------------------------------------------------------------- main

def stop_signals(name: str | None = None) -> list:
    """Signals that end the helper cleanly. Windows: Ctrl+C, Ctrl+Break, SIGTERM (unchanged).
    POSIX: SIGINT, SIGTERM and SIGHUP (terminal or parent session closed)."""
    if (os.name if name is None else name) == "nt":
        sigs = (signal.SIGINT, getattr(signal, "SIGBREAK", None), signal.SIGTERM)
    else:
        sigs = (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None))
    return [s for s in sigs if s is not None]


def install_signal_handlers(handler, name: str | None = None, register=None) -> list:
    register = register or signal.signal
    sigs = stop_signals(name)
    for sig in sigs:
        register(sig, handler)
    return sigs


def parent_gone(parent_pid: int, name: str | None = None, getppid=None) -> bool:
    """POSIX: re-parented (to launchd/init) means the process that started us is gone, even when
    our stdout pipe is still held open by someone else. Windows keeps its existing checks only
    (a closed stdout at the next heartbeat)."""
    if (os.name if name is None else name) == "nt":
        return False
    return (getppid or os.getppid)() != parent_pid


async def amain(args):
    global _stop_cb, TOKEN
    TOKEN = secrets.token_urlsafe(32)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    reason = {"text": "unknown"}

    def stop(why):
        if not stop_event.is_set():
            reason["text"] = why
            stop_event.set()

    _stop_cb = lambda why: loop.call_soon_threadsafe(stop, why)  # noqa: E731

    def on_signal(signum, _frame):
        _stop_cb(f"signal {signum}")

    install_signal_handlers(on_signal)
    parent_pid = os.getppid()

    exe, env = find_codex_exe(args.codex)
    app = AppServer(exe, env, child_guard(), interrupt_turns=not args.no_interrupt)
    server = None
    try:
        await app.start()
        await app.handshake()

        bridge = Bridge(loop, app, args)
        server = LimitedHTTPServer(("127.0.0.1", args.port), make_handler(bridge))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/"
        first = page_url(port, mint_page_key())
        # token and pageUrl go to stdout (the Mod) only: emit() keeps them out of the log and the
        # page's event buffer.
        emit({"type": "ready", "port": port, "pid": os.getpid(), "url": url, "threadId": app.thread_id,
              "log": LOG.path, "debugLog": DEBUG_LOG, "token": TOKEN, "pageUrl": first})
        emit({"type": "voice", "event": "selected", "voice": bridge.voice, "source": bridge.voice_source})
        if args.open:
            open_page(first)

        async def watchdogs():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), HEARTBEAT_SECONDS)
                    return
                except asyncio.TimeoutError:
                    pass
                if app.exited.is_set():
                    stop("codex app-server exited")
                elif parent_gone(parent_pid):
                    stop("parent gone")
                elif time.monotonic() - bridge.last_activity > args.idle:
                    stop(f"idle {args.idle}s")
                else:
                    emit({"type": "heartbeat"})

        wd = asyncio.create_task(watchdogs())
        if args.selftest_handoff:
            try:
                await selftest_handoff(app, args, bridge.voice)
            except Exception as e:
                LOG.write("SELFTEST", f"failed: {e!r}")
                emit({"type": "selftest", "error": str(e)})
            stop("selftest finished")
        elif args.selftest_text:
            try:
                await selftest(app, args, bridge.voice)
            except Exception as e:
                LOG.write("SELFTEST", f"failed: {e!r}")
                emit({"type": "selftest", "error": str(e)})
            stop("selftest finished")
        await stop_event.wait()
        wd.cancel()
    except McpIsolationError as e:
        # Refuse to start: no ready line, no page, no realtime session.
        LOG.write("ERROR", f"MCP isolation not confirmed: {e}")
        emit({"type": "mcp_blocked", "reason": str(e)})
        reason["text"] = f"refused: {e}"
    except Exception as e:
        LOG.write("ERROR", f"fatal: {e!r}")
        emit({"type": "realtime", "state": "error", "message": str(e)})
        reason["text"] = f"error: {e}"
    finally:
        emit({"type": "bye", "reason": reason["text"]})
        if server:
            server.shutdown()
        await app.close()


def main():
    global LOG
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0, help="HTTP port on 127.0.0.1 (0 = any free port)")
    parser.add_argument("--open", action="store_true", help="open the voice page (Windows: Edge app window; macOS: "
                             "Chrome/Edge/Brave/Chromium app window, else the default browser)")
    parser.add_argument("--version", choices=["v1", "v3"], default="v3",
                        help="realtime version: v3 = gpt-live-1-codex (default), v1 = gpt-realtime-1.5")
    parser.add_argument("--voice", default=None,
                        help="voice name, e.g. juniper, maple, spruce (overrides settings.json)")
    parser.add_argument("--data-dir", default=None,
                        help="folder for logs and settings.json (default: $VOICE_MOD_DATA_DIR, else "
                             "%%LOCALAPPDATA%%\\claude-code-voice-mod on Windows, ~/Library/Application "
                             "Support/claude-code-voice-mod on macOS)")
    parser.add_argument("--label", default="",
                        help="name of the Claude Code thread this page talks to (window title, voice prompt)")
    parser.add_argument("--codex", default=None, help="path to codex (default: from PATH)")
    parser.add_argument("--idle", type=int, default=1800, help="exit after N seconds without HTTP requests")
    parser.add_argument("--no-interrupt", action="store_true",
                        help="do not interrupt Codex turns started by handoffs (debug only)")
    parser.add_argument("--selftest-text", default=None, help="headless test: send this text, then exit")
    parser.add_argument("--selftest-wait", type=float, default=15, help="seconds to wait for a reply per try")
    parser.add_argument("--selftest-delay", type=float, default=2.0,
                        help="seconds between media connected and the first appendText in self tests")
    parser.add_argument("--selftest-handoff", default=None,
                        help="headless handoff test: send this text (should be handed off), then a fake answer")
    parser.add_argument("--selftest-audio", default=None,
                        help="with --selftest-handoff: play this 16-bit mono wav as the microphone instead of "
                             "sending the text")
    parser.add_argument("--selftest-answer",
                        default="The folder holds two subfolders, helper and hooks, and a README file.",
                        help="fake Claude answer used by --selftest-handoff")
    parser.add_argument("--debug-log", action="store_true",
                        help="also log conversation text (transcripts, requests; redacted). Default: metadata "
                             "only. Same as \"debugLog\": true in settings.json")
    parser.add_argument("--watch-group", type=int, default=None, help=argparse.SUPPRESS)  # POSIX watchdog
    args = parser.parse_args()
    if args.watch_group is not None:
        # The watchdog process (see _PosixGroup): no log, no data dir; it kills codex's process
        # group when the helper's pipe to it closes.
        watch_group(args.watch_group)
        return
    global LABEL, PERSONA, DEBUG_LOG
    if args.data_dir:
        set_data_dir(args.data_dir)
    LABEL = args.label.strip()[:60]
    DEBUG_LOG = bool(args.debug_log or _read_settings_quietly().get("debugLog") is True)
    LOG = Log()
    PERSONA = load_persona(load_settings())
    shown = {k: (v if DEBUG_LOG or not isinstance(v, str) or k in ("version", "voice", "codex", "data_dir")
                 else f"<{len(v)} chars>") for k, v in vars(args).items()}
    LOG.write("INFO", f"voice_helper start args={shown} debugLog={DEBUG_LOG} logsPruned={len(LOG.pruned)}")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    finally:
        LOG.write("INFO", "voice_helper exit")
        LOG.close()


if __name__ == "__main__":
    main()
