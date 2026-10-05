"""Check the repaired cart on the running simulator, then give the evidence to JEV."""

import json
from pathlib import Path
import subprocess


APP = Path("/app/expo-app")
LOGS = Path("/logs/verifier")
PRODUCTS = [
    {"id": "coffee", "name": "Coffee", "priceCents": 750},
    {"id": "tea", "name": "Tea", "priceCents": 300},
]
TSCONFIG = {
    "extends": "expo/tsconfig.base",
    "compilerOptions": {
        "strict": True,
        "paths": {"@/*": ["./src/*"], "@/assets/*": ["./assets/*"]},
    },
    "include": ["**/*.ts", "**/*.tsx", ".expo/types/**/*.ts", "expo-env.d.ts"],
}


def agent_tool_calls(trajectory: dict) -> list[dict]:
    return [
        {"step_id": step.get("step_id"), "function_name": call.get("function_name"),
         "arguments": call.get("arguments", {})}
        for step in trajectory["steps"] if step.get("source") == "agent"
        for call in step.get("tool_calls") or []
    ]


def cli(name: str, *args: str, timeout: int = 60) -> dict:
    command = ["bunx", "@expo/agent-cli", *args, "--json"]
    result = {"command": command, "exit_code": None, "timed_out": False, "report": {}}
    # Keep complete CLI output as artifacts; the judge only needs selected facts below.
    with (LOGS / f"{name}.stdout.json").open("w") as stdout, (LOGS / f"{name}.stderr.log").open("w") as stderr:
        try:
            result["exit_code"] = subprocess.run(
                command, cwd=APP, stdout=stdout, stderr=stderr, timeout=timeout,
            ).returncode
        except subprocess.TimeoutExpired:
            result["timed_out"] = True
        except OSError as error:
            result["error"] = str(error)
    try:
        report = json.loads((LOGS / f"{name}.stdout.json").read_text())
        if isinstance(report, dict):
            result["report"] = report
    except (ValueError, OSError):
        pass
    return result


def succeeded(result: dict) -> bool:
    return result["exit_code"] == 0 and not result["timed_out"]


def tree_values(result: dict) -> dict:
    """Read rendered text from exact testID nodes, never echoed arguments or followups."""
    ids = ("cart-title", "cart-total", "quantity-coffee", "quantity-tea")
    nodes = result["report"].get("nodes") or []
    return {
        test_id: [node.get("text") for node in nodes
                  if isinstance(node, dict) and node.get("testID") == test_id]
        for test_id in ids
    }


def matches_cart(result: dict, coffee: int, tea: int, total: str) -> bool:
    report = result["report"]
    return (succeeded(result) and report.get("ok") is True
            and report.get("truncated") is False and report.get("allScreens") is False
            and tree_values(result) == {
        "cart-title": ["Coffee cart"], "cart-total": [total],
        "quantity-coffee": [str(coffee)], "quantity-tea": [str(tea)],
    })


def smoke_passed(result: dict) -> bool:
    report = result["report"]
    route = report.get("routeCheck") or {}
    return (succeeded(result) and report.get("ok") is True
            and report.get("projectRootMatched") is True
            and report.get("platform") == "ios"
            and report.get("deviceBackend") == "local-ios"
            and report.get("runtimeSupported") is True
            and route.get("checked") is True and route.get("ok") is True)


def tapped(result: dict) -> bool:
    report = result["report"]
    return (succeeded(result) and report.get("ok") is True
            and report.get("called") is True and report.get("forced") is False
            and report.get("threw") is None)


def unchanged(path: Path, expected: object) -> bool:
    try:
        return json.loads(path.read_text()) == expected
    except (ValueError, OSError):
        return False


def screenshot_evidence(path: Path) -> dict:
    result = {"exists": False, "png_signature": False, "bytes": 0}
    try:
        with path.open("rb") as file:
            result["png_signature"] = file.read(8) == b"\x89PNG\r\n\x1a\n"
        result.update(exists=True, bytes=path.stat().st_size)
    except OSError:
        pass
    return result


