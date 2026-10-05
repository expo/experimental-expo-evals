import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


TASK = Path(__file__).resolve().parents[1] / "tasks/agent-cli-ios-repair"
spec = importlib.util.spec_from_file_location("repair_verifier", TASK / "tests/verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def tree_result(coffee="2", tea="1", total="$18.00"):
    return {
        "exit_code": 0, "timed_out": False,
        "report": {"ok": True, "truncated": False, "allScreens": False, "nodes": [
            {"testID": key, "text": value} for key, value in {
                "cart-title": "Coffee cart", "cart-total": total,
                "quantity-coffee": coffee, "quantity-tea": tea,
            }.items()
        ]},
    }


class AgentCliRepairTest(unittest.TestCase):
    def test_smoke_requires_local_runtime_and_checked_route(self):
        def smoke():
            return {"exit_code": 0, "timed_out": False, "report": {
                "ok": True, "projectRootMatched": True, "platform": "ios",
                "deviceBackend": "local-ios", "runtimeSupported": True,
                "routeCheck": {"checked": True, "ok": True},
            }}
        self.assertTrue(verifier.smoke_passed(smoke()))
        for key in ("ok", "projectRootMatched", "platform", "deviceBackend", "runtimeSupported", "routeCheck"):
            with self.subTest(missing=key):
                result = smoke()
                del result["report"][key]
                self.assertFalse(verifier.smoke_passed(result))
        for key in ("checked", "ok"):
            result = smoke()
            result["report"]["routeCheck"][key] = False
            self.assertFalse(verifier.smoke_passed(result))

    def test_exact_rendered_nodes_are_required(self):
        result = tree_result()
        self.assertTrue(verifier.matches_cart(result, 2, 1, "$18.00"))
        result["report"]["nodes"] = []
        result["report"]["followups"] = [tree_result()["report"]]
        self.assertFalse(verifier.matches_cart(result, 2, 1, "$18.00"))

    def test_changed_totals_and_quantities_are_required(self):
        initial = tree_result()
        self.assertFalse(verifier.matches_cart(initial, 3, 1, "$25.50"))
        self.assertFalse(verifier.matches_cart(tree_result(total="$25.50"), 3, 1, "$25.50"))
        self.assertTrue(verifier.matches_cart(tree_result("3", "1", "$25.50"), 3, 1, "$25.50"))
        self.assertTrue(verifier.matches_cart(tree_result("3", "2", "$28.50"), 3, 2, "$28.50"))

    def test_ambiguous_failed_or_timed_out_tree_cannot_pass(self):
        for mutation in ("duplicate", "failed", "timeout", "not-ok"):
            with self.subTest(mutation=mutation):
                result = tree_result()
                if mutation == "duplicate":
                    result["report"]["nodes"].append({"testID": "cart-total", "text": "$18.00"})
                elif mutation == "failed":
                    result["exit_code"] = 20
                elif mutation == "timeout":
                    result["timed_out"] = True
                else:
                    result["report"]["ok"] = False
                self.assertFalse(verifier.matches_cart(result, 2, 1, "$18.00"))

    def test_incomplete_or_multiple_screen_trees_cannot_pass(self):
        for key in ("truncated", "allScreens"):
            for value in (True, None):
                with self.subTest(field=key, value=value):
                    result = tree_result()
                    result["report"][key] = value
                    self.assertFalse(verifier.matches_cart(result, 2, 1, "$18.00"))

    def test_source_snapshot_budget_is_total_and_omissions_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "src/app").mkdir(parents=True)
            (project / "tsconfig.json").write_text("c" * 5)
            (project / "src/products.json").write_text("p" * 3)
            (project / "src/cart.ts").write_text("x" * 50)
            (project / "src/app/index.tsx").write_text("i" * 50)
            snapshot = verifier.source_snapshot(project, limit=12)["files"]
            self.assertEqual(sum(len(file.get("content", "")) for file in snapshot.values()), 12)
            self.assertEqual(snapshot["src/cart.ts"], {"content": "xxxx", "truncated": True})
            self.assertEqual(snapshot["src/app/index.tsx"], {"content": "", "truncated": True})
            self.assertEqual(snapshot["src/app/_layout.tsx"], {"missing": True})

    def test_forced_or_throwing_tap_cannot_pass(self):
        result = {"exit_code": 0, "timed_out": False,
                  "report": {"ok": True, "called": True, "forced": False, "threw": None}}
        self.assertTrue(verifier.tapped(result))
        result["report"]["forced"] = True
        self.assertFalse(verifier.tapped(result))
        result["report"].update(forced=False, threw={"text": "error"})
        self.assertFalse(verifier.tapped(result))

    def test_full_arguments_are_retained_once(self):
        arguments = {"command": "x" * 50000}
        calls = verifier.agent_tool_calls({"steps": [
            {"source": "agent", "step_id": 4, "tool_calls": [
                {"function_name": "Bash", "arguments": arguments,
                 "extra": {"raw_arguments": json.dumps(arguments)}}]},
            {"source": "system", "tool_calls": [{"arguments": {"command": "ignored"}}]},
        ]})
        self.assertEqual(calls, [{"step_id": 4, "function_name": "Bash", "arguments": arguments}])

    def test_preservation_contract_matches_seed_files(self):
        self.assertTrue(verifier.unchanged(TASK / "environment/app/src/products.json", verifier.PRODUCTS))
        self.assertTrue(verifier.unchanged(TASK / "environment/app/tsconfig.json", verifier.TSCONFIG))


if __name__ == "__main__":
    unittest.main()
