# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |

## Reporting a vulnerability

**Please don't report security issues in public issues, discussions or pull requests.**

Report them privately through GitHub's private vulnerability reporting:

1. Go to the [Security tab](https://github.com/Palatine-Labs/coniso/security) of this repository.
2. Click **Report a vulnerability**.
3. Describe the issue, the affected version, and steps to reproduce.

We aim to acknowledge reports within 5 business days. We'll keep you updated until a fix is released, and we'll credit you in the release notes unless you'd rather stay anonymous.

## Scope

Coniso runs a local Python script and drives Claude Code agents that read and write files and run commands in your repository. Reports we're especially interested in:
- the script running unintended commands, or writing outside the repository and `.coniso/`;
- the settings `init` writes granting broader permissions than documented;
- prompt-injection paths that let repository content escalate an agent's actions beyond its assignment.
