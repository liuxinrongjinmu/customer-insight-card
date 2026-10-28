"""本地 .env 读取与厂商切换。

存在的理由：评测要在多个厂商之间来回切（智谱 / DeepSeek / 千问），
而 adapter 只认 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 三个标准变量。
与其每次在命令行前面拼一长串环境变量（还容易把 A 的 key 配 B 的 base_url，
这种错不会报错，只会安静地返回 401 或给出别的模型的结果），
不如把每个厂商的三件套固化在 .env 里，用一个 --provider 名字切换。

零依赖：只用标准库解析，不引入 python-dotenv。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

# adapter 认的标准变量名
STD_KEYS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

#: 厂商三件套的变量名形状（GLM_API_KEY / DEEPSEEK_BASE_URL …）。
#: 用正则而不是遍历固定厂商列表：加一家厂商时不用改两处，
#: 而「改了两处只改一处」正是这套配置最容易出的错。
_PROVIDER_VAR = re.compile(r"^[A-Z0-9]+_(API_KEY|BASE_URL|MODEL)$")


def parse_env_file(path: Path | str = ENV_FILE) -> dict[str, str]:
    """解析 KEY=VALUE，支持 # 注释、引号、行尾注释。

    不做变量插值——这个项目里没有需要插值的场景，
    多一条语法就多一处「配错了但没人发现」的可能。
    """
    p = Path(path)
    if not p.exists():
        return {}

    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # 去掉成对引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # 去掉未加引号时的行尾注释（值里的 # 只可能出现在密钥中段，不常见）
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
        out[key] = value
    return out


def load_dotenv(path: Path | str = ENV_FILE, override: bool = False) -> dict[str, str]:
    """把 .env 灌进 os.environ。默认不覆盖已有变量（命令行优先级更高）。"""
    values = parse_env_file(path)
    for k, v in values.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return values


def _merged(path: Path | str = ENV_FILE) -> dict[str, str]:
    """`.env` 的值 + 进程环境变量，**进程环境优先**。

    为什么要这一层：`.env` 是一份明文密钥文件，放在仓库目录里，
    一旦哪天 `git add .` 就跟着进历史了，而 git 的历史删不掉。
    所以推荐的做法是把 key 放在 shell profile / 钥匙串里，
    `.env` 只留占位（见 `.env.example`）。

    但如果只有「从 .env 读」这一条路，想这么干的人就得先改代码——
    于是他不会这么干，而是继续把真 key 写在 .env 里。**提供一条更安全的路，
    人才会走它**；只写一句「请不要这样」，等于什么都没做。
    """
    values = dict(parse_env_file(path))
    for key, val in os.environ.items():
        if val and (key in STD_KEYS or _PROVIDER_VAR.match(key)):
            values[key] = val
    return values


def available_providers(path: Path | str = ENV_FILE) -> list[str]:
    """发现 .env 或 shell 环境里配了 key 的厂商，返回小写前缀列表。"""
    values = _merged(path)
    names = []
    for key, val in values.items():
        if key.endswith("_API_KEY") and val:
            names.append(key[: -len("_API_KEY")].lower())
    return sorted(names)


def use_provider(name: str, path: Path | str = ENV_FILE) -> dict[str, str]:
    """把某厂商的三件套注入标准变量，返回注入结果。

    缺项直接报错而不是回退到别的厂商——静默回退会让人以为
    在测 DeepSeek，实际测的还是智谱，这种混淆比直接失败更贵。
    """
    values = _merged(path)
    prefix = name.upper()
    mapping = {
        "LLM_API_KEY": values.get(f"{prefix}_API_KEY"),
        "LLM_BASE_URL": values.get(f"{prefix}_BASE_URL"),
        "LLM_MODEL": values.get(f"{prefix}_MODEL"),
    }
    missing = [k for k, v in mapping.items() if not v]
    if missing:
        raise SystemExit(
            f"厂商 {name} 配置不全，缺 {missing}。"
            f"请在 .env 里补 {prefix}_API_KEY / {prefix}_BASE_URL / {prefix}_MODEL。"
            f"当前可用的厂商：{available_providers(path) or '（无）'}"
        )
    os.environ.update({k: v for k, v in mapping.items() if v})
    return mapping


def describe(path: Path | str = ENV_FILE) -> str:
    """给人看的配置概览，密钥只显示尾部四位。"""
    values = parse_env_file(path)
    if not values:
        return f"（未找到 {path}）"
    lines = []
    for name in available_providers(path):
        prefix = name.upper()
        key = values.get(f"{prefix}_API_KEY", "")
        tail = f"…{key[-4:]}" if len(key) > 4 else "(空)"
        lines.append(
            f"  {name:<10} {values.get(f'{prefix}_MODEL', '?'):<18} "
            f"{values.get(f'{prefix}_BASE_URL', '?'):<50} {tail}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    print(f".env: {ENV_FILE}")
    print(describe())
    if len(sys.argv) > 1:
        # 用法：python3 -m src.envload deepseek && python3 -c "import os; print(os.environ['LLM_MODEL'])"
        # 实际筛选由 use_provider 完成，这里只做校验及预览
        mapping = use_provider(sys.argv[1])
        print()
        print(f"已选中 {sys.argv[1]}：")
        for k, v in mapping.items():
            print(f"  {k} = {'…' + v[-4:] if k == 'LLM_API_KEY' else v}")
