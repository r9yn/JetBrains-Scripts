#!/usr/bin/env python3
"""
Download JetBrains Full Line local models into the same cache layout used by
the IDE:

  full-line/models/
    models.xml
    <model-uid>/
      model.xml
      flcc.bpe
      flcc.json
      flcc.model
      full-line-inference.zip_extracted/
      ready.flag

The implementation follows the decompiled Full Line plugin logic in:
  org.jetbrains.completion.full.line.impl.local.files.LocalModelsDownloadManager
  org.jetbrains.completion.full.line.impl.local.files.LocalModelsFilesService
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests


MODEL_CDN_BASE = "https://download.jetbrains.com/resources/ml/full-line/models"
SERVER_CDN_BASE = "https://download.jetbrains.com/resources/ml/full-line/servers"
MAVEN_MODEL_BASE = "https://packages.jetbrains.team/maven/p/ccrm/flcc-local-models/org/jetbrains/completion/full/line"
MAVEN_SERVER_BASE = "https://packages.jetbrains.team/files/p/ccrm/native-server"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}
READY_FILENAME = "ready.flag"
DEFAULT_OUTPUT = "dist"
DEFAULT_MODEL_VERSION_PROFILE = "263.7307.200"

# The plugin determines this at runtime as:
# LocalModelsDownloadManager.download()
#   -> LocalModelDescriptorKt.selectModelVersion()
#   -> FullLineLanguageSupporter.Companion.modelSettingsFor(language)
#   -> ModelSettings.versions.selectVersionValueOrThrow(descriptor)
#
# For LLAMA_NATIVE models, ModelSettings.versions selects nativeLLaMAVersion.
# The defaults below are taken from the language supporter classes in
# fullLine/lib/modules/intellij.fullLine.*.local.jar or lib/fullLine.jar.
# Experiment classes may contain additional versions; the normal download path
# uses the supporter default.
MODEL_VERSIONS_BY_PROFILE = {
    # Full Line plugin 261.25134.95 bundled with IDEA 2026.1.3.
    "261.25134.95": {
        "css": "0.1.41-native-llama-bundle",
        "go": "0.1.39-native-llama-bundle",
        "html": "0.1.12-native-llama-bundle",
        "java": "0.1.87-native-llama-bundle",
        "javascript": "0.1.41-native-llama-bundle",
        "kotlin": "0.1.247-native-llama-bundle",
        "php": "0.1.40-native-llama-bundle",
        "python-v2": "0.1.198-native-llama-bundle",
        "ruby": "0.1.22-native-llama-bundle",
        "rust": "0.1.41-native-llama-bundle",
        "typescript": "0.1.41-native-llama-bundle",
        "ws": "0.1.41-native-llama-bundle",
        "cpp": "0.1.52-native-llama-bundle",
        "csharp": "0.1.46-native-llama-bundle",
        "terraform": "0.1.20-native-llama-bundle",
    },
    # Full Line plugin 263.7307.200 bundled with IDEA 2026.3. The stable model
    # defaults are unchanged; Kotlin also contains separate nightly versions.
    "263.7307.200": {
        "css": "0.1.41-native-llama-bundle",
        "go": "0.1.39-native-llama-bundle",
        "html": "0.1.12-native-llama-bundle",
        "java": "0.1.87-native-llama-bundle",
        "javascript": "0.1.41-native-llama-bundle",
        "kotlin": "0.1.247-native-llama-bundle",
        "php": "0.1.40-native-llama-bundle",
        "python-v2": "0.1.198-native-llama-bundle",
        "ruby": "0.1.22-native-llama-bundle",
        "rust": "0.1.41-native-llama-bundle",
        "typescript": "0.1.41-native-llama-bundle",
        "ws": "0.1.41-native-llama-bundle",
        "cpp": "0.1.52-native-llama-bundle",
        "csharp": "0.1.46-native-llama-bundle",
        "terraform": "0.1.20-native-llama-bundle",
    },
}

MODEL_VERSION_PROFILE_ALIASES = {
    "2026.1.3": "261.25134.95",
    "2026.3": "263.7307.200",
}

MODEL_ALIASES = {
    "c++": "cpp",
    "css": "ws",
    "js": "ws",
    "javascript": "ws",
    "python": "python-v2",
    "ts": "ws",
    "typescript": "ws",
}

LOCAL_JAR_TAGS = {
    "intellij.fullLine.css.local.jar": ("ws",),
    "intellij.fullLine.go.local.jar": ("go",),
    "intellij.fullLine.html.local.jar": ("html",),
    "intellij.fullLine.java.local.jar": ("java",),
    "intellij.fullLine.js.local.jar": ("ws",),
    "intellij.fullLine.kotlin.local.jar": ("kotlin",),
    "intellij.fullLine.php.local.jar": ("php",),
    "intellij.fullLine.python.local.jar": ("python-v2",),
    "intellij.fullLine.rider.cpp.local.jar": ("cpp",),
    "intellij.fullLine.rider.csharp.local.jar": ("csharp",),
    "intellij.fullLine.ruby.local.jar": ("ruby",),
    "intellij.fullLine.rust.local.jar": ("rust",),
    "intellij.fullLine.terraform.local.jar": ("terraform",),
}

# IDEA 2026.3 packages the Python supporter in the main plugin JAR instead of
# intellij.fullLine.python.local.jar. Select its class explicitly because the
# main JAR also contains supporters for other languages.
MAIN_JAR_SUPPORTER_TAGS = {
    "org/jetbrains/completion/full/line/python/PythonFullLineSupporter.class": ("python-v2",),
}

MODEL_VERSION_PATTERN = re.compile(rb"0\.1\.\d+(?:-[A-Za-z0-9_.-]+)?")


@dataclass(frozen=True)
class ModelRequest:
    tag: str
    version: str
    languages: tuple[str, ...]
    uid: str | None = None


@dataclass(frozen=True)
class NativeSchema:
    archive: str
    version: str


@dataclass(frozen=True)
class ModelSchema:
    uid: str
    tag: str
    version: str
    element: ET.Element
    model_xml: bytes
    binary_path: str
    bpe_path: str
    config_path: str
    native: NativeSchema | None
    languages: tuple[str, ...]


def log(message: str) -> None:
    print(message, file=sys.stderr)


def check_download_response(response: requests.Response, url: str) -> None:
    host = urllib.parse.urlsplit(response.url).hostname or ""
    if response.status_code == 404 and host.endswith(".jetbrains.com.cn"):
        raise requests.HTTPError(
            f"JetBrains China CDN returned 404 for {url}. "
            "Retry with --proxy <proxy-url> or set HTTPS_PROXY to use a working proxy.",
            response=response,
        )
    response.raise_for_status()


def download_should_retry(exc: Exception, attempt: int, retries: int) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 404:
        return False
    return attempt < retries


def read_url(url: str, *, timeout: int, retries: int, proxy: str | None = None) -> bytes:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=HEADERS, timeout=timeout, proxies=proxies) as response:
                check_download_response(response, url)
                return response.content
        except Exception as exc:
            if not download_should_retry(exc, attempt, retries):
                raise
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError(f"Failed to read {url}")


def download_file(url: str, destination: Path, *, timeout: int, retries: int, proxy: str | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=HEADERS, timeout=timeout, stream=True, proxies=proxies) as response:
                check_download_response(response, url)
                with tmp.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
            tmp.replace(destination)
            return
        except Exception as exc:
            if tmp.exists():
                tmp.unlink()
            if not download_should_retry(exc, attempt, retries):
                raise
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError(f"Failed to download {url}")


def safe_part(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("empty path part")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"unsafe path part: {value!r}")
    return value


def safe_relative_path(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise ValueError(f"unsafe relative path: {value!r}")
    for part in value.split("/"):
        safe_part(part)
    return value


def text_of(element: ET.Element, path: str) -> str | None:
    child = element.find(path)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def attr_of(element: ET.Element, name: str) -> str | None:
    value = element.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def first_text(element: ET.Element, paths: Iterable[str]) -> str | None:
    for path in paths:
        value = text_of(element, path)
        if value:
            return value
    return None


def first_attr(element: ET.Element, names: Iterable[str]) -> str | None:
    for name in names:
        value = attr_of(element, name)
        if value:
            return value
    return None


def parse_xml(data: bytes, source: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid XML from {source}: {exc}") from exc


def parse_model_schema(data: bytes, *, tag: str, requested_uid: str | None, source: str) -> ModelSchema:
    root = parse_xml(data, source)
    version = first_attr(root, ("version",)) or first_text(root, ("version", "./model/version"))
    if not version:
        raise RuntimeError(f"No model version found in {source}")

    binary_path = schema_file_path(root, "binary", "flcc.model")
    bpe_path = schema_file_path(root, "bpe", "flcc.bpe")
    config_path = schema_file_path(root, "config", "flcc.json")
    native = parse_native_schema(root)
    languages = parse_languages(root) or languages_for_model_name(tag)
    uid = requested_uid or schema_uid(root, tag=tag, version=version, languages=languages, source=source)

    return ModelSchema(
        uid=uid,
        tag=safe_part(tag.lower()),
        version=safe_part(version),
        element=root,
        model_xml=data,
        binary_path=binary_path,
        bpe_path=bpe_path,
        config_path=config_path,
        native=native,
        languages=languages,
    )


def schema_file_path(root: ET.Element, name: str, fallback: str) -> str:
    node = root.find(name)
    if node is None:
        node = root.find(f"./files/{name}")
    value = first_attr(node, ("path", "file", "name")) if node is not None else None
    value = value or first_text(
        root,
        (
            f"./{name}/path",
            f"./{name}/file",
            f"./{name}",
            f"./files/{name}/path",
        ),
    )
    return safe_relative_path(value or fallback)


def parse_native_schema(root: ET.Element) -> NativeSchema | None:
    native = root.find("native")
    if native is None:
        return None
    archive = first_attr(native, ("archive", "file", "path")) or first_text(native, ("archive", "file", "path"))
    version = attr_of(native, "version") or text_of(native, "version")
    if not archive or not version:
        return None
    return NativeSchema(archive=safe_relative_path(archive), version=safe_part(version))


def parse_languages(root: ET.Element) -> tuple[str, ...]:
    values: list[str] = []
    for language in root.findall("./languages/language"):
        value = first_attr(language, ("id", "name", "value"))
        if value:
            values.append(value)
        elif language.text and language.text.strip():
            values.append(language.text.strip())
    for language in root.findall("./language"):
        value = first_attr(language, ("id", "name", "value"))
        if value:
            values.append(value)
        elif language.text and language.text.strip():
            values.append(language.text.strip())
    return tuple(dict.fromkeys(values))


def schema_uid(root: ET.Element, *, tag: str, version: str, languages: tuple[str, ...], source: str) -> str:
    explicit = first_attr(root, ("uid", "uuid", "id", "modelUid", "modelId"))
    explicit = explicit or first_text(root, ("uid", "uuid", "id", "modelUid", "modelId"))
    if explicit:
        return str(uuid.UUID(explicit))

    fingerprint = "|".join([tag.lower(), version, ",".join(languages)])
    generated = str(uuid.uuid3(uuid.NAMESPACE_URL, fingerprint))
    log(f"No uid found in {source}; generated {generated} from {fingerprint!r}")
    return generated


def parse_model_request(value: str, model_versions: dict[str, str], profile: str) -> ModelRequest:
    parts = value.split(":")
    if len(parts) not in {1, 2, 3}:
        raise RuntimeError(f"Invalid --model {value!r}; expected <tag>, <tag>:<version>, or <tag>:<version>:<uid>.")
    tag = normalize_model_tag(parts[0])
    version = default_model_version(tag, model_versions, profile) if len(parts) == 1 else normalize_model_version(safe_part(parts[1]))
    uid_value = str(uuid.UUID(parts[2])) if len(parts) == 3 else None
    return ModelRequest(tag=tag, version=version, languages=languages_for_model_name(tag), uid=uid_value)


def normalize_model_tag(value: str) -> str:
    tag = value.strip().lower()
    tag = MODEL_ALIASES.get(tag, tag)
    tag = safe_part(tag)
    return tag


def normalize_model_profile(value: str) -> str:
    profile = value.strip()
    return MODEL_VERSION_PROFILE_ALIASES.get(profile, profile)


def model_versions_for_profile(profile: str, analyzed_profiles: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    if analyzed_profiles and profile in analyzed_profiles:
        return analyzed_profiles[profile]
    model_versions = MODEL_VERSIONS_BY_PROFILE.get(profile)
    if model_versions is None:
        known = sorted(set(MODEL_VERSIONS_BY_PROFILE) | set(MODEL_VERSION_PROFILE_ALIASES) | set(analyzed_profiles or {}))
        raise RuntimeError(f"No default model version profile for {profile!r}. Known profiles: {', '.join(known)}.")
    return model_versions


def default_model_version(tag: str, model_versions: dict[str, str], profile: str) -> str:
    version = model_versions.get(tag)
    if version is None:
        known = ", ".join(sorted(model_versions))
        raise RuntimeError(f"No default model version for {tag!r} in profile {profile}; pass {tag}:<version>. Known tags: {known}.")
    return version


def analyze_plugin_model_versions(plugin_dir: Path) -> tuple[str, dict[str, str]]:
    root = plugin_dir.expanduser().resolve()
    profile_source, plugin_xml = read_plugin_xml(root)
    profile = read_plugin_profile(plugin_xml, profile_source)
    modules = find_modules_dir(root)

    model_versions: dict[str, str] = {}
    for jar_path in (root / "lib" / "fullLine.jar", root / "fullLine.jar"):
        if not jar_path.is_file():
            continue
        for class_name, tags in MAIN_JAR_SUPPORTER_TAGS.items():
            version = read_default_model_version_from_jar(jar_path, class_name=class_name)
            if version:
                for tag in tags:
                    model_versions[tag] = version

    # Separate language modules take precedence when both layouts are present.
    for jar_path in sorted(modules.glob("intellij.fullLine*.local.jar")):
        tags = tags_for_local_jar(jar_path.name)
        if not tags:
            continue
        version = read_default_model_version_from_jar(jar_path)
        if not version:
            continue
        for tag in tags:
            model_versions[tag] = version

    if not model_versions:
        raise RuntimeError(f"No Full Line local model versions found in {modules}")
    return profile, model_versions


def read_plugin_xml(plugin_dir: Path) -> tuple[str, ET.Element]:
    candidates = (
        plugin_dir / "resources" / "META-INF" / "plugin.xml",
        plugin_dir / "META-INF" / "plugin.xml",
    )
    for path in candidates:
        if path.is_file():
            return str(path), ET.parse(path).getroot()

    jar_candidates = (
        plugin_dir / "lib" / "fullLine.jar",
        plugin_dir / "fullLine.jar",
    )
    for jar_path in jar_candidates:
        if not jar_path.is_file():
            continue
        with zipfile.ZipFile(jar_path) as archive:
            for name in ("META-INF/plugin.xml", "plugin.xml"):
                if name in archive.namelist():
                    root = ET.fromstring(archive.read(name))
                    return f"{jar_path}!/{name}", root

    raise RuntimeError(f"No META-INF/plugin.xml found under {plugin_dir} or {plugin_dir / 'lib' / 'fullLine.jar'}")


def read_plugin_profile(root: ET.Element, source: str) -> str:
    version = first_text(root, ("version",))
    if not version:
        idea_version = root.find("idea-version")
        version = idea_version.get("since-build") if idea_version is not None else None
    if not version:
        raise RuntimeError(f"No plugin version found in {source}")
    return version.strip()


def find_modules_dir(plugin_dir: Path) -> Path:
    candidates = (
        plugin_dir / "modules",
        plugin_dir / "lib" / "modules",
    )
    for path in candidates:
        if path.is_dir():
            return path
    raise RuntimeError(f"No modules directory found under {plugin_dir} or {plugin_dir / 'lib'}")


def tags_for_local_jar(jar_name: str) -> tuple[str, ...]:
    known = LOCAL_JAR_TAGS.get(jar_name)
    if known:
        return known

    prefix = "intellij.fullLine."
    suffix = ".local.jar"
    if not jar_name.startswith(prefix) or not jar_name.endswith(suffix):
        return ()
    raw = jar_name[len(prefix) : -len(suffix)]
    if not raw:
        return ()
    tag = raw.split(".")[-1]
    return (normalize_model_tag(tag),)


def read_default_model_version_from_jar(jar_path: Path, *, class_name: str | None = None) -> str | None:
    versions: list[str] = []
    with zipfile.ZipFile(jar_path) as archive:
        for name in archive.namelist():
            if not name.endswith("FullLineSupporter.class"):
                continue
            if class_name is not None and name != class_name:
                continue
            versions.extend(read_model_versions_from_class(archive.read(name)))
    if not versions:
        return None
    return versions[-1]


def read_model_versions_from_class(data: bytes) -> list[str]:
    versions: list[str] = []
    for match in MODEL_VERSION_PATTERN.finditer(data):
        version = match.group().decode("ascii")
        if "native-llama-bundle" in version:
            versions.append(normalize_model_version(version))
    return versions


def read_model_zip_requests(paths: list[str]) -> list[ModelRequest]:
    requests: list[ModelRequest] = []
    for path_text in paths:
        path = Path(path_text)
        tag = infer_model_name_from_zip_path(path)
        with zipfile.ZipFile(path) as archive:
            schema_name = find_zip_entry(archive, "model.xml")
            if schema_name:
                schema = parse_model_schema(
                    archive.read(schema_name),
                    tag=tag,
                    requested_uid=None,
                    source=f"{path}:{schema_name}",
                )
                requests.append(ModelRequest(schema.tag, schema.version, schema.languages, schema.uid))
                continue

            zip_version = read_zip_version(archive)
            dir_version = infer_model_version_from_zip(archive, tag)
            version = dir_version or (normalize_model_version(zip_version) if zip_version else None)
            if version is None:
                raise RuntimeError(f"Cannot determine model version from {path}; no model.xml, .version, or model root directory found")
            requests.append(ModelRequest(tag=tag, version=safe_part(version), languages=languages_for_model_name(tag)))
    return requests


def infer_model_name_from_zip_path(path: Path) -> str:
    name = path.name
    match = re.match(r"full-line-model-(.+)\.zip$", name)
    if match:
        return safe_part(match.group(1).lower())
    stem = path.stem
    if stem.startswith("local-model-"):
        return safe_part(stem.removeprefix("local-model-").split("-", 1)[0].lower())
    raise RuntimeError(f"Cannot infer model tag from {path}; expected full-line-model-<tag>.zip")


def read_zip_version(archive: zipfile.ZipFile) -> str | None:
    for name in archive.namelist():
        if posixpath.basename(name) == ".version":
            version = archive.read(name).decode("utf-8").strip()
            return version or None
    return None


def infer_model_version_from_zip(archive: zipfile.ZipFile, tag: str) -> str | None:
    pattern = re.compile(rf"(^|/){re.escape(tag)}-(.+-bundle)(/|$)")
    for name in archive.namelist():
        match = pattern.search(name)
        if match:
            return match.group(2)
    return None


def find_zip_entry(archive: zipfile.ZipFile, suffix: str) -> str | None:
    suffix = suffix.strip("/")
    for name in archive.namelist():
        if not name.endswith("/") and name.endswith(suffix):
            return name
    return None


def normalize_model_version(version: str | None) -> str:
    if not version:
        raise RuntimeError("model version is empty")
    if version.endswith("-bundle"):
        return version
    return f"{version}-native-llama-bundle"


def languages_for_model_name(name: str) -> tuple[str, ...]:
    if name == "java":
        return ("JAVA",)
    if name == "kotlin":
        return ("kotlin",)
    if name in {"python", "python-v2"}:
        return ("python", "python-v2")
    if name in {"go", "golang"}:
        return ("go",)
    if name == "ws":
        return ("css", "javascript", "html", "ws")
    return (name,)


def unique_requests(entries: list[ModelRequest]) -> list[ModelRequest]:
    result: list[ModelRequest] = []
    seen: set[tuple[str, str, str | None]] = set()
    for entry in entries:
        key = (entry.tag, entry.version, entry.uid)
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def repository_for(version: str, requested: str) -> str:
    if requested != "auto":
        return requested
    return "maven" if "-beta" in version else "cdn"


def model_base_url(*, tag: str, version: str, repository: str, model_cdn_base: str, maven_model_base: str) -> str:
    fixed_tag = urllib.parse.quote(safe_part(tag.lower()))
    fixed_version = urllib.parse.quote(safe_part(version))
    if repository == "maven":
        return "/".join([maven_model_base.rstrip("/"), f"local-model-{fixed_tag}", fixed_version])
    return "/".join([model_cdn_base.rstrip("/"), fixed_tag, fixed_version])


def model_schema_url(*, tag: str, version: str, repository: str, model_cdn_base: str, maven_model_base: str) -> str:
    return f"{model_base_url(tag=tag, version=version, repository=repository, model_cdn_base=model_cdn_base, maven_model_base=maven_model_base)}/model.xml"


def model_jar_url(*, tag: str, version: str, repository: str, model_cdn_base: str, maven_model_base: str) -> str:
    fixed_tag = safe_part(tag.lower())
    fixed_version = safe_part(version)
    return f"{model_base_url(tag=tag, version=version, repository=repository, model_cdn_base=model_cdn_base, maven_model_base=maven_model_base)}/local-model-{fixed_tag}-{fixed_version}.jar"


def server_url(*, native: NativeSchema, repository: str, server_cdn_base: str, maven_server_base: str, os_name: str, arch: str) -> str:
    base = maven_server_base if repository == "maven" else server_cdn_base
    return "/".join(
        [
            base.rstrip("/"),
            urllib.parse.quote(native.version),
            urllib.parse.quote(os_name),
            urllib.parse.quote(arch),
            urllib.parse.quote(native.archive),
        ]
    )


def model_root(output_dir: Path) -> Path:
    if output_dir.name == "models" and output_dir.parent.name == "full-line":
        return output_dir
    return output_dir / "full-line" / "models"


def install_model(
    request_entry: ModelRequest,
    *,
    root: Path,
    repository: str,
    model_cdn_base: str,
    server_cdn_base: str,
    maven_model_base: str,
    maven_server_base: str,
    os_name: str,
    arch: str,
    timeout: int,
    retries: int,
    proxy: str | None = None,
) -> ModelSchema:
    repo = repository_for(request_entry.version, repository)
    schema_url = model_schema_url(
        tag=request_entry.tag,
        version=request_entry.version,
        repository=repo,
        model_cdn_base=model_cdn_base,
        maven_model_base=maven_model_base,
    )
    log(f"Download model schema: {schema_url}")
    schema_data = read_url(schema_url, timeout=timeout, retries=retries, proxy=proxy)
    schema = parse_model_schema(schema_data, tag=request_entry.tag, requested_uid=request_entry.uid, source=schema_url)
    folder = root / schema.uid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "model.xml").write_bytes(schema.model_xml)

    jar_name = f"local-model-{schema.tag}-{schema.version}.jar"
    jar_path = folder / jar_name
    if not required_model_files_exist(folder, schema):
        jar_url = model_jar_url(
            tag=schema.tag,
            version=schema.version,
            repository=repo,
            model_cdn_base=model_cdn_base,
            maven_model_base=maven_model_base,
        )
        log(f"Download model bundle: {jar_url}")
        download_file(jar_url, jar_path, timeout=timeout, retries=retries, proxy=proxy)
        extract_model_files(jar_path, folder, schema)
        jar_path.unlink(missing_ok=True)
    else:
        log(f"Model files already exist: {folder}")

    if schema.native:
        extracted = folder / extracted_name(schema.native)
        if not extracted.exists():
            archive_path = folder / schema.native.archive
            url = server_url(
                native=schema.native,
                repository=repo,
                server_cdn_base=server_cdn_base,
                maven_server_base=maven_server_base,
                os_name=os_name,
                arch=arch,
            )
            log(f"Download native server: {url}")
            download_file(url, archive_path, timeout=timeout, retries=retries, proxy=proxy)
            extract_zip(archive_path, extracted)
            archive_path.unlink(missing_ok=True)
        else:
            log(f"Native server already extracted: {extracted}")

    (folder / READY_FILENAME).touch()
    return schema


def required_model_files_exist(folder: Path, schema: ModelSchema) -> bool:
    return all((folder / path).is_file() for path in ("model.xml", schema.binary_path, schema.bpe_path, schema.config_path))


def extract_model_files(jar_path: Path, folder: Path, schema: ModelSchema) -> None:
    required = {
        "model.xml": folder / "model.xml",
        schema.binary_path: folder / schema.binary_path,
        schema.bpe_path: folder / schema.bpe_path,
        schema.config_path: folder / schema.config_path,
    }
    with zipfile.ZipFile(jar_path) as archive:
        for inner_path, destination in required.items():
            entry = find_model_entry(archive, schema.tag, schema.version, inner_path)
            if entry is None:
                raise RuntimeError(f"No {inner_path} found in {jar_path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(entry))


def find_model_entry(archive: zipfile.ZipFile, tag: str, version: str, path: str) -> str | None:
    prefixes = (f"{tag}-{version}/", "")
    for prefix in prefixes:
        name = prefix + path
        if name in archive.namelist():
            return name
    return find_zip_entry(archive, path)


def extracted_name(native: NativeSchema) -> str:
    return f"{native.archive}_extracted"


def extract_zip(path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            target = destination / member.filename
            target.resolve().relative_to(destination.resolve())
            archive.extract(member, destination)


def write_models_xml(root: Path, schemas: Iterable[ModelSchema]) -> Path:
    path = root / "models.xml"
    existing_by_uid: dict[str, ET.Element] = {}
    if path.exists() and path.stat().st_size > 0:
        try:
            existing_root = parse_xml(path.read_bytes(), str(path))
            for model in existing_root.findall(".//model"):
                uid = first_text(model, ("uid", "uuid", "id", "modelUid", "modelId"))
                if uid:
                    existing_by_uid[str(uuid.UUID(uid))] = model
        except Exception as exc:
            log(f"Could not preserve existing models.xml entries ({exc}); rewriting it.")

    for schema in schemas:
        existing_by_uid[schema.uid] = clone_element(schema.element)

    models_root = ET.Element("LocalModelsSchema")
    models = ET.SubElement(models_root, "models")
    for _, element in sorted(existing_by_uid.items()):
        models.append(element)

    indent_xml(models_root)
    tree = ET.ElementTree(models_root)
    root.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def clone_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="utf-8"))


def indent_xml(element: ET.Element, level: int = 0) -> None:
    indent = "\n" + level * "  "
    if len(element):
        if not element.text or not element.text.strip():
            element.text = indent + "  "
        for child in element:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    if level and (not element.tail or not element.tail.strip()):
        element.tail = indent


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download JetBrains Full Line models into IDE cache layout.")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Model as <tag>, <tag>:<version>, or <tag>:<version>:<uid>, for example kotlin. Repeatable; comma-separated values are also accepted.",
    )
    parser.add_argument(
        "--model-zip",
        action="append",
        default=[],
        help="Bundled full-line-model-*.zip; used to read model.xml/version. Repeatable.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"IDE system path or full-line/models path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument("--repository", choices=("auto", "cdn", "maven"), default="auto", help="Model/server repository. Default: auto.")
    parser.add_argument("--model-cdn-base", default=MODEL_CDN_BASE, help="Full Line model CDN base URL.")
    parser.add_argument("--server-cdn-base", default=SERVER_CDN_BASE, help="Full Line server CDN base URL.")
    parser.add_argument("--maven-model-base", default=MAVEN_MODEL_BASE, help="Full Line beta model Maven base URL.")
    parser.add_argument("--maven-server-base", default=MAVEN_SERVER_BASE, help="Full Line beta native server base URL.")
    parser.add_argument(
        "--model-profile",
        default=DEFAULT_MODEL_VERSION_PROFILE,
        help=f"IDE/plugin version profile used for bare --model tags. Accepts a plugin build or alias like 2026.3. Default: {DEFAULT_MODEL_VERSION_PROFILE}.",
    )
    parser.add_argument(
        "--plugin-dir",
        help="Extract model-profile and tag versions from an unpacked or installed Full Line plugin directory, including <IDE>.app/Contents/plugins/fullLine.",
    )
    parser.add_argument("--os", default=os_name_default(), help="Runtime OS path part. Default: current OS.")
    parser.add_argument("--arch", default=arch_default(), help="Runtime architecture path part. Default: current architecture.")
    parser.add_argument("--proxy", help="HTTP(S) or SOCKS proxy URL for all downloads, for example http://127.0.0.1:7890. Default: requests/system proxy settings.")
    parser.add_argument("--list-models", action="store_true", help="Print model tags and default versions for the selected profile/plugin, then exit.")
    parser.add_argument("--list-model-profiles", action="store_true", help="Print built-in model version profiles, then exit.")
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout seconds. Default: 60.")
    parser.add_argument("--retries", type=int, default=3, help="Download retries. Default: 3.")
    return parser.parse_args(argv)


def os_name_default() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def arch_default() -> str:
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    if machine in {"arm64", "aarch64"}:
        return "arm_64"
    return "x86_64"


def expand_model_values(values: list[str]) -> list[str]:
    expanded: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                expanded.append(item)
    return expanded


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    analyzed_profiles: dict[str, dict[str, str]] = {}
    profile = normalize_model_profile(args.model_profile)
    if args.plugin_dir:
        analyzed_profile, analyzed_versions = analyze_plugin_model_versions(Path(args.plugin_dir))
        analyzed_profiles[analyzed_profile] = analyzed_versions
        profile = analyzed_profile

    if args.list_model_profiles:
        for known_profile in sorted(MODEL_VERSIONS_BY_PROFILE):
            aliases = sorted(alias for alias, target in MODEL_VERSION_PROFILE_ALIASES.items() if target == known_profile)
            suffix = f" ({', '.join(aliases)})" if aliases else ""
            print(f"{known_profile}{suffix}")
        for known_profile in sorted(analyzed_profiles):
            print(f"{known_profile} (from {Path(args.plugin_dir).expanduser()})")
        return 0

    model_versions = model_versions_for_profile(profile, analyzed_profiles)
    if args.list_models:
        print(f"# profile:{profile}")
        for tag, version in sorted(model_versions.items()):
            print(f"{tag}:{version}")
        return 0

    requests_to_install = unique_requests(
        [parse_model_request(value, model_versions, profile) for value in expand_model_values(args.model)]
        + read_model_zip_requests(args.model_zip)
    )
    if not requests_to_install:
        raise SystemExit("Nothing to do: pass --model or --model-zip.")

    root = model_root(Path(args.output).expanduser()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    installed: list[ModelSchema] = []
    for entry in requests_to_install:
        installed.append(
            install_model(
                entry,
                root=root,
                repository=args.repository,
                model_cdn_base=args.model_cdn_base,
                server_cdn_base=args.server_cdn_base,
                maven_model_base=args.maven_model_base,
                maven_server_base=args.maven_server_base,
                os_name=safe_part(args.os),
                arch=safe_part(args.arch),
                timeout=args.timeout,
                retries=args.retries,
                proxy=args.proxy,
            )
        )

    xml_path = write_models_xml(root, installed)
    log(f"Wrote {xml_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
