# JetBrains Offline Plugins and Full Line Models

[中文](README-CN.md)

Tools for preparing JetBrains IDE resources for restricted or offline environments.

- `jetbrains_plugins.py` mirrors the latest Marketplace plugin versions compatible with a target IDE build and writes an IntelliJ Platform `updatePlugins.xml` custom plugin repository.
- `jetbrains_fullline_models.py` downloads Inline Code Completion / Full Line local models and native inference runtimes into the cache layout used by JetBrains IDEs.

## Requirements

- Python 3.9 or newer.
- `requests`.
- Network access to JetBrains Marketplace, JetBrains Releases, and the Full Line model repositories while preparing the offline files.

Install the only external dependency if needed:

```bash
python3 -m pip install requests
```

## Mirror Marketplace Plugins

Mirror one or more plugins for a specific IDE version:

```bash
python3 jetbrains_plugins.py \
  --build 2026.1.3 \
  --plugin org.intellij.scala \
  --plugin com.intellij.plugins.watcher \
  --output dist
```

`--build` accepts either a real IDE build number, such as `261.25134.95`, or an IDE version, such as `2026.1.3`. When an IDE version is used, the script resolves it through the JetBrains Releases API.

The default product code is `IU` for IntelliJ IDEA Ultimate. Use `--product-code` for other IDEs. JetBrains Marketplace expects the build value in the form `<productCode>-<buildNumber>`, for example `IU-261.25134.95`.

JetBrains product codes are documented here:

```text
https://plugins.jetbrains.com/docs/marketplace/product-codes.html
```

The output directory looks like this:

```text
dist/
  updatePlugins.xml
  plugins/
    <plugin>.zip
```

If `updatePlugins.xml` already exists, only entries for the plugins mirrored in the current run are updated or appended. Existing entries for other plugins are preserved.

Serve `dist` as a static directory, for example with nginx, and add this custom plugin repository URL in the IDE:

```text
http://your-nginx-host/updatePlugins.xml
```

By default, plugin URLs inside `updatePlugins.xml` are relative, such as `plugins/<plugin>.zip`. To write absolute URLs, pass `--repo-url`:

```bash
python3 jetbrains_plugins.py \
  --build 2026.1.3 \
  --plugin org.intellij.scala \
  --output dist \
  --repo-url http://your-nginx-host/
```

### Batch Plugin Lists

Create a text file with one Marketplace plugin id per line:

```text
org.intellij.scala
com.intellij.plugins.watcher
```

Then run:

```bash
python3 jetbrains_plugins.py \
  --build 2026.1.3 \
  --plugins-file plugins.txt \
  --output dist
```

Blank lines and lines starting with `#` are ignored. Plugin ids can be XML ids such as `org.intellij.scala`, numeric Marketplace ids, or Marketplace plugin URLs. Numeric ids are resolved to XML ids when possible.

If Marketplace returns no compatible version for the requested build, that plugin is skipped and the script continues with the remaining plugins.

Useful options:

```text
--channel <name>       Use a specific Marketplace release channel.
--timeout <seconds>    Network timeout. Default: 60.
--retries <count>      Download retry count. Default: 3.
```

## Download Full Line Models

Download a single local model:

```bash
python3 jetbrains_fullline_models.py --model kotlin
```

Download multiple models:

```bash
python3 jetbrains_fullline_models.py --model kotlin,java,go
```

The default output is `dist/full-line/models`. If `--output` points directly to a `full-line/models` directory, the script writes there. Otherwise it creates `full-line/models` under the output directory.

The generated layout matches the IDE cache structure:

```text
full-line/
  models/
    models.xml
    <model-uid>/
      model.xml
      flcc.bpe
      flcc.json
      flcc.model
      full-line-inference.zip_extracted/
      ready.flag
```

To install models directly into an IDE system directory, pass the IDE system path or its `full-line/models` directory:

```bash
python3 jetbrains_fullline_models.py \
  --model java \
  --output ~/Library/Caches/JetBrains/IntelliJIdea2026.1
```

`models.xml` uses IDEA's `<models version="1">` format, and model directory UUIDs follow the plugin's `ModelSchema.uid()` algorithm. Incremental downloads preserve other models already in the index.

Repair caches produced by older script versions offline, without downloading the models again:

```bash
python3 jetbrains_fullline_models.py --repair-cache /path/to/full-line/models
```

Pass the directory containing the model UUID subdirectories directly to `--repair-cache`. It checks model files and destination conflicts before renaming directories and rebuilding the index. Model weights, native runtimes, and `ready.flag` files are retained. The original index is backed up once outside the models directory, at `full-line/models-index.xml.bak`. Exit the IDE before repairing its cache.

