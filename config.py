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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# gpt-realtime-2.1-mini = cheap ($10/$20 per 1M audio) reasoning voice model.
# Swap to "gpt-realtime-2.1" for top quality, or "gpt-realtime" for the GA stable alias.
MODEL = os.environ.get("JARVIS_MODEL", "gpt-realtime-2.1-mini")
VOICE = os.environ.get("JARVIS_VOICE", "cedar")  # cedar/marin/alloy/echo/shimmer...

# --- Audio -------------------------------------------------------------------
SAMPLE_RATE = 24000        # OpenAI Realtime uses 24 kHz mono pcm16 both directions
CHANNELS = 1
CHUNK_MS = 40              # mic frame size sent to the API
CHUNK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000

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
