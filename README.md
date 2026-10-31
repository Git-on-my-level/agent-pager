# page

Page a human from an agent or script. `page` adds what a one-line `curl` leaves out when agents are the ones calling:
repeat suppression, a cap on how often the phone can ring, backend fallback, and secret masking. It's one Python file
with no dependencies, so it still works when the things it reports on are down.

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
| `wake` | Bark, then ntfy | Bark rings through mute and Do Not Disturb for 30 seconds; the ntfy fallback uses max priority, which we found showed as a normal notification on iOS |

Keep `wake` to a short list of things worth waking someone for. Everything else is `info`.

## Limits on noise

- Repeats of a key inside its window are suppressed: 15 minutes for `wake`, 1 hour for `info`. Without `--dedupe`, the
  key comes from the title and message.
- Past 6 wakes an hour, further wakes go out as `info`.
- Messages are capped at 240 characters, and anything shaped like a token is masked. That can catch long file paths
  too, so put links in `--url`. In a URL, only the query and fragment are masked, and any login is removed.
- If the state directory can't be used, or another `page` holds its lock for more than 15 seconds, the page goes out
  anyway without suppression or the cap. A duplicate is better than a lost page.

Message text passes through third-party servers and shows on a lock screen. Send a summary and a link, never user data
or secrets. Masking is a backstop that won't catch everything. The default title includes the host's short name unless
you pass `--title` or `--source`, and `page test` always includes it.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | A backend accepted it, or it was suppressed as a repeat |
| 1 | Every backend failed, or none is set up |
| 2 | Bad usage |

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
or ntfy server, over https, or http on the same machine, because the secret is part of the URL. If the file can't be used, `page` reports that, ignores all of it and uses the defaults, so a bad edit
can't silence a page. `page status` shows whether that has happened.

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
