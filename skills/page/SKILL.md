---
name: page
description: Reach the human on their phone with the page CLI. Use when work has finished or failed while they may be away, when you are blocked on a decision only they can make, or when something is urgent enough to wake them. Covers choosing info or wake, deduping, what a page may contain and reading the result.
---

# Page a human

`page` sends a push to the human's phone. A `wake` rings through mute and Do Not Disturb, so it interrupts sleep and meetings.

Run `page status` if you aren't sure it's set up; it exits 1 if no backend is. If `page` is missing or no backend is set up, say so in your report and carry on. Don't call a push service directly or write your own sender.

## Choose the severity

Page only when the human would want to know now and may not be watching. Don't page about routine progress or anything they can see in this session.

**`info`** is a normal notification. Use it for a long task that finished or failed while they're away, or a decision you're blocked on that can wait until they next look.

```bash
page --dedupe "train-42" --source rollout --url "<link>" "Train 42 failed at eligibility, nothing deployed"
```

**`wake`** rings. Use it only when one of these is true:

- The agent coordinating the work has died or been replaced.
- Production has tripped, rolled back, or is harming users now, and nothing automatic is handling it.
- User data or audio is at risk unless a person acts soon.
- A blocked action needs their approval, and waiting will cause harm before they would normally look.

Your project's instructions may narrow this list. If you aren't sure it qualifies, use `info`.

```bash
page --sev wake --dedupe "lead-down" --source failover --url "<link>" "Lead silent 15 min, takeover started"
```

## Write the message

- **Lead with what they'd act on**, in one short line: `Backend rollback at 5%, T2 tripped`, not `Update on deployment`.
- **Always pass `--dedupe`** with a stable key for the event, such as `lead-down`. Every agent reporting the same event should use the same key if it can. Don't put timestamps or counters in the key.
- **Pass `--url`** to the card, PR or dashboard that has the detail.
- **Keep sensitive data out.** Text passes through third-party servers and shows on a lock screen. Never include secrets, tokens, user IDs, email addresses or transcript text. `page` masks text that looks like a token, but don't rely on it.

## Read the result

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0, `sent` | A backend accepted it | Report it as sent, not as seen |
| 0, `suppressed` | Same key inside its window | Nothing; don't change the key to get past it |
| 0, `capped, did not ring` | Over the hourly wake cap, so it went as info | Say in your report that it didn't ring |
| 0, `may not have rung` | The ringing backend failed and a fallback accepted it | Say in your report that it may not have rung, and use any other channel you have if it matters |
| 1 | Every backend failed, or none is set up | Report it and use any other channel you have |
| 2 | Bad usage | Fix the command |

`sent` doesn't mean the human saw it, and a page doesn't grant anything. Don't treat silence as approval. Keep following your instructions for guarded actions, and carry on with whatever else you can do safely.

## Testing

`page test --sev wake` checks the route without ringing. Use `page test --sev wake --loud` only when the human has asked for it, because it really rings.
