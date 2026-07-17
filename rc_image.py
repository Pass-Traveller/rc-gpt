#!/usr/bin/env python3
"""Submit and download asynchronous Right Code image generation tasks."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


GENERATE_URL = "https://www.right.codes/draw/v1/images/generations"
TASK_URL_TEMPLATE = "https://www.right.codes/v1/tasks/{task_id}"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / "config.json"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
IN_PROGRESS_STATUSES = {"queued", "processing", "in_progress"}
IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


class RcImageError(Exception):
    """Base error for user-facing failures."""


class ConfigError(RcImageError):
    """Raised when local configuration is missing or invalid."""


class ApiError(RcImageError):
    """Raised when the remote API returns an error or invalid response."""


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def load_api_key(config_path: Path) -> str:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"找不到配置文件：{config_path}。请复制 config.example.json 为 config.json 并填写 api_key。"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}：{exc}") from exc

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是有效 JSON：{exc}") from exc

    if not isinstance(config, dict):
        raise ConfigError("配置文件根节点必须是 JSON 对象。")
    api_key = config.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigError("配置文件必须包含非空字符串字段 api_key。")
    return api_key.strip()


def image_file_to_data_url(path: Path) -> str:
    if not path.is_file():
        raise ConfigError(f"参考图不存在或不是文件：{path}")
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type or not mime_type.startswith("image/"):
        raise ConfigError(f"无法识别参考图类型：{path}")
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise ConfigError(f"无法读取参考图 {path}：{exc}") from exc
    return f"data:{mime_type};base64,{encoded}"


def _safe_error_body(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str):
            return error
        if parsed.get("message"):
            return str(parsed["message"])
    return text[:500]


class RightCodeClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_timeout: float = 60.0,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.http_timeout = http_timeout
        self._open = opener
        self._sleep = sleeper
        self._clock = clock

    def _json_request(self, request: Request) -> dict[str, Any]:
        try:
            with self._open(request, timeout=self.http_timeout) as response:
                body = response.read()
        except HTTPError as exc:
            detail = _safe_error_body(exc.read())
            suffix = f"：{detail}" if detail else ""
            raise ApiError(f"接口返回 HTTP {exc.code}{suffix}") from exc
        except URLError as exc:
            raise ApiError(f"网络请求失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError("网络请求超时。") from exc
        except OSError as exc:
            raise ApiError(f"网络请求失败：{exc}") from exc

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("接口返回的内容不是有效 JSON。") from exc
        if not isinstance(payload, dict):
            raise ApiError("接口返回的 JSON 根节点不是对象。")
        return payload

    def _authorized_request(
        self, url: str, *, data: bytes | None = None, method: str = "GET"
    ) -> Request:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "rc-image/1.0",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        return Request(url, data=data, headers=headers, method=method)

    def submit(
        self,
        *,
        prompt: str,
        model: str,
        n: int,
        size: str | None,
        image_size: str | None,
        images: Iterable[Path],
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": n,
            "async": True,
        }
        if size:
            payload["size"] = size
        if image_size:
            payload["imageSize"] = image_size
        encoded_images = [image_file_to_data_url(path) for path in images]
        if encoded_images:
            payload["image"] = encoded_images

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = self._json_request(
            self._authorized_request(GENERATE_URL, data=data, method="POST")
        )
        task_id = response.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ApiError("提交成功响应中缺少 task_id。")
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ApiError("接口返回了格式异常的 task_id。")
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ConfigError("task_id 只能包含字母、数字、下划线和连字符。")
        url = TASK_URL_TEMPLATE.format(task_id=task_id)
        return self._json_request(self._authorized_request(url))

    def wait_for_task(
        self,
        task_id: str,
        *,
        interval: float,
        timeout: float,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        deadline = self._clock() + timeout
        last_message: str | None = None

        while True:
            response = self.get_task(task_id)
            status_value = response.get("status")
            status = status_value.lower() if isinstance(status_value, str) else None

            # The documented completed Images response may omit status entirely.
            if isinstance(response.get("data"), list) or isinstance(
                response.get("candidates"), list
            ):
                return response
            if status == "completed":
                return response
            if status == "failed":
                error = response.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or "未知错误")
                else:
                    message = str(error or "未知错误")
                raise ApiError(f"任务生成失败：{message}")
            if status not in IN_PROGRESS_STATUSES:
                raise ApiError(f"任务查询返回未知状态：{status_value!r}")

            percent = response.get("progress")
            message = f"任务 {task_id}：{status}"
            if isinstance(percent, (int, float)):
                message += f"，进度 {percent}%"
            if progress and message != last_message:
                progress(message)
                last_message = message

            now = self._clock()
            if now >= deadline:
                raise ApiError(f"等待任务超时（{timeout:g} 秒）：{task_id}")
            self._sleep(min(interval, max(0.0, deadline - now)))


def _candidate_urls(candidates: Any) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    if not isinstance(candidates, list):
        return results
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.startswith(("https://", "http://")):
                results.append({"url": text})
    return results


def extract_image_entries(task_response: dict[str, Any]) -> list[dict[str, Any]]:
    data = task_response.get("data")
    entries = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    if not entries:
        entries = _candidate_urls(task_response.get("candidates"))
    if not entries:
        raise ApiError("任务已完成，但响应中没有可下载的图片数据。")
    return entries


def _extension_from_url(url: str) -> str | None:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix == ".jpeg":
        return ".jpg"
    if suffix in {".png", ".jpg", ".webp", ".gif", ".avif"}:
        return suffix
    return None


def _extension_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    mime = content_type.split(";", 1)[0].strip().lower()
    return IMAGE_EXTENSIONS.get(mime)


def _decode_base64_entry(value: str) -> tuple[bytes, str]:
    extension = ".png"
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or ";base64" not in header.lower():
            raise ApiError("图片 data URL 格式无效。")
        mime = header[5:].split(";", 1)[0].lower()
        extension = IMAGE_EXTENSIONS.get(mime, ".png")
    try:
        return base64.b64decode(encoded, validate=True), extension
    except (binascii.Error, ValueError) as exc:
        raise ApiError("任务响应中的图片 base64 数据无效。") from exc


def _target_path(output_dir: Path, index: int, extension: str) -> Path:
    return output_dir / f"image_{index:03d}{extension}"


def _write_atomic(target: Path, data: bytes, overwrite: bool) -> bool:
    if target.exists() and not overwrite:
        return False
    temporary = target.with_name(target.name + ".part")
    try:
        temporary.write_bytes(data)
        if not data:
            raise ApiError(f"下载结果为空：{target.name}")
        if target.exists() and not overwrite:
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return True


def _download_url(
    url: str,
    output_dir: Path,
    index: int,
    overwrite: bool,
    *,
    http_timeout: float,
    opener: Callable[..., Any],
) -> tuple[Path, bool]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ApiError(f"不支持的图片 URL 协议：{parsed.scheme or '空'}")
    request = Request(url, headers={"User-Agent": "rc-image/1.0"})
    try:
        with opener(request, timeout=http_timeout) as response:
            content_type = response.headers.get("Content-Type")
            extension = _extension_from_content_type(content_type) or _extension_from_url(url) or ".png"
            target = _target_path(output_dir, index, extension)
            if target.exists() and not overwrite:
                return target, False
            temporary = target.with_name(target.name + ".part")
            try:
                total = 0
                with temporary.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        total += len(chunk)
                if total == 0:
                    raise ApiError(f"图片下载结果为空：{url}")
                if target.exists() and not overwrite:
                    temporary.unlink(missing_ok=True)
                    return target, False
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            return target, True
    except HTTPError as exc:
        raise ApiError(f"图片下载返回 HTTP {exc.code}：{url}") from exc
    except URLError as exc:
        raise ApiError(f"图片下载失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ApiError(f"图片下载超时：{url}") from exc
    except OSError as exc:
        raise ApiError(f"图片保存失败：{exc}") from exc


def download_images(
    task_response: dict[str, Any],
    task_id: str,
    output_root: Path,
    *,
    overwrite: bool,
    http_timeout: float,
    opener: Callable[..., Any] = urlopen,
) -> list[tuple[Path, bool]]:
    output_dir = output_root / task_id
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApiError(f"无法创建输出目录 {output_dir}：{exc}") from exc

    saved: list[tuple[Path, bool]] = []
    for index, entry in enumerate(extract_image_entries(task_response), start=1):
        url = entry.get("url")
        if isinstance(url, str) and url:
            saved.append(
                _download_url(
                    url,
                    output_dir,
                    index,
                    overwrite,
                    http_timeout=http_timeout,
                    opener=opener,
                )
            )
            continue

        encoded = next(
            (
                entry[key]
                for key in ("b64_json", "base64", "b64")
                if isinstance(entry.get(key), str) and entry[key]
            ),
            None,
        )
        if encoded is None:
            raise ApiError(f"第 {index} 个图片结果既没有 url，也没有 base64 数据。")
        data, extension = _decode_base64_entry(encoded)
        target = _target_path(output_dir, index, extension)
        saved.append((target, _write_atomic(target, data, overwrite)))
    return saved


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="图片输出根目录"
    )
    parser.add_argument("--interval", type=_positive_float, default=3.0, help="轮询间隔秒数")
    parser.add_argument("--timeout", type=_positive_float, default=600.0, help="总等待超时秒数")
    parser.add_argument(
        "--http-timeout", type=_positive_float, default=60.0, help="单次 HTTP 请求超时秒数"
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的图片")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="提交 Right Code gpt-image-2 异步任务并将结果下载到本地"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="提交新任务、轮询并下载")
    _add_common_arguments(generate)
    generate.add_argument("--prompt", required=True, help="图片提示词")
    generate.add_argument("--model", default="gpt-image-2", help="模型名")
    generate.add_argument("--n", type=_positive_int, default=1, help="生成数量")
    generate.add_argument("--size", help="比例或像素尺寸，例如 1:1 或 1024x1024")
    generate.add_argument(
        "--image-size", choices=("1K", "2K", "4K"), help="图片分辨率等级"
    )
    generate.add_argument(
        "--image", type=Path, action="append", default=[], help="本地参考图，可重复传入"
    )

    fetch = subparsers.add_parser("fetch", help="按已有 task_id 轮询并下载")
    _add_common_arguments(fetch)
    fetch.add_argument("task_id", help="Right Code 异步任务 ID")
    return parser


def run(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.config)
    client = RightCodeClient(api_key, http_timeout=args.http_timeout)

    if args.command == "generate":
        task_id = client.submit(
            prompt=args.prompt,
            model=args.model,
            n=args.n,
            size=args.size,
            image_size=args.image_size,
            images=args.image,
        )
        print(f"任务已提交：{task_id}")
    else:
        task_id = args.task_id
        print(f"开始查询任务：{task_id}")

    response = client.wait_for_task(
        task_id,
        interval=args.interval,
        timeout=args.timeout,
        progress=print,
    )
    results = download_images(
        response,
        task_id,
        args.output_dir,
        overwrite=args.overwrite,
        http_timeout=args.http_timeout,
    )
    for path, written in results:
        label = "已保存" if written else "已存在，已跳过"
        print(f"{label}：{path.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except RcImageError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