def source_snapshot(project: Path, limit: int = 24000) -> dict:
    source = {}
    remaining = limit
    # Preserve the check configuration and calculation before spending the budget on UI.
    for path in ("tsconfig.json", "src/products.json", "src/cart.ts", "src/app/index.tsx", "src/app/_layout.tsx"):
        try:
            with (project / path).open() as file:
                content = file.read(remaining + 1)
            source[path] = {"content": content[:remaining], "truncated": len(content) > remaining}
            remaining -= len(content[:remaining])
        except OSError:
            source[path] = {"missing": True}
    return {"source_character_limit": limit, "files": source}


def main():
    from rewardkit import format_trajectory, run

    LOGS.mkdir(parents=True, exist_ok=True)
    trajectory = Path("/logs/agent/trajectory.json")
    (LOGS / "tool-calls.json").write_text(json.dumps(
        agent_tool_calls(json.loads(trajectory.read_text())), indent=2,
    ))
    (LOGS / "trajectory.txt").write_text(format_trajectory(str(trajectory), max_tokens=6000))
    (LOGS / "source.json").write_text(json.dumps(source_snapshot(APP), indent=2))

    typecheck = cli("typecheck", "typecheck", timeout=90)
    smoke = cli("smoke", "smoke", "--ios", "--no-start", "--route", "/", "--timeout", "60s",
                "--screenshot", str(LOGS / "simulator.png"), timeout=90)
    smoke_report = smoke["report"]
    smoke_summary = {key: smoke[key] for key in ("command", "exit_code", "timed_out")}
    smoke_summary["report"] = {key: smoke_report.get(key) for key in
                               ("ok", "outcome", "projectRootMatched", "platform", "deviceBackend",
                                "runtimeSupported", "routeCheck")}
    smoke_summary["passed"] = smoke_passed(smoke)
    (LOGS / "smoke.json").write_text(json.dumps(smoke_summary, indent=2))

    stages = []
    # Smoke reloads the code on disk, resetting the fixture to its initial quantities.
    # Do not start or repair the agent's app in the verifier.
    if smoke_summary["passed"]:
        for name, button, coffee, tea, total in (
            ("initial", None, 2, 1, "$18.00"),
            ("add-coffee", "add-coffee", 3, 1, "$25.50"),
            ("add-tea", "add-tea", 3, 2, "$28.50"),
        ):
            tap = cli(f"{name}-tap", "runtime:tap", button, "--ios", "--verify") if button else None
            tree = cli(f"{name}-tree", "runtime:tree", "--ios", "--all")
            stages.append({
                "name": name, "expected": {"coffee": coffee, "tea": tea, "total": total},
                "observed": tree_values(tree), "tree_exit_code": tree["exit_code"],
                "tree_timed_out": tree["timed_out"],
                "tap_passed": tapped(tap) if tap else None,
                "passed": matches_cart(tree, coffee, tea, total) and (tap is None or tapped(tap)),
            })

    evidence = {
        "smoke_passed": smoke_summary["passed"], "cart_stages": stages,
        "cart_passed": len(stages) == 3 and all(stage["passed"] for stage in stages),
        "products_unchanged": unchanged(APP / "src/products.json", PRODUCTS),
        "tsconfig_unchanged": unchanged(APP / "tsconfig.json", TSCONFIG),
        "typecheck": {"exit_code": typecheck["exit_code"], "timed_out": typecheck["timed_out"],
                      "checked": typecheck["report"].get("checked"),
                      "error_count": typecheck["report"].get("errorCount")},
        "typecheck_passed": (succeeded(typecheck) and typecheck["report"].get("checked") is True
                             and typecheck["report"].get("errorCount") == 0),
        "agent_screenshot": screenshot_evidence(Path("/app/evidence/screenshot.png")),
        "interaction_scope": "React prop calls, not physical screen taps or a native visibility check.",
    }
    (LOGS / "evidence.json").write_text(json.dumps(evidence, indent=2))
    run("/tests", workspace=str(APP))


if __name__ == "__main__":
    main()
