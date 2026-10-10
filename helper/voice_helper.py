# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Voice helper: talk to an OpenAI realtime model through `codex app-server`
(ChatGPT login, no API key), with a local Edge page as the WebRTC audio endpoint.

    Edge page (mic/speaker, AEC, jitter buffer)  --SDP offer/answer via 127.0.0.1-->  this helper
    this helper  --stdio JSON-RPC-->  codex app-server  --(ChatGPT auth)-->  OpenAI realtime (AVAS)
    WebRTC media flows page <-> OpenAI directly; codex keeps a sideband control socket.

stdout protocol (one JSON object per line, read by the Claude Code plugin):
  {"type":"ready","port":N,"pid":P,"url":..,"threadId":..,"token":..}   token: per-run secret for HTTP
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
  {"type":"mcp_warning","name":..,"status":..}                  an MCP server started despite being disabled
  {"type":"heartbeat"} / {"type":"bye","reason":..}

HTTP (127.0.0.1 only). Every request must carry Host 127.0.0.1:<port> or localhost:<port>, and an
Origin, when sent, must be exactly http://127.0.0.1:<port> or http://localhost:<port>. Every path but
GET / (and /favicon.ico) needs the header X-Voice-Token: <token from the ready line>; the page gets
the token embedded by GET /. No CORS headers are ever sent, so other web pages cannot read it.
Anything refused gets 403.
  GET  /              the voice page        GET /health  {"ok":true,...}
  GET  /events?since=N   recent events for the page
  GET  /voices    voice list (listVoices, static fallback) + selected/active voice
  GET  /settings  <data dir>/settings.json   POST /settings {"voice":..}  remember the voice
  POST /offer   {"sdp":offer,"voice"?,"purpose"?:"preview"} -> {"ok":true,"sdp":answer}
                starts realtime (stops a running one first); voice is fixed for the session
  POST /stop    stop realtime               POST /say {"text":..,"mode":"text|speech"}  inject text
  POST /client-state {"state":..}  page reports RTCPeerConnection state
  POST /answer  {"id":..,"answer":..,"status":"ok|aborted|error|refusal|timeout|busy"}
                Claude's answer for a request -> trimmed -> thread/realtime/appendText, so the
                realtime model speaks a 1-2 sentence summary
  POST /shutdown  exit

Data (logs, settings.json with the voice and the persona, an empty working folder for codex) is
kept outside the plugin folder: --data-dir, else $VOICE_MOD_DATA_DIR, else
%LOCALAPPDATA%\claude-code-voice-mod (~/.local/share/claude-code-voice-mod elsewhere).

Run:  uv run --script helper/voice_helper.py [--open]          (what the plugin does)
Self test without microphone or browser (silent aiortc peer, sends one text):
      uv run --project helper --group selftest helper/voice_helper.py --selftest-text "..."
Handoff self test (text that should be handed off, then a fake answer is fed back):
      uv run --project helper --group selftest helper/voice_helper.py --selftest-handoff "..." \
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


def default_data_dir() -> str:
    """Per-user folder for logs and settings, outside the plugin folder (which Claude Code may
    watch and hot-reload, and which a plugin update may replace)."""
    env = os.environ.get("VOICE_MOD_DATA_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "claude-code-voice-mod")


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
# Names whose value is a secret in "name = value" / "name: value" (config files, env, JSON).
_SECRET_NAMES = (r"password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|"
                 r"client[_\-]?secret|authorization|credentials?")
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
    # name = value / name: value assignments (password, secret, token, api_key, ...): a quoted value
    # whole, else up to a space or separator; small numbers (max_tokens = 4096) are left alone
    (re.compile(r"(?i)(\b[\w.\-]*(?:" + _SECRET_NAMES + r")[\w.\-]*\b[\"']?\s*[:=]\s*)([\"'])(?!\[REDACTED\])"
                r"[^\"'\n]{3,}?\2"), r"\1\2[REDACTED]\2"),
    (re.compile(r"(?i)(\b[\w.\-]*(?:" + _SECRET_NAMES + r")[\w.\-]*\b[\"']?\s*[:=]\s*[\"']?)"
                r"(?!\[REDACTED\])(?!\d{1,6}\b)[^\s\"',;}]{3,}"), r"\1[REDACTED]"),
    (re.compile(r"((?:パスワード|暗証番号)\s*[:：=]\s*)\S+"), r"\1[REDACTED]"),
    # long hex strings (32+), e.g. API secrets, hashes
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])"), "[REDACTED]"),
]
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


