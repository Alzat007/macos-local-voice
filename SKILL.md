---
name: macos-local-voice
description: "扎提skill：Set up and use a local Chinese AI voice studio on Apple Silicon macOS with an authorized voice recording, Qwen3-TTS and MLX. Use for personal voice cloning, text-to-speech, a local voice panel, or its macOS background service; not for cloud avatars or general video editing."
---

# 扎提skill

将用户本人或明确获授权的录音配置为本地音色参考，通过中文面板或命令行生成 AI 配音。复用已有配置时无需重新提取录音；不是训练新的专属模型，不保证人耳相似度。

显示名称为“扎提skill”，调用标识保留 `$macos-local-voice`，以兼容现有安装、脚本和数据目录。

## 运行边界

- 推理后端仅面向 Apple Silicon macOS。先检查硬件、系统、Python 和 FFmpeg；Intel Mac、Windows 或 Linux 上不要安装 MLX 后假称可用。
- Skill 源码与私有数据分开。所有录音、参考文字、配置、模型、虚拟环境和成品进入 `--data-dir` 指定的私有目录；默认 `~/Library/Application Support/macos-local-voice`。不要放入 Skill 或 Git 仓库。
- 首次依赖安装及约 2.5 GB 模型下载需要网络；生成默认离线。先说明计划和磁盘需求，不上传录音，也不擅自改用云端服务。
- 仅处理已授权声音。确认授权和参考文字后才建立音色配置；用户让你开源工具不代表同意公开其声音、原文或成品。

## 常见任务

先根据当前 Skill 目录解析脚本的绝对路径，以下命令中的 `<skill>` 指该目录；所有路径带空格时正常引用。

### 首次设置

1. 用 Python 3.12 或以上执行 `python3 <skill>/scripts/setup.py --check`；需要预览安装动作时使用 `--plan`。缺少前提时先报告，不改系统 Python、不使用 sudo。
2. 用户确认安装/下载后，执行 `setup.py --install --download-model`，可用 `--data-dir` 选位置。已有依赖或模型先检查再复用，不在任务间重复下载。
3. 从用户录音选一段单人、无音乐、约 6–15 秒的清晰发言，核对逐字参考文稿。没有可靠转写时请用户提供，不能猜词。
4. 用 `prepare_reference.py --source <media> --start <seconds> --duration <seconds> --text-file <verified.txt> --consent-own-voice` 建立配置；需要时加 `--ffmpeg <executable>`。只读源文件，不覆盖既有声音配置。

实际 CLI 选项、数据结构和异常处理见 [references/workflow.md](references/workflow.md)，执行设置或排障时再读。

### 生成配音或打开面板

- 面板：`<data-dir>/.venv/bin/python <skill>/scripts/voice_app.py --data-dir <data-dir> --port 8766`，访问 `http://127.0.0.1:8766`。只绑定回环地址；端口占用时先辨别服务，不杀掉其他程序。
- 直接生成：同一虚拟环境执行 `scripts/synthesize.py --data-dir <data-dir> --text-file <稿件.txt> --output <data-dir>/outputs/<新名称>.wav`。也可使用 `--text`；每次最多 2000 字。
- 已保存多种音色时，在面板选择音色，或在生成命令中加 `--voice <音色ID>`；省略时使用音色目录中的默认项。单次选择不修改默认，也不替换其他参考录音。
- 说明实际阶段与耗时，不虚构百分比或完成时间。刷新可继续追踪正在运行的任务；服务自身重启会中断未完成生成，已完成文件从历史恢复，文稿可重试。
- 完成后核对非静音、时长、可解码及文稿内容，再提供试听和可点击的文件路径。需要本人试听判断音色；不要把 embedding 分数当作音色认证。

### 调整语气或保留多个音色

- 希望声音更开朗时，优先用带笑意、自然表达的补录建立新参考，并核对准确文稿；不要把音量、采样参数或不存在的情绪选项称为“开心模式”。用同一段新文稿、相同响度做试听，让用户判断，可同时保留多版。
- 多音色配置保存在私有主目录的 `voices.json`，每个参考使用独立子目录，保留现有配置；没有该文件时兼容原来的单音色用法。设置目录、处理转换峰值或核对选择方式时，读取 [工作流说明](references/workflow.md#多音色目录)。

### 本机后台服务

仅在用户要求常驻或登录启动时读取 `references/workflow.md` 的服务章节，先用 `scripts/service.py --help` 和 `--dry-run` 检查路径与端口，再执行所需操作。只管理本 Skill 的用户级服务，不需要管理员权限，不公开到互联网，也不删除声音资料。

### 分享或开源

代码按仓库许可证分享，模型和依赖按各自许可证使用。发布前运行 `scripts/audit_release.py`；仅提交源码、通用模板、说明和测试，排除录音、参考文稿、配置、成品、模型、环境、日志和密钥。提交或推送必须属于用户当前请求的仓库与范围；不能把本机配音目录整包上传。
