# Image policy

The prototype accepts JPEG, PNG, TIFF, and WebP image content. Animated and
unknown formats are rejected. Pillow EXIF orientation is applied before
render geometry, and opened files are detached and closed before rendering.

Defaults are deliberately bounded:

- `BEFORE_AFTER_IMAGE_MAX_BYTES=52428800` (50 MiB)
- `BEFORE_AFTER_IMAGE_MAX_PIXELS=60000000` (60 megapixels)

Set either environment variable before starting the CLI or web process to
override its limit. Pillow's built-in decompression-bomb protection remains
enabled; these limits do not disable or widen it. Keep the production media
root on the managed backup volume; the current prototype uses only temporary
upload files and does not persist media.