def loggable(raw: str, limit: int = 4000) -> str:
    """Raw JSON for the log, with SDP bodies replaced by a size note (they hold ICE credentials)."""
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


class Log:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
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


def emit(obj: dict):
    """One JSON line to stdout (+ log + page event buffer). A broken pipe means the parent is gone."""
    global _seq
    line = json.dumps(obj, ensure_ascii=False)
    public = {k: v for k, v in obj.items() if k != "token"}  # the token goes to stdout (the Mod) only
    LOG.write("OUT", json.dumps(public, ensure_ascii=False) if "token" in obj else line)
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


def user_mcp_server_names(path: str = CODEX_CONFIG_PATH) -> list[str]:
    """Names under [mcp_servers] in ~/.codex/config.toml (read only, never written)."""
    try:
        import tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:  # unreadable config: codex itself will complain; we just skip
        LOG.write("WARN", f"could not read {path}: {e!r}")
        return []
    servers = data.get("mcp_servers")
    return sorted(servers.keys()) if isinstance(servers, dict) else []


def mcp_disable_overrides(names: list[str]) -> list[str]:
    """`-c mcp_servers.<name>.enabled=false` for each configured server (config/src/mcp_types.rs:
    RawMcpServerConfig.enabled). Only servers defined in config.toml: an override for an unknown
    name would create a server entry without a transport, which codex rejects."""
    out = []
    for name in names:
        if _SAFE_KEY.match(name):
            out.append(f"mcp_servers.{name}.enabled=false")
        else:
            LOG.write("WARN", f"MCP server name {name!r} not overridable from the CLI; left as is")
    return out


def enabled_mcp_server_names(config: dict) -> list[str]:
    """Servers that `config/read` (the effective config, every layer: system, user, profile, project)
    reports as not disabled."""
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(name for name, server in servers.items()
                  if not (isinstance(server, dict) and server.get("enabled") is False))


