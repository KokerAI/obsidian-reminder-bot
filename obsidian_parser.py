"""
Модуль парсинга Markdown-заметок Obsidian.
Отвечает за чтение файлов, извлечение метаданных (YAML frontmatter),
парсинг инлайн-задач и кэширование данных для планировщика.
Всю логику сортировки и пагинации делегирует в bot.py / utils.py / handlers.py.
"""
import os
import re
import yaml
import logging
import config
import utils
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional

VAULT_PATH = config.VAULT_PATH
BASE_FOLDER = config.BASE_FOLDER
INACTIVE_STATUSES = config.INACTIVE_STATUSES

# ==============================================================================
# 1. ПОСТРОЕНИЕ РЕГУЛЯРНЫХ ВЫРАЖЕНИЙ
# ==============================================================================

_date_sep_pattern = "[" + re.escape("".join(config.DATE_DELIMITERS)) + "]"
_time_sep_pattern = "[" + re.escape("".join(config.TIME_DELIMITERS)) + "]"

# Умный паттерн: годы из 2/4 цифр, дни/месяцы из 2 цифр, опциональное время
_date_part = (
    rf'\d{{2,4}}{_date_sep_pattern}\d{{2}}{_date_sep_pattern}\d{{2,4}}'
    rf'(?:[ T]\d{{2}}(?:{_time_sep_pattern}\d{{2}}(?:{_time_sep_pattern}\d{{2}})?)?)?'
)

_task_date_regexes = []
# Эмодзи плагина Tasks (до и после даты)
if config.ENABLE_TASKS_EMOJI:
    _icons = "(?:" + "|".join(re.escape(ic) for ic in config.TASKS_DUE_EMOJI) + ")"
    _task_date_regexes.append(re.compile(rf'{_icons}\s*(?P<date>{_date_part})'))
    _task_date_regexes.append(re.compile(rf'(?P<date>{_date_part})\s*{_icons}'))
# Поля Dataview
if config.ENABLE_DATAVIEW:
    _dv_fields = "|".join(re.escape(f) for f in config.DUE_FIELDS)
    _task_date_regexes.append(re.compile(rf'\[\s*(?:{_dv_fields})\s*::\s*(?P<date>{_date_part})\s*\]'))
    _task_date_regexes.append(re.compile(rf'(?:{_dv_fields})\s*::\s*(?P<date>{_date_part})'))
# Голая дата (строго в конце строки)
if config.ENABLE_BARE_DATES:
    _task_date_regexes.append(re.compile(rf'(?P<date>{_date_part})\s*$'))
# Даты Kanban-плагина: нативный маркер "@{дата}" / "@{дата время}" (пару "@{дата} @@{время}"
# сливает _process_kanban_body). Маркер распознается в любом месте строки (как эмодзи Tasks),
# поэтому хвостовые приоритеты (⏫), теги и прочий текст не отрезают дедлайн.
if config.ENABLE_KANBAN_DATES:
    _task_date_regexes.append(re.compile(rf'@\{{\s*(?P<date>{_date_part})\s*\}}'))

# Паттерны классификации строк
_CHECKBOX_LINE = re.compile(r'^\s*[-*+]\s*\[(?P<status>[ xX])\]\s*(?P<rest>.*)$')
# Если чекбоксы включены, исключаем "[...]" из списков; если выключены —
# строки чекбоксов проваливаются в списки/тело
_list_cb_excl = r'(?!\[)' if config.SEARCH_CHECKBOX else ''
_LIST_LINE = re.compile(rf'^\s*(?:[-*+]\s+{_list_cb_excl}|\d{{1,3}}[.)]\s+)\s*(?P<rest>.*)$')
_BODY_LINE = re.compile(r'^\s*(?P<rest>\S.*)$')

def _classify_line(line: str):
    """Определяет тип строки (чекбокс, список, тело) с учетом тумблеров SEARCH_*."""
    m = _CHECKBOX_LINE.match(line)
    if m and config.SEARCH_CHECKBOX:
        return "checkbox", m.group("status"), m.group("rest")
    if config.SEARCH_LIST:
        m = _LIST_LINE.match(line)
        if m: return "list", None, m.group("rest")
    if config.SEARCH_BODY:
        m = _BODY_LINE.match(line)
        if m: return "body", None, m.group("rest")
    return None

# Кэш планировщика
_notify_cache = {"last_fetch": None, "data": []}

