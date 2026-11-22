"""Плагин Hermes: каталог-обёртка, из которого виден пакет `router`.

Почему отдельный каталог, а не корень репозитория: загрузчик Hermes импортирует каталог
плагина как `hermes_plugins.<slug>` и кладёт его в `__path__`. Если бы плагин-каталогом был
корень репозитория, `__init__.py` в корне сломал бы сборку тестов (pytest принимает такой
каталог за пакет `__init__`). Здесь же лежит только шим и симлинк `router -> ../router`.

Относительный импорт обязателен: абсолютный (`from router.provider import ...`) подхватил бы
пакет из `sys.path`, то есть возможно другой файл, а не тот, что лежит в репозитории.

Установка — симлинк этого каталога в плагины профиля:

    ln -s /root/repos/hermes-web-router/hermes_plugin ~/.hermes/plugins/web-router

Тогда плагин и CLI (`python -m router.cli`) видят один и тот же код: симлинк `router`
ведёт в тот же каталог пакета.
"""

from __future__ import annotations

from .router import RouterProvider, __version__

__all__ = ["RouterProvider", "register", "__version__"]


def register(ctx) -> None:
    """Точка входа плагина: регистрирует ОДИН бэкенд `router` в реестре Hermes."""
    ctx.register_web_search_provider(RouterProvider())
