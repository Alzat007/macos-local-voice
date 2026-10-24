# 扎提skill

在 Apple Silicon Mac 上，用本人或明确获授权的录音作为音色参考，将中文文稿生成 AI 配音。包含可安装的 Codex Skill、中文本地面板、多音色选择、草稿保存、配音历史、WAV 下载，以及可选的 macOS 用户后台服务。

项目显示名称为“扎提skill”。为兼容已有安装与分享链接，仓库地址、调用标识 `$macos-local-voice` 和私有数据目录保持不变。

**不包含作者的声音。** 录音、参考文稿、音色配置、配音成品、模型权重和 Python 环境全部保存在仓库外的私有目录，不会随 Skill 分享。

## 环境与边界

- Apple Silicon Mac（M 系列）、macOS 14 或以上；原型实机验证环境为 Apple Silicon / macOS 14 / Python 3.12。Intel Mac、Windows、Linux 不在此推理后端的支持范围内。
- Python 3.12、独立安装的 FFmpeg。脚本不会修改系统 Python，也不需要 sudo。
- 首次安装依赖和下载公开模型需要网络，模型约 2.5 GB，运行环境另需约 0.5 GB，建议至少预留 6 GB 空间。
- 生成采用 Qwen3-TTS Base + MLX 的原声参考合成，不是重新训练的私有模型。语音相似度与自然度需要本人试听判断。
- 默认只监听 `127.0.0.1:8766`。没有公网服务、云端声音上传、数字人或付费 API。

## 安装成 Skill

```bash
git clone https://github.com/Alzat007/macos-local-voice.git ~/.codex/skills/macos-local-voice
```

然后在 Codex 中调用 `$macos-local-voice`，例如：“用我自己的授权录音配置本地音色，生成这段中文文案。” Skill 的入口是 [SKILL.md](SKILL.md)，也可独立使用下方命令行，不依赖其他私人 Skill。

## 手动使用

在本仓库目录运行。先检查和预览，确认环境适合后再安装；`--install` 和 `--download-model` 会显式进行网络下载。

```bash
python3.12 scripts/setup.py --check
python3.12 scripts/setup.py --plan
python3.12 scripts/setup.py --install --download-model
```

准备一段清晰、单人、没有背景音乐的授权录音，以及与选定片段逐字一致的 UTF-8 文本。以下路径和时间需要替换成你自己的素材，不要上传到这个仓库。

```bash
python3.12 scripts/prepare_reference.py \
  --source "/path/to/your-recording.mov" \
  --start 0 --duration 10 \
  --text-file "/path/to/verified-transcript.txt" \
  --consent-own-voice
```

启动中文面板：

```bash
"$HOME/Library/Application Support/macos-local-voice/.venv/bin/python" scripts/voice_app.py
```

访问 [本机配音面板](http://127.0.0.1:8766)。每次最多 2000 字，生成时显示真实阶段和等待时间；可以刷新页面继续追踪任务。草稿保存在当前浏览器，完成文件保存在本机，历史支持试听、下载和复用文稿。

可以同时保留多个参考音色，在面板选择，或在命令行生成时使用 `--voice <音色ID>`。选择本次音色不会改掉默认项；结果与历史会标明所用音色。音色目录 `voices.json` 保存在私有数据目录，设置方法见[多音色工作流](references/workflow.md#多音色目录)。没有音色目录时，原来的单音色配置继续可用。

希望声音更开朗时，可补录一段带笑意、自然表达的参考声音，用同文案、同响度试听后选择或同时保留。当前流程不提供固定的“开心模式”，实际自然度仍需试听判断。

如需其他位置，各脚本均使用同一个 `--data-dir`；面板支持 `--port`。私有数据目录不能放在 Skill 源码目录里面。命令行生成、后台服务和排障详见 [工作流说明](references/workflow.md)。

## 隐私与声音授权

默认私有目录为 `~/Library/Application Support/macos-local-voice`，包含 `.venv/`、`models/`、`ref_voice.wav`、`voice_profile.json` 和 `outputs/`；启用多音色后还包含 `voices.json` 与候选参考子目录。生成程序开启模型离线模式，不上传录音和文稿；安装阶段仍需从 PyPI / Hugging Face 获取公开依赖与模型。

只使用本人或明确获授权的声音。发布合成音频时按平台要求标注 AI 合成，不把它冒充真人现场录音。参考文稿要准确，不能用猜测的转写生成音色配置。

开源只分享工具代码；不要将私有目录、运行截图、日志或成品整包上传。仓库包含 `.gitignore` 和发布检查：

```bash
python3.12 scripts/audit_release.py
```

检查会拒绝误加入的录音、模型、声音配置、本机个人路径、GitHub 令牌和私钥。使用 Git 时先审阅并暂存准备发布的文件；如果工作文件与暂存区不一致，检查也会拒绝，避免漏掉暂存区中的旧敏感内容。它不是完整的密钥扫描器，也不保证识别所有敏感信息，提交前仍需审阅差异。

## 测试与已知限制

```bash
python3.12 -m unittest discover -s tests -v
```

CI 使用合成测试夹具和 mock，不下载模型，不使用真实人声，不安装或重启系统服务。核心推理方案曾在上述 Apple Silicon 环境实际生成；可移植 Skill 的单元测试不等于在所有 Mac 上的实机验证。

后台异常恢复只恢复网页服务，不能断点续跑尚未完成的配音；草稿与已完成文件仍保留，可重新生成。长文稿可能需要几分钟，预计朗读时长不是生成耗时保证。当前只对中文流程做过验证。

## 许可证与致谢

本仓库原始代码使用 [MIT](LICENSE)。模型和第三方依赖不随仓库分发，仍遵循其各自许可证；见 [第三方说明](THIRD_PARTY_NOTICES.md)。感谢 [MLX-Audio](https://github.com/Blaizzy/mlx-audio)、[MLX](https://github.com/ml-explore/mlx) 和 [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)。
