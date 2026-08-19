import { createAuth } from "./auth.js";
import { client, database } from "./database.js";
import { user } from "./schema.js";

const migration = [
  `CREATE TABLE IF NOT EXISTS auth_user (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    email_verified INTEGER DEFAULT 0 NOT NULL,
    image TEXT,
    created_at INTEGER DEFAULT (cast(unixepoch('subsecond') * 1000 as integer)) NOT NULL,
    updated_at INTEGER DEFAULT (cast(unixepoch('subsecond') * 1000 as integer)) NOT NULL
  )`,
  `CREATE TABLE IF NOT EXISTS auth_session (
    id TEXT PRIMARY KEY NOT NULL,
    expires_at INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at INTEGER DEFAULT (cast(unixepoch('subsecond') * 1000 as integer)) NOT NULL,
    updated_at INTEGER NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    user_id TEXT NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE
  )`,
  "CREATE INDEX IF NOT EXISTS auth_session_user_id_idx ON auth_session(user_id)",
  `CREATE TABLE IF NOT EXISTS auth_account (
    id TEXT PRIMARY KEY NOT NULL,
    issuer TEXT NOT NULL,
    account_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE,
    access_token TEXT,
    refresh_token TEXT,
    id_token TEXT,
    access_token_expires_at INTEGER,
    refresh_token_expires_at INTEGER,
    scope TEXT,
    password TEXT,
    created_at INTEGER DEFAULT (cast(unixepoch('subsecond') * 1000 as integer)) NOT NULL,
    updated_at INTEGER NOT NULL
  )`,
  "CREATE UNIQUE INDEX IF NOT EXISTS auth_account_issuer_account_id_uidx ON auth_account(issuer, account_id)",
  "CREATE INDEX IF NOT EXISTS auth_account_user_id_idx ON auth_account(user_id)",
  `CREATE TABLE IF NOT EXISTS auth_verification (
    id TEXT PRIMARY KEY NOT NULL,
    identifier TEXT NOT NULL,
    value TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER DEFAULT (cast(unixepoch('subsecond') * 1000 as integer)) NOT NULL,
    updated_at INTEGER DEFAULT (cast(unixepoch('subsecond') * 1000 as integer)) NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS auth_verification_identifier_idx ON auth_verification(identifier)",
];

export async function prepareAuthDatabase(): Promise<void> {
  await client.batch(migration, "write");

  const bootstrapEmail = process.env.NBK_AUTH_BOOTSTRAP_EMAIL?.trim().toLowerCase();
  const bootstrapPassword = process.env.NBK_AUTH_BOOTSTRAP_PASSWORD?.trim();
  if (Boolean(bootstrapEmail) !== Boolean(bootstrapPassword)) {
    throw new Error(
      "NBK_AUTH_BOOTSTRAP_EMAIL and NBK_AUTH_BOOTSTRAP_PASSWORD must be set together",
    );
  }

  const existing = await database.select({ id: user.id }).from(user).limit(1);
  if (existing.length > 0) return;
  if (!bootstrapEmail || !bootstrapPassword) {
    throw new Error(
      "No dashboard user exists; set NBK_AUTH_BOOTSTRAP_EMAIL and NBK_AUTH_BOOTSTRAP_PASSWORD",
    );
  }

  const bootstrapAuth = createAuth(true);
  await bootstrapAuth.api.signUpEmail({
    body: {
      name: "NB Medier",
      email: bootstrapEmail,
      password: bootstrapPassword,
    },
  });
  console.info(`Created initial dashboard user ${bootstrapEmail}`);
}
