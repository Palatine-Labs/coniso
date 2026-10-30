# Contributing to Coniso

Thanks for helping. Coniso is small on purpose, so focused changes land fastest.

## Before you start
- **Bugs:** open an issue with the bug report template. Include your Claude Code version, OS, and the `preflight` output.
- **Features:** open an issue first, so we can agree on the shape before you write code.
- **Security issues:** don't open a public issue. See [SECURITY.md](SECURITY.md).

## Making a change
1. Fork the repo and create a branch from `main`.
2. Load your copy locally:
   ```bash
   claude plugin validate .
   ```
   ```bash
   claude --plugin-dir .
   ```
3. Keep to the project's rules:
   - `scripts/coniso.py` stays **standard-library Python** and works on Windows, macOS and Linux.
   - Anything deterministic belongs in the script, not in agent instructions.
   - Agent and skill text stays terse. Every line is paid for in tokens on every call.
   - Agents return at most 12 lines and pass paths, never file contents.
4. Update `CHANGELOG.md` under **Unreleased**.
5. Open a pull request and fill in the template.

## Licensing
By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
