# Third-party components

The MIT license in this repository applies to this Skill's original source code and interface. It does not relicense model weights or external dependencies. No model weights, Python packages, FFmpeg binaries, personal voice recordings or reference transcripts are distributed here.

The generation implementation calls the public APIs of [MLX-Audio](https://github.com/Blaizzy/mlx-audio) (MIT) and [MLX](https://github.com/ml-explore/mlx) (MIT). Additional dependencies include [Transformers](https://github.com/huggingface/transformers) and [Hugging Face Hub](https://github.com/huggingface/huggingface_hub) (Apache-2.0), [NumPy](https://numpy.org) and [SoundFile](https://github.com/bastibe/python-soundfile) (their upstream license notices apply).

The default model is the MLX conversion [mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16), pinned to revision `1eccf1cb2519b5a4e8a95b5f0544f3303568164f`. The model and upstream [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) have their own license and model-card terms. The setup helper downloads these public files directly to a user's private local data directory; users should review the model card and applicable license before use.

FFmpeg is a separately installed executable. Its applicable license depends on the build; consult that installation's notices. This project neither bundles nor modifies it.
