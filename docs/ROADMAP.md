# Roadmap

Ordered by value to you. Nothing here is built yet.

## Next (after the first live week)

1. **Fix whatever the first live run finds.** Expect source parsers and the
   Telegram/Gmail clients to need small adjustments once they meet the real
   services.
2. **Official sources.** Add the RSS or notice boards of the organisations you
   actually apply to (college, target companies, fellowship programmes). These
   verify without your confirmation and cut aggregator noise.
3. **Tune scoring from your outcomes.** The weekly summary already lists skip
   reasons and outcomes; use them to adjust keyword weights in the profile.

## Soon

4. **Media host for Instagram reels** (object storage with signed public URLs,
   deleted after publishing).
5. **Calendar-aware reminders**: avoid pinging during your busy blocks.
6. **Telegram voice notes** → transcribed objectives (needs a speech model).
7. **Dashboard editing of policies** with a two-step confirmation and an audit
   row (today policies are edited as files on the server, on purpose).

## Later

8. **Postgres option** if the database outgrows SQLite (unlikely for one owner).
9. **Off-site encrypted backups** to object storage on a schedule.
10. **More publishers** (X/Threads) behind the same policy gate.
11. **A second worker host** for heavy renders.

## Deliberately not planned

- Browser automation for applications, CAPTCHA solving, or using session
  cookies. These break platform terms and the brief's rules.
- Letting bots edit their own permissions or policies.
- Making Hermes a runtime dependency.
