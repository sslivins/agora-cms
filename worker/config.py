"""Worker configuration."""

from shared.config import SharedSettings


class WorkerSettings(SharedSettings):
    """Settings for the transcode worker."""

    # Worker mode: "listen" (Docker Compose) or "queue" (Azure Container Apps Job)
    worker_mode: str = "listen"

    # LISTEN/NOTIFY poll fallback interval (seconds)
    poll_interval: int = 60

    # Azure Storage Queue URL (queue mode trigger)
    azure_transcode_queue_url: str | None = None
