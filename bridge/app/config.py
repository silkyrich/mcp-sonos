import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"missing required env var {name}")
    return v  # type: ignore[return-value]


@dataclass(frozen=True)
class Settings:
    # ElevenLabs
    eleven_api_key: str
    eleven_voice_id: str
    eleven_model: str
    eleven_output_format: str

    # Bridge
    api_token: str            # bearer token callers must present
    host_ip: str              # LAN IP the Sonos players fetch audio from (this box)
    port: int
    cache_dir: str
    default_volume: int       # announcement volume, 0-100
    max_clip_seconds: int     # safety cap on how long we wait for a clip
    discovery_timeout: int


def load() -> Settings:
    return Settings(
        eleven_api_key=_env("ELEVENLABS_API_KEY", ""),  # optional: only `announce` needs it,
        eleven_voice_id=_env("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb"),  # "George"
        eleven_model=_env("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
        eleven_output_format=_env("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128"),
        api_token=_env("API_TOKEN", required=True),
        host_ip=_env("HOST_IP", required=True),
        port=int(_env("PORT", "8765")),
        cache_dir=_env("CACHE_DIR", "/data/cache"),
        default_volume=int(_env("DEFAULT_VOLUME", "35")),
        max_clip_seconds=int(_env("MAX_CLIP_SECONDS", "60")),
        discovery_timeout=int(_env("DISCOVERY_TIMEOUT", "5")),
    )
