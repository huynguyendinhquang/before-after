# Image policy

The prototype accepts BMP, JPEG, PNG, TIFF, and WebP image content. Animated
and unknown formats are rejected. Pillow EXIF orientation is applied before
render geometry, and opened files are detached and closed before rendering.

Defaults are deliberately bounded:

- `BEFORE_AFTER_IMAGE_MAX_BYTES=52428800` (50 MiB)
- `BEFORE_AFTER_IMAGE_MAX_PIXELS=60000000` (60 megapixels)
- `BEFORE_AFTER_IMAGE_MAX_REQUEST_BYTES` optionally caps the complete Flask
  request; otherwise the request cap is the configured image byte limit plus
  64 KiB for multipart fields.

Set either environment variable before starting the CLI or web process to
override its limit. Pillow's built-in decompression-bomb protection remains
enabled; these limits do not disable or widen it. Keep the production media
root on the managed backup volume; the current prototype uses only temporary
upload files and does not persist media.

Seekable caller-owned streams are measured from their actual contents and
their original cursor is restored after success or failure. Unknown-length or
inherently non-seekable streams are consumed into an owned buffer of at most
`max_bytes + 1` before Pillow decodes them; the policy never closes such
caller-owned streams.
