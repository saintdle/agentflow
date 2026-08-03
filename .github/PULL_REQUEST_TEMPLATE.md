## Outcome

<!-- Describe the user-visible result and link the issue or approved goal. -->

## Changes

<!-- Summarize the bounded implementation. -->

## Evidence

<!-- List exact commands and results. Mark anything not run. -->

- [ ] `python3 scripts/validate.py`
- [ ] `python3 -m unittest discover -s tests -p 'test_*.py'`
- [ ] Package build and clean-install check, when packaging changed
- [ ] `git diff --check`

## Risk and compatibility

<!-- Describe security, platform, provider, configuration, migration, and rollback effects. -->

- [ ] Tests cover behavior changes.
- [ ] User-visible changes are documented under `CHANGELOG.md` → `Unreleased`.
- [ ] Existing user configuration is preserved or an explicit migration is documented.
- [ ] No credentials, private paths, transcripts, customer data, or account details are included.
- [ ] Third-party material has licence and attribution records.
