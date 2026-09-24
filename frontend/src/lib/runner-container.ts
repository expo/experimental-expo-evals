import { Container, getContainer } from "@cloudflare/containers";
import type { RunnerPayload } from "./cloud-types";
export type { RunnerPayload } from "./cloud-types";

type RunnerEnv = {
  RUNNERS: DurableObjectNamespace;
  APP_ORIGIN?: string;
  ANTHROPIC_API_KEY?: string;
  OPENAI_API_KEY?: string;
  E2B_API_KEY?: string;
  EXPO_TOKEN?: string;
  TYPESAFE_API_KEY?: string;
};

/** A separate Container per run. It hosts Harbor; E2B hosts the agent and verifier. */
export class HarborRunner extends Container<RunnerEnv> {
  defaultPort = 8080;
  sleepAfter = "70m";
  enableInternet = true;

  async launch(payload: RunnerPayload, callbackToken: string) {
    if (!this.env.APP_ORIGIN) throw new Error("APP_ORIGIN is required.");
    const origin = new URL(this.env.APP_ORIGIN);
    if (
      (origin.protocol !== "https:" &&
        !(
          origin.protocol === "http:" &&
          ["localhost", "127.0.0.1", "host.docker.internal"].includes(
            origin.hostname,
          )
        )) ||
      origin.pathname !== "/" ||
      origin.search ||
      origin.hash ||
      origin.username ||
      origin.password
    ) {
      throw new Error(
        "APP_ORIGIN must be the trusted public application origin.",
      );
    }
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}$/.test(payload.runId)) {
      throw new Error("Invalid run ID.");
    }
    if (!callbackToken || callbackToken.length < 32)
      throw new Error("A per-run callback token is required.");
    const required = [
      "E2B_API_KEY",
      "EXPO_TOKEN",
      "TYPESAFE_API_KEY",
      payload.agent === "codex" ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY",
    ] as const;
    if (required.some((key) => !this.env[key]?.trim()))
      throw new Error("Required runner credentials are missing.");

    // This survives container restarts: an uncertain launch cannot rerun a paid eval.
    const claimed = await this.ctx.storage.transaction(async (transaction) => {
      const existing = await transaction.get<string>("acceptedRunId");
      if (existing) {
        if (existing !== payload.runId)
          throw new Error("Container already belongs to another run.");
        return false;
      }
      await transaction.put("acceptedRunId", payload.runId);
      return true;
    });
    if (!claimed)
      return Response.json(
        { runId: payload.runId, status: "accepted" },
        { status: 202 },
      );

    const envVars: Record<string, string> = {
      RUN_ID: payload.runId,
      CALLBACK_URL: `${origin.origin}/api/runner-callback/${payload.runId}`,
      CALLBACK_TOKEN: callbackToken,
    };
    for (const key of [
      "ANTHROPIC_API_KEY",
      "OPENAI_API_KEY",
      "E2B_API_KEY",
      "EXPO_TOKEN",
      "TYPESAFE_API_KEY",
    ] as const) {
      if (this.env[key]) envVars[key] = this.env[key];
    }
    await this.startAndWaitForPorts({
      startOptions: { envVars },
      cancellationOptions: { portReadyTimeoutMS: 60_000 },
    });
    const response = await this.containerFetch("http://container/run", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${callbackToken}`,
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok)
      throw new Error(`Harbor runner rejected the run (${response.status}).`);
    return response;
  }
}

export async function launchContainer(
  env: RunnerEnv,
  payload: RunnerPayload,
  callbackToken: string,
) {
  return getContainer(
    env.RUNNERS as unknown as DurableObjectNamespace<HarborRunner>,
    payload.runId,
  ).launch(payload, callbackToken);
}