def clear_cache():
    """Сбрасывает кэш планировщика. Вызывается Watchdog при изменении файлов."""
    global _notify_cache
    _notify_cache = {"last_fetch": None, "data": []}
    logging.info("[КЭШ] Кэш планировщика сброшен.")

# Генерация всех комбинаций (формат × разделитель) для strptime
_date_fmts = [d.join(f"%{t}" for t in df.split('%') if t) for df in config.DATE_FORMATS for d in config.DATE_DELIMITERS]
_time_fmts = [t.join(f"%{t2}" for t2 in tf.split('%') if t2) for tf in config.TIME_FORMATS for t in config.TIME_DELIMITERS]
_DATETIME_FORMATS = list(_date_fmts)
for df in _date_fmts:
    for tf in _time_fmts:
        _DATETIME_FORMATS.append(f"{df} {tf}")
        _DATETIME_FORMATS.append(f"{df}T{tf}")

# ==============================================================================
# 2. ФУНКЦИИ ПАРСИНГА
# ==============================================================================

def get_search_path(subfolder: str) -> Path:
    """Формирует путь поиска: пусто — BASE_FOLDER, абсолютный путь — как есть, иначе BASE_FOLDER/subfolder."""
    if not subfolder: return Path(VAULT_PATH) / BASE_FOLDER
    if os.path.isabs(subfolder): return Path(subfolder)
    return Path(VAULT_PATH) / BASE_FOLDER / subfolder

def parse_datetime(date_str) -> Optional[datetime]:
    """Парсит значение в naive datetime с обнуленными секундами.
    Объекты datetime приводятся к TZ и очищаются от tzinfo, строки перебирают
    все комбинации форматов. Возвращает None, если распознать не удалось."""
    if not date_str: return None
    if isinstance(date_str, datetime):
        if date_str.tzinfo is not None:
            date_str = date_str.astimezone(config.TZ) if config.TZ is not None else date_str.astimezone()
        return date_str.replace(second=0, microsecond=0, tzinfo=None)
    if isinstance(date_str, date):
        return datetime(date_str.year, date_str.month, date_str.day)
    for fmt in _DATETIME_FORMATS:
        try: return datetime.strptime(str(date_str), fmt).replace(second=0, microsecond=0)
        except ValueError: continue
    return None

