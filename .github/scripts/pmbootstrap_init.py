#!/usr/bin/env python3
"""
pmbootstrap_init.py — Drive `pmbootstrap init` non-interactively.

The device is fixed to xiaomi-polaris (per user requirement).
All other init choices are read from environment variables, which
are populated from GitHub Actions workflow_dispatch inputs.

Chinese font packages are ALWAYS added to the extra packages list.

Implementation
--------------
Uses subprocess with stdin/stdout pipes (NOT a PTY). This avoids
all the PTY echo, ANSI code, and pexpect buffering issues that
plagued previous versions.

Strategy:
  1. Pre-write ~/.config/pmbootstrap_v3.cfg with our desired values.
     pmbootstrap will use these as defaults for the prompts.
  2. Run `pmbootstrap init` with stdin=PIPE, stdout=PIPE.
  3. Read pmbootstrap's output until we see a prompt (line ending with ": ").
  4. Send the appropriate answer.
  5. Repeat until pmbootstrap exits.

For the conditional "Enable this package?" prompt (only appears for
some UIs), we detect it by reading the output and checking the text.
"""

import configparser
import os
import re
import subprocess
import sys
import select
import time
from pathlib import Path


# ---------------------------------------------------------------------
# Read configuration from environment.
# ---------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


channel        = _env("PMOS_CHANNEL",     "edge")
audio          = _env("PMOS_AUDIO",       "pipewire")
wifi           = _env("PMOS_WIFI",        "iwd")
usb_mode       = _env("PMOS_USB",         "developer")
ui             = _env("PMOS_UI",          "plasma-mobile")
systemd        = _env("PMOS_SYSTEMD",     "always")
ui_extra_raw   = _env("PMOS_UI_EXTRA",    "true").lower()
ui_extra       = "y" if ui_extra_raw in ("true", "1", "yes", "y") else "n"
extra_packages = _env("PMOS_EXTRA_PACKAGES", "")

VENDOR = "xiaomi"
DEVICE = "polaris"

LOCALE   = "en_US"
TIMEZONE = "Asia/Jakarta"
HOSTNAME = "xiaomi-polaris"

INTERNATIONAL_FONTS = [
    "font-noto",             # Noto fonts for international languages
    "font-noto-emoji",       # Emoji support
]


def build_extra_packages() -> str:
    extras: list[str] = []
    if extra_packages and extra_packages.lower() != "none":
        for p in extra_packages.split(","):
            p = p.strip()
            if p and p not in extras:
                extras.append(p)
    for font in INTERNATIONAL_FONTS:
        if font not in extras:
            extras.append(font)
    return ",".join(extras) if extras else "none"


extras_str = build_extra_packages()

print("=" * 60, flush=True)
print("pmbootstrap init — non-interactive driver (pipe-based)", flush=True)
print("=" * 60, flush=True)
print(f"  Vendor:           {VENDOR}  (FIXED)")
print(f"  Device:           {DEVICE}  (FIXED)")
print(f"  Channel:          {channel}")
print(f"  Audio backend:    {audio}")
print(f"  WiFi backend:     {wifi}")
print(f"  USB mode:         {usb_mode}")
print(f"  User interface:   {ui}")
print(f"  UI extra pkgs:    {ui_extra}")
print(f"  systemd:          {systemd}")
print(f"  Extra packages:   {extras_str}")
print(f"  Locale:           {LOCALE}.UTF-8")
print(f"  Timezone:         {TIMEZONE}")
print(f"  Hostname:         {HOSTNAME}")
print("=" * 60, flush=True)


# ---------------------------------------------------------------------
# Step 1: Pre-write the pmbootstrap config file.
# ---------------------------------------------------------------------
config_dir = Path.home() / ".config"
config_dir.mkdir(parents=True, exist_ok=True)
config_path = config_dir / "pmbootstrap_v3.cfg"

work_dir = os.environ.get("PMBOOTSTRAP_WORK", str(Path.home() / ".local/var/pmbootstrap"))
pmaports_dir = os.path.join(work_dir, "cache_git", "pmaports")

cfg = configparser.ConfigParser()
cfg["pmbootstrap"] = {
    "work": work_dir,
    "aports": pmaports_dir,
    "device": f"device-{VENDOR}-{DEVICE}",
    "user": "user",
    "ui": ui,
    "ui_extras": "true" if ui_extra == "y" else "false",
    "systemd": systemd,
    "locale": LOCALE,
    "keymap": "",
    "extra_packages": extras_str,
    "hostname": HOSTNAME,
    "boot_size": "512",
    "parallel_jobs": "16",
    "ccache_size": "5G",
    "sudo_timer": "false",
    "mirror": "http://mirror.postmarketos.org/postmarketos/",
    "is_default_channel": "false",
}
cfg["providers"] = {
    "postmarketos-base-ui-audio-backend": audio,
    "postmarketos-base-ui-wifi": wifi,
    "postmarketos-usb-moded-default-profile": usb_mode,
}
cfg["mirrors"] = {}

