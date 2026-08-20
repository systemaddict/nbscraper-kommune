import { cimd } from "@better-auth/cimd";
import { mcp } from "@better-auth/mcp";
import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { jwt } from "better-auth/plugins";

import { database } from "./database.js";
import * as schema from "./schema.js";
import { fetchClientMetadataResource } from "./cimd-fetch.js";

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export function createAuth(allowSignUp = false) {
  const mcpBaseUrl = process.env.NBK_MCP_BASE_URL?.trim().replace(/\/+$/, "");
  return betterAuth({
    appName: "Kommune scraper",
    baseURL: required("NBK_AUTH_BASE_URL"),
    secret: required("NBK_AUTH_SECRET"),
    database: drizzleAdapter(database, { provider: "sqlite", schema }),
    emailAndPassword: {
      enabled: true,
      disableSignUp: !allowSignUp,
    },
    // The dashboard can deploy before the MCP endpoint gets its Bunny hostname.
    // Once NBK_MCP_BASE_URL is configured, all OAuth endpoints enable together.
    plugins: mcpBaseUrl
      ? [
          jwt({ jwks: { keyPairConfig: { alg: "RS256", modulusLength: 2048 } } }),
          mcp({
            loginPage: "/login",
            consentPage: "/consent",
            resource: `${mcpBaseUrl}/mcp`,
            scopes: ["openid", "offline_access", "search:articles"],
            clientRegistrationDefaultScopes: [
              "openid",
              "offline_access",
              "search:articles",
            ],
            allowDynamicClientRegistration: true,
            allowUnauthenticatedClientRegistration: true,
          }),
          cimd({
            fetchClientMetadataResource,
            metadataProfile: "mcp-2026-07-28",
          }),
        ]
      : [],
    telemetry: { enabled: false },
  });
}
