# Development

## Tests

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s bulk-cleanup/tests -v
uv run python tests/lifecycle_insights/test_analyze_reuse.py
shellcheck lifecycle_insights/*.sh scheduled-cleanup/*.sh
```

`tests/cleanup/` and `tests/lifecycle_insights/` include the imported offline suites and opt-in
`validate_*.py` live harnesses. Live harnesses create tagged fixtures and clean them up; evidence
is written under gitignored `verification/`. See the tool guides before running them against
an account.

## Packaging a tool

For recurring cleanup, package the `scheduled-cleanup/` folder beneath a package root containing
`requirements.txt` and `LICENSE`; its README documents the direct-script commands. For
lifecycle analysis, package the scripts and README from `lifecycle_insights/` alongside
`requirements.txt` and `LICENSE` in one directory. These standalone packages do not include
the unified root command. Exclude credentials, journals, exports and verification data. `tests/lifecycle_insights/validate_customer_package_e2e.py`
builds and validates a lifecycle package, or checks the ZIP supplied through `LIFECYCLE_ZIP`.
