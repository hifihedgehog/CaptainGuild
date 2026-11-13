// Starts the HTTP server Reddit calls. createServer also sets up what the redis, settings and reddit helpers rely on.
import { createServer, getServerPort } from "@devvit/web/server";
import { serve } from "@hono/node-server";
import { app } from "./app";

serve({ fetch: app.fetch, createServer, port: getServerPort() });
