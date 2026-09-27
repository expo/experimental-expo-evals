"""Keep the plan-limit workaround compatible with Harbor's native E2B provider."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from harbor.environments.factory import EnvironmentFactory
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from environments.e2b import OneHourE2B


class E2BCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_provider_changes_only_the_sandbox_lifetime(self):
        configuration = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "harbor.yaml").read_text()
        )
        configured = TrialEnvironmentConfig.model_validate(configuration["environment"])
        configured.env = {"EVAL_MARKER": "job"}
        native = TrialEnvironmentConfig(
            type="e2b", env=configured.env, kwargs={"timeout": 3600}
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Dockerfile").write_text("FROM node:24\nWORKDIR /app\n")

            def environment(config, policy):
                return EnvironmentFactory.create_environment_from_config(
                    config=config,
                    environment_dir=root,
                    environment_name="fixture",
                    session_id="fixture__env",
                    trial_paths=TrialPaths(root / "trial"),
                    task_env_config=EnvironmentConfig(
                        env={"EVAL_MARKER": "task", "TASK_ONLY": "present"}
                    ),
                    network_policy=policy,
                )

            for policy in (
                NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
                NetworkPolicy(
                    network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["example.com"]
                ),
            ):
                with self.subTest(network=policy.network_mode):
                    adapter = environment(configured, policy)
                    self.assertIsInstance(adapter, OneHourE2B)
                    upstream = environment(native, policy)
                    with patch(
                        "e2b.AsyncSandbox.create", new_callable=AsyncMock
                    ) as create:
                        await upstream._create_sandbox()
                        upstream_options = dict(create.await_args.kwargs)
                        await adapter._create_sandbox()
                        adapter_options = dict(create.await_args.kwargs)

                    # A constructor kwarg alone is silently ignored by Harbor 0.23.
                    self.assertEqual(upstream_options.pop("timeout"), 86_400)
                    self.assertEqual(adapter_options.pop("timeout"), 3600)
                    self.assertEqual(adapter_options, upstream_options)
                    self.assertEqual(
                        adapter_options["envs"],
                        {"EVAL_MARKER": "job", "TASK_ONLY": "present"},
                    )
