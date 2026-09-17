/**
 * mcp-sonos — remote MCP connector for the local Sonos bridge.
 *
 * Wiring:
 *   OAuthProvider  guards the /mcp route (Claude must present a valid token).
 *   defaultHandler owns the browser-facing OAuth pages:
 *      /authorize  → stash Claude's OAuth request, hand the browser to Access
 *      /callback   → Access returns an identity; complete Claude's grant
 *   apiHandler     → the MCP JSON-RPC endpoint, reached only after auth.
 *
 * Authentication is delegated to Cloudflare Access (OIDC), backed by the
 * account's own identity providers and Access policies — the same upstream
 * pattern used by mcp-health. Access performs the real identity check; the
 * ALLOWED_EMAIL comparison below is a second, local check so this connector
 * stays bound to one person even if an Access policy is later widened.
 */

import OAuthProvider, { type OAuthHelpers, type AuthRequest } from "@cloudflare/workers-oauth-provider";
import { handleMcp } from "./mcp";
import { log, logError } from "./log";
import type { BridgeConfig } from "./bridge";

interface Env {
  OAUTH_KV: KVNamespace;
  OAUTH_PROVIDER: OAuthHelpers; // injected by the provider

  // Secrets (wrangler secret put ...)
  BRIDGE_URL: string; // https://<tunnel hostname>
  BRIDGE_TOKEN: string; // the bridge's API_TOKEN
  BRIDGE_ACCESS_CLIENT_ID?: string; // Access service token for the tunnel hostname
  BRIDGE_ACCESS_CLIENT_SECRET?: string;
  ALLOWED_EMAIL: string;
  ACCESS_TEAM_DOMAIN: string; // e.g. your-team.cloudflareaccess.com
  ACCESS_CLIENT_ID: string;
  ACCESS_CLIENT_SECRET: string;
}

// Props carried on the token and exposed to the API handler via ctx.props.
interface Props {
  email: string;
  [key: string]: unknown;
}

// A fetch handler with a *required* fetch method (what the provider expects).
type Handler = {
  fetch: (request: Request, env: Env, ctx: ExecutionContext) => Response | Promise<Response>;
};

const LOGIN_TTL_SECONDS = 600;

/** Access exposes a standard OIDC trio under the team domain. */
const oidc = (env: Env, leaf: "authorization" | "token" | "userinfo") =>
  `https://${env.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/sso/oidc/${env.ACCESS_CLIENT_ID}/${leaf}`;

function page(inner: string, status = 200): Response {
  return new Response(
    `<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>mcp-sonos</title>
<style>
 :root{color-scheme:light dark}
 body{font:16px/1.5 system-ui,sans-serif;max-width:26rem;margin:12vh auto;padding:0 1.25rem}
 h1{font-size:1.25rem;margin-bottom:.25rem}.sub{color:#666;margin-top:0}
 .err{color:#b00}.ok{color:#0a0}
</style>${inner}`,
    { status, headers: { "content-type": "text/html; charset=utf-8" } },
  );
}