### Model Versions

For bare model tags such as `--model kotlin`, the script defaults to the built-in IDEA `2026.3` profile (build `263.7307.200`). Use `--model-profile 2026.1.3` to select the older profile (build `261.25134.95`).

Web languages use JetBrains' shared `ws` model tag. For example, `--model css`, `--model javascript`, `--model js`, and `--model ts` download from `/models/ws/...`, matching the IDE behavior.

Python uses JetBrains' `python-v2` model tag. `--model python` downloads from `/models/python-v2/...`.

List available built-in profiles:

```bash
python3 jetbrains_fullline_models.py --list-model-profiles
```

List model tags and versions for a profile:

```bash
python3 jetbrains_fullline_models.py --model-profile 2026.3 --list-models
```

Override a model version explicitly:

```bash
python3 jetbrains_fullline_models.py --model kotlin:0.1.163
python3 jetbrains_fullline_models.py --model kotlin:0.1.163-native-llama-bundle
```

IDEA 2026.3 bundles Full Line in the application directory. On macOS, pass that directory to read the installed plugin build and model versions:

```bash
python3 jetbrains_fullline_models.py \
  --plugin-dir "/Applications/IntelliJ IDEA.app/Contents/plugins/fullLine" \
  --model kotlin,java,go,html,css,javascript,python,rust,ruby \
  --output dist
```

`--plugin-dir` only affects model tag/version selection. It does not override the target runtime platform.

Use `--plugin-dir "/Applications/IntelliJ IDEA.app/Contents/plugins/fullLine" --list-models` to inspect detected versions first. The script reads both `lib/fullLine.jar` and `lib/modules/`, including the Python model version moved into the main JAR in 2026.3.

Older user plugin directories (such as `~/Library/Application Support/JetBrains/IntelliJIdea2026.1/plugins/fullLine`) and decompiled layouts (`resources/META-INF/plugin.xml` plus `modules/`) remain supported.

### Runtime Platform

By default, the native inference runtime is downloaded for the current OS and architecture. Override it when preparing files for a different machine:

```bash
python3 jetbrains_fullline_models.py \
  --model kotlin,java,go \
  --os windows \
  --arch x86_64 \
  --output dist
```

Common OS values are `macos`, `windows`, and `linux`. Common architecture values are `x86_64` and `arm_64`.

If a download redirects to `download-cdn.*.jetbrains.com.cn` and returns 404, pass a working proxy with `--proxy http://127.0.0.1:7890` (replace the address as needed). This applies to model schemas, model bundles, and native runtimes. Without this option, the script uses the default `requests` proxy settings, including `HTTPS_PROXY`. When launching from an IDE, add `--proxy` to the script arguments.

The script fails immediately on 404. Rerunning reuses model files that were already fully extracted.

### Bundled Model ZIPs

If you already have a bundled `full-line-model-*.zip`, the script can read its model tag and version:

```bash
python3 jetbrains_fullline_models.py \
  --model-zip /path/to/full-line-model-kotlin.zip \
  --output dist
```

### Repositories

Normal model versions use JetBrains CDN by default:

```text
https://download.jetbrains.com/resources/ml/full-line/models/<tag>/<version>/model.xml
https://download.jetbrains.com/resources/ml/full-line/models/<tag>/<version>/local-model-<tag>-<version>.jar
https://download.jetbrains.com/resources/ml/full-line/servers/<native_version>/<OS>/<ARCH>/full-line-inference.zip
```

Versions containing `-beta` use JetBrains Packages by default. Use `--repository cdn` or `--repository maven` to force a repository.

## Typical Offline Workflow

1. Run the scripts on a machine with internet access.
2. Copy or publish the generated `dist` directory to the offline network.
3. Serve `dist/updatePlugins.xml` as an IntelliJ custom plugin repository.
4. Put `full-line/models` under the target IDE system cache directory, or distribute it as part of your offline setup process.

## Validation

Run the regression tests from this directory:

```bash
python3 -m unittest -v test_jetbrains_fullline_models
```

The tests include UUIDs recorded from a Windows IDEA cache. Set `FULL_LINE_IDEA_HOME` to also validate the generated cache using the installed plugin's XML deserializer and model readiness checks:

```bash
FULL_LINE_IDEA_HOME="/Applications/IntelliJ IDEA.app" \
  python3 -m unittest -v test_jetbrains_fullline_models
```

## License

See [LICENSE](LICENSE).
