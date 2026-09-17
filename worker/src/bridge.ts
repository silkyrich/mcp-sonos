/**
 * Client for the LAN bridge, reached through a Cloudflare Tunnel whose
 * hostname is protected by an Access service-token policy.
 *
 * Two credentials on every request:
 *   CF-Access-Client-Id/Secret  gets past Access at the tunnel's edge
 *   Authorization: Bearer       the bridge's own API_TOKEN (defence in depth,
 *                               and what protects it on the LAN)
 */

export interface BridgeConfig {
  url: string; // https://sonos-bridge.example.com
  token: string;
  accessClientId?: string;
  accessClientSecret?: string;
}

export class BridgeError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export class Bridge {
  constructor(private cfg: BridgeConfig) {}

  async call(method: "GET" | "POST" | "DELETE", path: string, body?: unknown): Promise<unknown> {
    const headers: Record<string, string> = { authorization: `Bearer ${this.cfg.token}` };
    if (body !== undefined) headers["content-type"] = "application/json";
    if (this.cfg.accessClientId && this.cfg.accessClientSecret) {
      headers["CF-Access-Client-Id"] = this.cfg.accessClientId;
      headers["CF-Access-Client-Secret"] = this.cfg.accessClientSecret;
    }
    const res = await fetch(`${this.cfg.url.replace(/\/$/, "")}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: "manual", // an Access login redirect means the service token was refused
    });
    const text = await res.text();
    if (res.status >= 300 && res.status < 400) {
      throw new BridgeError(res.status, "Cloudflare Access refused the service token (redirected to login)");
    }
    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
    if (!res.ok) {
      const detail = (data as { detail?: unknown })?.detail ?? text.slice(0, 300);
      throw new BridgeError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  get = (path: string) => this.call("GET", path);
  post = (path: string, body: unknown = {}) => this.call("POST", path, body);
  del = (path: string) => this.call("DELETE", path);
}

export const room = (r: unknown) => `/rooms/${encodeURIComponent(String(r))}`;