def _process_kanban_body(body: str) -> str:
    """Очищает тело Канбан-доски от служебного кода и нормализует чекбоксы/даты."""
    # Обрезаем служебный хвост доски: всё после разделителя "***" или блока "%% kanban"
    cut_idx_2 = body.find("%% kanban")
    match_sep = re.search(r'^\s*\*\*\*\s*$', body, re.MULTILINE)
    cut_idx_1 = match_sep.start() if match_sep else -1
    valid_indices = [i for i in [cut_idx_1, cut_idx_2] if i > 0]
    if valid_indices: body = body[:min(valid_indices)].strip()

    new_lines, skip_next_empty = [], False

    # "Сегодня" для привязки "@@{время}" без даты к текущему дню (режим ENABLE_KANBAN_TIME_ONLY)
    today_str = utils.get_now().strftime("%Y-%m-%d") if config.ENABLE_KANBAN_TIME_ONLY else ""

    for l in body.splitlines():
        if skip_next_empty and not l.strip():
            skip_next_empty = False
            continue
        skip_next_empty = False

        # Заголовки колонок ("## "): к имени done-колонки добавляем маркер " ✓"
        if l.startswith("## "):
            col_name = l[3:].strip()

            done_markers = ["done", "archive", "complete", "completed", "завершено", "выполнено", "архив"]
            if any(m in col_name.lower() for m in done_markers): l = l.rstrip() + " ✓"
            new_lines.append(l)
            continue

        # Метка **Complete**: помечаем последний пункт выполненным и чистим пустой хвост колонки
        if l.strip().lower() == "**complete**":
            last_non_empty_idx = -1
            for i in range(len(new_lines) - 1, -1, -1):
                if new_lines[i].strip():
                    last_non_empty_idx = i
                    break
            if last_non_empty_idx != -1 and new_lines[last_non_empty_idx].lstrip().startswith("- ") and " ✓" not in new_lines[last_non_empty_idx]:
                new_lines[last_non_empty_idx] = new_lines[last_non_empty_idx].rstrip() + " ✓"
            while new_lines and not new_lines[-1].strip():
                if len(new_lines) > 1 and new_lines[-2].startswith("## "): break
                new_lines.pop()
            skip_next_empty = True
            continue

        # Карточки: нормализуем в "- [ ] текст" / "- [x] текст"
        m = re.match(r'^[ \t]*([-*+]\s+)(\[[ xX]\]\s+)?(.*)$', l)
        if m:
            existing_cb = m.group(2) or ""
            text = m.group(3)
            if "**complete**" in text.lower():
                text = re.sub(r'\*\*[Cc]omplete\*\*', '', text, flags=re.IGNORECASE).strip()
                if " ✓" not in text: text += " ✓"
            # Даты Kanban-плагина. Сначала нормализуем пробел между "@"/"@@" и "{"
            # (ручной ввод " @ {…}"), затем пару "@{дата} @@{время}" сливаем в один
            # маркер "@{дата время}" (часы — строго 2 цифры, формат плагина HH:mm)
            if config.ENABLE_KANBAN_DATES:
                text = re.sub(r'(@+)\s+\{', r'\1{', text)
                text = re.sub(
                    rf'@\{{\s*({_date_part})\s*\}}\s*@@\{{\s*(\d{{2}}(?:{_time_sep_pattern}\d{{2}})?)\s*\}}',
                    r'@{\1 \2}',
                    text
                )
            # "@@{время}" без даты: привязываем время к текущему дню. Только если в
            # строке нет другой даты — маркера "@{…}" или эмодзи Tasks, иначе из
            # одной карточки вышло бы две задачи
            if config.ENABLE_KANBAN_DATES and config.ENABLE_KANBAN_TIME_ONLY and not re.search(r'(?<!@)@\{', text) and not any(ic in text for ic in config.TASKS_DUE_EMOJI):
                text = re.sub(rf'@@\{{\s*(\d{{2}}(?:{_time_sep_pattern}\d{{2}})?)\s*\}}', rf'@{{{today_str} \1}}', text)
            # (?<!@) защищает нативные маркеры "@{…}"/"@@{…}" от разворачивания скобок;
            # остальные случаи (эмодзи+скобки, обычные скобки) обрабатываются как раньше
            text = re.sub(
                r'([^\w\[\]\{\}\(\)]*)\s*(?<!@)[\{\(]([^}\)]*\d[^}\)]*)[\}\)]',
                lambda match: f" {match.group(1).strip()} {match.group(2)} " if match.group(1).strip() in config.TASKS_DUE_EMOJI else f" {match.group(2)} ",
                text
            )
            # Пробелы вокруг разделителей дат (цифра-разделитель-цифра) убираем,
            # остальной текст не трогаем
            text = re.sub(r'(?<=\d)\s*([\-./:])\s*(?=\d)', r'\1', text)
            # Схлопываем горизонтальные пробелы (переносы строк и одиночный пробел в "[ ]" не трогаем)
            text = re.sub(r'[ \t]{2,}', ' ', text).strip()

            is_checked = '[x]' in existing_cb.lower()
            l = f"- {'[x]' if is_checked else '[ ]'} {text}"
        new_lines.append(l)
    return "\n".join(new_lines)

def get_projects_list() -> list:
    """Сканирует папку Projects и возвращает список имен подпапок."""
    src_cfg = config.SOURCES.get('projects')
    if not src_cfg or not src_cfg.get("enabled"): return []
    search_path = get_search_path(src_cfg["folder"])
    if not search_path.exists(): return []

    projects = []
    for d in search_path.iterdir():
        # Скрытые (".", "_") папки пропускаем
        if d.is_dir() and not d.name.startswith('.') and not d.name.startswith('_'):
            projects.append(d.name)
    return sorted(projects)

# ==============================================================================
# 3. ОСНОВНЫЕ МЕТОДЫ ПОЛУЧЕНИЯ ДАННЫХ
# ==============================================================================

