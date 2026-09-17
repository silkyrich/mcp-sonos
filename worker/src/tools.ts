/**
 * MCP tool definitions.
 *
 * This connector is an *extension* to the official Sonos connector, not a
 * replacement. It only offers what Sonos's cloud Control API can't do:
 * announcements and sound clips, EQ, TV input, sleep timers, alarms, speaker
 * settings and arbitrary stream URLs. Normal playback, volume and grouping
 * stay with the official connector, and tool names here deliberately avoid
 * overlapping with its tools so a model doesn't pick the wrong one.
 *
 * Rooms can be named ("Kitchen", "living room") or given as the player ids
 * (RINCON_...) the official connector returns.
 */

import { Bridge, BridgeError, room } from "./bridge";

/** Text-to-speech runs here, on Workers AI, so the bridge needs no TTS key. */
export interface Speech {
  ai: Ai;
  model: string; // e.g. @cf/deepgram/aura-2-en
  voice: string; // e.g. draco
}

export interface Tool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  /** MCP tool annotations, so clients can tell writes apart and ask first. */
  annotations?: Record<string, boolean>;
  handler: (bridge: Bridge, args: Record<string, any>, speech: Speech) => Promise<unknown>;
}

async function sha256hex(s: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function toBytes(out: unknown): Promise<ArrayBuffer> {
  if (out instanceof ArrayBuffer) return out;
  if (ArrayBuffer.isView(out)) return out.buffer.slice(out.byteOffset, out.byteOffset + out.byteLength) as ArrayBuffer;
  if (out instanceof ReadableStream || out instanceof Response) return new Response(out as BodyInit).arrayBuffer();
  const audio = (out as { audio?: string })?.audio; // base64 (e.g. MeloTTS)
  if (typeof audio === "string") return Uint8Array.from(atob(audio), (c) => c.charCodeAt(0)).buffer;
  throw new Error("unexpected text-to-speech output");
}

/**
 * Speak `text`: the clip key is a hash of model, voice and text, so a phrase
 * is generated once and then replayed from the bridge's cache.
 */
async function announce(b: Bridge, a: Record<string, any>, speech: Speech): Promise<unknown> {
  const text = String(a.text ?? "").trim();
  if (!text) throw new Error("text is required");
  const voice = a.voice || speech.voice;
  const key = (await sha256hex(`workers-ai|${speech.model}|${voice}|${text}`)).slice(0, 32);
  const play = { clip: key, rooms: a.rooms ?? "all", volume: a.volume ?? null };
  try {
    return await b.post("/announce", play);
  } catch (e) {
    if (!(e instanceof BridgeError && e.status === 404 && e.message === "clip not cached")) throw e;
  }
  const out = await speech.ai.run(speech.model as keyof AiModels, { text, speaker: voice, encoding: "mp3" } as never);
  await b.putBytes(`/clips/${key}`, await toBytes(out), "audio/mpeg");
  return b.post("/announce", play);
}

const READ = { readOnlyHint: true };
const WRITE = { readOnlyHint: false, destructiveHint: false };

const ROOM = {
  type: "string",
  description: 'Room name (e.g. "Kitchen") or player id (RINCON_...) from the Sonos connector',
};
const ROOMS = {
  description: 'Room names or player ids, e.g. ["Kitchen", "Office"], or "all" (default)',
  anyOf: [{ type: "array", items: { type: "string" } }, { type: "string" }],
};
const NORMAL_PLAYBACK =
  "For normal playback (music, radio, volume, grouping) use the official Sonos connector instead.";

export const TOOLS: Tool[] = [
  {
    name: "local_rooms",
    description:
      "List the Sonos rooms the local bridge can see, with player ids (the same RINCON ids the official Sonos connector uses) " +
      "and whether each supports the audioClip fast path for announcements.",
    inputSchema: {
      type: "object",
      properties: { refresh: { type: "boolean", description: "Re-run network discovery first" } },
      additionalProperties: false,
    },
    annotations: READ,
    handler: (b, a) => b.get(`/rooms${a.refresh ? "?refresh=true" : ""}`),
  },
  {
    name: "announce",
    description:
      "Speak text aloud on Sonos speakers (text-to-speech). The music ducks or pauses for the announcement and then carries on. " +
      "Use for things like \"dinner's ready\" or reminders. " + NORMAL_PLAYBACK,
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", maxLength: 600, description: "What to say" },
        rooms: ROOMS,
        voice: {
          type: "string",
          description: "Optional voice, e.g. draco (British male), pandora (British female), luna, asteria, orion",
        },
        volume: { type: "integer", minimum: 0, maximum: 100, description: "Announcement volume (bridge default if omitted)" },
      },
      required: ["text"],
      additionalProperties: false,
    },
    annotations: WRITE,
    handler: announce,
  },
  {
    name: "search_sounds",
    description:
      "Find a sound effect to play with play_sound. Searches the bridge's local library, BBC Sound Effects (33,000 clips, " +
      "no key needed) and Freesound (if configured), returning descriptions, lengths and direct file URLs. " +
      "ALWAYS use this before searching the web for a sound: results here are real audio files the bridge can play. " +
      "For songs or music-like sounds (rain for sleep, a jingle), use the official Sonos connector and a music service instead.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: 'e.g. "thunder clap", "doorbell", "cat meow"' },
        limit: { type: "integer", minimum: 1, maximum: 25, description: "Results per source (default 8)" },
      },
      required: ["query"],
      additionalProperties: false,
    },
    annotations: READ,
    handler: (b, a) => b.get(`/sounds?q=${encodeURIComponent(String(a.query))}&limit=${a.limit ?? 8}`),
  },
  {
    name: "play_sound",
    description:
      "Play a sound effect over whatever is playing (the music ducks, then carries on). Give either `sound`, a name from the " +
      "bridge's library (see search_sounds; 'chime' is built in), or `url`, a direct link to an audio file. Any format works " +
      "(mp3, wav, ogg, flac, m4a): the bridge downloads and converts it, so pass the file's own URL, not a web page or player " +
      "page. To find one: 1) search_sounds, 2) the official Sonos connector for anything on a music service, 3) only then a web " +
      "search. For a stream that should keep playing, use play_stream_url.",
    inputSchema: {
      type: "object",
      properties: {
        sound: { type: "string", description: "Library name, e.g. chime" },
        url: { type: "string", description: "Direct http(s) URL of an audio file, e.g. from search_sounds" },
        rooms: ROOMS,
        volume: { type: "integer", minimum: 0, maximum: 100 },
      },
      additionalProperties: false,
    },
    annotations: WRITE,
    handler: (b, a) => {
      if (!a.sound && !a.url) throw new Error("pass sound (a library name) or url");
      return b.post("/play", { sound: a.sound ?? null, url: a.url ?? null, rooms: a.rooms ?? "all", volume: a.volume ?? null });
    },
  },
  {
    name: "play_stream_url",
    description:
      "Start playing an arbitrary internet stream URL (e.g. an Icecast/mp3 radio stream not available through a music service) " +
      "in a room, replacing what's playing. " + NORMAL_PLAYBACK,
    inputSchema: {
      type: "object",
      properties: { room: ROOM, url: { type: "string" }, title: { type: "string" } },
      required: ["room", "url"],
      additionalProperties: false,
    },
    annotations: WRITE,
    handler: (b, a) => b.post(`${room(a.room)}/stream`, { url: a.url, title: a.title ?? null }),
  },
  {
    name: "get_eq",
    description:
      "Read a room's sound settings: bass, treble, loudness, balance, and (where fitted) sub and surround levels.",
    inputSchema: { type: "object", properties: { room: ROOM }, required: ["room"], additionalProperties: false },
    annotations: READ,
    handler: (b, a) => b.get(`${room(a.room)}/eq`),
  },
  {
    name: "set_eq",
    description:
      "Change a room's sound settings. Only the fields passed change. Returns the previous values so the change can be undone. " +
      "Sub fields need a Sub; surround fields need a soundbar with surrounds.",
    inputSchema: {
      type: "object",
      properties: {
        room: ROOM,
        bass: { type: "integer", minimum: -10, maximum: 10 },
        treble: { type: "integer", minimum: -10, maximum: 10 },
        loudness: { type: "boolean" },
        balance: {
          type: "object",
          description: "Channel levels 0-100; {left:100,right:100} is centred",
          properties: { left: { type: "integer", minimum: 0, maximum: 100 }, right: { type: "integer", minimum: 0, maximum: 100 } },
          required: ["left", "right"],
        },
        sub_enabled: { type: "boolean" },
        sub_gain: { type: "integer", minimum: -15, maximum: 15 },
        surround_enabled: { type: "boolean" },
        surround_level: { type: "integer", minimum: -15, maximum: 15, description: "Surround level for TV audio" },
        music_surround_level: { type: "integer", minimum: -15, maximum: 15, description: "Surround level for music" },
        surround_full_volume_enabled: { type: "boolean", description: "Full (not ambient) surrounds for music" },
      },
      required: ["room"],
      additionalProperties: false,
    },
    annotations: { ...WRITE, idempotentHint: true },
    handler: (b, { room: r, ...changes }) => b.post(`${room(r)}/eq`, changes),
  },
  {
    name: "switch_to_tv",
    description: "Switch a soundbar (Arc, Beam, Ray, Playbar...) to its TV input.",
    inputSchema: { type: "object", properties: { room: ROOM }, required: ["room"], additionalProperties: false },
    annotations: { ...WRITE, idempotentHint: true },
    handler: (b, a) => b.post(`${room(a.room)}/tv`),
  },
  {
    name: "sleep_timer",
    description:
      "Read or set a room's sleep timer. Omit minutes to read it; minutes > 0 sets it; 0 cancels it.",
    inputSchema: {
      type: "object",
      properties: { room: ROOM, minutes: { type: "number", minimum: 0, maximum: 1440 } },
      required: ["room"],
      additionalProperties: false,
    },
    handler: (b, a) =>
      a.minutes === undefined ? b.get(`${room(a.room)}/sleep`) : b.post(`${room(a.room)}/sleep`, { minutes: a.minutes }),
  },
  {
    name: "speaker_settings",
    description:
      "Read or change a speaker's status light and touch controls (buttons). Omit both to read. Returns previous values when changing.",
    inputSchema: {
      type: "object",
      properties: {
        room: ROOM,
        status_light: { type: "boolean" },
        buttons_enabled: { type: "boolean", description: "false locks the touch controls" },
      },
      required: ["room"],
      additionalProperties: false,
    },
    handler: (b, a) =>
      a.status_light === undefined && a.buttons_enabled === undefined
        ? b.get(`${room(a.room)}/settings`)
        : b.post(`${room(a.room)}/settings`, { status_light: a.status_light ?? null, buttons_enabled: a.buttons_enabled ?? null }),
  },
  {
    name: "list_alarms",
    description: "List Sonos alarms: id, room, time, recurrence, enabled, volume, duration and what they play.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: READ,
    handler: (b) => b.get("/alarms"),
  },
  {
    name: "set_alarm",
    description:
      "Create an alarm (omit id) or edit one (pass id from list_alarms; only fields passed change, so an existing alarm keeps " +
      "its radio station or playlist). New alarms use the Sonos chime. Returns the previous version when editing.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "Alarm id to edit; omit to create" },
        room: ROOM,
        time: { type: "string", description: "24-hour HH:MM, local time" },
        recurrence: {
          description: 'daily | weekdays | weekends | once, or a list of days like ["mon","wed","fri"]',
          anyOf: [{ type: "string" }, { type: "array", items: { type: "string" } }],
        },
        enabled: { type: "boolean" },
        volume: { type: "integer", minimum: 0, maximum: 100 },
        include_grouped_rooms: { type: "boolean" },
        duration_minutes: { type: "integer", minimum: 0, description: "Stop after this long; 0 for no limit" },
      },
      additionalProperties: false,
    },
    annotations: WRITE,
    handler: (b, a) => b.post("/alarms", a),
  },
  {
    name: "delete_alarm",
    description: "Delete an alarm by id. Returns it in full so it can be recreated.",
    inputSchema: {
      type: "object",
      properties: { id: { type: "string" } },
      required: ["id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: true },
    handler: (b, a) => b.del(`/alarms/${encodeURIComponent(String(a.id))}`),
  },
];

export const TOOLS_BY_NAME = new Map(TOOLS.map((t) => [t.name, t]));
