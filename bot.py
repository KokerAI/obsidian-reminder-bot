"""
Главный модуль Telegram-бота.
Отвечает за инициализацию, хранение настроек, UI (клавиатуры, форматирование),
планировщик уведомлений и запуск.
Всю логику обработки команд (хэндлеры) делегирует в handlers.py.
"""
import os
import re
import json
import html
import asyncio
import logging
import tempfile
import config
import utils
import obsidian_parser as vault
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, TelegramObject, ReplyKeyboardMarkup, KeyboardButton, BotCommand)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiohttp import ClientError
from typing import Any, Callable, Dict, Awaitable, Optional
from logging.handlers import RotatingFileHandler

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
# CHANGED: Чтение уровня логирования из конфига (INFO по умолчанию, DEBUG для дебага)
LOG_LEVEL = getattr(logging, getattr(config, 'LOG_LEVEL', 'INFO').upper(), logging.INFO)
logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# Защита от дублирования хендлеров при повторных импортах (циклический импорт)
if not logger.handlers:
    # Запись в файл (макс 5 МБ, храним 3 последних файла)
    file_handler = RotatingFileHandler('bot.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Вывод в консоль
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# --- WATCHDOG (ОПЦИОНАЛЬНО) ---
# Библиотека для мгновенного обновления кэша при изменении файлов Obsidian.
# Если не установлена (pip install watchdog), бот просто использует таймер на 30 сек.
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False

if WATCHDOG_AVAILABLE:
    class VaultEventHandler(FileSystemEventHandler):
        def on_modified(self, event):
            if not event.is_directory and event.src_path.endswith('.md'):
                logging.info(f"[WATCHDOG] Файл изменен: {event.src_path}. Сброс кэша.")
                vault.clear_cache()
        def on_created(self, event):
            if not event.is_directory and event.src_path.endswith('.md'):
                logging.info(f"[WATCHDOG] Файл создан: {event.src_path}. Сброс кэша.")
                vault.clear_cache()
        def on_deleted(self, event):
            if not event.is_directory and event.src_path.endswith('.md'):
                logging.info(f"[WATCHDOG] Файл удален: {event.src_path}. Сброс кэша.")
                vault.clear_cache()

# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
# Создаем сессию с прокси и таймаутом из конфига
# Умная проверка прокси
active_proxy = utils.get_active_proxy()
session = AiohttpSession(proxy=active_proxy, timeout=config.REQUEST_TIMEOUT)
# session = AiohttpSession(proxy=config.PROXY_URL, timeout=config.REQUEST_TIMEOUT)
# NEW: Глобально отключаем превью ссылок во ВСЕХ сообщениях (send_message и edit_text),
# включая пуши, сводку и редактирование при пагинации. Тумблер — DISABLE_LINK_PREVIEW в config.py.
bot = Bot(token=config.BOT_TOKEN, session=session, default=DefaultBotProperties(link_preview_is_disabled=config.DISABLE_LINK_PREVIEW))
dp = Dispatcher()

# Абсолютный путь к файлу настроек (защита от потери при запуске из другой директории)
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

STATUS_BUTTONS = config.STATUS_BUTTONS
SOURCE_BUTTONS = config.SOURCE_BUTTONS
SOURCE_ICON = config.SOURCE_ICON
CARD_ICON = config.CARD_ICON

# Обратные словари для быстрого получения эмодзи по статусу/источнику
# Поддержка новых STATUS_BUTTONS (словарь словарей)
ALL_STATUSES = {}
for src_key, statuses in STATUS_BUTTONS.items():
    for btn, status in statuses.items():
        ALL_STATUSES[status] = btn.replace(status, "", 1).strip()

STATUS_EMOJI = ALL_STATUSES # Используем общий словарь
SOURCE_EMOJI = {src: btn.split(" ", 1)[0] if " " in btn else "" for btn, src in SOURCE_BUTTONS.items()}
SOURCE_ICON_BY_NAME = {cfg.get("name"): cfg.get("icon") or SOURCE_EMOJI.get(key) or SOURCE_ICON for key, cfg in config.SOURCES.items()}

def _status_icon(status: str) -> str:
    """Возвращает эмодзи статуса с пробелом или пустую строку."""
    emoji = STATUS_EMOJI.get(status, "")
    return f"{emoji} " if emoji else ""

def _source_icon_for(source_key: str) -> str:
    """Возвращает иконку источника по его ключу из конфига."""
    return config.SOURCES.get(source_key, {}).get("icon") or SOURCE_EMOJI.get(source_key) or SOURCE_ICON

def default_settings() -> Dict[str, Any]:
    """Возвращает словарь с настройками по умолчанию для нового пользователя."""
    # По умолчанию везде "Сначала новые" (new) и "Снизу вверх" (btt). Группировка по умолчанию везде ВЫКЛЮЧЕНА
    return {
        "intervals": [60], "at_start": True, "notified_tasks": set(), "is_active": True,
        "sort_order": "new", "sort_dir": "btt",
        "group_by_source": False, "dynamic_sort": True,
        "kanban_sort_order": "new", "kanban_sort_dir": "btt", "kanban_primary_group": "none",
        "kanban_dynamic_sort": True,
        "tasks_sort_order": "new", "tasks_sort_dir": "btt",
        "projects_sort_order": "new", "projects_sort_dir": "btt",
        "silent_notifications": False, "digest_enabled": False, "quiet_hours": False
    }

# --- РАБОТА С JSON (Сохранение настроек) ---
def load_user_settings() -> Dict[int, Dict[str, Any]]:
    """Загружает настройки из файла settings.json. Чистит пробелы в ключах, гарантирует типы данных."""
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        logging.info("Файл settings.json не найден. Начинаем с чистого листа.")
        return {}
    except json.JSONDecodeError:
        logging.error("Файл settings.json поврежден! Создаем бэкап и начинаем с чистого листа.")
        backup_path = SETTINGS_FILE + ".corrupted_backup"
        if not os.path.exists(backup_path): os.rename(SETTINGS_FILE, backup_path)
        return {}

    user_settings = {}
    for uid_str, settings in data.items():
        uid = int(uid_str)
        settings = {str(k).strip(): v for k, v in settings.items()}
        notified = settings.get("notified_tasks", [])
        settings["notified_tasks"] = set(notified) if isinstance(notified, list) else set()

        # ИСПРАВЛЕНО: Автоматическая миграция старых настроек asc/desc/far на новые new/old
        if settings.get("sort_order") in ["asc", "new"]: settings["sort_order"] = "new"
        elif settings.get("sort_order") in ["desc", "far", "old"]: settings["sort_order"] = "old"

        if settings.get("kanban_sort_order") in ["asc", "new"]: settings["kanban_sort_order"] = "new"
        elif settings.get("kanban_sort_order") in ["desc", "far", "old"]: settings["kanban_sort_order"] = "old"

        if settings.get("tasks_sort_order") in ["asc", "new"]: settings["tasks_sort_order"] = "new"
        elif settings.get("tasks_sort_order") in ["desc", "far", "old"]: settings["tasks_sort_order"] = "old"

        if settings.get("projects_sort_order") in ["asc", "new"]: settings["projects_sort_order"] = "new"
        elif settings.get("projects_sort_order") in ["desc", "far", "old"]: settings["projects_sort_order"] = "old"

        # Заполняем дефолтами, чтобы избежать KeyError
        defaults = default_settings()
        for d_key, d_val in defaults.items():
            settings.setdefault(d_key, d_val)

        try: settings["intervals"] = [int(x) for x in settings["intervals"]]
        except Exception: settings["intervals"] = [60]
        user_settings[uid] = settings

    logging.info("Настройки успешно загружены из settings.json")
    return user_settings

def save_user_settings():
    """Безопасно (атомарно) сохраняет настройки пользователя в файл settings.json."""
    data_to_save = {}
    for uid, settings in user_settings.items():
        temp = settings.copy()
        if isinstance(temp.get("notified_tasks"), set):
            # Сортируем по убыванию (от свежих к старым), чтобы срез оставлял последние 500 ключей.
            # set - неупорядоченное множество, без сортировки срез[-500:] удалял бы случайные свежие ключи (вызвал бы спам).
            notified_list = sorted(list(temp["notified_tasks"]), reverse=True)
            if len(notified_list) > 500:
                notified_list = notified_list[:500]
            temp["notified_tasks"] = notified_list
        data_to_save[str(uid)] = temp

    # АТОМАРНАЯ ЗАПИСЬ. Пишем во временный файл, затем безопасно заменяем основной.
    dir_name = os.path.dirname(SETTINGS_FILE)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, SETTINGS_FILE)
    except Exception as e:
        logging.error(f"Критическая ошибка сохранения settings.json: {e}")
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.remove(tmp_path)

