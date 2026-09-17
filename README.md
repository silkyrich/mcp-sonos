# mcp-sonos

An **extension** to the official Sonos connector for [Claude](https://claude.ai).
It adds what Sonos's cloud API doesn't offer: spoken announcements, sound
clips, EQ, TV input, sleep timers, alarms and speaker settings.

It doesn't replace the official connector. Keep that for music, volume and
grouping. This connector only fills the gaps, and its tool names don't overlap
with the official connector's, so Claude can use both side by side.

```
Claude ──▶ official Sonos connector ──▶ Sonos cloud ───────────────────────▶ speakers
Claude ──OAuth──▶ Worker ──Access──▶ Tunnel ──▶ bridge (Docker, your LAN) ──▶ speakers
                  (worker/)                     (bridge/)
```

The **bridge** is a small Python service (FastAPI + [SoCo](https://github.com/SoCo/SoCo))
that runs in Docker on a machine on the same network as your speakers. The
**Worker** is a Cloudflare Worker that speaks MCP to Claude and forwards calls
to the bridge through a Cloudflare Tunnel.

## Tools

| Tool | What it does | Why it's here |
|------|--------------|---------------|
| `announce` | Text-to-speech ([ElevenLabs](https://elevenlabs.io)) on chosen rooms or all. The music ducks and carries on afterwards. | Cloud API has no TTS or arbitrary-clip playback for personal integrations |
| `play_sound` | Plays a short mp3 URL (doorbell, chime) over whatever is playing | Same |
| `play_stream_url` | Starts playing any stream URL (e.g. an Icecast station) | Cloud API only plays content from music services |
| `get_eq` / `set_eq` | Bass, treble, loudness, balance, sub and surround levels | Not in the cloud API |
| `switch_to_tv` | Switches a soundbar to its TV input | Not in the cloud API |
| `sleep_timer` | Reads, sets or cancels a room's sleep timer | Not in the cloud API |
| `speaker_settings` | Status light and touch-control lock | Not in the cloud API |
| `list_alarms` / `set_alarm` / `delete_alarm` | Alarm management. Edits only change the fields passed. | Not in the cloud API |
| `local_rooms` | Rooms the bridge can see, with player ids and whether each supports the fast clip path | Diagnostics |

Rooms can be given by name (`"Kitchen"`, `"living room"`) or by the player id
(`RINCON_...`) the official connector returns, so Claude can pass ids between
the two. Every setter returns the previous values, so any change can be undone.

**If the official connector later adds one of these, remove the tool here.**
Don't keep two versions.

## How announcements play

Each speaker gets one of two methods:

- **audioClip** (most S2 speakers). The speaker's local API plays the clip over
  what's playing, ducking it, then carries on by itself. Takes well under a
  second, and nothing about grouping or playback changes, so the official
  connector never notices. The bridge checks each speaker's capabilities once.
- **UPnP with snapshot and restore** (speakers without audioClip). Saves the
  group layout and each speaker's playback and volume, temporarily groups the
  target rooms, plays the clip, then rebuilds the groups and restores playback.
  TV and line-in come back through their stream URIs. Two known limits: queues
  started from a music service in the Sonos app may not resume, and group ids
  seen by the official connector may change afterwards.

Spoken phrases are cached on disk by a hash of voice, model and text, so
repeats cost nothing and play immediately.

## Security model

- **No secrets in this repo.** All credentials live in `.env` on the bridge
  host (gitignored) or as encrypted Worker secrets. Templates are
  `bridge/.env.example` and `worker/.dev.vars.example`.
- **Claude → Worker:** OAuth, with login delegated to Cloudflare Access (OIDC).
  The Worker also rejects any identity other than `ALLOWED_EMAIL`.
- **Worker → bridge:** the tunnel hostname is protected by an Access
  *service token* policy, and the bridge also requires its own bearer token.
  The bridge never needs an open inbound port on your router.
- **On the LAN:** everything except `/health` and `/audio/*` needs the bearer
  token. `/audio/*` serves only cached announcement clips, because the
  speakers fetch them without credentials.
- **Writes are narrow and labelled.** Write tools carry MCP annotations
  (`readOnlyHint: false`, and `destructiveHint` on `delete_alarm`) so clients
  can ask before running them.
- **Unofficial interfaces:** the bridge uses UPnP and the speakers' local API
  on port 1443. The `X-Sonos-Api-Key` it sends is a widely published community
  value, not a credential. Sonos could change either interface in a firmware
  update.

## Setup

### Prerequisites

- A machine with Docker on the same network as the speakers (not a separate
  VLAN, since discovery uses multicast). Linux is best: the bridge needs
  `network_mode: host`, which Docker Desktop for Mac and Windows may not support.
- For `announce`, an ElevenLabs API key. Everything else works without one.
- For Claude, a Cloudflare account with Zero Trust (Access) and at least one
  identity provider.

### 1. Run the bridge

```bash
cd bridge
cp .env.example .env          # set API_TOKEN (openssl rand -hex 24), HOST_IP, ELEVENLABS_API_KEY
DEPLOY_HOST=you@docker-host ./deploy.sh
```

Or run `docker compose up -d --build` on the host itself. Then check it:

```bash
curl -H "Authorization: Bearer $API_TOKEN" http://HOST_IP:8765/rooms
./announce "Hello" Kitchen
```

If `/rooms` is empty, discovery is blocked: check host networking and that the
speakers are on the same subnet.

### 2. Expose the bridge

1. Zero Trust → **Networks → Tunnels** → create a tunnel. Add a public hostname
   (e.g. `sonos-bridge.example.com`) pointing to `http://localhost:8765`. Put
   the tunnel token in `.env` as `TUNNEL_TOKEN`, then run
   `PROFILES="--profile tunnel" DEPLOY_HOST=... ./deploy.sh`.
2. Access → **Service Auth** → create a service token and note its id and
   secret.
3. Access → **Applications** → add a *self-hosted* app for that hostname, with
   a policy whose action is **Service Auth** and which includes the token.

### 3. Deploy the Worker

1. Zero Trust → Access → **Applications** → add a **SaaS / OIDC** app with
   redirect URL `https://mcp-sonos.<subdomain>.workers.dev/callback`. Note the
   client id, client secret and team domain, and restrict its policy to you.
2. Deploy:

```bash
cd worker
npm install
npx wrangler kv namespace create OAUTH_KV     # paste the id into wrangler.jsonc
npx wrangler secret put BRIDGE_URL                   # https://sonos-bridge.example.com
npx wrangler secret put BRIDGE_TOKEN                 # the bridge's API_TOKEN
npx wrangler secret put BRIDGE_ACCESS_CLIENT_ID      # service token (step 2)
npx wrangler secret put BRIDGE_ACCESS_CLIENT_SECRET
npx wrangler secret put ALLOWED_EMAIL
npx wrangler secret put ACCESS_TEAM_DOMAIN           # your-team.cloudflareaccess.com
npx wrangler secret put ACCESS_CLIENT_ID             # OIDC app (above)
npx wrangler secret put ACCESS_CLIENT_SECRET
npm run deploy
```

The KV namespace id isn't a secret. It's only usable with your Cloudflare
account credentials. If you'd rather not commit it in a fork, keep that edit
local.

### 4. Add to Claude

Settings → **Connectors** → **Add custom connector** →
`https://mcp-sonos.<subdomain>.workers.dev/mcp`. Keep the official Sonos
connector enabled alongside it.

## Development

```bash
cd bridge
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q                      # fake SoCo household; no speakers needed

# against real speakers, from a machine on the LAN:
API_TOKEN=dev HOST_IP=<this machine's LAN IP> CACHE_DIR=/tmp/sonos-cache \
  .venv/bin/uvicorn app.server:app --host 0.0.0.0 --port 8765

cd ../worker && npm install && npm run typecheck
```

HTTP endpoints are documented at the top of `bridge/app/server.py`.

## Roadmap

- Chime before announcements; per-room default announcement volumes
- Queue with priority for overlapping announcements
- Alarms that play a Sonos favourite
- Reading and editing the local queue

## License

MIT
