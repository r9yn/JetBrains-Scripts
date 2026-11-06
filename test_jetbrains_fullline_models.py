import contextlib
import io
import os
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

import jetbrains_fullline_models as models


PYTHON_SUPPORTER = "org/jetbrains/completion/full/line/python/PythonFullLineSupporter.class"
KOTLIN_SUPPORTER = "org/jetbrains/completion/full/line/kotlin/KotlinFullLineSupporter.class"

# Directory names observed in a cache created by IDEA 263.7307.200 on Windows.
IDEA_MODELS = (
    ("kotlin", "0.1.247", ("kotlin",), "0905e9b0-0606-368a-b34d-122e77024c3a"),
    ("java", "0.1.87", ("java",), "f360ae9c-236b-3451-a2ff-a2545e1a3ba1"),
    ("html", "0.1.12", ("html",), "d75057fe-7724-304f-9a0a-067d93f49118"),
    ("ws", "0.1.41", ("css", "javascript", "html", "ws"), "6b923ccc-53b5-366f-8b16-580baab428b3"),
    ("python-v2", "0.1.198", ("python", "python-v2"), "baa3b306-cd6c-3e90-9dc3-a8d38bd594ed"),
)


def model_xml(tag: str, version: str, languages: tuple[str, ...]) -> bytes:
    languages_xml = "".join(f"<language>{language}</language>" for language in languages)
    return (
        f"<model><version>{version}-native-llama-bundle</version><size>5</size>"
        f"<jar>local-model-{tag}-{version}-native-llama-bundle.jar</jar>"
        f"<languages>{languages_xml}</languages>"
        "<binary>flcc.model</binary><bpe>flcc.bpe</bpe><config>flcc.json</config>"
        "<native><archive>full-line-inference.zip</archive><version>2.4.164</version></native></model>"
    ).encode()


def cached_model(root: Path, entry: tuple, folder_name: str | None = None) -> models.ModelSchema:
    tag, version, languages, _ = entry
    data = model_xml(tag, version, languages)
    schema = models.parse_model_schema(data, tag=tag, requested_uid=None, source="fixture")
    folder = root / (folder_name or schema.uid)
    folder.mkdir(parents=True)
    (folder / "model.xml").write_bytes(data)
    (folder / "flcc.model").write_bytes(b"model")
    (folder / "flcc.bpe").write_bytes(b"bpe")
    (folder / "flcc.json").write_bytes(b"{}")
    (folder / "ready.flag").touch()
    runtime = folder / "full-line-inference.zip_extracted"
    runtime.mkdir()
    (runtime / "full-line-inference.exe").write_bytes(b"MZruntime")
    return schema


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
        uid = IDEA_MODELS[0][3]
        schema = model_xml(*IDEA_MODELS[0][:3])
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
            index = ET.parse(folder.parent / "models.xml").getroot()
            self.assertEqual((index.tag, index.attrib), ("models", {"version": "1"}))
            self.assertEqual(len(index.findall("./model")), 1)
            self.assertIsNone(index.find("./models"))

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


