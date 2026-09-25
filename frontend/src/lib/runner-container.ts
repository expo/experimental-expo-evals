import {
  Container,
  getContainer,
  type StopParams,
} from "@cloudflare/containers";
import type { RunnerPayload } from "./cloud-types";
import { sandboxProvider } from "./task-provider";
export type { RunnerPayload } from "./cloud-types";

type RunnerEnv = {
  RUNNERS: DurableObjectNamespace;
  APP_ORIGIN?: string;
  ANTHROPIC_API_KEY?: string;
  OPENAI_API_KEY?: string;
  E2B_API_KEY?: string;
  EAS_PROJECT_ID?: string;
  EXPO_TOKEN?: string;
  TYPESAFE_API_KEY?: string;
};

/** A separate Container per run hosts Harbor and connects to the selected sandbox. */
export class HarborRunner extends Container<RunnerEnv> {
  defaultPort = 8080;
  sleepAfter = "70m";
  enableInternet = true;
  private callbackTokenForRedaction = "";

  private safeMessage(error: unknown) {
    const text = error instanceof Error ? error.message : String(error);
    const secrets = [
      this.callbackTokenForRedaction,
      this.env.ANTHROPIC_API_KEY,
      this.env.OPENAI_API_KEY,
      this.env.E2B_API_KEY,
      this.env.EXPO_TOKEN,
      this.env.TYPESAFE_API_KEY,
    ].filter((value): value is string => !!value);
    return secrets
      .sort((a, b) => b.length - a.length)
      .reduce((value, secret) => value.split(secret).join("[redacted]"), text)
      .slice(0, 1000);
  }

  private async phase(name: string, message?: string) {
    const event = {
      phase: name,
      at: new Date().toISOString(),
      ...(message ? { message: this.safeMessage(message) } : {}),
    };
    await this.ctx.storage.put("launchPhase", event);
    const runId = await this.ctx.storage.get<string>("acceptedRunId");
    console.log(
      JSON.stringify({
        event: "harbor_runner",
        runId: runId || null,
        ...event,
      }),
    );
  }

  /** Read durable diagnostics only. This never starts or restarts the container. */
  async inspect() {
    const [state, acceptedRunId, launchPhase] = await Promise.all([
      this.getState(),
      this.ctx.storage.get<string>("acceptedRunId"),
      this.ctx.storage.get<{ phase: string; at: string; message?: string }>(
        "launchPhase",
      ),
    ]);
    const running = this.ctx.container?.running ?? false;
    let health: {
      status?: number;
      ok?: boolean;
      runId?: string;
      runnerStatus?: string;
      error?: string;
    } | null = null;
    if (running && this.ctx.container) {
      try {
        // Low-level TCP fetch cannot auto-start a stopped container, unlike containerFetch().
        const response = await this.ctx.container
          .getTcpPort(8080)
          .fetch("http://container/health", {
            signal: AbortSignal.timeout(3000),
          });
        const body = (await response.json()) as Record<string, unknown>;
        health = {
          status: response.status,
          ...(typeof body.ok === "boolean" ? { ok: body.ok } : {}),
          ...(typeof body.runId === "string"
            ? { runId: this.safeMessage(body.runId) }
            : {}),
          ...(typeof body.status === "string"
            ? { runnerStatus: this.safeMessage(body.status) }
            : {}),
          ...(typeof body.error === "string"
            ? { error: this.safeMessage(body.error) }
            : {}),
        };
      } catch (error) {
        health = { error: this.safeMessage(error) };
      }
    }
    return {
      state,
      running,
      health,
      acceptedRunId: acceptedRunId || null,
      launchPhase: launchPhase || null,
    };
  }

  override async onStart() {
    await this.phase("ready");
  }

  override async onStop(params: StopParams) {
    await this.phase(
      "stopped",
      `Container exited: ${this.safeMessage(JSON.stringify(params))}`,
    );
  }

  override async onError(error: unknown) {
    const message = this.safeMessage(error);
    await this.phase("error", message);
    throw new Error(message);
  }

  async launch(payload: RunnerPayload, callbackToken: string) {
    this.callbackTokenForRedaction = callbackToken;
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
      sandboxProvider(payload.files) === "e2b"
        ? "E2B_API_KEY"
        : "EAS_PROJECT_ID",
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
    if (!claimed) return { runId: payload.runId, status: "accepted" as const };

    const envVars: Record<string, string> = {
      RUN_ID: payload.runId,
      CALLBACK_URL: `${origin.origin}/api/runner-callback/${payload.runId}`,
      CALLBACK_TOKEN: callbackToken,
    };
    for (const key of [
      "ANTHROPIC_API_KEY",
      "OPENAI_API_KEY",
      "E2B_API_KEY",
      "EAS_PROJECT_ID",
      "EXPO_TOKEN",
      "TYPESAFE_API_KEY",
    ] as const) {
      if (this.env[key]) envVars[key] = this.env[key];
    }
    let dispatched = false;
    try {
      await this.phase("starting");
      await this.startAndWaitForPorts({
        startOptions: { envVars },
        cancellationOptions: { portReadyTimeoutMS: 60_000 },
      });
      await this.phase("dispatching");
      if (!this.ctx.container?.running)
        throw new Error("Container stopped before run dispatch.");
      // From this point a lost response may still mean Harbor was accepted.
      dispatched = true;
      const response = await this.ctx.container
        .getTcpPort(8080)
        .fetch("http://container/run", {
          method: "POST",
          headers: {
            "content-type": "application/json",
            authorization: `Bearer ${callbackToken}`,
          },
          body: JSON.stringify(payload),
        });
      if (!response.ok) {
        const message = this.safeMessage(await response.text());
        throw new Error(
          `Harbor runner rejected the run (${response.status}): ${message}`,
        );
      }
      // Drain the HTTP body locally; no response stream needs to survive this RPC.
      await response.arrayBuffer();
      await this.phase("accepted");
      return { runId: payload.runId, status: "accepted" as const };
    } catch (error) {
      const message = this.safeMessage(error);
      if (!dispatched) {
        // No /run request was sent, so this cannot terminate a paid eval.
        // Release a partially started container as well as the database slot.
        await this.destroy().catch(() => {});
        await this.phase("launch_failed", message);
        return {
          runId: payload.runId,
          status: "failed" as const,
          error: message,
        };
      }
      await this.phase("launch_failed", message);
      throw new Error(message);
    }
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
