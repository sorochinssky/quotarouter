"""E2E-стенд: плагин внутри настоящего Hermes, в изолированном HERMES_HOME.

Зачем отдельный стенд, если есть юнит-тесты: между «адаптер ответил» и «агент получил
результат» стоят загрузчик плагинов, реестр провайдеров (он отказывает любому провайдеру,
который не наследует `WebSearchProvider`), конверт ответа тула и чтение конфига. Каждую из
этих четырёх частей ломает отдельная ошибка, и все четыре офлайн-тесты не видят.

Изоляция: прогон живёт в своём `HERMES_HOME` (config.yaml + plugins + state). Живой профиль
не читается и не пишется — это проверяется шагом `isolation`.

Запуск:

    python tests/e2e/harness.py                  # живой прогон, тратит 1 поиск + 1 страницу
    python tests/e2e/harness.py --no-live        # только шаги без ключей (деградация, изоляция)
    python tests/e2e/harness.py --keep-home      # оставить каталог стенда для разбора

Код возврата: 0 — все шаги прошли, 1 — есть упавший шаг, 2 — стенд не собрался.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
LIVE_HOME = Path(os.environ.get("HERMES_LIVE_HOME", str(Path.home() / ".hermes")))
CHILD_TIMEOUT = float(os.environ.get("ROUTER_E2E_CHILD_TIMEOUT", "180"))

# Ключи, которые стенд передаёт дочернему процессу. Отсутствующие просто не попадут в env.
VENDOR_KEYS = (
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "DEEPSEEK_API_KEY",
    "TAVILY_API_KEY",
    "KEENABLE_API_KEY",
    "JINA_API_KEY",
)


# ---------------------------------------------------------------------------------------
# Дочерний сценарий. Живёт отдельным файлом, потому что должен исполняться в процессе с
# другим HERMES_HOME: подмена env внутри своего процесса не переинициализирует Hermes.
# ---------------------------------------------------------------------------------------
CHILD_SOURCE = r'''
"""Один прогон шагов внутри Hermes. Печатает JSON-отчёт в stdout."""

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERMES_ROOT = os.environ["E2E_HERMES_ROOT"]
sys.path.insert(0, HERMES_ROOT)

STEPS = []


def step(name):
    def wrap(fn):
        STEPS.append((name, fn))
        return fn
    return wrap


def _result(payload):
    """Разобрать ответ тула: строка JSON или уже dict."""
    if isinstance(payload, str):
        return json.loads(payload)
    return payload


@step("plugin_registry")
def plugin_registry(state):
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins(force=True)
    loaded = get_plugin_manager()._plugins.get("web-router")
    if loaded is None:
        raise AssertionError("плагин web-router не найден загрузчиком")
    if not loaded.enabled:
        raise AssertionError(f"плагин не включён: {loaded.error}")

    from agent.web_search_registry import get_provider

    provider = get_provider("router")
    if provider is None:
        raise AssertionError("реестр Hermes не отдал провайдера 'router'")
    state["provider"] = type(provider).__module__ + "." + type(provider).__name__
    return {"provider": state["provider"], "available": provider.is_available()}


@step("search_tool")
def search_tool(state):
    from tools.web_tools import web_search_tool

    data = _result(web_search_tool(state["query"], 3))
    if not data.get("success"):
        raise AssertionError(f"search вернул ошибку: {data.get('error')}")
    web = (data.get("data") or {}).get("web") or []
    if not web:
        raise AssertionError("search вернул успех без результатов")
    backend = (data.get("data") or {}).get("backend") or ""
    state["backend"] = backend
    state["search_urls"] = [row.get("url") for row in web[:3]]
    return {"backend": backend, "results": len(web), "first": web[0].get("url")}


@step("extract_tool")
def extract_tool(state):
    from tools.web_tools import web_extract_tool

    data = _result(asyncio.run(web_extract_tool([state["url"]])))
    results = data.get("results") or []
    if not results:
        raise AssertionError(f"extract не вернул страниц: {data}")
    content = results[0].get("content") or ""
    minimum = int(os.environ.get("ROUTER_MIN_PAGE_CHARS", "40"))
    if len(content) < minimum:
        raise AssertionError(f"страница короче порога {minimum}: {len(content)} символов, "
                            f"ошибка={results[0].get('error')}")
    state["extract_chars"] = len(content)
    return {"chars": len(content), "url": results[0].get("url")}


@step("search_degradation")
def search_degradation(state):
    """Ни одна ступень не может обслужить: ждём честную ошибку, а не трейсбек."""
    from tools.web_tools import web_search_tool

    data = _result(web_search_tool(state["query"], 3))
    if data.get("success"):
        raise AssertionError("ожидали отказ, получили успех — в стенде остались ключи вендоров")
    error = str(data.get("error") or "")
    if "router" not in error:
        raise AssertionError(f"ошибка не от роутера: {error[:200]}")
    state["degraded_error"] = error
    return {"error_head": error[:200]}


@step("state_isolated")
def state_isolated(state):
    """Учёт должен появиться под ЭТИМ HERMES_HOME."""
    state_dir = Path(os.environ["ROUTER_STATE_DIR"])
    ledger_path = state_dir / "ledger.json"
    routes_path = state_dir / "routes.jsonl"
    if not routes_path.exists():
        raise AssertionError(f"журнал маршрутов не создан: {routes_path}")
    lines = [json.loads(line) for line in routes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise AssertionError("журнал маршрутов пуст")
    served = [line for line in lines if line.get("ok") and line.get("served_by")]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    return {
        "state_dir": str(state_dir),
        "routes": len(lines),
        "served_by": sorted({line["served_by"] for line in served}),
        "vendors_tracked": sorted((ledger.get("vendors") or {}).keys()),
    }


def main():
    state = {
        "query": os.environ.get("E2E_QUERY", "Nous Research Hermes Agent"),
        "url": os.environ.get("E2E_URL", "https://example.com/"),
    }
    only = [part.strip() for part in os.environ.get("E2E_ONLY_STEP", "").split(",") if part.strip()]
    selected = [(name, fn) for name, fn in STEPS if not only or name in only]
    report = {"steps": [], "ok": True}
    for name, fn in selected:
        started = time.time()
        try:
            detail = fn(state) or {}
            report["steps"].append({"name": name, "ok": True, "seconds": round(time.time() - started, 2),
                                    "detail": detail})
        except Exception as exc:
            report["steps"].append({"name": name, "ok": False, "seconds": round(time.time() - started, 2),
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "traceback": traceback.format_exc()[-1500:]})
            report["ok"] = False
            break  # дальнейшие шаги зависят от предыдущих
    report["state"] = {k: v for k, v in state.items() if k in ("backend", "search_urls", "extract_chars")}
    print("===E2E-REPORT===")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
'''


@dataclass
class StepReport:
    name: str
    ok: bool
    seconds: float = 0.0
    detail: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "seconds": self.seconds, "detail": self.detail}


@dataclass
class RunReport:
    ok: bool = True
    steps: list[StepReport] = field(default_factory=list)
    home: str = ""
    spent: dict[str, int] = field(default_factory=lambda: {"search": 0, "extract": 0})
    notes: list[str] = field(default_factory=list)

    def add(self, step: StepReport) -> None:
        self.steps.append(step)
        self.ok = self.ok and step.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "home": self.home,
            "spent": self.spent,
            "notes": self.notes,
            "steps": [s.as_dict() for s in self.steps],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }


# ---------------------------------------------------------------------------------------
# Стенд
# ---------------------------------------------------------------------------------------
def vendor_keys(present_only: bool = True) -> dict[str, str]:
    keys = {name: os.environ[name] for name in VENDOR_KEYS if os.environ.get(name)}
    if present_only:
        return keys
    return {name: os.environ.get(name, "") for name in VENDOR_KEYS}


def build_home(base: Path, *, keys: dict[str, str], allow_keyless: bool = False,
               search_order: Optional[str] = None, extract_order: Optional[str] = None) -> Path:
    """Собрать изолированный HERMES_HOME: конфиг, .env, плагин.

    Плагин подключён симлинком на `hermes_plugin/`: внутри него симлинк `router -> ../router`,
    то есть стенд проверяет ровно тот пакет, который лежит в репозитории.
    """
    home = base / "hermes-home"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)

    plugin_link = home / "plugins" / "web-router"
    if plugin_link.is_symlink() or plugin_link.exists():
        plugin_link.unlink() if plugin_link.is_symlink() else shutil.rmtree(plugin_link)
    plugin_link.symlink_to(REPO_ROOT / "hermes_plugin", target_is_directory=True)

    config = [
        "plugins:",
        "  enabled:",
        "    - web-router",
        "web:",
        "  search_backend: router",
        "  extract_backend: router",
        f"  keyless_rescue: {'true' if allow_keyless else 'false'}",
    ]
    (home / "config.yaml").write_text("\n".join(config) + "\n", encoding="utf-8")

    env_lines = [f"{name}={value}" for name, value in keys.items() if value]
    if not allow_keyless:
        env_lines.append("ROUTER_KEYLESS_LAST_RESORT=false")
    if search_order:
        env_lines.append(f"ROUTER_SEARCH_ORDER={search_order}")
    if extract_order:
        env_lines.append(f"ROUTER_EXTRACT_ORDER={extract_order}")
    (home / ".env").write_text("\n".join(env_lines) + ("\n" if env_lines else ""), encoding="utf-8")
    return home


def child_python() -> str:
    """Интерпретатор Hermes: в нём есть httpx и сам пакет агента."""
    candidate = HERMES_ROOT / "venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def run_child(home: Path, *, label: str, extra_env: Optional[dict[str, str]] = None,
              keys: Optional[dict[str, str]] = None) -> tuple[StepReport | None, dict[str, Any]]:
    """Прогнать дочерний сценарий в изолированном home и разобрать его отчёт."""
    child = home / "e2e_child.py"
    child.write_text(CHILD_SOURCE, encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HERMES_HOME": str(home),
        "ROUTER_STATE_DIR": str(home / "state" / "web-router"),
        "E2E_HERMES_ROOT": str(HERMES_ROOT),
        "PYTHONPATH": str(HERMES_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        **(keys or {}),
        **(extra_env or {}),
    }
    started = time.time()
    proc = subprocess.run([child_python(), str(child)], capture_output=True, text=True,
                          env=env, timeout=CHILD_TIMEOUT, cwd=str(REPO_ROOT))
    elapsed = round(time.time() - started, 2)
    stdout = proc.stdout or ""
    if "===E2E-REPORT===" not in stdout:
        detail = (stdout[-800:] + "\n" + (proc.stderr or "")[-800:]).strip()
        return StepReport(f"child[{label}]", False, elapsed, f"нет отчёта, код {proc.returncode}: {detail}"), {}
    payload = stdout.split("===E2E-REPORT===", 1)[1].strip().splitlines()[0]
    report = json.loads(payload)
    return StepReport(f"child[{label}]", bool(report.get("ok")), elapsed, None), report


def live_state_fingerprint() -> str:
    """Отпечаток состояния живого профиля: доказательство, что стенд его не тронул."""
    digest = hashlib.sha256()
    config = LIVE_HOME / "config.yaml"
    if config.exists():
        digest.update(config.read_bytes())
    state_dir = LIVE_HOME / "state" / "web-router"
    if state_dir.exists():
        for path in sorted(state_dir.iterdir()):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(str(path.stat().st_size).encode())
                digest.update(str(int(path.stat().st_mtime)).encode())
    return digest.hexdigest()


def run(live: bool = True, keep_home: bool = False, base_dir: Optional[Path] = None) -> RunReport:
    """Полный прогон стенда. Возвращает отчёт; исключений не бросает."""
    report = RunReport()
    if not (HERMES_ROOT / "tools" / "web_tools.py").exists():
        report.add(StepReport("hermes_available", False, 0.0, f"нет установки Hermes в {HERMES_ROOT}"))
        return report

    base = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="web-router-e2e-"))
    report.home = str(base)
    before = live_state_fingerprint()

    try:
        # --- 1. живой путь: плагин, поиск, вытяжка, учёт -------------------------------
        if live:
            keys = vendor_keys()
            if not keys:
                report.add(StepReport("vendor_keys", False, 0.0,
                                      "ни одного ключа вендора в окружении — живой прогон невозможен"))
            else:
                home = build_home(base, keys=keys)
                _, child = run_child(home, label="live", keys=keys, extra_env={
                    # деградация проверяется отдельным стендом без ключей; здесь её шаг
                    # неизбежно упал бы, потому что ключи есть
                    "E2E_ONLY_STEP": "plugin_registry,search_tool,extract_tool,state_isolated",
                })
                for item in child.get("steps", []):
                    report.add(StepReport(item["name"], item["ok"], item.get("seconds", 0.0), item.get("detail")))
                if child.get("ok"):
                    report.spent["search"] += 1
                    report.spent["extract"] += 1

        # --- 2. деградация: без ключей стенд обязан отказать честно -------------------
        degraded_base = base / "degraded"
        degraded_base.mkdir(parents=True, exist_ok=True)
        degraded_home = build_home(degraded_base, keys={}, allow_keyless=False)
        _, child = run_child(degraded_home, label="degraded", keys={},
                             extra_env={"E2E_ONLY_STEP": "plugin_registry,search_degradation"})
        steps = child.get("steps", [])
        names = [s["name"] for s in steps]
        degraded_ok = bool(child.get("ok")) and "search_degradation" in names
        report.add(StepReport(
            "degradation_without_keys", degraded_ok,
            0.0, next((s.get("detail") for s in steps if s["name"] == "search_degradation"), "шаг не выполнен")))
    finally:
        after = live_state_fingerprint()
        report.add(StepReport(
            "isolation", before == after, 0.0,
            "живой профиль не изменён" if before == after else "живой профиль ИЗМЕНИЛСЯ во время прогона"))
        if not keep_home:
            shutil.rmtree(base, ignore_errors=True)
        else:
            report.notes.append(f"каталог стенда сохранён: {base}")

    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E2E-прогон плагина web-router внутри Hermes.")
    parser.add_argument("--no-live", action="store_true", help="без вызовов вендоров (нулевая стоимость)")
    parser.add_argument("--keep-home", action="store_true", help="не удалять каталог стенда")
    parser.add_argument("--report", default=str(REPO_ROOT / "e2e-report.json"), help="куда писать отчёт")
    args = parser.parse_args(argv)

    report = run(live=not args.no_live, keep_home=args.keep_home)
    path = Path(args.report)
    path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for step in report.steps:
        mark = "OK  " if step.ok else "FAIL"
        detail = step.detail if isinstance(step.detail, str) else json.dumps(step.detail, ensure_ascii=False)
        print(f"{mark} {step.name} ({step.seconds}s) {detail or ''}")
    print(f"\nотчёт: {path}")
    print("ИТОГ:", "все шаги прошли" if report.ok else "есть упавшие шаги")
    if not report.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
