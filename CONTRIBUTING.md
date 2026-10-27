# Contributing

Install development dependencies with `pip install -e '.[dev,agents]'` and run `pytest`.
The default tests use local fixtures and do not allocate cloud environments.

For task issues, include the task ID, package checksum, public operation and a minimal reproduction. Keep model credentials out of logs. Tool and evaluator changes should include a regression test for the affected behavior.
