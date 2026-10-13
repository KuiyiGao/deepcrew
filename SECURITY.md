# Security

## Report a vulnerability

Please do not open a public issue. Use **Report a vulnerability** under the Security tab of this repository, which reaches
the maintainers privately. Say what you found, how to reproduce it, and what it lets someone do. You will hear back within a
few days.

## What counts

deepcrew runs a crew of Claude Code processes on your computer with your rights. These are in scope.

- The API key: it is kept in the system keychain or in `~/.config/deepcrew/.env` with mode 0600, and reaches Claude Code only
  through its `apiKeyHelper`. A way to read it from a member, from the team page or from logs is a vulnerability.
- Permissions: a member changing files outside its region, running a command that should have waited for approval, or reaching
  a folder or a site that was not allowed.
- The team page: it listens on 127.0.0.1 only and every request needs the token in its address. A way around either is a
  vulnerability.
- The sandbox: with the presets that turn it on, a command escaping what Claude Code's sandbox allows.

Out of scope: what a member does with rights you granted it (an allowed command, a linked folder, an added site), and bugs in
Claude Code, the model provider or the operating system. Report those to them.

## Versions

Fixes go into the latest release.
