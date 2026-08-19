import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";

import { database } from "./database.js";
import * as schema from "./schema.js";

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export function createAuth(allowSignUp = false) {
  return betterAuth({
    appName: "Kommune scraper",
    baseURL: required("NBK_AUTH_BASE_URL"),
    secret: required("NBK_AUTH_SECRET"),
    database: drizzleAdapter(database, { provider: "sqlite", schema }),
    emailAndPassword: {
      enabled: true,
      disableSignUp: !allowSignUp,
    },
    telemetry: { enabled: false },
  });
}

export const auth = createAuth();