user_settings = load_user_settings()

# --- MIDDLEWARE ---
class AccessMiddleware(BaseMiddleware):
    """Middleware для ограничения доступа к боту по CHAT_ID."""
    async def __call__(self, handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: Dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if not user or user.id != config.ALLOWED_ID:
            if isinstance(event, Message): await event.answer("⛔️ У вас нет доступа к этому боту.")
            elif isinstance(event, CallbackQuery): await event.answer("⛔️ Нет доступа", show_alert=True)
            return None
        return await handler(event, data)

# Привязываем middleware к глобальному Dispatcher (dp), чтобы она работала для всех роутеров.
dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ UI ---
async def safe_edit(callback: CallbackQuery, text: Optional[str] = None, reply_markup=None, parse_mode="HTML"):
    """Централизованная и безопасная функция редактирования сообщений (DRY).
    Предотвращает падение бота при ошибке 'message is not modified'."""
    try:
        # Проверка на None, чтобы пустая строка не считывалась как отсутствие текста
        if text is not None:
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await callback.message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.error(f"Ошибка редактирования сообщения: {e}")
            raise e

# --- КЛАВИАТУРЫ ---
def main_menu_kb() -> ReplyKeyboardMarkup:
    """Создает главную клавиатуру (Reply), скрывая кнопки отключенных источников."""
    # Тексты берутся из config.py
    kb = [[KeyboardButton(text=config.BTN_DEADLINES)]]
    # ---
    # Группировка кнопок источников в один ряд
    # row = [KeyboardButton(text=btn_text) for btn_text, src_key in SOURCE_BUTTONS.items() if config.SOURCES.get(src_key, {}).get("enabled")]
    # if row: kb.append(row)

    # Собираем все включенные кнопки источников
    src_buttons = [KeyboardButton(text=btn_text) for btn_text, src_key in SOURCE_BUTTONS.items() if config.SOURCES.get(src_key, {}).get("enabled")]

    # Разбиваем кнопки по 3 в ряд, чтобы 4-я кнопка переносилась на новую строку
    for i in range(0, len(src_buttons), 3):
        kb.append(src_buttons[i:i+3])
    # ---
    kb.append([KeyboardButton(text=config.BTN_SETTINGS)])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def settings_menu_kb() -> ReplyKeyboardMarkup:
    """Создает клавиатуру меню настроек (Reply)."""
    # CHANGED: Тексты берутся из config.py, а кнопки источников генерируются автоматически
    kb = [
        [KeyboardButton(text=config.BTN_NOTIFY), KeyboardButton(text=config.BTN_DEADLINES)],
        [KeyboardButton(text=btn_text) for btn_text, src_key in SOURCE_BUTTONS.items() if config.SOURCES.get(src_key, {}).get("enabled")],
        [KeyboardButton(text=config.BTN_EXTRA_SETTINGS)],
        [KeyboardButton(text=config.BTN_MAIN_MENU)]
    ]
    kb = [row for row in kb if row]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def upcoming_kb(period, page, total_count, limit=config.PAGINATION_LIMIT) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру для дедлайнов (период сверху, пагинация снизу)."""
    rows = [
        [InlineKeyboardButton(text="1 день", callback_data="up|1|0"), InlineKeyboardButton(text="2 дня", callback_data="up|2|0"), InlineKeyboardButton(text="4 дня", callback_data="up|4|0"), InlineKeyboardButton(text="Неделя", callback_data="up|7|0")],
        [InlineKeyboardButton(text="2 недели", callback_data="up|14|0"), InlineKeyboardButton(text="Месяц", callback_data="up|30|0"), InlineKeyboardButton(text="3 месяца", callback_data="up|90|0"), InlineKeyboardButton(text="Все", callback_data="up|all|0")],
        [InlineKeyboardButton(text="🔴 Просроченные", callback_data="up|overdue|0")]
    ]
    max_pages = max(0, (total_count - 1) // limit) if total_count > 0 else 0
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"up|{period}|{page - 1}"))
    if page < max_pages: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"up|{period}|{page + 1}"))
    if nav_row: rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Меню выбора проекта (если их несколько)
def projects_menu_kb(projects_list) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру для выбора подпапки проекта."""
    rows = []
    for proj in projects_list:
        rows.append([InlineKeyboardButton(text=f"📁 {proj}", callback_data=f"prjset|{proj}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ИЗМЕНЕНО: Добавлен параметр project_name и логика уникальных статусов
def tasks_nav_kb(source, status, page, total_count, project_name=None, limit=config.PAGINATION_LIMIT) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру для списка задач. Для Projects добавляет кнопки статусов сверху."""
    rows = []

    # Обработка проектов и их уникальных статусов
    statuses = {}
    if source == 'projects':
        statuses = STATUS_BUTTONS.get(project_name, STATUS_BUTTONS.get("_default", {}))
        rows.append([InlineKeyboardButton(text="🔙 К выбору проекта", callback_data="back_to_prj_menu")])

    cb_proj = project_name if project_name else "_"

    if statuses:
        rows.append([InlineKeyboardButton(text=f"Все{' ✅' if status == 'All' else ''}", callback_data=f"pg|{source}|{cb_proj}|All|0")])
        status_items = list(statuses.items())
        for i in range(0, len(status_items), 3):
            row = []
            for text, st_val in status_items[i:i+3]:
                is_active = (st_val == status)
                row.append(InlineKeyboardButton(text=f"{text}{' ✅' if is_active else ''}", callback_data=f"pg|{source}|{cb_proj}|{st_val}|0"))
            rows.append(row)

    # Пагинация
    max_pages = max(0, (total_count - 1) // limit) if total_count > 0 else 0
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"pg|{source}|{cb_proj}|{status}|{page - 1}"))
    if page < max_pages: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"pg|{source}|{cb_proj}|{status}|{page + 1}"))
    if nav_row: rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)

def settings_kb(user_id) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру с настройками интервалов уведомлений."""
    s = user_settings.get(user_id, default_settings())
    time_options = [(5, "5 мин"), (15, "15 мин"), (30, "30 мин"), (45, "45 мин"), (60, "1 час"), (90, "1.5 часа"), (120, "2 часа"), (180, "3 часа"), (1440, "За день"), (2880, "За 2 дня"), (4320, "За 3 дня"), (10080, "За неделю")]
    kb_rows = [[InlineKeyboardButton(text=f"{label} {'✅' if mins in s.get('intervals', []) else '❌'}", callback_data=f"tog_min_{mins}")] for mins, label in time_options]
    kb_rows.append([InlineKeyboardButton(text=f"В момент начала {'✅' if s.get('at_start') else '❌'}", callback_data="tog_start")])
    # Тумблер беззвучных уведомлений
    kb_rows.append([InlineKeyboardButton(text=f"🔕 Беззвучные уведомления {'✅' if s.get('silent_notifications') else '❌'}", callback_data="tog_silent")])
    kb_rows.append([InlineKeyboardButton(text="🔄 Сбросить интервалы", callback_data="clear_intervals"), InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_settings_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# Дополнительные настройки
def extra_settings_kb(user_id) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру дополнительных настроек (сводка, тихие часы)."""
    s = user_settings.get(user_id, default_settings())
    row1 = [InlineKeyboardButton(text=f"🌅 Утренняя сводка {'✅' if s.get('digest_enabled') else '❌'}", callback_data="tog_digest")]
    row2 = [InlineKeyboardButton(text=f"🌙 Тихие часы {'✅' if s.get('quiet_hours') else '❌'}", callback_data="tog_quiet")]
    row3 = [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_settings_menu")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])

def display_settings_kb(user_id) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру настроек отображения дедлайнов (4 независимые кнопки)."""
    s = user_settings.get(user_id, default_settings())
    row1 = [
        InlineKeyboardButton(text=f"🆕 Сначала новые{' ✅' if s.get('sort_order') == 'new' else ''}", callback_data="set_sort_new"),
        InlineKeyboardButton(text=f"📅 Сначала старые{' ✅' if s.get('sort_order') == 'old' else ''}", callback_data="set_sort_old")
    ]
    row2 = [
        InlineKeyboardButton(text=f"⬇️ Сверху вниз{' ✅' if s.get('sort_dir') == 'ttb' else ''}", callback_data="set_dir_ttb"),
        InlineKeyboardButton(text=f"⬆️ Снизу вверх{' ✅' if s.get('sort_dir') == 'btt' else ''}", callback_data="set_dir_btt")
    ]
    # Единое название и эмодзи для Группировки
    row3 = [InlineKeyboardButton(text=f"🔀 Группировка {'✅' if s.get('group_by_source') else '❌'}", callback_data="tog_group")]
    row4 = [InlineKeyboardButton(text=f"⚡️ Группы в порядке срочности {'✅' if s.get('dynamic_sort') else '❌'}", callback_data="tog_dyn_sort")]
    row5 = [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_notify_settings")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3, row4, row5])

def kanban_settings_kb(user_id) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру настроек Kanban (4 независимые кнопки + группировка)."""
    s = user_settings.get(user_id, default_settings())
    kanban_group = s.get("kanban_primary_group", "none")
    is_grouped = (kanban_group != "none")

    # 4 независимые кнопки для Kanban
    row1 = [
        InlineKeyboardButton(text=f"🆕 Сначала новые{' ✅' if s.get('kanban_sort_order') == 'new' else ''}", callback_data="set_kanban_sort_new"),
        InlineKeyboardButton(text=f"📅 Сначала старые{' ✅' if s.get('kanban_sort_order') == 'old' else ''}", callback_data="set_kanban_sort_old")
    ]
    row2 = [
        InlineKeyboardButton(text=f"⬇️ Сверху вниз{' ✅' if s.get('kanban_sort_dir') == 'ttb' else ''}", callback_data="set_kanban_dir_ttb"),
        InlineKeyboardButton(text=f"⬆️ Снизу вверх{' ✅' if s.get('kanban_sort_dir') == 'btt' else ''}", callback_data="set_kanban_dir_btt")
    ]
    row3 = [
        InlineKeyboardButton(text=f"📋 Сначала борды{' ✅' if is_grouped and kanban_group == 'boards_first' else ''}", callback_data="set_kanban_group_boards"),
        InlineKeyboardButton(text=f"📝 Сначала заметки{' ✅' if is_grouped and kanban_group == 'notes_first' else ''}", callback_data="set_kanban_group_notes")
    ]
    # Единое название и эмодзи для Группировки
    row4 = [InlineKeyboardButton(text=f"🔀 Группировка {'✅' if is_grouped else '❌'}", callback_data="tog_kanban_group")]

    # Динамическая сортировка групп в Канбан
    row5 = [InlineKeyboardButton(text=f"⚡️ Группы в порядке срочности {'✅' if s.get('kanban_dynamic_sort', True) else '❌'}", callback_data="tog_kanban_dyn_sort")]

    row6 = [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_settings_menu")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3, row4, row5, row6])


def tasks_settings_kb(user_id) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру настроек Tasks (4 независимые кнопки)."""
    s = user_settings.get(user_id, default_settings())
    row1 = [
        InlineKeyboardButton(text=f"🆕 Сначала новые{' ✅' if s.get('tasks_sort_order') == 'new' else ''}", callback_data="set_tasks_sort_new"),
        InlineKeyboardButton(text=f"📅 Сначала старые{' ✅' if s.get('tasks_sort_order') == 'old' else ''}", callback_data="set_tasks_sort_old")
    ]
    row2 = [
        InlineKeyboardButton(text=f"⬇️ Сверху вниз{' ✅' if s.get('tasks_sort_dir') == 'ttb' else ''}", callback_data="set_tasks_dir_ttb"),
        InlineKeyboardButton(text=f"⬆️ Снизу вверх{' ✅' if s.get('tasks_sort_dir') == 'btt' else ''}", callback_data="set_tasks_dir_btt")
    ]
    row3 = [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_settings_menu")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])

def projects_settings_kb(user_id) -> InlineKeyboardMarkup:
    """Создает inline-клавиатуру настроек Projects (4 независимые кнопки без настроек групп)."""
    s = user_settings.get(user_id, default_settings())
    row1 = [
        InlineKeyboardButton(text=f"🆕 Сначала новые{' ✅' if s.get('projects_sort_order') == 'new' else ''}", callback_data="set_projects_sort_new"),
        InlineKeyboardButton(text=f"📅 Сначала старые{' ✅' if s.get('projects_sort_order') == 'old' else ''}", callback_data="set_projects_sort_old")
    ]
    row2 = [
        InlineKeyboardButton(text=f"⬇️ Сверху вниз{' ✅' if s.get('projects_sort_dir') == 'ttb' else ''}", callback_data="set_projects_dir_ttb"),
        InlineKeyboardButton(text=f"⬆️ Снизу вверх{' ✅' if s.get('projects_sort_dir') == 'btt' else ''}", callback_data="set_projects_dir_btt")
    ]
    row3 = [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_settings_menu")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, row3])

# --- ФОРМАТИРОВАНИЕ ---
def format_note_card(note: Dict[str, Any], show_source: bool = False) -> str:
    """Форматирует карточку заметки с динамическим выводом свойств и экранированием HTML."""
    text = ""
    if show_source:
        text += f"{SOURCE_ICON_BY_NAME.get(note['source'], SOURCE_ICON)} <i>{html.escape(str(note['source']))}</i>\n"
    text += f"{CARD_ICON} <b>{html.escape(str(note['filename']))}</b>\n➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n"

    props_text = ""
    if config.SHOW_PROPERTIES:
        hidden_fields = set(config.HIDDEN_FIELDS)
        def format_val(val):
            """Вспомогательная функция для форматирования значений свойств."""
            if val is None: return ""
            if isinstance(val, bool): return "true" if val else "false"
            if isinstance(val, list): return ", ".join(str(v) for v in val)
            if isinstance(val, datetime): return val.strftime("%d.%m.%Y %H:%M")
            if isinstance(val, date): return val.strftime("%d.%m.%Y")  # Проверка datetime идет ПЕРВОЙ, т.к. он наследник date
            return str(val)

        for key, val in note.get("raw_metadata", {}).items():
            if key in hidden_fields: continue
            val_str = ""

            # --- АВТОМАТИЧЕСКАЯ ПОДСТАНОВКА ЭМОДЗИ ДЛЯ ВИЗУАЛЬНОГО ЕДИНООБРАЗИЯ ---
            if key == "status":
                # _status_icon уже возвращает "Эмодзи " (с пробелом)
                val_str = _status_icon(note['status']) + note['status']
            elif key in config.DUE_FIELDS:
                val_str = "📅 " + note['due_str']
            elif key in config.CREATED_FIELDS:
                val_str = "➕ " + note['created_str'] # ➕ - стандарт Obsidian Tasks для created
            elif key == "tags":
                val_str = ' '.join(f"#{t}" for t in note['tags']) if note['tags'] else ""
            else:
                val_str = format_val(val)

            if val_str: props_text += f"<b>{html.escape(str(key))}:</b> {html.escape(str(val_str))}\n"
            elif config.SHOW_EMPTY_PROPERTIES: props_text += f"<b>{html.escape(str(key))}:</b> -\n"

    text += props_text + "\n" if props_text else ""
    body_text = note.get('body', '')
    if config.SHOW_BODY and body_text:
        body_text = html.escape(body_text)
        body_text = re.sub(r'^##\s+(.*)$', r'<b>📌 \1</b>', body_text, flags=re.MULTILINE)
        def indent_to_nbsp(match):
            """Заменяет пробелы/табы в начале строки на невидимый символ Брайля для сохранения отступов."""
            indent = match.group(1).expandtabs(4)
            return "⠀" * len(indent)

        # ИСПРАВЛЕНО: Используем [ \t] вместо \s, чтобы не съедать переносы строк (\n)
        body_text = re.sub(r'^([ \t]*)-[ \t]*\[[ \t]*\][ \t]*', lambda m: indent_to_nbsp(m) + config.CHECKBOX_UNCHECKED + ' ', body_text, flags=re.MULTILINE)
        body_text = re.sub(r'^([ \t]*)-[ \t]*\[[xXvV]\][ \t]*', lambda m: indent_to_nbsp(m) + config.CHECKBOX_CHECKED + ' ', body_text, flags=re.MULTILINE)
        body_text = re.sub(r'^([ \t]*)[-*][ \t]+', lambda m: indent_to_nbsp(m) + '• ', body_text, flags=re.MULTILINE)
        body_text = re.sub(r'^([ \t]*)(\d+)\.[ \t]+', lambda m: indent_to_nbsp(m) + f'<b>{m.group(2)}.</b> ', body_text, flags=re.MULTILINE)
        body_text = re.sub(r'^([ \t]+)(?=\S)', indent_to_nbsp, body_text, flags=re.MULTILINE)
        if len(body_text) > 800: body_text = body_text[:800] + "..."
        text += f"{body_text}\n"

    # Трюк для растягения blockquote на всю ширину (60 невидимых символов)
    text = text.rstrip(' \n') + "\n" + "⠀" * 60
    return f"<blockquote>{text}</blockquote>\n"


# NEW: Карточка для пуша по подзадаче (карточке Kanban / чекбоксу). В компактном
# режиме (NOTIFY_CARD_ONLY в конфиге) — только файл и дата, без тела всей заметки/борды.
def _task_notify_card(note: Dict[str, Any], task_text: Optional[str], due_dt: datetime) -> str:
    if task_text and config.NOTIFY_CARD_ONLY:
        text = f"{CARD_ICON} <b>{html.escape(str(note['filename']))}</b>\n📅 {due_dt.strftime('%d.%m.%Y %H:%M')}\n"
        # Трюк для растягения blockquote на всю ширину (60 невидимых символов)
        text = text.rstrip(' \n') + "\n" + "⠀" * 60
        return f"<blockquote>{text}</blockquote>\n"
    return format_note_card(note)


# --- ПЛАНИРОВЩИК И УВЕДОМЛЕНИЯ ---
async def check_and_notify():
    """Фоновая задача планировщика (APScheduler). Запускается каждую минуту."""
    now = utils.get_now()
    logging.info(f"[ПЛАНИРОВЩИК] Проверка дедлайнов. Текущее время: {now}")

    if config.ALLOWED_ID not in user_settings:
        user_settings[config.ALLOWED_ID] = default_settings()
        save_user_settings()

    for user_id, settings in user_settings.items():
        if user_id != config.ALLOWED_ID: continue
        if not settings.get("is_active", True): continue
        if not settings.get("intervals") and not settings.get("at_start"): continue

        try:
            notes = vault.get_active_notes_for_notify()
            logging.info(f"[ПЛАНИРОВЩИК] Найдено активных заметок с дедлайнами: {len(notes)}")
            for note in notes:
                # Проверяем основной дедлайн заметки (если он не взят из текста - fallback)
                if note['due'] and not note.get('due_is_fallback'):
                    logging.debug(f"[ПЛАНИРОВЩИК] Проверка заметки '{note['filename']}'. Дедлайн: {note['due']}")
                    await process_due(user_id, settings, note['due'], note)
                # Проверяем дедлайны подзадач
                for t in note['tasks']:
                    if t['due'] and not t['done']:
                        logging.debug(f"[ПЛАНИРОВЩИК] Проверка подзадачи '{t['text']}'. Дедлайн: {t['due']}")
                        await process_due(user_id, settings, t['due'], note, t['text'])
        except Exception as e:
            logging.error(f"[КРИТИЧЕСКАЯ ОШИБКА] Ошибка нотификации: {e}", exc_info=True)

# Утренняя сводка
async def send_daily_digest():
    """Фоновая задача для утренней сводки (APScheduler). Запускается по расписанию."""
    logging.info("[ПЛАНИРОВЩИК] Проверка утренней сводки...")
    if config.ALLOWED_ID not in user_settings: return
    settings = user_settings[config.ALLOWED_ID]
    if not settings.get("is_active", True): return
    if not settings.get("digest_enabled", False): return

    now = utils.get_now()
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Выбор горизонта для сводки (24 часа или до конца дня)
    if getattr(config, 'DIGEST_DAY_VIEW', 'today') == "today":
        end_of_today = now.replace(hour=23, minute=59, second=0, microsecond=0)
    else:
        end_of_today = now + timedelta(days=1)

    # Собираем задачи на сегодня и просроченные
    upcoming = vault.get_upcoming_notes(max_date=end_of_today)
    overdue = vault.get_overdue_notes()

    # ИСПРАВЛЕНО: Оставляем только те задачи, дедлайн которых строго сегодня (отсекаем старые просрочки и будущие)
    notes_set = set()
    all_notes = []
    for n in upcoming + overdue:
        # Фильтр скрытия выполненных задач
        if getattr(config, 'HIDE_COMPLETED_IN_DEADLINES', False) and n['status'] in config.INACTIVE_STATUSES: continue

        due = utils.get_note_due(n)
        # CHANGED: Проверяем не только дедлайн заметки, но и подзадачи (карточки Kanban / чекбоксы):
        # иначе задача на сегодня не попадет в сводку, если у заметки/борды есть более ранняя дата.
        in_window = due != datetime.max and start_of_today <= due <= end_of_today
        if not in_window:
            in_window = any(
                t['due'] and not t['done'] and start_of_today <= t['due'] <= end_of_today
                for t in n.get('tasks', [])
            )
        if in_window:
            if n['filename'] not in notes_set:
                notes_set.add(n['filename'])
                all_notes.append(n)

    if not all_notes:
        msg = "🌅 <b>Доброе утро!</b>\nАктивных задач на сегодня нет. Хорошего дня!"
    else:
        msg = "🌅 <b>Доброе утро! Твои задачи на сегодня:</b>\n\n"
        limit_reached = False
        for n in all_notes:
            card = format_note_card(n)
            if len(msg) + len(card) > 4000:
                msg += "\n<blockquote>... (остальные заметки не поместились)</blockquote>\n"
                limit_reached = True
                break
            msg += card
        if not limit_reached: msg += "\n"

    try:
        await bot.send_message(config.ALLOWED_ID, msg, parse_mode="HTML")
        logging.info("[УВЕДОМЛЕНИЕ] Утренняя сводка успешно отправлена!")
    except Exception as e:
        logging.error(f"[ОШИБКА ОТПРАВКИ] Не удалось отправить сводку: {e}")

async def process_due(user_id, settings, due_dt, note, task_text=None):
    """Сравнивает время дедлайна с текущим временем и отправляет уведомления с учетом Grace Window (2 мин)."""
    if not isinstance(settings.get("notified_tasks"), set):
        settings["notified_tasks"] = set(settings.get("notified_tasks") or [])

    # Проверка тихих часов из пользовательских настроек
    if settings.get("quiet_hours", False):
        current_hour = utils.get_now().hour
        start_q, end_q = config.QUIET_HOURS
        if start_q < end_q:
            if start_q <= current_hour < end_q: return
        else:
            if current_hour >= start_q or current_hour < end_q: return

    now = utils.get_now()
    settings_changed = False
    due_key_str = due_dt.strftime("%Y%m%d%H%M")
    safe_task_text = (task_text or "")[:50]
    grace = timedelta(minutes=2)

    # Читаем настройку беззвучных пушей
    disable_notif = settings.get("silent_notifications", False)

    # 1. Проверка интервалов (за X минут до дедлайна)
    for interval in settings.get("intervals", []):
        trigger_time = due_dt - timedelta(minutes=interval)
        # Дебаг-лог для проверки интервалов
        logging.debug(f"[DEBUG] Интервал {interval} мин. Now: {now}, Trigger: {trigger_time}, Grace end: {trigger_time + grace}")
        if trigger_time <= now <= trigger_time + grace:
            # Дата (due_key_str) перенесена в начало ключа, чтобы sorted() работал хронологически.
            notify_key = f"{due_key_str}_{note['filename']}_{safe_task_text}_{interval}"
            if notify_key not in settings["notified_tasks"]:
                logging.info(f"[УВЕДОМЛЕНИЕ] Срабатывание интервала -{interval} мин. Ключ: {notify_key}")
                msg = f"⏰ <b>Напоминание (-{interval} мин)!</b>\n"
                # CHANGED: Экранируем текст задачи (HTML) — карточки с <, >, & раньше ломали отправку пуша
                if task_text: msg += f"Задача: {html.escape(task_text)}\n"
                # CHANGED: Компактный режим пуша по задаче (NOTIFY_CARD_ONLY) или полная карточка заметки
                msg += _task_notify_card(note, task_text, due_dt)
                try:
                    await bot.send_message(user_id, msg, parse_mode="HTML", disable_notification=disable_notif)
                    settings["notified_tasks"].add(notify_key)
                    settings_changed = True
                    logging.info(f"[УВЕДОМЛЕНИЕ] Сообщение успешно отправлено!")
                except Exception as e:
                    logging.error(f"[ОШИБКА ОТПРАВКИ] Не удалось отправить сообщение: {e}")

    # 2. Проверка "В момент начала"
    if settings.get("at_start") and due_dt <= now <= due_dt + grace:
        # Дебаг-лог для проверки "В момент начала"
        logging.debug(f"[DEBUG] 'В момент начала'. Now: {now}, Due: {due_dt}, Grace end: {due_dt + grace}")
        # Дата (due_key_str) перенесена в начало ключа.
        notify_key = f"{due_key_str}_{note['filename']}_{safe_task_text}_start"
        if notify_key not in settings["notified_tasks"]:
            logging.info(f"[УВЕДОМЛЕНИЕ] Срабатывание 'В момент начала'. Ключ: {notify_key}")
            msg = f"🚨 <b>ПРЯМО СЕЙЧАС!</b>\n"
            # CHANGED: Экранируем текст задачи (HTML) — карточки с <, >, & раньше ломали отправку пуша
            if task_text: msg += f"Задача: {html.escape(task_text)}\n"
            # CHANGED: Компактный режим пуша по задаче (NOTIFY_CARD_ONLY) или полная карточка заметки
            msg += _task_notify_card(note, task_text, due_dt)
            try:
                await bot.send_message(user_id, msg, parse_mode="HTML", disable_notification=disable_notif)
                settings["notified_tasks"].add(notify_key)
                settings_changed = True
                logging.info(f"[УВЕДОМЛЕНИЕ] Сообщение успешно отправлено!")
            except Exception as e:
                logging.error(f"[ОШИБКА ОТПРАВКИ] Не удалось отправить сообщение: {e}")

    if settings_changed: save_user_settings()

# --- ЗАПУСК БОТА ---
async def main():
    """Главная асинхронная функция. Регистрирует команды, настраивает планировщик, запускает поллинг."""
    # Импортируем роутер из handlers.py здесь, чтобы избежать циклических импортов
    import handlers
    dp.include_router(handlers.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота / включить уведомления"),
        BotCommand(command="stop", description="Остановить уведомления"),
        BotCommand(command="menu", description="Открыть главное меню"),
        BotCommand(command="help", description="Справка по боту")
    ])
    scheduler = AsyncIOScheduler(timezone=config.TZ)
    scheduler.add_job(check_and_notify, 'cron', minute='*')

    # Добавляем задачу для утренней сводки
    try:
        d_hour, d_minute = map(int, config.DIGEST_TIME.split(':'))
        scheduler.add_job(send_daily_digest, 'cron', hour=d_hour, minute=d_minute)
    except Exception as e:
        logging.error(f"Ошибка парсинга DIGEST_TIME: {e}")

    # NEW: Предупреждение о "ножницах" в конфиге: конвертация "@{дата}" в "📅 …" на карточках
    # Kanban имеет смысл только при ENABLE_TASKS_EMOJI = True — эмодзи единственный маркер,
    # который парсер распознает в любом месте строки (голая дата требует конца строки).
    if config.SOURCES.get("kanban", {}).get("enabled") and not config.ENABLE_TASKS_EMOJI:
        logging.warning("[КОНФИГ] Источник Kanban включен, но ENABLE_TASKS_EMOJI = False: "
                        "даты @{…} на карточках борд распознаваться НЕ будут.")

    scheduler.start()
    logging.info(f"Планировщик запущен. Бот работает для CHAT_ID: {config.ALLOWED_ID}")

    # ИНИЦИАЛИЗАЦИЯ WATCHDOG
    observer = None
    if WATCHDOG_AVAILABLE:
        observer = Observer()
        vault_path = config.VAULT_PATH
        if os.path.exists(vault_path):
            observer.schedule(VaultEventHandler(), vault_path, recursive=True)
            observer.start()
            logging.info(f"Watchdog запущен для папки: {vault_path}")
        else:
            logging.warning(f"Папка не найдена для Watchdog: {vault_path}")
    else:
        logging.info("Библиотека watchdog не установлена. Кэш обновляется по таймеру (30 сек).")

    try:
        while True:
            try:
                logging.info("Попытка запустить поллинг...")
                await dp.start_polling(bot)
                break
            except (ClientError, ConnectionError, OSError) as e:
                logging.error(f"Потеряно соединение: {e}. Переподключение через 15 секунд...")
                await asyncio.sleep(15)
            except Exception as e:
                logging.exception(f"Критическая ошибка бота: {e}")
                break
    finally:
        scheduler.shutdown()
        if observer:
            observer.stop()
            observer.join()
        await bot.session.close()