class CacheCompatibilityTest(unittest.TestCase):
    def test_uids_match_official_windows_cache(self) -> None:
        for tag, version, languages, uid in IDEA_MODELS:
            with self.subTest(tag=tag):
                schema = models.parse_model_schema(model_xml(tag, version, languages), tag=tag, requested_uid=None, source="fixture")
                self.assertEqual(schema.uid, uid)

    def test_java_v2_language_uses_java_uid(self) -> None:
        self.assertEqual(models.schema_uid("0.1.87-native-llama-bundle", ("java-v2",)), IDEA_MODELS[1][3])

    def test_uid_does_not_depend_on_tag_or_an_xml_uid_field(self) -> None:
        data = model_xml(*IDEA_MODELS[0][:3]).replace(b"<model>", b'<model uid="12345678-1234-5678-1234-567812345678">')
        schema = models.parse_model_schema(data, tag="different-tag", requested_uid=None, source="fixture")
        self.assertEqual(schema.uid, IDEA_MODELS[0][3])

    def test_explicit_uid_must_match_idea_uid(self) -> None:
        data = model_xml(*IDEA_MODELS[0][:3])
        valid = models.parse_model_schema(data, tag="kotlin", requested_uid=IDEA_MODELS[0][3], source="fixture")
        self.assertEqual(valid.uid, IDEA_MODELS[0][3])
        with self.assertRaisesRegex(RuntimeError, "does not match IDEA"):
            models.parse_model_schema(data, tag="kotlin", requested_uid="f4bb50d9-a645-3c5d-bfb2-ccc202182a04", source="fixture")

    def test_incremental_index_preserves_models_without_uid_fields(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                opening, closing = (b"<LocalModelsSchema><models>", b"</models></LocalModelsSchema>") if legacy else (b'<models version="1">', b"</models>")
                (root / "models.xml").write_bytes(opening + model_xml(*IDEA_MODELS[1][:3]) + closing)
                kotlin = models.parse_model_schema(model_xml(*IDEA_MODELS[0][:3]), tag="kotlin", requested_uid=None, source="fixture")
                for _ in range(2):
                    models.write_models_xml(root, [kotlin])
                    index = ET.parse(root / "models.xml").getroot()
                    self.assertEqual((index.tag, index.attrib), ("models", {"version": "1"}))
                    self.assertEqual({m.findtext("version") for m in index.findall("./model")}, {"0.1.247-native-llama-bundle", "0.1.87-native-llama-bundle"})
                    self.assertEqual(len(index.findall("./model")), 2)

    def test_malformed_index_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "models.xml"
            index.write_bytes(b"<malformed")
            with self.assertRaisesRegex(RuntimeError, "Invalid XML"):
                models.write_models_xml(root, [])
            self.assertEqual(index.read_bytes(), b"<malformed")

    def test_offline_repair_reuses_files_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "models"
            old_uid = "f4bb50d9-a645-3c5d-bfb2-ccc202182a04"
            cached_model(root, IDEA_MODELS[0], old_uid)
            original_index = b"<LocalModelsSchema><models>" + model_xml(*IDEA_MODELS[0][:3]) + b"</models></LocalModelsSchema>"
            (root / "models.xml").write_bytes(original_index)
            old_folder = root / old_uid
            payload = {p.relative_to(old_folder): p.read_bytes() for p in old_folder.rglob("*") if p.is_file()}
            inode = (old_folder / "flcc.model").stat().st_ino
            with mock.patch.object(models.requests, "get", side_effect=AssertionError("Offline repair attempted a download")):
                with contextlib.redirect_stderr(io.StringIO()):
                    for _ in range(2):
                        self.assertEqual(models.main(["--repair-cache", str(root)]), 0)
            folder = root / IDEA_MODELS[0][3]
            self.assertFalse(old_folder.exists())
            self.assertEqual((folder / "flcc.model").stat().st_ino, inode)
            self.assertEqual({p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}, payload)
            self.assertEqual((root.parent / "models-index.xml.bak").read_bytes(), original_index)
            index = ET.parse(root / "models.xml").getroot()
            self.assertEqual((index.tag, index.attrib), ("models", {"version": "1"}))
            self.assertEqual(len(index.findall("./model")), 1)
            self.assertEqual(sorted(p.name for p in root.iterdir()), [IDEA_MODELS[0][3], "models.xml"])

    def test_repair_checks_all_conflicts_before_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "models"
            cached_model(root, IDEA_MODELS[1], "old-java")
            cached_model(root, IDEA_MODELS[0], "old-kotlin")
            (root / IDEA_MODELS[0][3]).mkdir()
            (root / "models.xml").write_bytes(b"old index")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                models.repair_model_cache(root)
            self.assertTrue((root / "old-java").is_dir())
            self.assertTrue((root / "old-kotlin").is_dir())
            self.assertEqual((root / "models.xml").read_bytes(), b"old index")
            self.assertFalse((root.parent / "models-index.xml.bak").exists())

    def test_repair_rejects_incomplete_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "models"
            cached_model(root, IDEA_MODELS[0], "old-kotlin")
            (root / "old-kotlin/flcc.model").unlink()
            with self.assertRaisesRegex(RuntimeError, "Incomplete model"):
                models.repair_model_cache(root)
            self.assertTrue((root / "old-kotlin").is_dir())
            self.assertFalse((root / IDEA_MODELS[0][3]).exists())

    @unittest.skipUnless(os.environ.get("FULL_LINE_IDEA_HOME"), "Set FULL_LINE_IDEA_HOME to run the installed IDEA plugin compatibility check")
    def test_cache_loads_with_actual_idea_serializer_and_ready_check(self) -> None:
        idea_home = Path(os.environ["FULL_LINE_IDEA_HOME"])
        if (idea_home / "Contents").is_dir():
            idea_home /= "Contents"
        java = idea_home / ("jbr/Contents/Home/bin/java" if os.sys.platform == "darwin" else "jbr/bin/java.exe" if os.name == "nt" else "jbr/bin/java")
        classpath = os.pathsep.join(str(idea_home / path) for path in ("lib/*", "plugins/fullLine/lib/*"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "models"
            schemas = [cached_model(root, entry) for entry in IDEA_MODELS]
            models.write_models_xml(root, schemas)
            probe = Path(temporary) / "CacheProbe.java"
            probe.write_text('''
import java.nio.file.*;
import com.intellij.openapi.util.JDOMUtil;
import com.intellij.util.xmlb.XmlSerializer;
import org.jetbrains.completion.full.line.local.LocalModelsSchema;
import org.jetbrains.completion.full.line.impl.local.files.LocalModelsCorruptedManager;
class CacheProbe {
  public static void main(String[] args) throws Exception {
    Path root = Path.of(args[0]);
    var schema = XmlSerializer.deserialize(JDOMUtil.load(Files.readString(root.resolve("models.xml"))), LocalModelsSchema.class);
    if (!Integer.valueOf(1).equals(schema.getVersion()) || schema.getModels().size() != 5) throw new AssertionError("Invalid model index");
    var checker = new LocalModelsCorruptedManager(root.toFile());
    for (var model : schema.getModels()) {
      if (!checker.hasReadyFlag(model)) throw new AssertionError("Model not ready: " + model.uid());
      if (!Files.isRegularFile(root.resolve(model.uid()).resolve("flcc.model"))) throw new AssertionError("Missing model");
    }
    System.out.println("5 models indexed and ready");
  }
}
''', encoding="utf-8")
            result = subprocess.run([str(java), "-cp", classpath, str(probe), str(root)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("5 models indexed and ready", result.stdout)


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
