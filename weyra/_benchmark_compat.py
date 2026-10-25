"""源码检出环境下基准测试与历史评测的兼容加载器（Benchmark Compat Loader）。

架构定位与设计哲学:
    本模块负责在源码仓库环境（Source Checkout）中安全加载 `benchmarks/` 目录下的评测基准套件，
    同时确保在作为 pip 安装的生产级分发包（Wheel / Sdist）时，不会将实验性基准脚本作为产品代码意外打入包内。
    借鉴 DeepSeek-Harness 的源码平面与制品平面隔离原则（Source vs Artifact Plane Separation）。
"""

import importlib
import importlib.util
import sys
from pathlib import Path
import runpy



def _require_source():
    """核验并加载源码仓库下的 benchmarks 基准测试包。

    【核心不变性与安全约束】
    遵循 DeepSeek-Harness 源码平面与制品平面隔离原则。核查当前是否存在 `benchmarks/__init__.py`，
    并动态将其安全挂载至 `sys.modules["benchmarks"]`。

    Returns:
        Path: 指向基准测试根目录的绝对路径。

    Raises:
        ImportError: 当未在源码检出环境中运行或存在其他命名冲突包时抛出。
    """
    directory = Path(__file__).resolve().parents[1] / "benchmarks"
    if not (directory / "__init__.py").is_file():
        raise ImportError("Historical benchmarks require the Weyra source repository or source archive. "
                          "Extract the source, install it with pip install -e '.[dev]', "
                          "then run python -m benchmarks.<module>. Daily evaluation: weyra eval --offline")
    existing = sys.modules.get("benchmarks")
    if existing is None:
        spec = importlib.util.spec_from_file_location(
            "benchmarks", directory / "__init__.py", submodule_search_locations=[str(directory)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules["benchmarks"] = package
        spec.loader.exec_module(package)
    elif Path(getattr(existing, "__file__", "") or ".").resolve() != directory / "__init__.py":
        raise ImportError("Another benchmarks package is loaded; run from the Weyra source checkout")
    return directory


def load(name):
    """从源码检出的 benchmarks 包中安全加载指定测试模块。

    Args:
        name: 模块相对名称（如 'suite_polyglot'）。

    Returns:
        types.ModuleType: 已导入的基准测试模块对象。

    Raises:
        ImportError: 当模块无法导入或解析路径逃逸出源码根目录时抛出。
    """
    directory = _require_source()
    module = importlib.import_module("benchmarks." + name)
    if Path(module.__file__).resolve().parent != directory:
        raise ImportError("Historical benchmarks resolved outside the Weyra source repository")
    return module


def run(name):
    """以主入口脚本模式 (`__main__`) 执行指定的基准测试模块。

    Args:
        name: 模块相对名称。

    Raises:
        SystemExit: 当环境缺少源码检出或依赖未满足时退出并输出指导信息。
    """
    try:
        _require_source()
    except ImportError as error:
        raise SystemExit(str(error)) from None
    runpy.run_module("benchmarks." + name, run_name="__main__")
