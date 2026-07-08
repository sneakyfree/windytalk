"""Windy Jarvis — central config. Everything here is overridable via env or .env."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_dotenv():
    """Tiny .env loader (no external dep). Values already in the environment win."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()

# --- Brain (voice model) -----------------------------------------------------
# Which brain to start with. Any key in providers.REGISTRY: "local", "gemini", "openai".
# Default "local" = free, on the Veron-1-5090 GPU box.
PROVIDER = os.environ.get("JARVIS_PROVIDER", "local")

# Hands-free wake word: wait for "Hey Jarvis" before listening (also --wake flag).
WAKE = os.environ.get("JARVIS_WAKE", "0") == "1"

# Brain server endpoint. Default = the public, license-gated Veron-5090 endpoint
# (works anywhere over the internet). For lowest latency on the LAN/fleet, set
# JARVIS_LOCAL_URL=ws://localhost:8765 and run.sh will open the SSH tunnel instead.
LOCAL_SERVER_URL = os.environ.get("JARVIS_LOCAL_URL", "wss://jarvis.thewindstorm.uk")

# License key for this copy (Grant issues these; blank = fine while the server has
# no licenses.json, i.e. gating disabled). The server can lock/expire it remotely.
LICENSE = os.environ.get("JARVIS_LICENSE", "")

# OpenAI Realtime
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("JARVIS_OPENAI_MODEL", "gpt-realtime-2.1-mini")
OPENAI_VOICE = os.environ.get("JARVIS_OPENAI_VOICE", "cedar")

# Google Gemini Live (free key: https://aistudio.google.com/apikey)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("JARVIS_GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
GEMINI_VOICE = os.environ.get("JARVIS_GEMINI_VOICE", "Puck")

# --- Audio -------------------------------------------------------------------
CHANNELS = 1
CHUNK_MS = 40             # mic frame size (ms) streamed to the brain

# --- Hands (Linux desktop control) ------------------------------------------
YDOTOOL_SOCKET = os.environ.get("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")
# Guarded shell: refuse commands matching these substrings unless JARVIS_ALLOW_DANGEROUS=1
SHELL_DENYLIST = ["rm -rf", "mkfs", "dd if=", ":(){", "shutdown", "reboot",
                  "> /dev/sd", "chmod -R 000", "sudo rm", "--no-preserve-root"]
ALLOW_DANGEROUS = os.environ.get("JARVIS_ALLOW_DANGEROUS", "0") == "1"

# --- Persona -----------------------------------------------------------------
SYSTEM_PROMPT = os.environ.get("JARVIS_PROMPT", """\
You are Windy, a fast, friendly voice assistant that operates this Fedora Linux \
computer for Grant. You can hear him and speak back naturally. When he asks you to \
DO something on the machine — open an app, search the web, type text, press keys, \
take a screenshot, read what's on screen, run a command — call the matching tool. \
Keep spoken replies short and conversational; don't read long output aloud unless \
asked. Confirm briefly after you act ("Opening Firefox", "Done"). If a request is \
ambiguous or looks destructive, ask before acting. You are talking out loud, so \
never spell out URLs or code character-by-character unless asked.""")
