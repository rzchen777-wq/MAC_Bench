from __future__ import annotations

import io
import warnings
from typing import Any, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from .runtime import (
    ImageContent,
    ToolContext,
    ToolErrorCode,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 8_000
HIGH_DETAIL_DIMENSION = 2_048
SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP"}
MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def read_image(context: ToolContext, arguments: Mapping[str, Any]) -> ToolResult:
    path = str(arguments.get("path") or "").strip()
    detail = str(arguments.get("detail") or "high").strip().lower()
    if detail not in {"high", "original"}:
        raise ToolExecutionError(
            ToolErrorCode.INVALID_ARGUMENTS,
            "detail must be 'high' or 'original'",
        )
    safe, raw = context.environment.read_bytes(path, max_bytes=MAX_IMAGE_BYTES)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                source_format = str(source.format or "").upper()
                if source_format not in SUPPORTED_FORMATS:
                    raise ToolExecutionError(
                        ToolErrorCode.UNSUPPORTED_FORMAT,
                        f"image format {source_format or 'unknown'!r} is not supported",
                        details={"supported_formats": sorted(SUPPORTED_FORMATS)},
                    )
                width, height = source.size
                _check_dimensions(path, width, height)
                source.load()
                image = ImageOps.exif_transpose(source)
                original_width, original_height = image.size
                resized = False
                if detail == "high" and max(image.size) > HIGH_DETAIL_DIMENSION:
                    image.thumbnail(
                        (HIGH_DETAIL_DIMENSION, HIGH_DETAIL_DIMENSION),
                        Image.Resampling.LANCZOS,
                    )
                    resized = image.size != (original_width, original_height)
                output_format = source_format
                if output_format == "JPEG" and image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                elif output_format in {"PNG", "WEBP"} and image.mode not in {
                    "1",
                    "L",
                    "LA",
                    "P",
                    "RGB",
                    "RGBA",
                }:
                    image = image.convert("RGBA")
                output = io.BytesIO()
                save_options: dict[str, Any] = {}
                if output_format == "JPEG":
                    save_options = {"quality": 95, "optimize": True}
                elif output_format == "WEBP":
                    save_options = {"quality": 95, "method": 4}
                image.save(output, format=output_format, **save_options)
                encoded = output.getvalue()
                if len(encoded) > MAX_IMAGE_BYTES:
                    raise ToolExecutionError(
                        ToolErrorCode.FILE_TOO_LARGE,
                        "processed image exceeds the image output byte limit",
                        details={"byte_count": len(encoded), "max_bytes": MAX_IMAGE_BYTES},
                    )
                final_width, final_height = image.size
    except ToolExecutionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning) as exc:
        raise ToolExecutionError(
            ToolErrorCode.DECODE_ERROR,
            f"failed to decode image {path!r}: {exc}",
        ) from exc
    content = ImageContent(
        data=encoded,
        media_type=MEDIA_TYPES[output_format],
        width=final_width,
        height=final_height,
        detail=detail,
    )
    source_version = context.environment.version(raw)
    context.observe_version(safe.relative_path, source_version)
    return ToolResult.completed(
        {
            "path": safe.input_path,
            "resolved_path": safe.resolved_path,
            "width": final_width,
            "height": final_height,
            "original_width": original_width,
            "original_height": original_height,
            "media_type": content.media_type,
            "detail": detail,
            "resized": resized,
            "byte_count": len(encoded),
            "sha256": content.sha256,
            "source_version": source_version,
        },
        images=(content,),
    )


def _check_dimensions(path: str, width: int, height: int) -> None:
    pixels = width * height
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or pixels > MAX_IMAGE_PIXELS:
        raise ToolExecutionError(
            ToolErrorCode.FILE_TOO_LARGE,
            f"image {path!r} exceeds dimension or pixel limits",
            details={
                "width": width,
                "height": height,
                "pixels": pixels,
                "max_dimension": MAX_IMAGE_DIMENSION,
                "max_pixels": MAX_IMAGE_PIXELS,
                "suggestion": "resize or crop the image before reading it",
            },
        )


def read_image_tool_spec() -> ToolSpec:
    return ToolSpec(
        name="read_image",
        capability="image_input",
        description="Read a PNG, JPEG, or WebP image as native multimodal model input.",
        properties={
            "path": {"type": "string"},
            "detail": {"type": "string", "enum": ["high", "original"]},
        },
        required=("path",),
        handler=read_image,
    )
