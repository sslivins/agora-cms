"""Agora CMS configuration."""

from pydantic import Field

from shared.config import SharedSettings


class Settings(SharedSettings):

    # Auth
    secret_key: str = Field(default="change-me-in-production")
    admin_username: str = "admin"
    admin_password: str = "agora"
    admin_email: str = "admin@localhost"
    reset_password: bool = False

    # MCP Server
    mcp_server_url: str = "http://mcp:8000"  # Docker default; override for Azure

    # Asset downloads
    asset_base_url: str | None = None  # override base URL for device asset downloads

    # Device defaults
    default_device_storage_mb: int = 500  # assumed device flash budget for assets
    api_key_rotation_hours: int = 24  # rotate device API keys every N hours
    pending_device_ttl_hours: int = 24  # auto-purge pending devices not seen for N hours

    # MCP service key file (shared volume between CMS and MCP containers)
    service_key_path: str = "/shared/mcp-service.key"
    # Azure Key Vault URI for MCP service key exchange (Azure deployments)
    azure_keyvault_uri: str | None = None

    # Transcode worker signaling (Azure)
    azure_transcode_queue_url: str | None = None

    # Device transport — issue #344 Stage 2b.2
    # "local" = direct WebSocket (today's behaviour; /ws/device handler).
    # "wps"   = Azure Web PubSub; CMS sends via REST and receives events
    #           via the upstream webhook receiver mounted at
    #           /internal/wps/events.  Requires wps_connection_string.
    device_transport: str = "local"
    wps_connection_string: str | None = None
    wps_hub: str = "agora"
    wps_token_lifetime_minutes: int = 60
    # Optional allow-list for the WebHook-Request-Origin handshake.
    # If unset, the receiver echoes back whatever Azure sent (dev-friendly).
    wps_webhook_allowed_origin: str | None = None

    # SMTP is configured via the web UI settings page (stored in DB)
    base_url: str | None = None  # public URL for login links in emails

    # Log-request drainer (issue #345 Stage 3d).  Self-healing loop for
    # the ``log_requests`` outbox: retries stuck ``pending`` rows with
    # exponential backoff and rescues rows that are stuck in ``sent``
    # past ``sent_timeout_sec``.
    log_drainer_interval_sec: float = 5.0
    log_drainer_batch_size: int = 25
    log_drainer_sent_timeout_sec: int = 900
    log_drainer_max_attempts: int = 10

    # Log chunk assembler (issue #345 Stage 3c).  Pi firmware splits
    # large log bundles into sequential binary WS frames tagged with
    # the ``LGCK`` magic.  The assembler buffers frames in-process
    # keyed by ``(device_id, request_id)`` until the ``is_final`` bit
    # arrives, then writes the reassembled tar.gz to blob storage.
    # ``max_count`` and ``max_bytes`` bound memory use per request;
    # ``buffer_ttl_sec`` lets the TTL reaper evict stalled transfers.
    log_chunk_max_count: int = 30
    log_chunk_max_bytes: int = 22_020_096  # 21 MiB
    log_chunk_buffer_ttl_sec: int = 300
    log_chunk_reaper_interval_sec: float = 5.0

    # Log-blob reaper (issue #345 Stage 3e).  Periodically scans the
    # ``log_requests`` table for rows whose ``expires_at`` has passed,
    # deletes the associated blob, and flips the row to ``expired``.
    log_reaper_interval_sec: float = 600.0  # 10 minutes
    log_reaper_batch_size: int = 100

    # Bootstrap redesign (umbrella issue #420), Stage A.3.
    # ------------------------------------------------------------------
    # FLEET_REGISTER_SECRETS is a JSON map of ``fleet_id -> base64 secret``
    # used to validate the HMAC on the anonymous ``POST /api/devices/register``
    # endpoint.  Empty map = reject all /register calls (secure by default
    # until the operator provisions at least one fleet secret).
    fleet_register_secrets: dict[str, str] = Field(default_factory=dict)
    # Hard cap on unadopted ``pending_registrations`` rows — /register
    # returns 503 once the cap is reached to protect the DB from
    # registration spam.
    pending_registrations_max: int = 50_000
    # TTLs for the ``pending_registrations`` reaper loop.  All in
    # seconds; set 0 to disable a given sweep.
    # * ``unpolled`` — row created but the device never polled.  Tight
    #   window because this is where registration-spam junk accumulates
    #   (1h default).
    # * ``polled`` — device polled but admin hasn't adopted.  Generous
    #   window so a technician has time to scan the QR and hit /adopt
    #   from the UI (24h default).
    # * ``adopted`` — row was adopted; device almost certainly fetched
    #   the payload already.  Keep briefly for troubleshooting, then
    #   drop (24h default).
    bootstrap_reaper_unpolled_ttl_seconds: int = 3600
    bootstrap_reaper_polled_ttl_seconds: int = 86_400
    bootstrap_reaper_adopted_ttl_seconds: int = 86_400
    # How often the reaper loop wakes up to run a sweep.
    bootstrap_reaper_interval_seconds: int = 3600
    # TTL for the in-memory nonce cache used by ``/register`` (scope=fleet)
    # and ``/connect-token`` (scope=connect-token).  Must be >= the larger
    # of the two timestamp-skew windows (300s for /register, 60s for
    # /connect-token) so that any message still inside its skew window
    # is also still in the nonce cache.
    bootstrap_nonce_ttl_seconds: int = 600
    # WPS JWT lifetime for tokens issued via /connect-token and /adopt.
    bootstrap_wps_jwt_minutes: int = 60
