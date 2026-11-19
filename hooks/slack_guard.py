#!/usr/bin/env python3
"""PreToolUse hook: pins every Slack post of a run to one destination.

runner.py sets, per claude session:
  SLACK_GUARD_CHANNEL  the only channel_id allowed (the asked thread's channel, or the owner's user id for DMs)
  SLACK_GUARD_THREAD   the only thread_ts allowed; unset means "no thread" (a DM to the owner)
  SLACK_GUARD_LOG      file where blocked attempts are appended

Anything else (other channel, other thread, a top-level post, reply_broadcast) is denied, whatever the model
was told by a Slack message. Missing configuration or unreadable input also denies (fail closed).
"""
import json
import os
import sys
import time

SEND_TOOLS = {"mcp__claude_ai_Slack__slack_send_message"}


def decide(decision, reason=""):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))
    return 0


def deny(reason, tool_input):
    log = os.environ.get("SLACK_GUARD_LOG")
    if log:
        with open(log, "a") as f:
            f.write(f"{time.strftime('%F %T')} blocked: {reason} | input: {json.dumps(tool_input)[:300]}\n")
    return decide("deny", f"Blocked by slack_guard: {reason}. Post only where this run is allowed to.")


def main():
    try:
        event = json.load(sys.stdin)
        tool, args = event.get("tool_name", ""), event.get("tool_input") or {}
    except Exception as e:  # unreadable input: block the call (exit 2 = blocking error)
        print(f"slack_guard: unreadable hook input ({e})", file=sys.stderr)
        return 2
    if tool not in SEND_TOOLS:
        return 0
    channel = os.environ.get("SLACK_GUARD_CHANNEL", "")
    thread = os.environ.get("SLACK_GUARD_THREAD", "")
    if not channel:
        return deny("no allowed destination is configured for this run", args)
    if args.get("channel_id") != channel:
        return deny(f"channel {args.get('channel_id')!r} is not the allowed {channel!r}", args)
    if (args.get("thread_ts") or "") != thread:
        return deny(f"thread {args.get('thread_ts')!r} is not the allowed {thread or 'none'!r}", args)
    if args.get("reply_broadcast"):
        return deny("reply_broadcast would also post to the channel", args)
    # Never answer "allow": that would bypass --allowedTools (e.g. post during a DRY_RUN, where the send tool is
    # left out on purpose). The guard only blocks; a matching destination falls through to the normal permissions.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # any bug here must block the post, never let it through
        print(f"slack_guard: internal error ({e})", file=sys.stderr)
        sys.exit(2)
