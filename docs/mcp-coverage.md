# MCP tool coverage

<!-- GENERATED FILE — DO NOT EDIT BY HAND.
     Run `python scripts/generate_mcp_coverage.py` and commit the result.
     CI job `mcp-check` fails if this file is out of sync. -->

Every `/api/**` route in the CMS, and the MCP tool that exposes it to the
Assistant (`mcp/server.py`), if any. Routes are taken from
`docs/openapi.yaml`, which `openapi-check` keeps in sync with the code.

**A `—` is not a bug.** Many routes are UI plumbing, device-facing, or
otherwise a poor fit for an LLM tool. The point of this file is that when a
new feature adds routes, they appear here as new `—` lines in the pull
request diff — so *whether* to expose them over MCP becomes an explicit
decision instead of an oversight.

Coverage: **53 of 164** `/api` routes exposed as MCP tools.

| Method | Path | MCP tool |
| --- | --- | --- |
| GET | `/api/asset-views` | `list_asset_views` |
| POST | `/api/asset-views` | `create_asset_view` |
| DELETE | `/api/asset-views/{view_id}` | `delete_asset_view` |
| PATCH | `/api/asset-views/{view_id}` | `update_asset_view` |
| GET | `/api/assets` | `list_assets` |
| POST | `/api/assets/bulk` | `tag_asset`, `untag_asset` |
| GET | `/api/assets/page` | — |
| POST | `/api/assets/slideshow` | — |
| GET | `/api/assets/status` | — |
| POST | `/api/assets/status` | — |
| POST | `/api/assets/stream` | — |
| POST | `/api/assets/upload` | — |
| GET | `/api/assets/variants/{variant_id}/download` | — |
| GET | `/api/assets/variants/{variant_id}/preview` | — |
| POST | `/api/assets/webpage` | `create_webpage_asset` |
| DELETE | `/api/assets/{asset_id}` | `delete_asset` |
| GET | `/api/assets/{asset_id}` | `get_asset`, `play_now` |
| PATCH | `/api/assets/{asset_id}` | `update_asset` |
| POST | `/api/assets/{asset_id}/assistant/thread` | — |
| GET | `/api/assets/{asset_id}/download` | — |
| POST | `/api/assets/{asset_id}/global` | `toggle_asset_global` |
| GET | `/api/assets/{asset_id}/preview` | — |
| POST | `/api/assets/{asset_id}/recapture` | `recapture_stream` |
| GET | `/api/assets/{asset_id}/row` | — |
| DELETE | `/api/assets/{asset_id}/share` | — |
| POST | `/api/assets/{asset_id}/share` | — |
| GET | `/api/assets/{asset_id}/slides` | `get_slideshow` |
| PUT | `/api/assets/{asset_id}/slides` | `set_slideshow_slides` |
| GET | `/api/audit-log` | — |
| GET | `/api/audit-log/count` | — |
| GET | `/api/chat/approvals/{approval_id}` | — |
| POST | `/api/chat/approvals/{approval_id}/approve` | — |
| POST | `/api/chat/approvals/{approval_id}/reject` | — |
| GET | `/api/chat/feature` | — |
| GET | `/api/chat/threads` | — |
| POST | `/api/chat/threads` | — |
| DELETE | `/api/chat/threads/{thread_id}` | — |
| GET | `/api/chat/threads/{thread_id}/approvals` | — |
| POST | `/api/chat/threads/{thread_id}/message` | — |
| GET | `/api/chat/threads/{thread_id}/messages` | — |
| POST | `/api/chat/threads/{thread_id}/stream` | — |
| GET | `/api/chat/usage` | — |
| GET | `/api/cms/logs` | — |
| GET | `/api/device-events` | — |
| GET | `/api/device-events/count` | — |
| DELETE | `/api/device-tags/{tag_id}` | — |
| PATCH | `/api/device-tags/{tag_id}` | — |
| GET | `/api/devices` | `list_devices` |
| POST | `/api/devices/adopt` | — |
| POST | `/api/devices/adopt-pending` | — |
| GET | `/api/devices/bootstrap-status` | — |
| POST | `/api/devices/check-updates` | `check_device_updates` |
| POST | `/api/devices/connect-token` | — |
| GET | `/api/devices/groups/` | `list_groups` |
| POST | `/api/devices/groups/` | `create_group` |
| DELETE | `/api/devices/groups/{group_id}` | `delete_group` |
| PATCH | `/api/devices/groups/{group_id}` | `update_group` |
| GET | `/api/devices/groups/{group_id}/panel` | — |
| GET | `/api/devices/pending` | — |
| DELETE | `/api/devices/pending/{pending_id}` | — |
| POST | `/api/devices/register` | — |
| DELETE | `/api/devices/{device_id}` | `delete_device` |
| GET | `/api/devices/{device_id}` | `get_device` |
| PATCH | `/api/devices/{device_id}` | `update_device` |
| POST | `/api/devices/{device_id}/adopt` | `adopt_device` |
| POST | `/api/devices/{device_id}/connect-token` | — |
| POST | `/api/devices/{device_id}/factory-reset` | `factory_reset_device` |
| POST | `/api/devices/{device_id}/local-api` | `toggle_device_local_api` |
| POST | `/api/devices/{device_id}/logs/{request_id}/upload` | — |
| POST | `/api/devices/{device_id}/password` | `set_device_password` |
| POST | `/api/devices/{device_id}/reboot` | `reboot_device` |
| GET | `/api/devices/{device_id}/schedule-status` | — |
| POST | `/api/devices/{device_id}/ssh` | `toggle_device_ssh` |
| GET | `/api/devices/{device_id}/tags` | — |
| PUT | `/api/devices/{device_id}/tags` | — |
| POST | `/api/devices/{device_id}/upgrade` | `upgrade_device` |
| GET | `/api/features` | — |
| PUT | `/api/features/{name}` | — |
| GET | `/api/groups/{group_id}/tags` | — |
| POST | `/api/groups/{group_id}/tags` | — |
| GET | `/api/imager/base-images` | — |
| POST | `/api/imager/base-images` | — |
| DELETE | `/api/imager/base-images/{base_image_id}` | — |
| POST | `/api/imager/build` | — |
| GET | `/api/imager/catalog` | — |
| GET | `/api/imager/download-url/{job_id}` | — |
| GET | `/api/imager/download/{job_id}` | — |
| GET | `/api/imager/fleets` | — |
| POST | `/api/imager/fleets` | — |
| DELETE | `/api/imager/fleets/{fleet_id}` | — |
| GET | `/api/imager/jobs/{job_id}` | — |
| GET | `/api/imager/provisioned-images` | — |
| DELETE | `/api/imager/provisioned-images/{provisioned_image_id}` | — |
| GET | `/api/imager/settings` | — |
| PUT | `/api/imager/settings` | — |
| POST | `/api/imager/softplayer-credentials` | — |
| POST | `/api/issues/report` | — |
| GET | `/api/issues/report/config` | — |
| GET | `/api/keys` | — |
| POST | `/api/keys` | — |
| GET | `/api/keys/my` | — |
| POST | `/api/keys/my` | — |
| DELETE | `/api/keys/my/{key_id}` | — |
| POST | `/api/keys/my/{key_id}/regenerate` | — |
| DELETE | `/api/keys/{key_id}` | — |
| POST | `/api/keys/{key_id}/regenerate` | — |
| POST | `/api/logs/requests` | `get_device_logs` |
| GET | `/api/logs/requests/{request_id}` | — |
| GET | `/api/logs/requests/{request_id}/download` | — |
| GET | `/api/mcp/auth` | — |
| GET | `/api/notification-preferences` | — |
| PUT | `/api/notification-preferences` | — |
| GET | `/api/notification-preferences/email-status` | — |
| DELETE | `/api/notifications` | — |
| GET | `/api/notifications` | — |
| GET | `/api/notifications/count` | — |
| POST | `/api/notifications/read-all` | — |
| DELETE | `/api/notifications/{notification_id}` | — |
| POST | `/api/notifications/{notification_id}/read` | — |
| GET | `/api/profiles` | `list_profiles` |
| POST | `/api/profiles` | `create_profile` |
| POST | `/api/profiles/clear-errors` | — |
| GET | `/api/profiles/status` | — |
| DELETE | `/api/profiles/{profile_id}` | `delete_profile` |
| PUT | `/api/profiles/{profile_id}` | `update_profile` |
| POST | `/api/profiles/{profile_id}/copy` | `copy_profile` |
| POST | `/api/profiles/{profile_id}/disable` | `disable_profile` |
| POST | `/api/profiles/{profile_id}/enable` | `enable_profile` |
| POST | `/api/profiles/{profile_id}/reset` | `reset_profile` |
| GET | `/api/profiles/{profile_id}/row` | — |
| GET | `/api/roles` | — |
| POST | `/api/roles` | — |
| GET | `/api/roles/permissions/catalogue` | — |
| DELETE | `/api/roles/{role_id}` | — |
| GET | `/api/roles/{role_id}` | — |
| PATCH | `/api/roles/{role_id}` | — |
| GET | `/api/schedules` | `list_schedules` |
| POST | `/api/schedules` | `create_schedule`, `play_now` |
| DELETE | `/api/schedules/{schedule_id}` | `delete_schedule` |
| GET | `/api/schedules/{schedule_id}` | `get_schedule` |
| PATCH | `/api/schedules/{schedule_id}` | `update_schedule` |
| POST | `/api/schedules/{schedule_id}/end-now` | `end_schedule_now` |
| GET | `/api/schedules/{schedule_id}/row` | — |
| GET | `/api/settings/assistant` | — |
| PUT | `/api/settings/assistant/budget` | — |
| GET | `/api/streams/probe` | — |
| GET | `/api/tags` | `list_tags` |
| POST | `/api/tags` | `create_tag` |
| DELETE | `/api/tags/{tag_id}` | `delete_tag` |
| PATCH | `/api/tags/{tag_id}` | `update_tag` |
| GET | `/api/tags/{tag_id}/members` | — |
| GET | `/api/users` | — |
| POST | `/api/users` | — |
| GET | `/api/users/me` | — |
| POST | `/api/users/me/password` | — |
| DELETE | `/api/users/{user_id}` | — |
| GET | `/api/users/{user_id}` | — |
| PATCH | `/api/users/{user_id}` | — |
| POST | `/api/users/{user_id}/resend-invite` | — |
| POST | `/api/voice-announcements` | `create_voice_announcement` |
| POST | `/api/voice-announcements/preview` | — |
| GET | `/api/voice-announcements/voices` | `list_voices` |
| GET | `/api/voice-announcements/{asset_id}` | `get_voice_announcement_status` |
| PUT | `/api/voice-announcements/{asset_id}` | `update_voice_announcement` |
