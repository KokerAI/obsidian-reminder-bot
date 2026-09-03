"""
Модуль общих утилит проекта.
Содержит вспомогательные функции для дат, прокси и централизованной сортировки,
чтобы избежать дублирования кода (DRY) и циклических зависимостей между модулями.
"""
import os
import socket
import logging
import config
from datetime import datetime
from typing import List, Dict, Any, Optional

def get_active_proxy() -> Optional[str]:
    """
    Автоопределение прокси:
    1. Если PROXY_URL задан в .env — использовать его (жесткий режим).
    2. Иначе перебрать популярные порты локальных прокси (HAPP, INCI, v2ray, Psiphon);
       на открытом порте собрать URL из PROXY_TYPE, PROXY_USER, PROXY_PASS.
    3. Если все порты закрыты — вернуть None (прямое соединение).
    """
    if config.PROXY_URL:
        return config.PROXY_URL

    host = "127.0.0.1"
    # Популярные порты локальных VPN-клиентов
    ports_to_check = [10808, 10809, 12334, 1080, 8080]

    proxy_type = os.getenv("PROXY_TYPE", "socks5").lower()
    proxy_user = os.getenv("PROXY_USER")
    proxy_pass = os.getenv("PROXY_PASS")

    for port in ports_to_check:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)  # Таймаут проверки порта — 1 секунда
                if s.connect_ex((host, port)) == 0:
                    logging.info(f"[Utils] Порт {port} открыт. Включаю прокси.")

                    # С логином и паролем
                    if proxy_user and proxy_pass:
                        return f"{proxy_type}://{proxy_user}:{proxy_pass}@{host}:{port}"
                    # Без авторизации
                    else:
                        return f"{proxy_type}://{host}:{port}"
        except Exception as e:
            logging.error(f"[Utils] Ошибка при проверке порта {port}: {e}")
            continue  # Ошибка на порте (например, занят) — просто идем к следующему

    logging.info("[Utils] Все проверенные порты закрыты. Прокси выключен.")
    return None

def get_now() -> datetime:
    """
    Единая функция получения текущего времени для всего проекта.
    Возвращает naive datetime (без tzinfo) с отброшенными секундами,
    чтобы корректно сравниваться с распарсенными датами из заметок.
    """
    if config.TZ is not None:
        now = datetime.now(config.TZ)
    else:
        now = datetime.now()
    # Явно убираем tzinfo: везде сравниваются naive datetime
    return now.replace(second=0, microsecond=0, tzinfo=None)

def get_note_due(note: Dict[str, Any]) -> datetime:
    """
    Извлекает дату дедлайна из заметки по принципу приоритета:
    1. Основной дедлайн заметки (YAML).
    2. Дедлайн первой невыполненной задачи (чекбокса/списка).
    3. datetime.max — если дат нет вообще, заметка улетает в самый конец списка.
    """
    if note.get('due'):
        return note['due']
    for t in note.get('tasks', []):
        # Игнорируем выполненные задачи при поиске даты дедлайна
        if t.get('due') and not t.get('done'):
            return t['due']
    return datetime.max

def get_priority(note: Dict[str, Any]) -> int:
    """Извлекает приоритет заметки на основе config.PRIORITY_SOURCES и config.PRIORITY_MAP.
    Возвращает 99, если приоритет не найден (ниже самого низкого)."""
    # 1. Ищем в YAML
    if config.PRIORITY_SOURCES.get("yaml", True):
        p = note.get('raw_metadata', {}).get('priority')
        if p:
            p_str = str(p).lower()
            for key, val in config.PRIORITY_MAP.items():
                if key in p_str: return val

    # 2. Ищем в названии файла
    if config.PRIORITY_SOURCES.get("title", True):
        title_str = note.get('filename', '').lower()
        for key, val in config.PRIORITY_MAP.items():
            if key in title_str: return val

    # 3. Ищем в теле заметки (если включено)
    if config.PRIORITY_SOURCES.get("body", False):
        body_str = note.get('body', '').lower()
        for key, val in config.PRIORITY_MAP.items():
            if key in body_str: return val

    return 99


def sort_notes(
    notes: List[Dict[str, Any]],
    group_by_source: bool = False,
    dynamic_sort: bool = False,
    source_order_map: Optional[Dict[str, int]] = None,
    virtual_group_config: Optional[Dict[str, Any]] = None,
    reverse_base_sort: bool = False
) -> List[Dict[str, Any]]:
    """
    Универсальная функция сортировки и группировки заметок.
    Гарантирует, что группы не будут разрываться при пагинации: возвращает
    плоский, но строго сгруппированный список, готовый к нарезке на страницы.

    Args:
        notes: Список заметок (словарей).
        group_by_source: Группировать ли заметки по источнику (для Дедлайнов).
        dynamic_sort: Сортировать ли группы по срочности (по дедлайну первой заметки группы).
        source_order_map: Словарь {имя_источника: порядок} для статической группировки.
        virtual_group_config: Конфиг виртуальных групп (Kanban): "group_names_order" и "key_func".
        reverse_base_sort: Направление базовой сортировки по дате (False — по возрастанию).

    Returns:
        Плоский отсортированный список заметок, готовый к пагинации.
    """
    # 1. Базовая сортировка по дате (направление — reverse_base_sort);
    # заметки без дат (datetime.max) всегда отправляются в конец списка
    # независимо от направления
    notes_with_dates = [n for n in notes if get_note_due(n) != datetime.max]
    notes_without_dates = [n for n in notes if get_note_due(n) == datetime.max]

    # Сортировка по приоритету — кортежем (дата, приоритет), вторичный ключ
    if config.ENABLE_PRIORITY_SORT:
        notes_with_dates.sort(key=lambda n: (get_note_due(n), get_priority(n)), reverse=reverse_base_sort)
    else:
        notes_with_dates.sort(key=get_note_due, reverse=reverse_base_sort)

    # Объединяем списки: сначала с датами, затем без дат
    notes = notes_with_dates + notes_without_dates

    if not group_by_source and not virtual_group_config:
        return notes

    # 2. Группировка
    grouped = {}
    if virtual_group_config:
        # Виртуальные группы (для Канбана: разделяем на доски и обычные заметки)
        for n in notes:
            g_name = virtual_group_config["key_func"](n)
            grouped.setdefault(g_name, []).append(n)

        # Учитываем dynamic_sort для виртуальных групп
        group_names = list(grouped.keys())
        if dynamic_sort:
            # Динамическая сортировка: самые срочные группы (по дедлайну первой заметки) вверх
            group_names.sort(key=lambda g: get_note_due(grouped[g][0]), reverse=reverse_base_sort)
        else:
            # Статическая сортировка: по порядку из конфига (борды/заметки)
            group_names = sorted(
                group_names,
                key=lambda g: virtual_group_config["group_names_order"].index(g)
                if g in virtual_group_config["group_names_order"] else 99
            )
    else:
        # Обычная группировка по источнику
        for n in notes:
            g_name = n.get('source', 'Unknown')
            grouped.setdefault(g_name, []).append(n)

        group_names = list(grouped.keys())
        if dynamic_sort:
            group_names.sort(key=lambda g: get_note_due(grouped[g][0]), reverse=reverse_base_sort)
        else:
            # Статическая сортировка: по порядку из config.py
            group_names.sort(key=lambda g: source_order_map.get(g, 99) if source_order_map else 99)

    # 3. Сборка в плоский список
    flat = []
    for g in group_names:
        flat.extend(grouped[g])
    return flat