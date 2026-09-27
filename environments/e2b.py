"""Harbor 0.23 requests 24 hours; E2B's base plan permits one hour."""

from e2b import AsyncSandbox
from harbor.environments.e2b import E2BEnvironment
from harbor.models.task.config import NetworkMode
from tenacity import retry, stop_after_attempt, wait_exponential


class OneHourE2B(E2BEnvironment):
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=10), reraise=True)
    async def _create_sandbox(self):
        self._sandbox = await AsyncSandbox.create(
            template=self._template_name,
            metadata={"environment_name": self.environment_name, "session_id": self.session_id},
            envs=self._startup_env(),
            timeout=3600,
            allow_internet_access=self.network_policy.network_mode != NetworkMode.NO_NETWORK,
            network=self._sandbox_create_network_options(),
        )
