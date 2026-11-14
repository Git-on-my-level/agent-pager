# page

Page a human from an agent or script. `page` adds what a one-line `curl` leaves out when agents are the ones calling:
repeat suppression, a cap on how often the phone can ring, backend fallback, and secret masking. It uses two small Python files
with no third-party dependencies, so it still works when the things it reports on are down.

```sh
page "build finished"
page --sev wake --dedupe lead-down --url https://example.test/card "Lead has been silent for 15 minutes"
```

**Early and small.** It runs on macOS and Linux with Python 3.9 or later, and it has been used by one person's agent
fleet. `page` can't yet tell you that someone saw a page, and it won't notice on its own that your agents have died.
See [Limits](#limits).

## Quick start

1. Install [Bark](https://github.com/Finb/Bark) on your iPhone, allow Critical Alerts, and copy your device key.
2. Install `page` and save the key:

   ```sh
   mkdir -p ~/.local/bin
   curl -fsSL https://raw.githubusercontent.com/Git-on-my-level/agent-pager/main/page -o ~/.local/bin/page
   curl -fsSL https://raw.githubusercontent.com/Git-on-my-level/agent-pager/main/page_support.py -o ~/.local/bin/page_support.py
   chmod +x ~/.local/bin/page
   mkdir -p -m 700 ~/.config/page/secrets
   pbpaste > ~/.config/page/secrets/bark-key   # or write the file another way on Linux
   chmod 600 ~/.config/page/secrets/bark-key
   ```

3. Check it. The first command doesn't ring; the second really does:

   ```sh
   page test --sev wake
   page test --sev wake --loud
   ```

To add [ntfy](https://ntfy.sh) for `info` messages and as a fallback, subscribe to a long random topic in its app, then
save the topic name to `~/.config/page/secrets/ntfy-topic` with mode 0600. Without a paid plan, anyone who knows the
name of an ntfy.sh topic can read and post to it, so treat the name as a password.

A backend with no secret file is skipped, so either one works alone. Bark is iPhone only, and `wake` won't ring through
Do Not Disturb without it.

## Severity

| Severity | Default route | On the phone |
| --- | --- | --- |
| `info` | ntfy, then Bark | A normal notification |
| `wake` | Bark, then ntfy | Bark rings through mute and Do Not Disturb for 30 seconds; the ntfy fallback uses max priority, which we found showed as a normal notification on iOS, so its output says `may not have rung` |

Keep `wake` to a short list of things worth waking someone for. Everything else is `info`.

## Limits on noise

- Repeats of a key inside its window are suppressed: 15 minutes for `wake`, 1 hour for `info`. Without `--dedupe`, the
  key comes from the title and message.
- Past 6 wakes an hour, further wakes go out as `info`.
- Messages are capped at 240 characters, and anything shaped like a token is masked. Long runs that read as joined-up
  words, such as branch names and paths, are kept, while hashes and UUIDs are masked. In a URL, the query and fragment
  are masked the same way, the path loses only known credential shapes so commit links still open, and any login is
  removed.
- If the state directory can't be used, or another `page` holds its lock for more than 15 seconds, the page goes out
  anyway without suppression or the cap. A duplicate is better than a lost page.

Message text passes through third-party servers and shows on a lock screen. Send a summary and a link, never user data
or secrets. Masking is a backstop that won't catch everything. The default title includes the host's short name unless
you pass `--title` or `--source`, and `page test` always includes it.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Accepted, suppressed, or deferred by incident policy; inspect the printed outcome |
| 1 | Every backend failed, or none is set up |
| 2 | Bad usage |

`page status` exits 0 if at least one backend is set up and 1 if none is. A message that is only the word `status` or
`test` needs a separator: `page -- status`.

## Configuration

`~/.config/page/config.json` is optional. Anything you leave out keeps its default, shown here:

```json
{
  "routes": {"wake": ["bark", "ntfy"], "info": ["ntfy", "bark"]},
  "windows": {"wake": 900, "info": 3600},
  "wake_cap_per_hour": 6,
  "backends": {
    "bark": {"server": "https://api.day.app", "secret_file": "secrets/bark-key", "volume": 5},
    "ntfy": {"server": "https://ntfy.sh", "secret_file": "secrets/ntfy-topic"}
  }
}
```

`secret_file` is relative to the config directory, or an absolute or `~` path. `server` lets you use a self-hosted Bark
or ntfy server, over https, or http on the same machine, because the secret is part of the URL. A bad setting is reported
and keeps its default, and the rest of the file still applies, so a bad edit can't silence a page. If the file isn't valid
JSON, all of it is ignored, so a secret at a custom path won't be found until it's fixed: keep secrets at the default
paths to stay reachable through that too. `page status` shows whether either has happened.

## Agent skill

`skills/page/SKILL.md` tells agents when and how to page. It's a standard Agent Skills directory, and `page` has no
installer of its own. Use whatever installs skills on your host:

- [Vercel's skills CLI](https://github.com/vercel-labs/skills): `npx skills add Git-on-my-level/agent-pager --skill page`.
  It collects anonymous usage data unless `DISABLE_TELEMETRY=1` is set.
- [agentctl](https://github.com/Git-on-my-level/agentctl): a config bundle can select `skills/agentctl-pack.json` as its
  manifest, or add the skill to the Skill Hub it already uses.
- By hand: copy `skills/page` into your agent's skills directory.

## Limits

- **No acknowledgement.** A successful send means the server accepted it, not that anyone saw it. A page you sleep
  through rings once for 30 seconds. Neither backend reports back.
- **No watchdog.** `page` only runs when something calls it. If all your agents die together, nothing pages. Run a
  separate check that doesn't depend on them.
- **Limits are per host and per user account.** Two hosts noticing one outage will both ring.
- **Public servers.** By default messages go through `api.day.app` and `ntfy.sh`. Bark's author [can see request
  logs](https://github.com/Finb/Bark/blob/master/docs/en-us/privacy.md) on the public server, and `page` doesn't use
  Bark's encryption yet. Both services may rate-limit you.
- **No proxy support.** `page` ignores proxy settings and connects directly.

## Tests

```sh
python3 test_page.py
```

They run the real CLI against a local fake server, so nothing reaches a phone or a public server.

## License

[MIT](LICENSE)


## Managed incidents (0.3)

Use one stable incident name, not a timestamp or a generated notification ID.
Report each current observation (including recovery) at a cadence below
`--max-age` (default 300 seconds):

```sh
page --source batch-custody --incident audio-custody --sev wake \
  --condition firing --observed-at "2026-11-15T01:55:00+07:00" \
  --human-action "Investigate persistent audio storage risk" \
  --owner batch-responder --wake-after 600 --resolve-after 3600 \
  --url https://example.org/incident "3 audio items lack verified storage; no automatic repair"
```

The triage interval is audit-only: no phone notification. A wake needs fresh, continuous firing evidence
for `--wake-after` seconds and a concrete `--human-action`. `--handling automatic`
keeps it local while an effective recovery is running; an owner name alone does
not establish recovery. The caller must stop claiming automatic handling if it
fails. Use `--wake-after 0` only for verified imminent harm. Missing human action
keeps an incident local. Stale, future and unknown observations never
wake or clear. Report `--condition resolved` until the recovery hold expires;
brief clear/firing flaps retain one episode and restart the persistence clock.
A gap beyond the evidence freshness budget restarts the firing clock.

The action is prepended to a wake's body. State is per host, source and incident;
it is not a distributed lock or proof of human acknowledgement. A restart keeps
incident identity. Info and wake deliveries have separate dedupe identities;
failed deliveries retry. Policy deferral prints `page: deferred <reason>` and
exits 0. Suppression is also exit 0. Scripts must distinguish these from
`page: sent`; exit 0 alone never proves a notification was sent.

Install **both `page` and `page_support.py` in the same directory**. Keep legacy
calls for emergency/manual use while migrating monitors; legacy wakes keep
their existing behavior and are explicitly visible in the caller index.

## Audit and caller inventory

Every parsed send attempt records its outcome: sent, capped, suppressed,
failed, refused, or deferred. Inspect without sending any notification:

```sh
page history --source batch-custody --limit 100 --json
page callers --json
page callers --scan /path/to/config-or-scripts --json
```

The runtime caller index groups observed sources, parent executable basenames,
first/last activity, outcomes and legacy-call counts. A missing source appears as
`unattributed`. The source scan lists candidate file/line/literal-source sites,
including dormant scripts; it does not claim they are active or prove exhaustive
coverage of dynamically assembled commands. It skips symlinks, secrets, caches
and large files and never emits source text. Audit each host: these logs are
local, not a fleet-wide total. Before a complete rollout, older installations
and calls predating the upgrade are not represented.

Audit storage is `~/.local/state/page/audit/` (or `PAGE_STATE_DIR/audit`). Daily
JSONL files retain 30 days with at most four 2 MiB segments/day. Directories are
0700 and new files 0600. It records masked transmitted text/URL for sent events,
source, host, policy decision, incident, delivery result and parent executable
basename; no parent argv or credentials are collected. As with lock-screen
messages, do not put private customer data in a page. Audit failure warns but
never blocks emergency transport. Parser errors before a call is formed and
SIGKILL/process crashes are outside the durable audit guarantee. The short
`sent.jsonl` remains the independent delivery/dedupe state.

Tests use fake local backends only: `python3 -B -m unittest test_page test_incidents`.
