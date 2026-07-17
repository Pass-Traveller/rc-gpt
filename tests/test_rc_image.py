import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

import rc_image


class FakeHeaders(dict):
    def get(self, key, default=None):
        for current_key, value in self.items():
            if current_key.lower() == key.lower():
                return value
        return default


class FakeResponse:
    def __init__(self, body=b"", content_type="application/json"):
        self.stream = io.BytesIO(body)
        self.headers = FakeHeaders({"Content-Type": content_type})

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class SequenceOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def json_response(value):
    return FakeResponse(json.dumps(value).encode("utf-8"))


class ConfigTests(unittest.TestCase):
    def test_load_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"api_key":"  sk-test  "}', encoding="utf-8")
            self.assertEqual(rc_image.load_api_key(path), "sk-test")

    def test_missing_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(rc_image.ConfigError):
                rc_image.load_api_key(path)

    def test_reference_image_becomes_data_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.png"
            path.write_bytes(b"png-data")
            result = rc_image.image_file_to_data_url(path)
            self.assertEqual(
                result,
                "data:image/png;base64," + base64.b64encode(b"png-data").decode("ascii"),
            )


class ClientTests(unittest.TestCase):
    def test_submit_sends_expected_payload_and_auth(self):
        opener = SequenceOpener([json_response({"task_id": "task_abc"})])
        client = rc_image.RightCodeClient("sk-secret", opener=opener)

        task_id = client.submit(
            prompt="橘猫",
            model="gpt-image-2",
            n=2,
            size="1:1",
            image_size="2K",
            images=[],
        )

        self.assertEqual(task_id, "task_abc")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, rc_image.GENERATE_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-secret")
        self.assertEqual(timeout, 60.0)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "model": "gpt-image-2",
                "prompt": "橘猫",
                "n": 2,
                "async": True,
                "size": "1:1",
                "imageSize": "2K",
            },
        )

    def test_submit_includes_multiple_reference_images(self):
        opener = SequenceOpener([json_response({"task_id": "task_refs"})])
        client = rc_image.RightCodeClient("sk-test", opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one.png"
            second = Path(directory) / "two.jpg"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            client.submit(
                prompt="edit",
                model="gpt-image-2",
                n=1,
                size=None,
                image_size=None,
                images=[first, second],
            )
        payload = json.loads(opener.requests[0][0].data.decode("utf-8"))
        self.assertEqual(len(payload["image"]), 2)
        self.assertTrue(payload["image"][0].startswith("data:image/png;base64,"))
        self.assertTrue(payload["image"][1].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("size", payload)
        self.assertNotIn("imageSize", payload)

    def test_wait_polls_until_documented_completed_shape(self):
        opener = SequenceOpener(
            [
                json_response({"status": "queued", "progress": 0}),
                json_response({"status": "in_progress", "progress": 50}),
                json_response({"data": [{"url": "https://cdn.test/image.png"}]}),
            ]
        )
        messages = []
        client = rc_image.RightCodeClient(
            "sk-test", opener=opener, sleeper=lambda _: None, clock=lambda: 0
        )
        result = client.wait_for_task(
            "task_abc", interval=1, timeout=60, progress=messages.append
        )
        self.assertIn("data", result)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("/v1/tasks/task_abc" in item[0].full_url for item in opener.requests))

    def test_failed_task_uses_server_message(self):
        opener = SequenceOpener(
            [json_response({"status": "failed", "error": {"message": "上游失败"}})]
        )
        client = rc_image.RightCodeClient("sk-test", opener=opener)
        with self.assertRaisesRegex(rc_image.ApiError, "上游失败"):
            client.wait_for_task("task_abc", interval=1, timeout=60)

    def test_wait_times_out(self):
        times = iter([0, 10])
        opener = SequenceOpener([json_response({"status": "queued"})])
        client = rc_image.RightCodeClient(
            "sk-test", opener=opener, sleeper=lambda _: None, clock=lambda: next(times)
        )
        with self.assertRaisesRegex(rc_image.ApiError, "超时"):
            client.wait_for_task("task_abc", interval=1, timeout=5)

    def test_http_error_is_sanitized_and_descriptive(self):
        error = HTTPError(
            rc_image.GENERATE_URL,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":{"message":"invalid key"}}'),
        )
        client = rc_image.RightCodeClient("sk-secret", opener=SequenceOpener([error]))
        with self.assertRaisesRegex(rc_image.ApiError, "HTTP 401.*invalid key") as context:
            client.get_task("task_abc")
        self.assertNotIn("sk-secret", str(context.exception))

    def test_invalid_task_id_is_rejected_before_request(self):
        client = rc_image.RightCodeClient("sk-test", opener=SequenceOpener([]))
        with self.assertRaises(rc_image.ConfigError):
            client.get_task("../../bad")


class DownloadTests(unittest.TestCase):
    def test_download_url_uses_content_type_and_no_authorization(self):
        opener = SequenceOpener([FakeResponse(b"image-bytes", "image/webp")])
        response = {"data": [{"url": "https://cdn.test/result"}]}
        with tempfile.TemporaryDirectory() as directory:
            results = rc_image.download_images(
                response,
                "task_url",
                Path(directory),
                overwrite=False,
                http_timeout=10,
                opener=opener,
            )
            path, written = results[0]
            self.assertTrue(written)
            self.assertEqual(path.name, "image_001.webp")
            self.assertEqual(path.read_bytes(), b"image-bytes")
        request, timeout = opener.requests[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 10)

    def test_base64_and_multiple_images_are_saved(self):
        first = base64.b64encode(b"first").decode("ascii")
        second = "data:image/webp;base64," + base64.b64encode(b"second").decode("ascii")
        response = {"data": [{"b64_json": first}, {"base64": second}]}
        with tempfile.TemporaryDirectory() as directory:
            results = rc_image.download_images(
                response,
                "task_b64",
                Path(directory),
                overwrite=False,
                http_timeout=10,
            )
            self.assertEqual(results[0][0].read_bytes(), b"first")
            self.assertEqual(results[0][0].suffix, ".png")
            self.assertEqual(results[1][0].read_bytes(), b"second")
            self.assertEqual(results[1][0].suffix, ".webp")

    def test_existing_file_is_not_overwritten_by_default(self):
        encoded = base64.b64encode(b"new").decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            target_dir = Path(directory) / "task_existing"
            target_dir.mkdir()
            target = target_dir / "image_001.png"
            target.write_bytes(b"old")
            results = rc_image.download_images(
                {"data": [{"b64_json": encoded}]},
                "task_existing",
                Path(directory),
                overwrite=False,
                http_timeout=10,
            )
            self.assertFalse(results[0][1])
            self.assertEqual(target.read_bytes(), b"old")

    def test_gemini_candidate_url_is_supported(self):
        response = {
            "candidates": [
                {"content": {"parts": [{"text": "https://cdn.test/image.png"}]}}
            ]
        }
        opener = SequenceOpener([FakeResponse(b"png", "image/png")])
        with tempfile.TemporaryDirectory() as directory:
            results = rc_image.download_images(
                response,
                "task_candidate",
                Path(directory),
                overwrite=False,
                http_timeout=10,
                opener=opener,
            )
            self.assertEqual(results[0][0].read_bytes(), b"png")

    def test_empty_completed_result_is_rejected(self):
        with self.assertRaisesRegex(rc_image.ApiError, "没有可下载"):
            rc_image.extract_image_entries({"status": "completed", "data": []})


if __name__ == "__main__":
    unittest.main()
