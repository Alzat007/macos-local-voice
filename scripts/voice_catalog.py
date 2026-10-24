"""Optional private voice catalog; never changes the selected default or assets."""

import json
import os
from pathlib import Path
import re
import stat

from voice_paths import resolve_data_dir


VOICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
MAX_CATALOG_BYTES = 32768


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("音色目录包含重复字段。")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("音色目录包含无效数值。")


def valid_voice_id(value):
    return isinstance(value, str) and VOICE_ID.fullmatch(value) is not None


def valid_voice_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return not any(ord(char) < 32 for char in value)


def _voice_directory(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("音色目录必须使用私有目录内的相对路径。")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or relative.startswith("~"):
        raise ValueError("音色目录必须使用私有目录内的相对路径。")
    try:
        selected = (root / path).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("无法解析音色目录。") from None
    if selected != root and root not in selected.parents:
        raise ValueError("音色目录不能指向私有目录以外。")
    return selected


def load_catalog(data_dir):
    root = resolve_data_dir(data_dir)
    catalog = root / "voices.json"
    if catalog.is_symlink():
        raise ValueError("音色目录配置必须为普通 JSON 文件。")
    try:
        descriptor = os.open(catalog, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"version": 1, "default_voice": "default", "voices": [
            {"id": "default", "name": "默认音色", "data_dir": "."},
        ]}
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("音色目录配置必须为普通 JSON 文件。")
        raw = handle.read(MAX_CATALOG_BYTES + 1)
    if not raw or len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("音色目录配置为空或过大。")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields,
                           parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("音色目录不是有效 JSON。") from None
    if (not isinstance(value, dict) or set(value) != {"version", "default_voice", "voices"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["voices"], list) or not 1 <= len(value["voices"]) <= 32):
        raise ValueError("音色目录格式无效。")
    ids = set()
    for entry in value["voices"]:
        if (not isinstance(entry, dict) or set(entry) != {"id", "name", "data_dir"}
                or not valid_voice_id(entry["id"]) or not valid_voice_name(entry["name"])):
            raise ValueError("音色条目需要有效的编号、名称和相对目录。")
        if entry["id"] in ids:
            raise ValueError("音色编号不能重复。")
        ids.add(entry["id"])
        _voice_directory(root, entry["data_dir"])
    if not valid_voice_id(value["default_voice"]) or value["default_voice"] not in ids:
        raise ValueError("默认音色必须对应目录中的有效编号。")
    return value


def resolve_voice(data_dir, voice_id=None):
    root = resolve_data_dir(data_dir)
    catalog = load_catalog(root)
    selected = catalog["default_voice"] if voice_id is None else voice_id
    if not valid_voice_id(selected):
        raise ValueError("音色编号无效。")
    for entry in catalog["voices"]:
        if entry["id"] == selected:
            return {"id": entry["id"], "name": entry["name"],
                    "directory": _voice_directory(root, entry["data_dir"])}
    raise ValueError("没有找到所选音色，请重新选择。")
