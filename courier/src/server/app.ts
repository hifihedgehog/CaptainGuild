// The courier's routes: the scheduled run, and the two checks Reddit calls when a moderator saves a setting.
import type { TaskRequest, TaskResponse } from "@devvit/web/server";
import type { SettingsValidationRequest, SettingsValidationResponse } from "@devvit/web/shared";
import { Hono } from "hono";
import { parseAccounts, parseWebhookUrl } from "../shared/protocol";
import { deliverAll } from "./deliver";

export const app = new Hono();

app.post("/internal/scheduler/deliver", async (c) => {
  await c.req.json<TaskRequest>().catch(() => undefined);
  try {
    await deliverAll();
  } catch (error) {
    console.error(`deliver: the run failed: ${String(error)}`);
  }
  // Always answered as done. The next run is a minute away, and it picks up whatever this one left.
  return c.json<TaskResponse>({ status: "ok" }, 200);
});

/** What Reddit is told about a setting a moderator is saving. An empty one is fine: the courier then does nothing. */
function verdict(value: string | undefined, check: (value: string) => unknown): SettingsValidationResponse {
  if (!value) return { success: true };
  try {
    check(value);
    return { success: true };
  } catch (error) {
    return { success: false, error: (error as Error).message };
  }
}

app.post("/internal/settings/webhook-url", async (c) => {
  const { value } = await c.req.json<SettingsValidationRequest<string>>();
  return c.json<SettingsValidationResponse>(verdict(value, parseWebhookUrl));
});

app.post("/internal/settings/accounts", async (c) => {
  const { value } = await c.req.json<SettingsValidationRequest<string>>();
  return c.json<SettingsValidationResponse>(verdict(value, parseAccounts));
});
