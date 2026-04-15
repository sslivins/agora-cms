"""Storage layer — re-exported from shared package for backward compatibility."""

from shared.services.storage import (  # noqa: F401
    AzureStorageBackend,
    LocalStorageBackend,
    StorageBackend,
    get_storage,
    init_storage,
)