print(f"\n[init] Pre-writing config: {config_path}", flush=True)
with open(config_path, "w") as f:
    cfg.write(f)
print(f"[init] Config written.", flush=True)


# ---------------------------------------------------------------------
# Step 2: Start pmbootstrap init with pipe I/O.
# ---------------------------------------------------------------------
print(f"\n[init] Starting pmbootstrap init with pipe-based I/O...\n", flush=True)

proc = subprocess.Popen(
    ["pmbootstrap", "init"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=0,  # unbuffered
    env={**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"},
)


# ---------------------------------------------------------------------
# Helper: read pmbootstrap output until a prompt appears or process exits.
# A "prompt" is a line ending with ": " (colon space) or just ":".
# Returns the accumulated output text.
# ---------------------------------------------------------------------
def read_until_prompt(timeout=300, existing_buf=""):
    """Read output until we see a NEW prompt (line ending with ': ').
    Returns (text, is_prompt). Prints output to stdout as we read.

    `existing_buf` is any leftover text from a previous read that should
    be prepended. We start reading fresh from the pipe.
    """
    buf = existing_buf
    deadline = time.time() + timeout
    fd = proc.stdout.fileno()

    # If existing_buf already ends with a prompt, return it immediately
    # (but only if it's a COMPLETE prompt we haven't seen before).
    # Actually, to avoid re-matching old prompts, we always read at least
    # some new data first.

    while time.time() < deadline:
        remaining = deadline - time.time()
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                # Process exited — drain remaining output
                try:
                    remaining_data = os.read(fd, 65536).decode("utf-8", errors="replace")
                    if remaining_data:
                        buf += remaining_data
                        sys.stdout.write(remaining_data)
                        sys.stdout.flush()
                except OSError:
                    pass
                return buf, False
            # Check if buf already contains a prompt (from existing_buf)
            if buf:
                stripped_check = buf.rstrip("\r\n")
                if stripped_check.endswith(": ") or stripped_check.endswith(":"):
                    # Wait a bit more to make sure no more data is coming
                    # (the prompt might be incomplete)
                    time.sleep(0.1)
                    ready2, _, _ = select.select([fd], [], [], 0.1)
                    if not ready2:
                        return buf, True
            continue

        try:
            data = os.read(fd, 65536).decode("utf-8", errors="replace")
        except OSError:
            break
        if not data:
            break

        buf += data
        sys.stdout.write(data)
        sys.stdout.flush()

        # Check if the last line is a prompt (ends with ": " or ":")
        stripped = buf.rstrip("\r\n")
        if stripped.endswith(": ") or stripped.endswith(":"):
            # Wait a tiny bit to see if more data comes (prompt might
            # be followed by more text in some edge cases)
            time.sleep(0.05)
            ready2, _, _ = select.select([fd], [], [], 0.05)
            if not ready2:
                return buf, True
            # More data is coming — keep reading

    return buf, False


def send_answer(answer):
    """Send an answer (a line) to pmbootstrap's stdin."""
    print(f"\n[init]   -> sending: {answer!r}", flush=True)
    try:
        proc.stdin.write(answer + "\n")
        proc.stdin.flush()
    except BrokenPipeError:
        print(f"[init] ERROR: pmbootstrap closed stdin (process exited)", file=sys.stderr, flush=True)
        sys.exit(1)


# ---------------------------------------------------------------------
# Step 3: Drive the prompts using PROMPT-BASED matching.
#
# Instead of a fixed-index answer list (which breaks when pmbootstrap
# changes prompt order or adds/removes prompts), we match each prompt
# by its text content and send the appropriate answer.
#
# This is robust against:
#   - Locale prompting once vs. twice (pmbootstrap version-dependent)
#   - The conditional "Enable this package?" prompt (UI-dependent)
#   - The "Zap existing chroots?" prompt (appears when config changes)
#   - Hostname re-prompting after validation error
# ---------------------------------------------------------------------

# Counters for prompts that can appear multiple times
provider_count = 0
locale_count = 0
hostname_count = 0

while True:
    # Read until we see a prompt
    text, is_prompt = read_until_prompt(timeout=300, existing_buf="")

    if not is_prompt:
        # Process may have exited (normal after the last answer)
        if proc.poll() is not None:
            print(f"\n[init] pmbootstrap exited (returncode={proc.returncode})", flush=True)
            break
        if "DONE!" in text:
            print(f"\n[init] pmbootstrap finished (DONE! detected)", flush=True)
            break
        print(f"\n[init] ERROR: timeout or no prompt seen", file=sys.stderr, flush=True)
        print(f"[init] last 1000 chars: {text[-1000:]}", file=sys.stderr, flush=True)
        sys.exit(1)

    # Strip ANSI from the text for matching
    stripped = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    # Get the last line (the prompt)
    last_line = stripped.rstrip().split('\n')[-1]

    # Match the prompt against known patterns and send the right answer.
    # This is robust against prompt order changes and optional prompts.
    answer = None
    desc = None

    if re.search(r"Work path \[[^\]]*\]:", last_line):
        answer, desc = "", "Work path"

    elif re.search(r"pmaports path \[[^\]]*\]:", last_line):
        answer, desc = "", "pmaports path"

    elif re.search(r"Channel \[[^\]]*\]:", last_line):
        answer, desc = channel, "Channel"

    elif re.search(r"Vendor \[[^\]]*\]:", last_line):
        answer, desc = VENDOR, "Vendor"

    elif re.search(r"Device codename:", last_line):
        answer, desc = DEVICE, "Device codename"

    elif re.search(r"Username \[[^\]]*\]:", last_line):
        answer, desc = "", "Username"

    elif re.search(r"Provider \[[^\]]*\]:", last_line):
        # Provider prompt appears 3 times: audio, wifi, usb_mode
        # The default in brackets may be "default" or the actual provider name
        # (if config was pre-written with a specific provider).
        provider_count += 1
        if provider_count == 1:
            answer, desc = audio, "Audio provider"
        elif provider_count == 2:
            answer, desc = wifi, "WiFi provider"
        elif provider_count == 3:
            answer, desc = usb_mode, "USB mode provider"
        else:
            answer, desc = "", f"Provider #{provider_count} (default)"

    elif re.search(r"User interface \[[^\]]*\]:", last_line):
        answer, desc = ui, "User interface"

    elif re.search(r"Enable this package\? \(y/n\) \[[yn]\]:", last_line):
        answer, desc = ui_extra, "Enable UI extra (conditional)"

    elif re.search(r"Install systemd\? \(default/always/never\) \[(default|always|never)\]:", last_line):
        answer, desc = systemd, "systemd"

    elif re.search(r"Which service manager should be used\? \(default/openrc/systemd\) \[[^\]]*\]:", last_line):
        if systemd == "always":
            ans = "systemd"
        elif systemd == "never":
            ans = "openrc"
        else:
            ans = "default"
        answer, desc = ans, "service manager"

    elif re.search(r"Change them\? \(y/n\) \[n\]:", last_line):
        answer, desc = "", "Change additional options"

    elif re.search(r"Extra packages \[[^\]]*\]:", last_line):
        # Default may be "none" or the pre-written package list.
        answer, desc = extras_str, "Extra packages"

    elif re.search(r"Use this timezone instead of GMT\? \(y/n\) \[y\]:", last_line):
        answer, desc = "", "Timezone"

    elif re.search(r"Locale \[[^\]]*\]:", last_line):
        # Locale may prompt once or twice depending on pmbootstrap version.
        # Always send LOCALE — if it prompts twice, we send it twice.
        locale_count += 1
        answer, desc = LOCALE, f"Locale (#{locale_count})"

    elif re.search(r"Device hostname[^:]*\[[^\]]*\]:", last_line):
        # Hostname may re-prompt if validation fails.
        # Always send empty (accept default: xiaomi-polaris).
        hostname_count += 1
        answer, desc = "", f"Hostname (#{hostname_count})"

    elif re.search(r"Build outdated packages.*\(y/n\) \[y\]:", last_line):
        answer, desc = "y", "Build outdated"

    elif re.search(r"Zap existing chroots.*\(y/n\) \[y\]:", last_line):
        # This prompt appears when config changed and chroots need zapping.
        # Always answer "y" to zap and apply the new config.
        answer, desc = "y", "Zap existing chroots"

    elif re.search(r"Continue\? \(y/n\) \[n\]:", last_line):
        # This prompt appears when selecting an archived device
        answer, desc = "y", "Continue (archived device)"

    else:
        # Unknown prompt — print it and fail with context
        print(f"\n[init] ERROR: unrecognized prompt", file=sys.stderr, flush=True)
        print(f"[init] prompt text: {last_line!r}", file=sys.stderr, flush=True)
        print(f"[init] full output: {stripped[-500:]}", file=sys.stderr, flush=True)
        sys.exit(1)

    print(f"\n[init] {desc} -> sending: {answer!r}", flush=True)
    send_answer(answer)


# ---------------------------------------------------------------------
# Wait for pmbootstrap to finish.
# ---------------------------------------------------------------------
print(f"\n[init] Waiting for pmbootstrap init to complete...", flush=True)
try:
    remaining, _ = proc.communicate(timeout=600)
    if remaining:
        sys.stdout.write(remaining)
        sys.stdout.flush()
except subprocess.TimeoutExpired:
    print(f"\n[init] ERROR: pmbootstrap init timed out", file=sys.stderr, flush=True)
    proc.terminate()
    sys.exit(1)

if proc.returncode != 0:
    print(f"\n[init] ERROR: pmbootstrap init failed (exit code {proc.returncode})", file=sys.stderr, flush=True)
    sys.exit(proc.returncode)

print("\n[init] ===========================================", flush=True)
print("[init] pmbootstrap init completed successfully!", flush=True)
print("[init] ===========================================\n", flush=True)
