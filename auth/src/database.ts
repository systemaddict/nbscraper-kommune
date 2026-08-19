import { createClient } from "@libsql/client";
import { drizzle } from "drizzle-orm/libsql";

import * as schema from "./schema.js";

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export const client = createClient({
  url: required("BUNNY_DATABASE_URL"),
  authToken: process.env.BUNNY_DATABASE_AUTH_TOKEN?.trim() || undefined,
});

export const database = drizzle(client, { schema });
