# 本地配音执行与排障

## 数据与运行时

生成、面板与服务使用同一个主 `--data-dir`，或统一设置 `LOCAL_VOICE_DATA_DIR`。默认是 `~/Library/Application Support/macos-local-voice`，不能位于本 Skill 源码目录内。新增参考时，`prepare_reference.py --data-dir` 指向独立候选子目录，见下方多音色目录。模型不会自动替换用户已有文件，参考配置不覆盖。

私有目录内容：

```text
<data-dir>/
  .venv/                    独立 Python 环境
  models/qwen3-base/        固定 revision 的公开模型
  ref_voice.wav             24 kHz / PCM16 / 单声道参考录音
  voice_profile.json        授权、参考文字和相对路径
  voices.json               可选的私有音色目录
  voice-variants/           可选的其他参考配置与模型
  outputs/                  配音 WAV 与生成元数据 JSON
```

音色配置包含 `consent_confirmed: true`、`reference_audio: ref_voice.wav`、`reference_text`、`model_path: models/qwen3-base`、模型仓库及方法说明。它属于私有数据，不能当成通用模板提交。记录 `consent_confirmed` 是用户确认的配置标记，不是生物身份验证。

## 首次安装

使用 Python 3.12，运行 `scripts/setup.py --check` 查看环境、依赖和模型情况；`--plan` 只预览，不创建环境或联网。`--install` 才创建独立环境并安装 requirements，`--download-model` 才下载固定模型。下载的是公开权重，不是上传录音。不要将安装失败解释为声音克隆成功。

模型默认固定为 `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16` 的 revision `1eccf1cb2519b5a4e8a95b5f0544f3303568164f`。`requirements.txt` 固定顶层依赖版本，不是完整的哈希锁文件。不要顺手升级系统或声称其他硬件已经验证。

FFmpeg 从 PATH 查找，也可使用 `prepare_reference.py --ffmpeg <可执行文件>`。没有 FFmpeg 时说明缺失，让用户选择已有安装或单独安装；不要依赖另一份私人视频 Skill 的路径。

## 参考片段

用户确认本人或有权使用的声音后，选一段没有其他人、音乐或长静音的清晰发言，建议 6–15 秒，提供与该片段逐字一致的 UTF-8 文本。`--start` 与 `--duration` 以秒为单位。源文件只读，FFmpeg 使用拒绝覆盖模式。

`--consent-own-voice` 只在确有本人/授权确认时传入。没可靠转写时请用户给出文字，不凭空补词。已有 `voice_profile.json` 或 `ref_voice.wav` 时使用新的私有数据目录，不能直接覆盖原来的声音。

AAC 等压缩录音解码后的浮点峰值可能超过满刻度。`prepare_reference.py` 先将选段裁切、重采样为浮点音频，分块测峰；仅超出 PCM16 可表示范围时，自动固定衰减至 −1 dBFS，再转换为 PCM16。正常录音不降电平，不做动态压缩、变速或变调。命令结果与私有配置中的 `reference_preparation` 记录实际峰值和衰减量。对已经硬削波的源录音调低音量不能恢复波形，应重新录制或使用未削波的源文件。

调整表现力时，优先调整参考录音的说话方式，并保留逐字匹配的参考文稿。当前 Qwen3-TTS Base 流程没有可直接启用的“开心”情绪旋钮；提高音量或改采样参数不等于改变语气。对比时，两版生成同一段未照抄参考录音的新文稿，统一试听响度并保持音高与速度，再由用户选择；用户要求都保留时，不用新的覆盖旧的。

## 多音色目录

主目录保留原有 `voice_profile.json`、`ref_voice.wav` 和模型。每个新增候选目录也包含自己的 `voice_profile.json`、`ref_voice.wav` 及配置引用的 `models/`。模型与参考路径必须留在对应候选目录内，不能用指向外部的符号链接；已有同版本模型可在本地复制或使用 APFS 克隆，无需重新下载。生成时共用主目录的 Python 环境。

由 Codex 在私有主目录创建或更新 `voices.json`。以下是结构示例，路径和名称应替换为已准备好的本地配置；A/B 是自定义 ID，不是随 Skill 附带的声音：

```json
{
  "version": 1,
  "default_voice": "A",
  "voices": [
    {"id": "A", "name": "原音色", "data_dir": "."},
    {"id": "B", "name": "新音色", "data_dir": "voice-variants/new-reference"}
  ]
}
```

`data_dir` 只能使用主目录以内的相对路径，`.` 表示原有根配置。每个 ID 唯一，`default_voice` 对应已登记的 ID；添加新候选不必更改默认项。没有 `voices.json` 时继续使用原来的根配置。这个目录文件与声音配置一样属于私有数据，不提交到仓库。

生成时加 `--voice B` 即可选用登记的 B；省略时用 `default_voice`。面板提供同一目录的音色选择，并在生成结果和历史中显示所用音色。选择只针对本次生成，不会改写 `default_voice` 或替换参考文件。

## 面板与直接生成

```bash
"<data-dir>/.venv/bin/python" "<skill>/scripts/voice_app.py" --data-dir "<data-dir>" --port 8766
```

端口占用时辨认已有程序，改端口或复用同配置服务；不终止未知 PID。服务只接受回环地址，Host / Origin / CSRF 校验不是公网身份认证，不能用隧道或反向代理直接公开。

直接生成：

```bash
"<data-dir>/.venv/bin/python" "<skill>/scripts/synthesize.py" \
  --data-dir "<data-dir>" --text-file "/path/to/script.txt" \
  --output "<data-dir>/outputs/new-voiceover.wav"
```

如已登记多种音色，在上述命令加入 `--voice B`。解释器和 `--data-dir` 仍使用主目录，无需切换到候选目录启动另一套面板。

输出必须在主数据目录的 `outputs/`，不覆盖已有 WAV 或 JSON。JSON 记录文案、音色、模型、时长与合成标记，同样是私有资料。不要将参考原声冒充新生成的 AI 结果。

完成后检查完整解码、非静音、时长与目标文稿；自动转写核对不代替本人听感。未做人耳试听时明确说明，不用 embedding 分数认证相似度。

## 可选 macOS 后台服务

只有用户要求后台常驻时才执行。先 `scripts/service.py --help` 查看选项，使用同一 `--data-dir` 和 `--port`。按需运行 `--install --dry-run` 预览，再 `--install` 写入用户级 LaunchAgent，`--start` 启动；默认 `--status` 只查询。

`--stop` 暂停本 Skill 的服务，`--uninstall` 移除其后台注册；均不得删除录音、模型、草稿或配音成品。不要修改原来的其他配音工具服务。配置用户登录启动不等于已经实测整机重启；只报告实际做过的验证。

生成中关掉浏览器不取消任务；服务进程本身崩溃或被停止会中断生成。页面恢复后应保留草稿供重试，历史从已完成 WAV/JSON 恢复，不伪造任务断点续跑。

## 开源发布检查

审阅 `git diff --cached` 与 `git ls-files`，执行 `scripts/audit_release.py`，只提交通用源码、通用 HTML、说明和无私人数据的测试。发布包不得包含模型、录音、转写、profile、outputs、日志、浏览器存储、截图或本机目录路径。若发现敏感资料已经进入提交历史，停止推送并报告，不能只删工作区文件后继续发布。