/** Browser-facing OAuth + landing pages. */
const defaultHandler: Handler = {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    const redirectUri = new URL("/callback", request.url).toString();

    // ── Step 1: Claude sends the user here to authorize ─────────────────
    if (url.pathname === "/authorize") {
      let oauthReq: AuthRequest;
      try {
        oauthReq = await env.OAUTH_PROVIDER.parseAuthRequest(request);
      } catch (e) {
        logError("oauth.authorize.parse_failed", { reason: (e as Error).message });
        return page(`<h1 class="err">Invalid authorization request</h1>`, 400);
      }
      const client = await env.OAUTH_PROVIDER.lookupClient(oauthReq.clientId);
      if (!client) {
        logError("oauth.authorize.unknown_client", { clientId: oauthReq.clientId });
        return page(`<h1 class="err">Unknown OAuth client</h1>`, 400);
      }
      log("oauth.authorize.start", {
        clientId: oauthReq.clientId,
        clientName: client.clientName,
        scope: oauthReq.scope,
      });

      // Stash Claude's request across the Access round-trip.
      const state = crypto.randomUUID();
      await env.OAUTH_KV.put(`login:${state}`, JSON.stringify(oauthReq), {
        expirationTtl: LOGIN_TTL_SECONDS,
      });

      const a = new URL(oidc(env, "authorization"));
      a.searchParams.set("response_type", "code");
      a.searchParams.set("client_id", env.ACCESS_CLIENT_ID);
      a.searchParams.set("redirect_uri", redirectUri);
      a.searchParams.set("scope", "openid email profile");
      a.searchParams.set("state", state);
      return Response.redirect(a.toString(), 302);
    }

    // ── Step 2: Access redirects back here with a code ──────────────────
    if (url.pathname === "/callback") {
      const code = url.searchParams.get("code");
      const state = url.searchParams.get("state");
      if (!code || !state) {
        logError("oauth.callback.missing_params", {
          hasCode: !!code,
          hasState: !!state,
          upstreamError: url.searchParams.get("error"),
        });
        return page(`<h1 class="err">Missing code</h1><p class="sub">Start the connection again.</p>`, 400);
      }

      const stashed = await env.OAUTH_KV.get(`login:${state}`);
      if (!stashed) {
        logError("oauth.callback.state_expired");
        return page(`<h1 class="err">Session expired</h1><p class="sub">This took too long. Reconnect from Claude.</p>`, 400);
      }
      const oauthReq = JSON.parse(stashed) as AuthRequest;

      const tokenRes = await fetch(oidc(env, "token"), {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: "authorization_code",
          code,
          client_id: env.ACCESS_CLIENT_ID,
          client_secret: env.ACCESS_CLIENT_SECRET,
          redirect_uri: redirectUri,
        }),
      });
      if (!tokenRes.ok) {
        const detail = (await tokenRes.text()).slice(0, 300);
        logError("oauth.access.token_failed", { status: tokenRes.status, detail });
        return page(
          `<h1 class="err">Sign-in failed</h1><p class="sub">Access returned ${tokenRes.status}.</p>`,
          502,
        );
      }
      const tokens = (await tokenRes.json()) as { access_token?: string };

      // Ask Access who this is rather than trusting the id_token unchecked.
      let email: string | null = null;
      if (tokens.access_token) {
        const who = await fetch(oidc(env, "userinfo"), {
          headers: { authorization: `Bearer ${tokens.access_token}` },
        });
        if (who.ok) {
          const info = (await who.json()) as { email?: string };
          email = info.email ?? null;
        } else {
          logError("oauth.access.userinfo_failed", { status: who.status });
        }
      }
      if (!email) {
        return page(`<h1 class="err">No identity returned</h1><p class="sub">Access did not provide an email.</p>`, 502);
      }

      // Local bind: this connector serves exactly one person.
      if (env.ALLOWED_EMAIL && email !== env.ALLOWED_EMAIL) {
        logError("oauth.denied", { attemptedEmail: email });
        return page(
          `<h1 class="err">Access denied</h1><p class="sub">This connector is restricted to a single account.</p>`,
          403,
        );
      }

      await env.OAUTH_KV.delete(`login:${state}`);
      const props: Props = { email };
      const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
        request: oauthReq,
        userId: email,
        metadata: { via: "cloudflare-access" },
        scope: oauthReq.scope,
        props,
      });
      log("oauth.granted", { email, clientId: oauthReq.clientId, via: "cloudflare-access" });
      return page(
        `<h1 class="ok">Connected</h1><p class="sub">Signed in as ${email}. You can close this tab.</p>
         <script>location.href=${JSON.stringify(redirectTo)}</script>
         <p><a href="${redirectTo}">Continue</a></p>`,
      );
    }

    // ── Unauthenticated liveness only ───────────────────────────────────
    if (url.pathname === "/") return new Response("mcp-sonos");

    return page(`<h1>Not found</h1>`, 404);
  },
};

/** The MCP endpoint — only reached with a valid token. */
const apiHandler: Handler = {
  async fetch(request, env, ctx): Promise<Response> {
    const props = (ctx as ExecutionContext & { props?: Props }).props;
    // Defence in depth: the token should only exist for ALLOWED_EMAIL.
    if (!props || (env.ALLOWED_EMAIL && props.email !== env.ALLOWED_EMAIL)) {
      logError("mcp.forbidden", { email: props?.email ?? null });
      return new Response("Forbidden", { status: 403 });
    }
    const cfg: BridgeConfig = {
      url: env.BRIDGE_URL,
      token: env.BRIDGE_TOKEN,
      accessClientId: env.BRIDGE_ACCESS_CLIENT_ID,
      accessClientSecret: env.BRIDGE_ACCESS_CLIENT_SECRET,
    };
    return handleMcp(request, cfg);
  },
};

export default new OAuthProvider<Env>({
  apiRoute: "/mcp",
  apiHandler,
  defaultHandler,
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",
  scopesSupported: ["mcp"],
});