def thread_mcp_overrides(names: list[str]) -> dict:
    """thread/start `config` overrides that disable each named server for the helper's thread
    (dotted keys, like -c). A name with a dot cannot be addressed this way and is reported."""
    out = {}
    for name in names:
        if "." in name or not name:
            LOG.write("WARN", f"MCP server name {name!r} cannot be disabled by a thread override")
        else:
            out[f"mcp_servers.{name}.enabled"] = False
    return out


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
        self._session_gen = 0
        # handoffs from the realtime model -> requests for Claude Code
        self.requests: dict[str, dict] = {}  # handoff id -> {"text", "at", "answered"}
        self.recent_user: deque = deque(maxlen=4)  # last final user utterances (context for requests)
        self.assistant_seq = 0  # bumps on each final assistant transcript
        self.emit_requests = True
        self.mcp_warned: set = set()  # (name, status) already reported

    async def start(self):
        args = [self.exe, "app-server"]
        mcp = mcp_disable_overrides(user_mcp_server_names())
        LOG.write("INFO", f"user MCP servers disabled for this app-server: {mcp}")
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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
                    LOG.write("RPC<?", text[:2000])
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
            LOG.write("SREQ<", raw[:4000])
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
        """Every MCP server is disabled for this app-server; one that still starts is reported."""
        if (name, status) in self.mcp_warned:
            return
        self.mcp_warned.add((name, status))
        LOG.write("WARN", f"MCP server {name!r} {status} although MCP servers are disabled for the voice helper")
        emit({"type": "mcp_warning", "name": name, "status": status})

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
            LOG.write("WARN", f"handoff without id or text: {json.dumps(item, ensure_ascii=False)[:500]}")
            return
        if hid in self.requests:
            LOG.write("INFO", f"duplicate handoff {hid} ignored")
            return
        self.requests[hid] = {"text": text, "at": time.time(), "answered": False}
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
        req = self.requests.get(rid) or {"text": "", "at": time.time(), "answered": False}
        self.requests[rid] = req
        if not self.realtime_active:
            emit({"type": "answer_undelivered", "id": rid, "error": "realtime not running"})
            return {"ok": False, "error": "realtime not running"}
        # Claude's answer goes to the realtime model (OpenAI): secret-looking strings out first.
        msg = answer_message(status, req["text"], redact_secrets(answer))
        late = req["answered"]
        req["answered"] = True
        seq = self.assistant_seq
        await self.request("thread/realtime/appendText",
                           {"threadId": self.thread_id, "text": msg, "role": "user"}, timeout=20)
        emit({"type": "answer_delivered", "id": rid, "status": status, "chars": len(msg),
              "via": "appendText", "again": late})
        asyncio.create_task(self._ensure_spoken(rid, msg, seq))
        return {"ok": True, "chars": len(msg)}

    async def _ensure_spoken(self, rid: str, msg: str, seq: int):
        for _ in range(ANSWER_SPOKEN_CHECK_SECONDS * 2):
            await asyncio.sleep(0.5)
            if self.assistant_seq > seq or not self.realtime_active:
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
            "clientInfo": {"name": "claude_voice_mod", "title": "Claude Voice Mod", "version": "0.2.0"},
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
        asyncio.create_task(self.check_mcp_servers())
        LOG.write("INFO", f"thread {self.thread_id} model={res.get('model')} provider={res.get('modelProvider')} "
                          f"approval={res.get('approvalPolicy')} sandbox={json.dumps(res.get('sandbox'))}")
        await self.list_voices()
        return res

    async def mcp_thread_overrides(self) -> dict:
        """No MCP server may run for the helper. The -c overrides at spawn cover the servers in
        config.toml, and --disable apps/plugins the app and plugin ones; codex merges -c tables
        (rust-v0.160.0 config/src/merge.rs), so `mcp_servers={}` cannot empty the table. Any server
        the effective config (config/read: system, user, profile and project layers) still has
        enabled is disabled for the thread through thread/start `config`."""
        try:
            res = await self.request("config/read", {"cwd": CODEX_CWD}, timeout=30)
        except Exception as e:
            LOG.write("WARN", f"config/read failed ({e}); relying on the -c overrides only")
            return {}
        names = enabled_mcp_server_names((res or {}).get("config") or {})
        LOG.write("INFO", f"MCP servers still enabled after the -c overrides: {names}")
        return thread_mcp_overrides(names)

    async def check_mcp_servers(self):
        """After thread/start: reports any MCP server of the thread that is not disabled."""
        await asyncio.sleep(3)
        try:
            res = await self.request("mcpServerStatus/list", {"threadId": self.thread_id,
                                                              "detail": "toolsAndAuthOnly"}, timeout=30)
        except Exception as e:
            LOG.write("WARN", f"mcpServerStatus/list failed: {e}")
            return
        rows = (res or {}).get("data") or []
        states = {str(r.get("name")): r.get("runtimeStatus") for r in rows if isinstance(r, dict)}
        LOG.write("INFO", f"MCP servers for the voice thread: {states}")
        for name, status in states.items():
            if status not in ("disabled", None):
                self._on_mcp_status(name, str(status))

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
        if self.realtime_active:
            await self.stop_realtime()
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
        await self.request("thread/realtime/start", params, timeout=30)
        answer = await asyncio.wait_for(self._sdp_wait, SDP_TIMEOUT)
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
        if self._session_gen == gen and self.realtime_active and self.purpose == "preview":
            LOG.write("INFO", f"preview still running after {PREVIEW_MAX_SECONDS}s; stopping it")
            await self.stop_realtime()

    async def stop_realtime(self):
        if not self.thread_id:
            return
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
            try:
                self.proc.kill()
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


