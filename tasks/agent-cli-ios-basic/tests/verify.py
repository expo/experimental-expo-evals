"""Give JEV agent tool calls, the trace, and an independent simulator smoke test."""

import json
from pathlib import Path
import subprocess


def agent_tool_calls(trajectory: dict) -> list[dict]:
    return [
        {"step_id": step.get("step_id"), **call}
        for step in trajectory["steps"] if step.get("source") == "agent"
        for call in step.get("tool_calls") or []
    ]


def screenshot_evidence(path: Path) -> dict:
    evidence = {"path": str(path), "exists": False, "bytes": 0, "png_signature": False}
    try:
        with path.open("rb") as file:
            signature = file.read(8)
        evidence.update(exists=True, bytes=path.stat().st_size,
                        png_signature=signature == b"\x89PNG\r\n\x1a\n")
    except OSError:
        pass
    return evidence


def main():
    from rewardkit import format_trajectory, run

    logs = Path("/logs/verifier")
    logs.mkdir(parents=True, exist_ok=True)
    trajectory = Path("/logs/agent/trajectory.json")
    # Preserve tool arguments independently of the formatted trace's output budget.
    (logs / "tool-calls.json").write_text(json.dumps(
        agent_tool_calls(json.loads(trajectory.read_text())), indent=2,
    ))
    (logs / "trajectory.txt").write_text(format_trajectory(str(trajectory), max_tokens=8000))
    (logs / "screenshot.json").write_text(json.dumps(
        screenshot_evidence(Path("/app/evidence/screenshot.png")), indent=2,
    ))
    command = ["bunx", "@expo/agent-cli", "smoke", "--ios", "--no-start",
               "--timeout", "60s", "--screenshot", str(logs / "simulator.png"), "--json"]
    smoke = {"command": command, "exit_code": None, "timed_out": False}
    with (logs / "smoke.stdout.json").open("w") as stdout, (logs / "smoke.stderr.log").open("w") as stderr:
        try:
            result = subprocess.run(command, cwd="/app/expo-app", stdout=stdout,
                                    stderr=stderr, timeout=90)
            smoke["exit_code"] = result.returncode
        except subprocess.TimeoutExpired:
            smoke["timed_out"] = True
        except OSError as error:
            smoke["error"] = str(error)
    smoke["stdout"] = (logs / "smoke.stdout.json").read_text(errors="replace")
    smoke["stderr"] = (logs / "smoke.stderr.log").read_text(errors="replace")[-4000:]
    (logs / "smoke.json").write_text(json.dumps(smoke, indent=2))
    run("/tests", workspace="/app/expo-app")


if __name__ == "__main__":
    main()
