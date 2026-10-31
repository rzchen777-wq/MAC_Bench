# Contributing

## Develop and test

From the repository root:

```bash
sudo apt-get install ripgrep
pip install -e '.[dev,agents]'
pytest
```

The default suite covers local fixtures, task loading, downloads, environment lifecycle and runtime behavior. Installed task packages also receive material and verifier checks. Native baseline integration is available with `pytest --run-baselines`; see [Tests](tests/README.md) for dependencies. Tests use scripted model responses and do not start cloud environments.

For a change to environment loading, run `mac-bench environment check --task-id TASK_ID --out outputs/environment-check` on an affected task. See [Environments](docs/environments.md) for setup and result fields.

## Report an issue

Include the task ID, command, relevant configuration and a small reproduction. For evaluation failures, include the episode status and relevant log excerpt. Remove credentials from any shared logs.

## Submit a change

Describe the behavior being changed and how it was checked. Tool or evaluator fixes should include a regression test. Update the corresponding guide when a command, task interface or result field changes.
