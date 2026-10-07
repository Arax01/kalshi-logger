# Notes for Claude working on this project

## Workflow (set by the owner)
- Work on a branch, never directly on `main`.
- When a milestone is done, open a pull request into `main` with a plain-English summary of what
  changed and how to test it. The owner reviews and merges; do not merge yourself.
- Commit after each working milestone with a plain-English commit message.

## Hard rules
- Read-only: no code that places, modifies or cancels orders, not even disabled or commented out.
- Public market-data endpoints only. If a key is ever needed, ask the owner first and explain its permissions.
- No paid signups without asking.
- Keys go in `.env` (git-ignored), never in code. Never commit the database, logs or reports.
- Check Kalshi's current docs (docs.kalshi.com, `llms.txt`) rather than relying on memory for
  endpoints, rate limits or fees.

## Practical
- The owner runs the logger on Windows; keep `.bat` files working and the README plain English.
- The logger is moving to a DigitalOcean Ubuntu 24.04 server (SERVER.md, `server/`), run as the `kalshi`
  user by systemd, with backups to Backblaze B2. The laptop .bat files reach it over SSH. Keep both working.
- Run tests with `python -m unittest discover tests`.
