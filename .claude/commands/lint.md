Run the full linting suite from inside `pueo/`:

1. `black --check .` — formatting
2. `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics` — hard errors only
3. `flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics` — warnings
4. `mypy --ignore-missing-imports .` — type checking
5. `bandit -r . -x ./tests` — security scan

Report all failures, then summarize what needs fixing.
