"""Best-effort image thumbnail generation from UNTRUSTED bytes (ENG-118, §6).

:func:`render_thumbnail` decodes arbitrary uploaded bytes with Pillow and re-encodes
a small WEBP thumbnail. The input is HOSTILE by assumption — a malicious PNG/JPEG
header, a decompression bomb, a truncated or non-image payload — so the entire
function is written to the discipline "**bound it, contain it, and never trust the
result**":

* **Decompression-bomb bound.** ``Image.MAX_IMAGE_PIXELS`` caps the source W×H Pillow
  will decode. A tiny compressed file can claim enormous dimensions (a 100000×100000
  PNG is a few bytes of zlib but tens of GB decoded); the bytes-on-disk cap
  (``file_max_size_bytes``) does nothing against that, so this DECODED-pixel bound is
  the real guard. Pillow raises :class:`Image.DecompressionBombError` past 2×
  the limit and emits a :class:`Image.DecompressionBombWarning` past 1×; we promote
  that warning to an exception locally (``simplefilter("error", ...)``) so both land
  in the reject path. The guard is checked at ``Image.open`` — before the pixels are
  actually decoded — so a bomb is rejected without ever allocating its buffer.
* **Containment.** EVERYTHING is wrapped in try/except → return ``None``. Pillow's
  many decoders raise a zoo of exceptions on malformed input
  (:class:`UnidentifiedImageError`, :class:`OSError` from a truncated stream, plus
  decoder-specific errors), so the reject path catches the specific security-relevant
  types AND a broad ``Exception`` fallback. This function NEVER raises — a hostile or
  non-image input is simply "no thumbnail", identical to a plain text file.
* **Never trust the result — re-encode.** We do not pass the source bytes through. We
  decode to a pixel raster and RE-ENCODE to a WEBP we control, so the output is a
  known-safe raster with no active content (no SVG script, no HTML, no embedded EXIF
  payload) regardless of what the input was. ``LOAD_TRUNCATED_IMAGES`` is left at its
  default ``False`` so a truncated/malformed image RAISES (→ ``None``) instead of
  silently loading a half-decoded garbage buffer.

``Image.MAX_IMAGE_PIXELS`` is a Pillow MODULE GLOBAL, so setting it here mutates
process-wide state for the duration of the call. That is acceptable for this
single-process server (the value is always set to the same configured bound before
each decode, so concurrent renders agree), and is the interface Pillow exposes — there
is no per-``Image`` decompression cap. The per-call assignment documents the coupling
and keeps the bound co-located with the decode it protects.

The function is PURE and SYNCHRONOUS (CPU-bound decode/encode). The caller offloads it
with ``asyncio.to_thread`` so a slow or adversarial decode never blocks the event loop.
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

__all__ = ["render_thumbnail"]

# WEBP quality for the generated thumbnail. 80 is the usual quality/size sweet spot
# for photographic content; the thumbnail is a preview, not an archival copy.
_WEBP_QUALITY = 80

# Source modes that carry an alpha channel (or palette transparency). These are
# flattened onto white before RGB conversion so a transparent PNG does not thumbnail
# with black (or undefined) fill.
_ALPHA_MODES = frozenset({"RGBA", "LA", "PA"})


def render_thumbnail(source: bytes, *, max_px: int, max_source_pixels: int) -> bytes | None:
    """Decode ``source`` and return a downscaled WEBP thumbnail, or ``None``.

    Returns the encoded WEBP bytes when ``source`` is a decodable raster image, else
    ``None`` for ANYTHING that is not — a non-image, a truncated/malformed image, a
    decompression bomb over ``max_source_pixels``, or any other decode failure. It
    NEVER raises: the caller treats ``None`` as "no thumbnail" and succeeds the upload
    regardless (thumbnails are strictly best-effort).

    Args:
        source: raw, UNTRUSTED image bytes (an uploaded blob).
        max_px: longest-edge bound of the output thumbnail; only ever downscales.
        max_source_pixels: decompression-bomb guard — the max source W×H Pillow will
            decode before rejecting (wired into ``Image.MAX_IMAGE_PIXELS``).
    """
    # Per-call decompression-bomb bound (Pillow module global; see module docstring).
    Image.MAX_IMAGE_PIXELS = max_source_pixels
    try:
        with warnings.catch_warnings():
            # A DecompressionBombWarning (source between 1× and 2× the limit) becomes a
            # raised exception, landing in the reject path alongside the hard Error.
            warnings.simplefilter("error", Image.DecompressionBombWarning)

            with Image.open(io.BytesIO(source)) as img:
                # Honor EXIF orientation so a rotated phone photo thumbnails upright
                # (and its width/height are swapped when the camera stored it sideways).
                oriented = ImageOps.exif_transpose(img)
                # exif_transpose returns a new image when EXIF is present, else the same
                # object; fall back to the original defensively.
                flat = _flatten_to_rgb(oriented if oriented is not None else img)
                # thumbnail() preserves aspect ratio and ONLY downscales — a small
                # source is returned unchanged rather than blown up.
                flat.thumbnail((max_px, max_px))

                buf = io.BytesIO()
                flat.save(buf, format="WEBP", quality=_WEBP_QUALITY)
                return buf.getvalue()
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError):
        # The named, expected hostile-input failures: an oversized decode, bytes that
        # are not a recognized image, and a truncated/unreadable stream.
        return None
    except Exception:
        # Belt-and-suspenders: Pillow's decoder plugins can raise a wide, version-
        # dependent set of errors on crafted input. A thumbnail is never worth
        # propagating a decode failure, so anything else is also "no thumbnail".
        return None


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Return ``img`` as an RGB image, compositing any alpha onto a white background.

    WEBP could carry alpha, but we deliberately flatten to opaque RGB: it is the one
    mode every source (palette ``P``, grayscale, ``CMYK``, ``RGBA``, ...) converts to
    cleanly, and a preview thumbnail has no need to preserve transparency. Transparent
    regions composite onto WHITE rather than defaulting to black.
    """
    has_alpha = img.mode in _ALPHA_MODES or (img.mode == "P" and "transparency" in img.info)
    if has_alpha:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img
