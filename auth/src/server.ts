import { serve } from "@hono/node-server";
import { existsSync } from "node:fs";

if (existsSync(".env")) process.loadEnvFile(".env");

const { prepareAuthDatabase } = await import("./setup.js");

function argument(name: string, fallback: string): string {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] ?? fallback : fallback;
}

await prepareAuthDatabase();
const { createGateway } = await import("./app.js");

const hostname = argument("host", "0.0.0.0");
const port = Number.parseInt(argument("port", "8000"), 10);
const upstream = argument("upstream", "http://127.0.0.1:8001");

serve({ fetch: createGateway(upstream).fetch, hostname, port }, (info) => {
  console.info(`Protected dashboard listening on http://${info.address}:${info.port}`);
});
