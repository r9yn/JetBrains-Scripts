import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import jetbrains_fullline_models as models


PYTHON_SUPPORTER = "org/jetbrains/completion/full/line/python/PythonFullLineSupporter.class"
KOTLIN_SUPPORTER = "org/jetbrains/completion/full/line/kotlin/KotlinFullLineSupporter.class"


def write_jar(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def http_response(data: bytes, url: str, status: int = 200) -> models.requests.Response:
    response = models.requests.Response()
    response.status_code = status
    response.url = url
    response._content = data
    response._content_consumed = True
    return response


class DownloadTransportTest(unittest.TestCase):
    def test_cli_proxy_applies_to_schema_bundle_and_windows_runtime(self) -> None:
        uid = "12345678-1234-5678-1234-567812345678"
        schema = (
            f'<model uid="{uid}"><version>0.1.247-native-llama-bundle</version>'
            '<languages><language>kotlin</language></languages>'
            '<native><archive>full-line-inference.zip</archive><version>2.4.164</version></native></model>'
        ).encode()
        responses = [
            http_response(schema, "https://download.jetbrains.com/model.xml"),
            http_response(zip_bytes({"model.xml": schema, "flcc.bpe": b"bpe", "flcc.json": b"{}", "flcc.model": b"model"}), "https://download.jetbrains.com/model.jar"),
            http_response(zip_bytes({"full-line-inference.exe": b"MZruntime"}), "https://download.jetbrains.com/runtime.zip"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(models.requests, "get", side_effect=responses) as get:
                with contextlib.redirect_stderr(io.StringIO()):
                    result = models.main([
                        "--model", "kotlin", "--os", "windows", "--arch", "x86_64",
                        "--proxy", "http://127.0.0.1:7890", "--output", temporary,
                    ])
            self.assertEqual(result, 0)
            self.assertEqual(get.call_count, 3)
            for call in get.call_args_list:
                self.assertEqual(call.kwargs["proxies"], {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})
            self.assertIn("/2.4.164/windows/x86_64/full-line-inference.zip", get.call_args_list[-1].args[0])
            folder = Path(temporary) / "full-line/models" / uid
            self.assertTrue((folder / "ready.flag").is_file())
            self.assertEqual((folder / "full-line-inference.zip_extracted/full-line-inference.exe").read_bytes(), b"MZruntime")
            self.assertTrue((folder.parent / "models.xml").is_file())

    def test_regional_cdn_404_reports_proxy_and_does_not_retry(self) -> None:
        response = http_response(b"not found", "https://download-cdn.clf.jetbrains.com.cn/runtime.zip?Signature=example", 404)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "runtime.zip"
            partial = destination.with_suffix(".zip.part")
            partial.write_bytes(b"incomplete")
            with mock.patch.object(models.requests, "get", return_value=response) as get:
                with mock.patch.object(models.time, "sleep") as sleep:
                    with self.assertRaisesRegex(models.requests.HTTPError, "--proxy"):
                        models.download_file("https://download.jetbrains.com/runtime.zip", destination, timeout=10, retries=3)
            get.assert_called_once()
            sleep.assert_not_called()
            self.assertFalse(partial.exists())
            self.assertFalse(destination.exists())

    def test_default_proxy_settings_remain_available(self) -> None:
        response = http_response(b"schema", "https://download.jetbrains.com/model.xml")
        with mock.patch.object(models.requests, "get", return_value=response) as get:
            self.assertEqual(models.read_url(response.url, timeout=10, retries=3), b"schema")
        self.assertIsNone(get.call_args.kwargs["proxies"])

    def test_transient_server_errors_still_retry(self) -> None:
        url = "https://download.jetbrains.com/model.xml"
        responses = [http_response(b"unavailable", url, 503), http_response(b"schema", url)]
        with mock.patch.object(models.requests, "get", side_effect=responses) as get:
            with mock.patch.object(models.time, "sleep") as sleep:
                self.assertEqual(models.read_url(url, timeout=10, retries=3), b"schema")
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(2)


class PluginModelVersionsTest(unittest.TestCase):
    def test_2026_3_bundled_plugin_lists_python_from_main_jar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "IntelliJ IDEA.app/Contents/plugins/fullLine"
            write_jar(
                plugin / "lib/fullLine.jar",
                {
                    "META-INF/plugin.xml": b"<idea-plugin><version>263.7307.200</version></idea-plugin>",
                    PYTHON_SUPPORTER: b"\xca\xfe\xba\xbe\x000.1.198-native-llama-bundle\x00",
                    # Other supporters and experiments in this JAR must not
                    # supply the version for Python.
                    "com/intellij/fullLine/cpp/OCFullLineSupporter.class": b"0.1.47-native-llama-bundle",
                    "PythonExperiment.class": b"0.1.999-native-llama-bundle",
                },
            )
            write_jar(
                plugin / "lib/modules/intellij.fullLine.kotlin.local.jar",
                {
                    KOTLIN_SUPPORTER: (
                        b"0.1.249-native-llama-bundle\x00"
                        b"0.1.248-native-llama-bundle\x00"
                        b"0.1.247-native-llama-bundle\x00"
                    ),
                },
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = models.main(["--plugin-dir", str(plugin), "--list-models"])
            self.assertEqual(result, 0)
            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    "# profile:263.7307.200",
                    "kotlin:0.1.247-native-llama-bundle",
                    "python-v2:0.1.198-native-llama-bundle",
                ],
            )

    def test_separate_python_module_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary)
            write_jar(
                plugin / "lib/fullLine.jar",
                {
                    "META-INF/plugin.xml": b"<idea-plugin><version>261.25134.95</version></idea-plugin>",
                    PYTHON_SUPPORTER: b"0.1.197-native-llama-bundle",
                },
            )
            write_jar(
                plugin / "lib/modules/intellij.fullLine.python.local.jar",
                {PYTHON_SUPPORTER: b"0.1.198-native-llama-bundle"},
            )
            profile, versions = models.analyze_plugin_model_versions(plugin)
            self.assertEqual(profile, "261.25134.95")
            self.assertEqual(versions, {"python-v2": "0.1.198-native-llama-bundle"})

    def test_decompiled_plugin_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary)
            descriptor = plugin / "resources/META-INF/plugin.xml"
            descriptor.parent.mkdir(parents=True)
            descriptor.write_text("<idea-plugin><version>261.25134.95</version></idea-plugin>", encoding="utf-8")
            write_jar(
                plugin / "modules/intellij.fullLine.python.local.jar",
                {PYTHON_SUPPORTER: b"0.1.198-native-llama-bundle"},
            )
            profile, versions = models.analyze_plugin_model_versions(plugin)
            self.assertEqual(profile, "261.25134.95")
            self.assertEqual(versions, {"python-v2": "0.1.198-native-llama-bundle"})

    def test_default_and_explicit_profiles(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            models.main(["--list-models"])
        self.assertTrue(output.getvalue().startswith("# profile:263.7307.200\n"))
        for alias, build in (("2026.3", "263.7307.200"), ("2026.1.3", "261.25134.95")):
            with self.subTest(alias=alias):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    models.main(["--model-profile", alias, "--list-models"])
                self.assertTrue(output.getvalue().startswith(f"# profile:{build}\n"))
                self.assertIn("python-v2:0.1.198-native-llama-bundle\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
