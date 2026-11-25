# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Report a vulnerability" in the Security tab). You should get an answer within a week.

Examples of what to report: a way to send orders without `LIVE_TRADING=true` and the
typed confirmation, credentials or tokens written to disk or logs, an order replayed
after an uncertain outcome, or a dependency with a known exploit.

## Your own secrets

- Never commit `.env`, tokens, account numbers or real state folders. `.gitignore`
  blocks the usual paths, but check `git status` before every commit.
- Never paste credentials, account ids, balances or screenshots of your account in an
  issue or discussion. Maintainers will never ask for them.
- Paper mode needs no credentials at all.

## Supported versions

Only the latest commit on `main` receives fixes.
