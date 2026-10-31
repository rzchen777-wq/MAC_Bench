"""Print scores, usage and trace locations from an evaluation summary."""
import argparse
import json
from pathlib import Path


def percent(value):
    return "n/a" if value is None else f"{value:.2%}"


def render_summary(report):
    metrics = report.get("metrics", {})
    episodes = report.get("episodes", [])
    lines = [
        f"Task: {report['task_id']}",
        f"System: {report['baseline']} | Status: {report['status']}",
        f"Scored episodes: {metrics.get('scored_episodes', 0)}/{len(episodes)}",
        f"PS: {percent(metrics.get('ps'))} | RR: {percent(metrics.get('rr'))}",
    ]
    for index, episode in enumerate(episodes, 1):
        lines.append(
            f"Episode {index}: PS {percent(episode.get('ps'))} | "
            f"RR {percent(episode.get('rr'))} | stop {episode.get('stop_reason', 'n/a')}"
        )
        lines.append(
            f"  Tokens: {episode.get('input_tokens', 0):,} input + "
            f"{episode.get('output_tokens', 0):,} output | "
            f"Tool calls: {episode.get('tool_calls', 0)}"
        )
        location = episode.get("cache", {}).get("artifact_directory")
        if location:
            lines.append(f"  Episode files: {Path(location) / 'episode'}")
    if metrics.get("excluded"):
        lines.append("Excluded from means: " + json.dumps(metrics["excluded"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    args = parser.parse_args(argv)
    print(render_summary(json.loads(args.summary.read_text())))


if __name__ == "__main__":
    main()
