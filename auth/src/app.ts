import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { Hono } from "hono";

import { auth } from "./auth.js";

const loginPath = fileURLToPath(new URL("../static/login.html", import.meta.url));

function wantsHtml(request: Request): boolean {
  return request.headers.get("accept")?.includes("text/html") ?? false;
}

async function sessionState(headers: Headers) {
  try {
    const session = await auth.api.getSession({ headers });
    return session
      ? { status: "ok" as const, session }
      : { status: "unauthenticated" as const };
  } catch (error) {
    console.error("Unable to verify dashboard session", error);
    return { status: "error" as const };
  }
}

function unavailable(request: Request): Response {
  if (!wantsHtml(request)) {
    return Response.json({ detail: "Authentication service unavailable" }, { status: 503 });
  }
  return new Response(
    `<!doctype html><html lang="da"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Serveren svarer ikke</title><style>body{font-family:system-ui;max-width:34rem;margin:15vh auto;padding:1.5rem;color:#18181b}a{color:inherit}</style><h1>Kan ikke kontakte serveren</h1><p>Din session er ikke blevet fjernet. Prøv igen om et øjeblik.</p><p><a href="/">Prøv igen</a></p></html>`,
    { status: 503, headers: { "content-type": "text/html; charset=UTF-8" } },
  );
}

export function createGateway(upstream: string): Hono {
  const app = new Hono();

  app.all("/api/auth/*", (context) => auth.handler(context.req.raw));

  app.get("/login", async (context) => {
    const state = await sessionState(context.req.raw.headers);
    if (state.status === "error") return unavailable(context.req.raw);
    if (state.status === "ok") return context.redirect("/");
    return context.html(await readFile(loginPath, "utf8"));
  });

  app.get("/healthz", async (context) => {
    try {
      const response = await fetch(`${upstream}/healthz`, {
        headers: { accept: "application/json" },
      });
      return context.json({ status: response.ok ? "ok" : "unhealthy" }, response.ok ? 200 : 503);
    } catch {
      return context.json({ status: "unhealthy" }, 503);
    }
  });

  app.all("*", async (context) => {
    const state = await sessionState(context.req.raw.headers);
    if (state.status === "error") return unavailable(context.req.raw);
    if (state.status === "unauthenticated") {
      if (context.req.method === "GET" && wantsHtml(context.req.raw)) {
        const url = new URL(context.req.url);
        const next = encodeURIComponent(`${url.pathname}${url.search}`);
        return context.redirect(`/login?next=${next}`);
      }
      return context.json({ detail: "Authentication required" }, 401);
    }

    const incoming = context.req.raw;
    const target = new URL(incoming.url);
    const destination = new URL(`${target.pathname}${target.search}`, upstream);
    const headers = new Headers(incoming.headers);
    headers.delete("host");
    headers.delete("x-nbk-user-email");
    headers.set("x-nbk-user-email", state.session.user.email);
    const requestInit: RequestInit & { duplex: "half" } = {
      method: incoming.method,
      headers,
      body: incoming.method === "GET" || incoming.method === "HEAD" ? undefined : incoming.body,
      redirect: "manual",
      duplex: "half",
    };
    const response = await fetch(destination, requestInit);
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  });

  return app;
}
