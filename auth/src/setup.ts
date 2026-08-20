import { createAuth } from "./auth-config.js";
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
  `CREATE TABLE IF NOT EXISTS auth_jwks (
    id TEXT PRIMARY KEY NOT NULL,
    public_key TEXT NOT NULL,
    private_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    alg TEXT,
    crv TEXT
  )`,
  `CREATE TABLE IF NOT EXISTS auth_oauth_client (
    id TEXT PRIMARY KEY NOT NULL,
    client_id TEXT NOT NULL UNIQUE,
    client_secret TEXT,
    client_discovery_id TEXT,
    disabled INTEGER DEFAULT 0,
    skip_consent INTEGER,
    enable_end_session INTEGER,
    subject_type TEXT,
    scopes TEXT,
    client_credentials_scopes TEXT DEFAULT '[]',
    user_id TEXT REFERENCES auth_user(id) ON DELETE CASCADE,
    created_at INTEGER,
    updated_at INTEGER,
    name TEXT,
    uri TEXT,
    icon TEXT,
    contacts TEXT,
    tos TEXT,
    policy TEXT,
    software_id TEXT,
    software_version TEXT,
    software_statement TEXT,
    redirect_uris TEXT NOT NULL,
    post_logout_redirect_uris TEXT,
    backchannel_logout_uri TEXT,
    backchannel_logout_session_required INTEGER,
    token_endpoint_auth_method TEXT,
    application_type TEXT,
    jwks TEXT,
    jwks_uri TEXT,
    grant_types TEXT,
    response_types TEXT,
    require_pkce INTEGER,
    dpop_bound_access_tokens INTEGER DEFAULT 0,
    reference_id TEXT,
    metadata TEXT
  )`,
  "CREATE INDEX IF NOT EXISTS auth_oauth_client_user_id_idx ON auth_oauth_client(user_id)",
  `CREATE TABLE IF NOT EXISTS auth_oauth_resource (
    id TEXT PRIMARY KEY NOT NULL,
    identifier TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    access_token_ttl INTEGER,
    refresh_token_ttl INTEGER,
    signing_algorithm TEXT,
    signing_key_id TEXT,
    allowed_scopes TEXT,
    custom_claims TEXT,
    dpop_bound_access_tokens_required INTEGER DEFAULT 0,
    disabled INTEGER DEFAULT 0,
    created_at INTEGER,
    updated_at INTEGER,
    policy_version INTEGER DEFAULT 1,
    metadata TEXT
  )`,
  `CREATE TABLE IF NOT EXISTS auth_oauth_client_resource (
    id TEXT PRIMARY KEY NOT NULL,
    client_id TEXT NOT NULL REFERENCES auth_oauth_client(client_id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES auth_oauth_resource(identifier) ON DELETE CASCADE,
    metadata TEXT,
    created_at INTEGER
  )`,
  "CREATE UNIQUE INDEX IF NOT EXISTS auth_oauth_client_resource_client_resource_uidx ON auth_oauth_client_resource(client_id, resource_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_client_resource_client_id_idx ON auth_oauth_client_resource(client_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_client_resource_resource_id_idx ON auth_oauth_client_resource(resource_id)",
  `CREATE TABLE IF NOT EXISTS auth_oauth_refresh_token (
    id TEXT PRIMARY KEY NOT NULL,
    token TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL REFERENCES auth_oauth_client(client_id) ON DELETE CASCADE,
    session_id TEXT REFERENCES auth_session(id) ON DELETE SET NULL,
    user_id TEXT NOT NULL REFERENCES auth_user(id) ON DELETE CASCADE,
    reference_id TEXT,
    authorization_code_id TEXT,
    resources TEXT,
    requested_user_info_claims TEXT,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    revoked INTEGER,
    rotated_at INTEGER,
    rotation_replay_response TEXT,
    rotation_replay_expires_at INTEGER,
    auth_time INTEGER,
    confirmation TEXT,
    scopes TEXT NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS auth_oauth_refresh_token_client_id_idx ON auth_oauth_refresh_token(client_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_refresh_token_session_id_idx ON auth_oauth_refresh_token(session_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_refresh_token_user_id_idx ON auth_oauth_refresh_token(user_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_refresh_token_code_id_idx ON auth_oauth_refresh_token(authorization_code_id)",
  `CREATE TABLE IF NOT EXISTS auth_oauth_access_token (
    id TEXT PRIMARY KEY NOT NULL,
    token TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL REFERENCES auth_oauth_client(client_id) ON DELETE CASCADE,
    session_id TEXT REFERENCES auth_session(id) ON DELETE SET NULL,
    user_id TEXT REFERENCES auth_user(id) ON DELETE CASCADE,
    reference_id TEXT,
    authorization_code_id TEXT,
    resources TEXT,
    requested_user_info_claims TEXT,
    refresh_id TEXT REFERENCES auth_oauth_refresh_token(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    revoked INTEGER,
    confirmation TEXT,
    scopes TEXT NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS auth_oauth_access_token_client_id_idx ON auth_oauth_access_token(client_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_access_token_session_id_idx ON auth_oauth_access_token(session_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_access_token_user_id_idx ON auth_oauth_access_token(user_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_access_token_code_id_idx ON auth_oauth_access_token(authorization_code_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_access_token_refresh_id_idx ON auth_oauth_access_token(refresh_id)",
  `CREATE TABLE IF NOT EXISTS auth_oauth_consent (
    id TEXT PRIMARY KEY NOT NULL,
    client_id TEXT NOT NULL REFERENCES auth_oauth_client(client_id) ON DELETE CASCADE,
    user_id TEXT REFERENCES auth_user(id) ON DELETE CASCADE,
    reference_id TEXT,
    resources TEXT,
    requested_user_info_claims TEXT,
    scopes TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  )`,
  "CREATE INDEX IF NOT EXISTS auth_oauth_consent_client_id_idx ON auth_oauth_consent(client_id)",
  "CREATE INDEX IF NOT EXISTS auth_oauth_consent_user_id_idx ON auth_oauth_consent(user_id)",
  `CREATE TABLE IF NOT EXISTS auth_oauth_client_assertion (
    id TEXT PRIMARY KEY NOT NULL,
    expires_at INTEGER NOT NULL
  )`,
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
