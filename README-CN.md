# JetBrains 离线插件与 Full Line 模型

这个仓库用于为受限网络或离线环境准备 JetBrains IDE 资源。

- `jetbrains_plugins.py`：从 JetBrains Marketplace 下载目标 IDE build 兼容的最新插件版本，并生成 IntelliJ Platform custom plugin repository 可用的 `updatePlugins.xml`。
- `jetbrains_fullline_models.py`：下载 Inline Code Completion / Full Line 本地模型和原生推理运行时，并整理成 JetBrains IDE 实际使用的缓存目录结构。

## 环境要求

- Python 3.9 或更新版本。
- `requests`。
- 准备离线文件时，需要能访问 JetBrains Marketplace、JetBrains Releases 和 Full Line 模型仓库。

如果本机没有 `requests`，先安装：

```bash
python3 -m pip install requests
```

## 镜像 Marketplace 插件

下载某个 IDE 版本兼容的插件：

```bash
python3 jetbrains_plugins.py \
  --build 2026.1.3 \
  --plugin org.intellij.scala \
  --plugin com.intellij.plugins.watcher \
  --output dist
```

`--build` 可以传真实 IDE build 号，例如 `261.25134.95`；也可以传 IDE 版本号，例如 `2026.1.3`。传 IDE 版本号时，脚本会通过 JetBrains Releases API 查询对应 build。

默认产品代码是 IntelliJ IDEA Ultimate 的 `IU`。如果要给其它 IDE 准备插件，用 `--product-code` 指定产品代码。Marketplace 下载接口里的 build 参数格式是 `<产品代码>-<build号>`，例如 `IU-261.25134.95`。

JetBrains 产品代码说明：

```text
https://plugins.jetbrains.com/docs/marketplace/product-codes.html
```

输出目录结构：

```text
dist/
  updatePlugins.xml
  plugins/
    <plugin>.zip
```

如果 `updatePlugins.xml` 已存在，脚本只会更新或追加本次镜像到的插件条目，不会删除其它已有插件条目。

把 `dist` 作为静态目录发布，例如放到 nginx 下，然后在 IDE 的 custom plugin repositories 里配置：

```text
http://你的-nginx/updatePlugins.xml
```

默认写入 `updatePlugins.xml` 的插件地址是相对路径，例如 `plugins/<plugin>.zip`。如果需要写绝对 URL，传 `--repo-url`：

```bash
python3 jetbrains_plugins.py \
  --build 2026.1.3 \
  --plugin org.intellij.scala \
  --output dist \
  --repo-url http://你的-nginx/
```

### 批量插件列表

创建 `plugins.txt`，每行一个 Marketplace 插件 ID：

```text
org.intellij.scala
com.intellij.plugins.watcher
```

执行：

```bash
python3 jetbrains_plugins.py \
  --build 2026.1.3 \
  --plugins-file plugins.txt \
  --output dist
```

空行和以 `#` 开头的行会被忽略。插件 ID 可以是 `org.intellij.scala` 这种 XML ID，也可以是 Marketplace 数字 ID 或插件页面 URL；数字 ID 会尽量解析成 XML ID。

如果 Marketplace 没有返回目标 build 兼容的插件版本，脚本会跳过该插件并继续处理其它插件。

常用参数：

```text
--channel <名称>       指定 Marketplace 发布 channel。
--timeout <秒数>       网络超时时间，默认 60。
--retries <次数>       下载重试次数，默认 3。
```

## 下载 Full Line 模型

下载单个模型：

```bash
python3 jetbrains_fullline_models.py --model kotlin
```

一次下载多个模型：

```bash
python3 jetbrains_fullline_models.py --model kotlin,java,go
```

默认输出到 `dist/full-line/models`。如果 `--output` 直接指向某个 `full-line/models` 目录，脚本会直接写入该目录；否则会在输出目录下创建 `full-line/models`。

生成的结构与 IDE 缓存结构一致：

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

如果要直接写入某个 IDE 的 system cache，可以把 IDE system path 或其中的 `full-line/models` 目录传给 `--output`：

```bash
python3 jetbrains_fullline_models.py \
  --model java \
  --output ~/Library/Caches/JetBrains/IntelliJIdea2026.1
```

### 模型版本