def get_notes(status_filter: str = None, source: str = 'projects', project_name: str = None) -> list:
    """
    Считывает и парсит файлы. Возвращает ПОЛНЫЙ список заметок без пагинации
    (пагинация применяется централизованно в handlers.py после сортировок).
    Для Projects можно ограничиться подпапкой проекта (project_name).
    """
    notes = []
    src_cfg = config.SOURCES.get(source)
    if not src_cfg or not src_cfg.get("enabled"): return []

    # В Projects при указанном проекте спускаемся в его подпапку
    if source == 'projects' and project_name:
        search_path = get_search_path(src_cfg["folder"]) / project_name
    else:
        search_path = get_search_path(src_cfg["folder"])

    src_name = src_cfg["name"]
    if not search_path.exists(): return []

    for md_file in search_path.rglob("*.md"):
        if ".obsidian" in md_file.parts or ".trash" in md_file.parts: continue
        try:
            # utf-8-sig срезает BOM в начале файла: без него frontmatter файла
            # с BOM молча не распознавался (строка начиналась с \ufeff, а не с '---')
            content = md_file.read_text(encoding="utf-8-sig")
            metadata = {}
            body = content

            # Извлекаем YAML frontmatter
            if content.startswith('---'):
                end_fm = content.find('---', 3)
                if end_fm != -1:
                    yaml_block = content[3:end_fm]
                    body = content[end_fm + 3:].strip()
                    # Ошибочный YAML логируем с именем файла; заметка продолжит
                    # обработку без frontmatter, но не уронит парсер
                    try: metadata = yaml.safe_load(yaml_block) or {}
                    except yaml.YAMLError as e:
                        logging.error(f"[PARSER] YAML не спарсился в {md_file.name}: {e}")

                    # Защита от YAML, не являющегося словарем (например, просто строка или список)
                    if not isinstance(metadata, dict):
                        metadata = {}

            # Доски (kanban-plugin в YAML) попадают только в источник kanban;
            # обычные заметки в папке Kanban — при parse_regular_notes
            has_kanban_plugin = 'kanban-plugin' in metadata
            if source == 'kanban' and not has_kanban_plugin and not src_cfg.get('parse_regular_notes', False): continue
            if source != 'kanban' and has_kanban_plugin: continue
            if has_kanban_plugin: body = _process_kanban_body(body)

            status = metadata.get('status')
            status = status.strip().title() if isinstance(status, str) else "Backlog"
            if source == 'projects' and status_filter and status != status_filter: continue

            # Дедлайн и дата создания из YAML
            due_dt = None
            if config.SEARCH_PROPERTY:
                for field in config.DUE_FIELDS:
                    if metadata.get(field):
                        due_dt = parse_datetime(metadata.get(field))
                        if due_dt: break

            created_dt = None
            for field in config.CREATED_FIELDS:
                if metadata.get(field):
                    created_dt = parse_datetime(metadata.get(field))
                    if created_dt: break

            # Теги: строка оборачивается в список
            tags = metadata.get('tags', [])
            if isinstance(tags, str): tags = [tags]
            if not isinstance(tags, list): tags = []

            if config.IGNORE_TAGS:
                note_tags_lower = [str(t).lower() for t in tags]
                if any(ignore_tag.lower() in note_tags_lower for ignore_tag in config.IGNORE_TAGS): continue

            # Позиция (для базовой сортировки Projects)
            pos = metadata.get('position')
            if isinstance(pos, list): pos = pos[0] if pos else 9999
            elif pos is None: pos = 9999
            try:
                pos_val = float(str(pos).strip())
                pos = int(pos_val) if pos_val.is_integer() else pos_val
            except (ValueError, TypeError): pos = 9999

            # Инлайн-задачи (чекбоксы/списки/тело с датами)
            parsed_tasks = []
            # Для канбан-борд отслеживаем текущую колонку по заголовкам "## " тела —
            # колонка хранится как техническое поле задачи, в текст карточки не вшивается
            current_col = None
            for line in body.splitlines():
                # Заголовок борды — запоминаем колонку (маркер done-колонок " ✓" в имя
                # колонки не включаем); сама строка не парсится как задача
                if has_kanban_plugin and line.startswith("## "):
                    current_col = re.sub(r'\s*✓\s*$', '', line[3:].strip()) or None
                    continue
                classified = _classify_line(line)
                if not classified: continue
                kind, status_mark, rest = classified
                # Занятые интервалы дат: не даем разным маркерам создать дубли задачи из одной даты
                matched_spans = []

                for regex in _task_date_regexes:
                    for match in regex.finditer(rest):
                        match_start, match_end = match.start(), match.end()
                        if any(match_start < prev_end and match_end > prev_start for prev_start, prev_end in matched_spans): continue
                        task_due = parse_datetime(match.group("date"))
                        if not task_due: continue
                        task_text = (rest[:match.start()] + rest[match.end():]).strip()
                        parsed_tasks.append({
                            "text": task_text,
                            "due": task_due,
                            "due_str": task_due.strftime("%d.%m %H:%M") if task_due else "",
                            "done": kind == "checkbox" and (status_mark or "").lower() == "x",
                            "column": current_col  # Колонка канбан-карточки (для задач из обычных заметок — None)
                        })
                        matched_spans.append((match_start, match_end))

            # Fallback дедлайна: если в YAML нет даты, берем первую дату невыполненной
            # подзадачи (визуальный якорь в карточке; пуши по-прежнему идут по подзадачам)
            due_is_fallback = False
            if not due_dt and parsed_tasks:
                for t in parsed_tasks:
                    if t['due'] and not t['done']:
                        due_dt = t['due']
                        due_is_fallback = True
                        break

            note = {
                "filename": md_file.stem, "due": due_dt,
                "due_str": due_dt.strftime("%d.%m.%Y %H:%M") if due_dt else "",
                "created": created_dt, "created_str": created_dt.strftime("%d.%m.%Y") if created_dt else "",
                "tags": tags, "status": status, "source": src_cfg["name"], "position": pos,
                "body": body, "tasks": parsed_tasks, "due_is_fallback": due_is_fallback,
                "raw_metadata": metadata
            }
            # В Tasks показываем только заметки с инлайн-задачами или своей датой
            if source == 'tasks' and not note['tasks'] and not note['due']: continue
            # if source == 'tasks' and not note['tasks']: continue
            notes.append(note)
        except Exception as e:
            logging.error(f"Ошибка чтения файла {md_file.name}: {e}", exc_info=True)
            continue

    # Базовая сортировка по позиции (для Projects)
    notes.sort(key=lambda n: (n['position'], n['filename']))
    return notes