def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
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

        def _json(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8") or "{}")

        def _refusal(self, open_path: bool) -> str | None:
            """Why this request is refused (403), or None. Host must be this server (no DNS
            rebinding), an Origin must be this server's own, and every path but the page itself
            needs the per-run token."""
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
            if why:
                LOG.write("HTTP", f"403 GET {url.path}: {why}")
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
                                  "activeVoice": bridge.app.active_voice, "purpose": bridge.app.purpose})
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
                since = int((parse_qs(url.query).get("since") or ["0"])[0])
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
                self._reply(403, {"ok": False, "error": why})
                return
            bridge.last_activity = time.monotonic()
            try:
                data = self._json()
            except ValueError:
                self._reply(400, {"ok": False, "error": "body is not JSON"})
                return
            try:
                if path == "/offer":
                    purpose = "preview" if data.get("purpose") == "preview" else "conversation"
                    voice = data.get("voice") or bridge.voice
                    answer = bridge.run(bridge.app.start_realtime(
                        data["sdp"], data.get("version") or bridge.args.version, voice, purpose),
                        SDP_TIMEOUT + 40)
                    self._reply(200, {"ok": True, "sdp": answer, "voice": bridge.app.active_voice,
                                      "purpose": purpose})
                elif path == "/settings":
                    voice = data.get("voice")
                    voices = bridge.app.voice_options(bridge.args.version)[0]
                    if not isinstance(voice, str) or voice not in voices:
                        self._reply(400, {"ok": False, "error": f"unknown voice: {voice!r}", "voices": voices})
                        return
                    saved = save_settings({"voice": voice})
                    bridge.voice, bridge.voice_source = voice, "settings"
                    LOG.write("INFO", f"settings saved: {json.dumps(saved, ensure_ascii=False)}")
                    emit({"type": "voice", "event": "selected", "voice": voice, "source": "settings"})
                    self._reply(200, {"ok": True, "settings": saved, **bridge.voices_info()})
                elif path == "/label":
                    global LABEL
                    label = data.get("label")
                    if not isinstance(label, str) or not label.strip():
                        self._reply(400, {"ok": False, "error": "label is empty"})
                        return
                    LABEL = label.strip()[:60]
                    LOG.write("INFO", f"label set: {LABEL}")
                    emit({"type": "label", "label": LABEL})
                    # The realtime prompt is fixed for a running session: the new name reaches
                    # the voice model at the next Start.
                    self._reply(200, {"ok": True, "label": LABEL, "appliesToVoice": "next start"
                                      if bridge.app.realtime_active else "now"})
                elif path == "/stop":
                    bridge.run(bridge.app.stop_realtime(), 15)
                    self._reply(200, {"ok": True})
                elif path == "/say":
                    bridge.run(bridge.app.say(str(data.get("text", "")), data.get("mode", "text")), 20)
                    self._reply(200, {"ok": True})
                elif path == "/client-state":
                    state = str(data.get("state", ""))
                    if state in ("connected", "failed", "disconnected", "closed"):
                        mapped = {"connected": "media-connected", "failed": "media-failed"}.get(state, "media-" + state)
                        emit({"type": "realtime", "state": mapped, "by": "page"})
                    else:
                        LOG.write("PAGE", json.dumps(data, ensure_ascii=False)[:2000])
                    self._reply(200, {"ok": True})
                elif path == "/answer":
                    rid = str(data.get("id") or "")
                    if not rid:
                        self._reply(400, {"ok": False, "error": "id is required"})
                        return
                    answer = data.get("answer") if isinstance(data.get("answer"), str) else ""
                    status = str(data.get("status") or "ok")
                    os.makedirs(LOG_DIR, exist_ok=True)
                    with open(ANSWERS_PATH, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"receivedAt": datetime.now().isoformat(), "id": rid, "status": status,
                                            "chars": len(answer), "sha256": short_hash(answer),
                                            "turnId": data.get("turnId")},
                                           ensure_ascii=False) + "\n")
                    emit({"type": "ack", "id": rid, "status": status})
                    res = bridge.run(bridge.app.deliver_answer(rid, answer, status), 30)
                    self._reply(200 if res.get("ok") else 409, res)
                elif path == "/shutdown":
                    self._reply(200, {"ok": True})
                    bridge.loop.call_soon_threadsafe(_stop_cb, "shutdown requested")
                else:
                    self._reply(404, {"ok": False, "error": "not found"})
            except Exception as e:
                LOG.write("ERROR", f"{path}: {e!r}")
                self._reply(500, {"ok": False, "error": str(e) or type(e).__name__})

        def log_message(self, fmt, *args):
            if "/events" not in (args[0] if args else ""):
                LOG.write("HTTP", fmt % args)

    return Handler


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
        LOG.write("SELFTEST", f"append{mode} -> {args.selftest_text!r}")
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
            LOG.write("SELFTEST", f"datachannel {t}: {json.dumps(obj, ensure_ascii=False)[:1500]}")

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
            LOG.write("SELFTEST", f"appendText (user) -> {args.selftest_handoff!r}")
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
            LOG.write("SELFTEST", f"fake answer for {req['id']}: {fake!r}")
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

    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None), signal.SIGTERM):
        if sig is not None:
            signal.signal(sig, on_signal)

    exe, env = find_codex_exe(args.codex)
    app = AppServer(exe, env, _JobKiller(), interrupt_turns=not args.no_interrupt)
    server = None
    try:
        await app.start()
        await app.handshake()

        bridge = Bridge(loop, app, args)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(bridge))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/"
        emit({"type": "ready", "port": port, "pid": os.getpid(), "url": url, "threadId": app.thread_id,
              "log": LOG.path, "token": TOKEN})
        emit({"type": "voice", "event": "selected", "voice": bridge.voice, "source": bridge.voice_source})
        if args.open:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        async def watchdogs():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), HEARTBEAT_SECONDS)
                    return
                except asyncio.TimeoutError:
                    pass
                if app.exited.is_set():
                    stop("codex app-server exited")
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
    parser.add_argument("--open", action="store_true", help="open the voice page in Edge (app window)")
    parser.add_argument("--version", choices=["v1", "v3"], default="v3",
                        help="realtime version: v3 = gpt-live-1-codex (default), v1 = gpt-realtime-1.5")
    parser.add_argument("--voice", default=None,
                        help="voice name, e.g. juniper, maple, spruce (overrides settings.json)")
    parser.add_argument("--data-dir", default=None,
                        help="folder for logs and settings.json (default: $VOICE_MOD_DATA_DIR, else "
                             "%%LOCALAPPDATA%%\\claude-code-voice-mod)")
    parser.add_argument("--label", default="",
                        help="name of the Claude Code thread this page talks to (window title, voice prompt)")
    parser.add_argument("--codex", default=None, help="path to codex.exe (default: from PATH)")
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
    args = parser.parse_args()
    global LABEL, PERSONA
    if args.data_dir:
        set_data_dir(args.data_dir)
    LABEL = args.label.strip()[:60]
    LOG = Log()
    PERSONA = load_persona(load_settings())
    LOG.write("INFO", f"voice_helper start args={vars(args)}")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    finally:
        LOG.write("INFO", "voice_helper exit")
        LOG.close()


if __name__ == "__main__":
    main()
