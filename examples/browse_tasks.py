"""Read public task metadata without starting a native environment."""
import json
from mac_bench.runtime.paths import repository_root

index = repository_root() / "data/public_tasks.json"
tasks = json.loads(index.read_text())["tasks"]
for task in tasks:
    if task["split"] == "dev":
        print(task["task_id"], task["category"], task["split"])
        print(task["instruction"])