只写模型 tag 时，例如 `--model kotlin`，脚本默认使用 IDEA `2026.3` 的内置 profile（build `263.7307.200`）。仍可通过 `--model-profile 2026.1.3` 使用旧版 profile（build `261.25134.95`）。

Web 语言使用 JetBrains 共享的 `ws` 模型 tag。例如 `--model css`、`--model javascript`、`--model js`、`--model ts` 会从 `/models/ws/...` 下载，和 IDE 行为一致。

Python 使用 JetBrains 的 `python-v2` 模型 tag。`--model python` 会从 `/models/python-v2/...` 下载。

查看内置 profile：

```bash
python3 jetbrains_fullline_models.py --list-model-profiles
```

查看某个 profile 下的模型 tag 和默认版本：

```bash
python3 jetbrains_fullline_models.py --model-profile 2026.3 --list-models
```

也可以显式指定模型版本，短版本和完整 bundle 版本都支持：

```bash
python3 jetbrains_fullline_models.py --model kotlin:0.1.163
python3 jetbrains_fullline_models.py --model kotlin:0.1.163-native-llama-bundle
```

IDEA 2026.3 已将 Full Line 插件内置到应用程序目录。macOS 下可直接传入该目录，让脚本读取实际安装的插件 build 和模型版本：

```bash
python3 jetbrains_fullline_models.py \
  --plugin-dir "/Applications/IntelliJ IDEA.app/Contents/plugins/fullLine" \
  --model kotlin,java,go,html,css,javascript,python,rust,ruby \
  --output dist
```

`--plugin-dir` 只影响模型 tag 和版本选择，不会覆盖目标运行平台。

可以先用 `--plugin-dir "/Applications/IntelliJ IDEA.app/Contents/plugins/fullLine" --list-models` 查看检测结果。脚本会读取 `lib/fullLine.jar` 和 `lib/modules/`，包括 2026.3 移到主 JAR 中的 Python 模型版本。

旧版用户插件目录（例如 `~/Library/Application Support/JetBrains/IntelliJIdea2026.1/plugins/fullLine`）和反编译布局（`resources/META-INF/plugin.xml` + `modules/`）仍然支持。

### 运行平台

默认会下载当前机器系统和架构对应的原生推理运行时。如果是在一台机器上给另一种平台准备离线文件，可以指定 `--os` 和 `--arch`：

```bash
python3 jetbrains_fullline_models.py \
  --model kotlin,java,go \
  --os windows \
  --arch x86_64 \
  --output dist
```

常用 OS 值包括 `macos`、`windows`、`linux`。常用架构值包括 `x86_64` 和 `arm_64`。

如果下载跳转到 `download-cdn.*.jetbrains.com.cn` 后返回 404，可以通过 `--proxy http://127.0.0.1:7890` 指定可用代理（请替换成实际地址）。该参数同时用于模型描述、模型文件和推理运行时下载；未指定时沿用 `requests` 默认代理设置，包括 `HTTPS_PROXY` 环境变量。从 IDE 运行脚本时，可直接将 `--proxy` 加到运行参数中。

脚本遇到 404 会立即报错；重新运行时会复用已完整解压的模型文件。

### 从模型 ZIP 读取版本

如果已经有插件包里的 `full-line-model-*.zip`，可以让脚本读取其中的模型 tag 和版本：

```bash
python3 jetbrains_fullline_models.py \
  --model-zip /path/to/full-line-model-kotlin.zip \
  --output dist
```

### 下载仓库

普通版本默认使用 JetBrains CDN：

```text
https://download.jetbrains.com/resources/ml/full-line/models/<tag>/<version>/model.xml
https://download.jetbrains.com/resources/ml/full-line/models/<tag>/<version>/local-model-<tag>-<version>.jar
https://download.jetbrains.com/resources/ml/full-line/servers/<native_version>/<OS>/<ARCH>/full-line-inference.zip
```

版本号包含 `-beta` 时，默认使用 JetBrains Packages。也可以用 `--repository cdn` 或 `--repository maven` 强制指定下载仓库。

## 推荐离线流程

1. 在可联网机器上运行脚本，生成 `dist`。
2. 把 `dist` 复制或发布到离线网络。
3. 将 `dist/updatePlugins.xml` 配置为 IntelliJ custom plugin repository。
4. 将 `full-line/models` 放到目标 IDE 的 system cache 目录，或纳入自己的离线部署流程。

## License

见 [LICENSE](LICENSE)。
