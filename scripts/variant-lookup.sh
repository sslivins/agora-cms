#!/usr/bin/env bash
# variant-lookup.sh — Look up variant info from the database.
#
# Usage:
#   ./variant-lookup.sh                  # list all variants
#   ./variant-lookup.sh <uuid-or-name>   # search by variant ID or source filename
#
# Requires: docker (must be run on the CMS host)

set -euo pipefail

DB_CONTAINER="${AGORA_DB_CONTAINER:-agora-cms-db-1}"
DB_USER="${AGORA_DB_USER:-agora}"
DB_NAME="${AGORA_DB_NAME:-agora_cms}"

query() {
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "$1"
}

if [ $# -eq 0 ]; then
    # List all variants with human-readable info
    query "
        SELECT
            v.id AS variant_id,
            v.filename AS disk_file,
            a.filename AS source_asset,
            p.name AS profile,
            v.status,
            pg_size_pretty(v.size_bytes::bigint) AS size,
            v.checksum
        FROM asset_variants v
        JOIN assets a ON v.source_asset_id = a.id
        JOIN device_profiles p ON v.profile_id = p.id
        ORDER BY a.filename, p.name;
    "
else
    SEARCH="$1"
    # Sanitize: only allow alphanumeric, hyphens, underscores, dots, and spaces
    if [[ ! "$SEARCH" =~ ^[a-zA-Z0-9_.\ -]+$ ]]; then
        echo "Error: search term contains invalid characters" >&2
        exit 1
    fi
    # Try UUID match first, then fuzzy filename match
    query "
        SELECT
            v.id AS variant_id,
            v.filename AS disk_file,
            a.filename AS source_asset,
            p.name AS profile,
            v.status,
            pg_size_pretty(v.size_bytes::bigint) AS size,
            v.checksum,
            v.width || 'x' || v.height AS resolution,
            v.video_codec,
            v.frame_rate AS fps
        FROM asset_variants v
        JOIN assets a ON v.source_asset_id = a.id
        JOIN device_profiles p ON v.profile_id = p.id
        WHERE v.id::text ILIKE '%${SEARCH}%'
           OR v.filename ILIKE '%${SEARCH}%'
           OR a.filename ILIKE '%${SEARCH}%'
           OR p.name ILIKE '%${SEARCH}%'
        ORDER BY a.filename, p.name;
    "
fi
