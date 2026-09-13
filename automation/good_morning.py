#!/usr/bin/env python3
"""goodmorning claude — pre-warm Claude's 5h usage window (Pro/Max).

Starts the 5-hour Claude plan usage window as early as possible,
staying in sync with your account's REAL reset time.

How it works:
1. Reads the real reset time saved in state.json from the last run.
2. If the reset is in the future -> window still active -> exits immediately,
   zero API calls.
3. If the reset is in the past (or state is missing) -> sends the "goodmorning
   claude": a minimal call (1 token, haiku model) to /v1/messages with the
   Claude Code OAuth token. That single call starts the new 5h window AND
   returns the real reset time in the anthropic-ratelimit-unified-5h-*
   response headers.
4. Saves the new reset time to state.json (committed by the workflow):
   subsequent runs know exactly when the window expires, even if you
   started it yourself by using Claude before the automation ran.

Note: the /api/oauth/usage endpoint is NOT usable here — the token from
`claude setup-token` lacks the user:profile scope (returns 403). The
rate-limit headers from the messages response are the equivalent official source.

Designed to be called in a loop by GitHub Actions (a check every ~5 min
within the same run, see the workflow), with the PC off. Each invocation
is single-shot: checks once and either sends or exits.

Optional: set TARGET_RESET_UTC="HH:MM" to align the window so it resets
at a specific UTC time (e.g. "12:00"). When the target is <5h away, the
script delays starting the new window until target-5h so it lands exactly.
When >=5h away, it starts immediately (the window will naturally expire
and need another cycle before the target). Set this as a GitHub Actions
repository variable (vars.TARGET_RESET_UTC). Leave unset for the default
behavior of keeping the window perpetually warm.

Requires: CLAUDE_CODE_OAUTH_TOKEN (generated with `claude setup-token`).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
STATE_FILE = Path(__file__).parent / "state.json"
WINDOW_HOURS = 5  # fallback estimate, used only if the reset header disappears
GREETING = "gooodmorning claudeee!!!  (dont respond to this message)"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # cheapest model: 1 token is enough
RESET_HEADER = "anthropic-ratelimit-unified-5h-reset"
UTILIZATION_HEADER = "anthropic-ratelimit-unified-5h-utilization"
HTTP_TIMEOUT_S = 60
# Optional: set TARGET_RESET_UTC="HH:MM" to align the window so it resets at
# that UTC time. If the target is <5h away, the script delays starting the
# window until target-5h. If >=5h away, it starts immediately (the window
# will expire and need another cycle before the target).
TARGET_RESET_UTC = os.environ.get("TARGET_RESET_UTC", "").strip()
# The `claude setup-token` token is accepted by /v1/messages only when
# presenting as Claude Code: requires beta header and dedicated system prompt.
OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def get_token() -> str:
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        log("ERROR: CLAUDE_CODE_OAUTH_TOKEN environment variable is missing.")
        log("Generate it locally with `claude setup-token` and set it as a secret.")
        sys.exit(1)
    return token


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    tmp_path = STATE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(STATE_FILE)


def parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_reset_header(headers) -> datetime | None:
    """The reset header is an epoch timestamp in seconds."""
    raw = headers.get(RESET_HEADER)
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def compute_hold_until(now: datetime) -> datetime | None:
    """If TARGET_RESET_UTC is set, return the earliest time we should start
    the next window so it expires at (or near) the target. Returns None if
    no target is configured or we can start immediately."""
    if not TARGET_RESET_UTC:
        return None
    try:
        hour, minute = map(int, TARGET_RESET_UTC.split(":"))
    except (ValueError, TypeError):
        log(f"WARNING: TARGET_RESET_UTC={TARGET_RESET_UTC!r} is not valid HH:MM, ignoring.")
        return None

    target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Pick the next occurrence of this time
    target = target_today if target_today > now else target_today + timedelta(days=1)

    hours_until_target = (target - now).total_seconds() / 3600
    if hours_until_target >= WINDOW_HOURS:
        return None  # safe to start now, window will expire before target
    # Delay: start at target - 5h so the window lands on the target
    start_at = target - timedelta(hours=WINDOW_HOURS)
    if start_at <= now:
        return None  # start time already passed, start now
    return start_at


def describe(dt: datetime, now: datetime) -> str:
    delta = str(dt - now).split(".")[0]
    return f"{dt.isoformat(timespec='seconds')} (in {delta})"


def send_good_morning(token: str) -> datetime | None:
    """Send the "goodmorning claude" and return the real reset time, or None on failure."""
    log(f"Sending {GREETING!r} (1 token, {CLAUDE_MODEL})...")
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1,
        "system": CLAUDE_CODE_SYSTEM,
        "messages": [{"role": "user", "content": GREETING}],
    }).encode("utf-8")
    req = urllib.request.Request(
        MESSAGES_ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/2.0.0 (external, cli)",
        },
    )
    now = datetime.now(timezone.utc)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            reset = parse_reset_header(resp.headers)
            utilization = resp.headers.get(UTILIZATION_HEADER, "?")
            if reset is None:
                log(f"WARNING: response OK but missing {RESET_HEADER} header "
                    f"(API may have changed). Using {WINDOW_HOURS}h estimate from now.")
                return now + timedelta(hours=WINDOW_HOURS)
            log(f"Message sent. 5h window: utilization {utilization}, "
                f"real reset at {describe(reset, now)}.")
            return reset
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 429:
            # Rate limit hit = the window is necessarily active: the headers
            # still tell us when it resets.
            reset = parse_reset_header(exc.headers)
            if reset is not None:
                log(f"5h limit already exhausted: window is still active, "
                    f"reset at {describe(reset, now)}.")
                return reset
            log(f"ERROR: rate limited (429) but no reset header. Detail: {body}")
            return None
        if exc.code in (401, 403):
            log(f"ERROR: token is invalid or expired (HTTP {exc.code}).")
            log("Regenerate the token with `claude setup-token` and update the "
                "CLAUDE_CODE_OAUTH_TOKEN repo secret.")
            log(f"API detail: {body}")
            return None
        log(f"ERROR: API responded with HTTP {exc.code}. Detail: {body}")
        log("Will retry automatically on the next scheduled run.")
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        log(f"Network ERROR reaching the API ({exc}). Will retry on the next run.")
        return None


def main() -> int:
    token = get_token()
    now = datetime.now(timezone.utc)

    resets_at = parse_iso(read_state().get("resets_at"))
    if resets_at is None:
        log("No reset time saved in state.json: sending 'goodmorning claude' "
            "to discover (and possibly start) the current window.")
    elif now < resets_at:
        log(f"5h window still active: reset at {describe(resets_at, now)}. "
            f"No API call needed, exiting.")
        return 0
    else:
        log(f"Previous window reset at "
            f"{resets_at.isoformat(timespec='seconds')}.")
        hold_until = compute_hold_until(now)
        if hold_until is not None:
            log(f"Holding: TARGET_RESET_UTC={TARGET_RESET_UTC}, "
                f"will start new window at {describe(hold_until, now)}.")
            return 0
        log("Starting a new window.")

    new_reset = send_good_morning(token)
    if new_reset is None:
        return 1

    write_state({
        "resets_at": new_reset.isoformat(timespec="seconds"),
        "checked_at": now.isoformat(timespec="seconds"),
    })
    log(f"Synced with real limit: next reset at "
        f"{describe(new_reset, datetime.now(timezone.utc))}. Have a great day!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
