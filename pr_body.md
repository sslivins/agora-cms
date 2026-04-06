## Summary

Adds `pixel_format` and `color_space` fields to the DeviceProfile model and wires the existing `video_codec` field into the transcoder (previously ignored).

## Changes

### Model & Migration
- Added `pixel_format` (default: `yuv420p`) and `color_space` (default: `bt709`) columns to `device_profiles` table
- Auto-migration in `database.py` adds columns with defaults for existing profiles

### Transcoder
- **Fixed bug**: `video_codec` field was stored but never used — transcoder always hardcoded `libx264`
- Added `CODEC_ENCODER_MAP` (`h264` → `libx264`, `h265` → `libx265`) and use it in `_build_ffmpeg_args_safe()`
- Use `profile.pixel_format` instead of hardcoded `yuv420p` in ffmpeg filter chain
- Use `profile.color_space` instead of hardcoded `bt709` in setparams + colorspace args

### API & Schemas
- Added `pixel_format` and `color_space` to `ProfileOut`, `ProfileCreate`, `ProfileUpdate` schemas
- Updated all `ProfileOut` constructions in the profiles router

### Web UI
- Added Pixel Format and Color Space dropdowns to the create form and edit modal
- Table now displays actual profile values instead of hardcoded `yuv420p` / `BT.709 (SDR)` placeholders