def get_upcoming_notes(max_date: datetime = None) -> list:
    """Собирает заметки с дедлайнами (у заметки или подзадачи) в окне [now, max_date] по всем включенным источникам."""
    upcoming = []
    now = utils.get_now()
    if max_date is None: max_date = now + timedelta(days=1)
    active_sources = [key for key, val in config.SOURCES.items() if val.get("enabled")]
    for src in active_sources:
        # Сканируем всю папку источника (корень и подпапки) одним вызовом
        for n in get_notes(source=src):
            # Фильтр скрытия выполненных задач
            if getattr(config, 'HIDE_COMPLETED_IN_DEADLINES', False) and n['status'] in config.INACTIVE_STATUSES: continue
            if n['due'] and now <= n['due'] <= max_date: upcoming.append(n); continue
            for t in n['tasks']:
                if t['due'] and not t['done'] and now <= t['due'] <= max_date: upcoming.append(n); break
    return upcoming

def get_overdue_notes() -> list:
    """Собирает заметки с просроченными дедлайнами (у заметки или подзадачи) за последние OVERDUE_DAYS_LIMIT дней."""
    overdue = []
    now = utils.get_now()
    min_date = now - timedelta(days=getattr(config, 'OVERDUE_DAYS_LIMIT', 30))
    active_sources = [key for key, val in config.SOURCES.items() if val.get("enabled")]
    for src in active_sources:
        # Сканируем всю папку источника (корень и подпапки) одним вызовом
        for n in get_notes(source=src):
            # Фильтр скрытия выполненных задач
            if getattr(config, 'HIDE_COMPLETED_IN_DEADLINES', False) and n['status'] in config.INACTIVE_STATUSES: continue
            # Основной дедлайн заметки (включая fallback)
            if n['due'] and min_date <= n['due'] < now:
                overdue.append(n)
                continue

            # Если основного дедлайна нет (или он не просрочен) — проверяем подзадачи
            for t in n['tasks']:
                if t['due'] and not t['done'] and min_date <= t['due'] < now:
                    overdue.append(n)
                    break
    return overdue

def get_active_notes_for_notify(use_cache: bool = True) -> list:
    """Возвращает заметки с активными статусами для планировщика (с кэшем на 30 сек)."""
    now = utils.get_now()
    if use_cache and _notify_cache["last_fetch"] and (now - _notify_cache["last_fetch"]).total_seconds() < 30:
        return _notify_cache["data"]
    notes = []
    active_sources = [key for key, val in config.SOURCES.items() if val.get("enabled")]
    for src in active_sources:
        # Сканируем всю папку источника (корень и подпапки) одним вызовом
        for n in get_notes(source=src):
            if config.ACTIVE_STATUSES:
                if n['status'] not in config.ACTIVE_STATUSES: continue
            else:
                if n['status'] in INACTIVE_STATUSES: continue
            if n['due'] or any(t['due'] for t in n['tasks'] if not t['done']): notes.append(n)
    _notify_cache["last_fetch"] = now
    _notify_cache["data"] = notes
    return notes