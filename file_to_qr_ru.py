#!/usr/bin/env python3
"""
File to QR — утилита для холодного офлайн-хранения зашифрованных файлов в QR-кодах.

Работает в Termux на Android. Использует:
  - openssl CLI для AES-256-CBC шифрования (pbkdf2)
  - qrencode CLI для генерации QR
  - zbarimg для чтения QR из PNG/фото
  - termux-camera-photo для доступа к камере (Режим 3)
  - rich (единственная pip-зависимость) для TUI

НИКАКИХ сетевых вызовов. Полностью офлайн.
"""

import base64
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.prompt import Prompt, Confirm
    from rich.live import Live
except ImportError:
    print("❌ Не найдена библиотека 'rich'. Установи: pip install rich")
    sys.exit(1)

try:
    from PIL import Image, ImageOps, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

console = Console()

# ---------------------------------------------------------------------------
# Единая цветовая/иконочная семантика UI
# ---------------------------------------------------------------------------
# Раньше success/error/info-сообщения красились литералами ("red"/"green"/
# "blue") в каждом месте отдельно — цвета и иконки понемногу разъехались
# (где-то ❌, где-то без иконки; где-то "blue" для инфо, где-то "cyan").
# Здесь один источник правды: используем эти хелперы вместо голых
# console.print(f"[red]...[/red]") по всему файлу, чтобы стиль был
# гарантированно одинаковым везде и правился в одном месте.
_C_OK = "bright_green"      # успех — тот же зелёный, что в баннере/меню
_C_ERR = "bold red"         # ошибка
_C_WARN = "yellow"          # предупреждение
_C_INFO = "cyan"            # нейтральная информация/подсказка
_C_DIM = "bright_black"     # второстепенный/приглушённый текст
_C_ACCENT = "bright_cyan"   # заголовки подрежимов (вместо разнобоя blue/cyan)


def _ok(msg: str):
    console.print(f"[{_C_OK}]✓ {msg}[/{_C_OK}]")


def _err(msg: str):
    console.print(f"[{_C_ERR}]❌ {msg}[/{_C_ERR}]")


def _warn(msg: str):
    console.print(f"[{_C_WARN}]⚠ {msg}[/{_C_WARN}]")


def _info(msg: str):
    console.print(f"[{_C_INFO}]{msg}[/{_C_INFO}]")


def _section_panel(title: str) -> Panel:
    """Единый заголовок подрежима — раньше каждый menu_* делал свой
    Panel("[bold blue]...") с рассинхроном цвета и без отступов; теперь все
    заголовки подрежимов используют один стиль (bright_cyan, с паддингом),
    консистентный с рамкой главного меню (bright_green)."""
    return Panel(
        f"[bold {_C_ACCENT}]{title}[/bold {_C_ACCENT}]",
        border_style=_C_ACCENT,
        expand=False,
        padding=(0, 2),
    )


def _styled_table(title: str) -> Table:
    """Единый стиль таблиц результатов/настроек — раньше Table(title=...)
    создавались без border_style/паддинга и выглядели площе панелей рядом
    с ними; теперь рамка и заголовок в цвет остального UI."""
    return Table(
        title=title,
        title_style=f"bold {_C_ACCENT}",
        border_style=_C_DIM,
        header_style=f"bold {_C_INFO}",
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

MAX_INPUT_FILE_BYTES = 52 * 1024  # 52 КБ — жёсткий лимит на исходный файл

# Практический потолок размера сырого (до base64) куска данных на один QR,
# подобранный так, чтобы итоговая QR-версия оставалась печатаемой на 5x5 см
# и надёжно читаемой камерой телефона без макро-режима.
# При уровне коррекции M это соответствует QR-версии примерно 25-27.
DEFAULT_CHUNK_SIZE = 1300  # байт сырых данных до base64-раздутия

# Абсолютный потолок (мягкое предупреждение выше этого, hard-cap ниже версии 40)
PRACTICAL_MAX_CHUNK_SIZE = 1900  # байт сырых данных — примерно версия 30-32 при M

TEMP_DIR = Path("/data/data/com.termux/files/usr/tmp")
if not TEMP_DIR.exists():
    # fallback для тестирования вне Termux
    TEMP_DIR = Path("/tmp")

CONFIG_PATH = Path.home() / ".file_to_qr_config.json"
# Папка вывода по умолчанию — рядом со скриптом, а не в домашней папке.
SCRIPT_DIR = Path(__file__).resolve().parent

# Диагностика скорости сканирования: FILE_TO_QR_DEBUG_TIMING=1 python3 file_to_qr.py
# печатает в stderr, сколько времени уходит на съёмку/обработку/распознавание
# каждого кадра — полезно, если снимок кажется медленнее, чем ожидалось
# (например, для автоцикла в Режиме 5 или ручного снимка в Режиме 3).
FILE_TO_QR_DEBUG_TIMING = os.environ.get("FILE_TO_QR_DEBUG_TIMING") == "1"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "file_to_qr_output"
DEFAULT_DOWNLOADS = Path.home() / "storage" / "downloads"

QR_PREFIX = "QRVAULT"


# ---------------------------------------------------------------------------
# Проверка окружения
# ---------------------------------------------------------------------------

REQUIRED_BINARIES = {
    "openssl": "pkg install openssl-tool",
    "qrencode": "pkg install qrencode",
    "zbarimg": "pkg install zbar",
}

OPTIONAL_BINARIES = {
    "termux-camera-photo": "pkg install termux-api (и установи приложение Termux:API из F-Droid)",
}


# ---------------------------------------------------------------------------
# Визуальный фидбек на завершение операций (без звука/вибрации)
# ---------------------------------------------------------------------------

def _fx_flash_success(text: str):
    """Короткая победная анимация: текст мигает 3 раза ярким/тусклым тоном
    перед тем как остаться на экране постоянно. Используется в моментах
    завершения крупных операций (например, полный сбор всех QR-кусков),
    чтобы момент был визуально заметнее одной строчки текста.

    Best-effort — если Live не отрисуется на нестандартном терминале, просто
    печатает текст один раз статично и едет дальше.
    """
    try:
        # refresh_per_second был 8 — ниже, чем частота наших собственных
        # time.sleep(0.12-0.18) переключений кадров, из-за чего Live иногда
        # "проглатывал" промежуточный тусклый кадр и мигание читалось как
        # рывок, а не плавное пульсирование. 24 Гц с запасом покрывает шаг
        # анимации и убирает эту рассинхронизацию.
        with Live(console=console, refresh_per_second=24, transient=True) as live:
            for _ in range(3):
                live.update(f"[bold {_C_OK}]{text}[/bold {_C_OK}]")
                time.sleep(0.16)
                live.update(f"[{_C_DIM}]{text}[/{_C_DIM}]")
                time.sleep(0.10)
        console.print(f"[bold {_C_OK}]{text}[/bold {_C_OK}]")
    except Exception:
        try:
            console.print(f"[bold {_C_OK}]{text}[/bold {_C_OK}]")
        except Exception:
            pass


def check_environment(require_camera: bool = False) -> bool:
    """Проверяет наличие нужных бинарников. Возвращает True если всё ок.

    Раньше проверка была мгновенной и молчаливой — пользователь не видел,
    что вообще что-то проверяется (особенно заметно на первом запуске,
    когда check_environment вызывается до интро-баннера). Добавлен короткий
    статус-спиннер: сам поиск бинарников быстрый, но фидбек "идёт проверка"
    важнее скорости — иначе экран просто молчит долю секунды и это читается
    как зависание, а не как "всё ок, работаем дальше".
    """
    binaries_to_check = dict(REQUIRED_BINARIES)
    if require_camera:
        binaries_to_check["termux-camera-photo"] = OPTIONAL_BINARIES["termux-camera-photo"]

    with console.status(f"[{_C_INFO}]Проверка окружения...[/{_C_INFO}]", spinner="dots"):
        missing = [
            (binary, hint) for binary, hint in binaries_to_check.items()
            if shutil.which(binary) is None
        ]
        time.sleep(0.15)  # без этого спиннер иногда не успевает отрисоваться на быстром диске

    if missing:
        console.print()
        for binary, hint in missing:
            _err(f"Не найден '{binary}'. Установи: {hint}")
        console.print()
        return False

    return True


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

@dataclass
class Config:
    output_dir: str = str(DEFAULT_OUTPUT_DIR)
    chunk_size: int = DEFAULT_CHUNK_SIZE
    error_correction: str = "M"  # L / M / Q / H

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                return cls(
                    output_dir=data.get("output_dir", str(DEFAULT_OUTPUT_DIR)),
                    chunk_size=data.get("chunk_size", DEFAULT_CHUNK_SIZE),
                    error_correction=data.get("error_correction", "M"),
                )
            except (json.JSONDecodeError, OSError):
                _warn("Не удалось прочитать конфиг, использую значения по умолчанию")
        return cls()

    def save(self):
        data = {
            "output_dir": self.output_dir,
            "chunk_size": self.chunk_size,
            "error_correction": self.error_correction,
        }
        try:
            CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            _err(f"Не удалось сохранить конфиг: {e}")


# ---------------------------------------------------------------------------
# Оценка QR-версии (для превентивных предупреждений)
# ---------------------------------------------------------------------------

# Приблизительная ёмкость (байт, byte-mode) для уровней коррекции по версиям QR.
# Это огрублённая таблица, достаточная для UX-предупреждений, не для точных расчётов.
_QR_CAPACITY_BYTE_M = [
    0, 14, 26, 42, 62, 84, 106, 122, 152, 180, 213,  # версии 0-10
    251, 287, 331, 362, 412, 450, 504, 560, 624, 666,  # 11-20
    711, 779, 857, 911, 997, 1059, 1125, 1190, 1264, 1370,  # 21-30
    1452, 1538, 1628, 1722, 1809, 1911, 1989, 2099, 2213, 2331,  # 31-40
]


def estimate_qr_version(payload_len: int, level: str = "M") -> int:
    """Грубая оценка версии QR, которая понадобится для payload_len байт."""
    # для L ёмкость выше, для Q/H ниже; берём M-таблицу с поправочным коэффициентом
    factor = {"L": 1.27, "M": 1.0, "Q": 0.72, "H": 0.55}.get(level, 1.0)
    for version, cap in enumerate(_QR_CAPACITY_BYTE_M):
        if payload_len <= cap * factor:
            return version
    return 40  # больше максимума


def chunk_payload_len(raw_chunk_size: int) -> int:
    """Оценивает итоговую длину QR-payload для заданного размера сырого куска."""
    b64_len = int(raw_chunk_size * 4 / 3) + 4
    # + служебные поля QRVAULT|index|total|hash|
    overhead = len(QR_PREFIX) + 1 + 6 + 1 + 6 + 1 + 8 + 1
    return b64_len + overhead


def warn_if_chunk_too_big(raw_chunk_size: int, level: str) -> bool:
    """Возвращает True если размер куска в пределах практического потолка печати."""
    payload_len = chunk_payload_len(raw_chunk_size)
    version = estimate_qr_version(payload_len, level)
    if raw_chunk_size > PRACTICAL_MAX_CHUNK_SIZE or version > 30:
        _warn(
            f"Кусок размером {raw_chunk_size} байт даст QR примерно версии {version} "
            f"(уровень {level}). Такой QR может плохо сканироваться камерой при печати 5×5 см "
            f"(модули слишком мелкие). Рекомендуется размер до {PRACTICAL_MAX_CHUNK_SIZE} байт."
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Крипто: openssl-обёртки
# ---------------------------------------------------------------------------

class CryptoError(Exception):
    pass


def encrypt_file(input_path: Path, output_path: Path, password: str):
    """Шифрует файл через openssl AES-256-CBC + pbkdf2. Пароль передаётся через stdin."""
    cmd = [
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
        "-in", str(input_path),
        "-out", str(output_path),
        "-pass", "stdin",
    ]
    try:
        result = subprocess.run(
            cmd,
            # openssl's BIO password reader expects a line terminated by \n —
            # empty stdin (empty password, no newline) makes some openssl
            # builds report "Error reading password from BIO" instead of
            # accepting an empty password. Always send the trailing newline.
            input=password.encode("utf-8") + b"\n",
            capture_output=True,
            timeout=60,
        )
    finally:
        password = None  # noqa: F841 — попытка избавиться от ссылки на пароль
    if result.returncode != 0:
        raise CryptoError(result.stderr.decode("utf-8", errors="replace").strip() or "Неизвестная ошибка openssl")


def decrypt_file(input_path: Path, output_path: Path, password: str):
    """Расшифровывает файл через openssl. Пароль передаётся через stdin."""
    cmd = [
        "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
        "-in", str(input_path),
        "-out", str(output_path),
        "-pass", "stdin",
    ]
    try:
        result = subprocess.run(
            cmd,
            input=password.encode("utf-8") + b"\n",
            capture_output=True,
            timeout=60,
        )
    finally:
        password = None  # noqa: F841
    if result.returncode != 0:
        raise CryptoError("Неверный пароль или повреждённые данные")


def sha256_short(path: Path) -> str:
    """Первые 8 символов SHA256 файла."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()[:8]


# ---------------------------------------------------------------------------
# Генерация QR-кусков
# ---------------------------------------------------------------------------

def generate_qr_chunks(encrypted_path: Path, filehash: str, chunk_size: int,
                        output_dir: Path, error_correction: str = "M") -> list[Path]:
    """
    Base64-кодирует зашифрованный файл, режет на куски, для каждого куска
    генерирует QR PNG в output_dir. Возвращает список путей к PNG.
    """
    data = encrypted_path.read_bytes()
    b64_data = base64.b64encode(data).decode("ascii")

    chunks = [b64_data[i:i + chunk_size] for i in range(0, len(b64_data), chunk_size)] or [""]
    total = len(chunks)

    output_dir.mkdir(parents=True, exist_ok=True)
    png_paths = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Генерация QR-кодов...", total=total)
        for idx, chunk in enumerate(chunks, start=1):
            payload = f"{QR_PREFIX}|{idx}|{total}|{filehash}|{chunk}"
            png_path = output_dir / f"chunk_{idx:04d}_of_{total:04d}.png"
            cmd = [
                "qrencode", "-l", error_correction, "-o", str(png_path), payload
            ]
            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                raise CryptoError(
                    f"qrencode не смог сгенерировать QR для куска {idx}/{total} "
                    f"(возможно, кусок слишком большой): {stderr}"
                )
            png_paths.append(png_path)
            progress.update(task, advance=1)

    return png_paths


# ---------------------------------------------------------------------------
# Чтение QR
# ---------------------------------------------------------------------------

@dataclass
class QRChunk:
    index: int
    total: int
    filehash: str
    data: str


def parse_qr_payload(raw: str) -> QRChunk | None:
    """Парсит строку формата QRVAULT|index|total|filehash|base64_chunk."""
    parts = raw.split("|", 4)
    if len(parts) != 5 or parts[0] != QR_PREFIX:
        return None
    try:
        index = int(parts[1])
        total = int(parts[2])
    except ValueError:
        return None
    filehash = parts[3]
    data = parts[4]
    return QRChunk(index=index, total=total, filehash=filehash, data=data)


def read_qr_from_file(png_path: Path) -> QRChunk | None:
    """Запускает zbarimg на PNG-файле, парсит результат."""
    result = subprocess.run(
        ["zbarimg", "-q", "--raw", str(png_path)],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    return parse_qr_payload(raw)


def read_any_qr_from_file(image_path: Path) -> tuple[list[str], str]:
    """Читает ВСЕ QR-коды с изображения (не только формата QRVAULT) —
    обычные ссылки, текст, кастомные/цветные QR (например Telegram).
    zbarimg распознаёт по контрасту модулей и не зависит от цвета кода.

    Возвращает (список_raw_строк, статус). Список может содержать
    несколько значений, если на одном изображении несколько QR-кодов.
    """
    result = subprocess.run(
        ["zbarimg", "-q", "--raw", str(image_path)],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        if result.returncode == 4:
            return [], "no_qr_found"
        err = result.stderr.decode("utf-8", errors="replace").strip()
        return [], f"zbar_fail: {err or 'unknown'}"
    raw_out = result.stdout.decode("utf-8", errors="replace").strip()
    if not raw_out:
        return [], "no_qr_found"
    # zbarimg разделяет несколько найденных кодов символом новой строки
    values = [line for line in raw_out.split("\n") if line]
    return values, "ok"


def _parse_zbar_xml(xml_text: str) -> tuple[str | None, tuple[int, int, int, int] | None]:
    """Парсит XML-вывод zbarimg -Sxml, достаёт данные и bounding box QR-кода.

    Разные версии zbar отдают полигон по-разному:
      - старый формат: отдельные теги <point x="123" y="456"/>
      - новый формат (zbar >= 0.23): один атрибут polygon="+123,+456 +789,+10"
        внутри <symbol ...>, координаты со знаком (+/-).
    Поддерживаем оба, иначе на части устройств bbox всегда будет None.

    Возвращает (raw_data, (x_min, y_min, x_max, y_max)) или (None, None), если
    не удалось распарсить (не считается ошибкой — просто не будет рамки).
    """
    import re
    # <data><![CDATA[...]]></data>
    data_match = re.search(r"<data>\s*<!\[CDATA\[(.*?)\]\]>\s*</data>", xml_text, re.DOTALL)
    raw = data_match.group(1) if data_match else None

    bbox = None

    # Новый формат: <polygon points='+698,+1956 +673,+2388 +1098,+2385 +1109,+1958'/>
    poly_match = re.search(r'<polygon\s+points=[\'"]([^\'"]+)[\'"]', xml_text)
    if poly_match:
        coord_pairs = re.findall(r'([+-]?\d+),([+-]?\d+)', poly_match.group(1))
        if coord_pairs:
            xs = [int(x) for x, _ in coord_pairs]
            ys = [int(y) for _, y in coord_pairs]
            bbox = (min(xs), min(ys), max(xs), max(ys))

    # Старый формат: <point x="123" y="456"/> — обычно 4 точки полигона вокруг QR
    if bbox is None:
        points = re.findall(r'<point\s+x="(-?\d+)"\s+y="(-?\d+)"\s*/>', xml_text)
        if points:
            xs = [int(x) for x, _ in points]
            ys = [int(y) for _, y in points]
            bbox = (min(xs), min(ys), max(xs), max(ys))

    return raw, bbox


def _parse_zbar_xml_multi(xml_text: str) -> list[tuple[str, tuple[int, int, int, int] | None]]:
    """Парсит XML-вывод zbarimg -Sxml и достаёт ВСЕ найденные на кадре
    QR-коды (а не только первый, как _parse_zbar_xml) — нужно для
    мульти-QR захвата (один снимок с несколькими кодами в кадре).

    Возвращает список (raw_data, bbox_или_None) — по одной записи на
    каждый распознанный <symbol> в XML. Порядок в списке соответствует
    порядку в XML (обычно порядок обнаружения zbar, НЕ гарантированно
    осмысленный) — вызывающий код не должен полагаться на этот порядок
    для сборки файла, только на chunk.index из самих данных.
    """
    import re
    results = []
    # Каждый <symbol ...>...</symbol> содержит один найденный код
    for sym_match in re.finditer(r"<symbol\b[^>]*>.*?</symbol>", xml_text, re.DOTALL):
        sym_xml = sym_match.group(0)
        data_match = re.search(r"<data>\s*<!\[CDATA\[(.*?)\]\]>\s*</data>", sym_xml, re.DOTALL)
        if not data_match:
            continue
        raw = data_match.group(1)

        bbox = None
        poly_match = re.search(r'<polygon\s+points=[\'"]([^\'"]+)[\'"]', sym_xml)
        if poly_match:
            coord_pairs = re.findall(r'([+-]?\d+),([+-]?\d+)', poly_match.group(1))
            if coord_pairs:
                xs = [int(x) for x, _ in coord_pairs]
                ys = [int(y) for _, y in coord_pairs]
                bbox = (min(xs), min(ys), max(xs), max(ys))
        if bbox is None:
            points = re.findall(r'<point\s+x="(-?\d+)"\s+y="(-?\d+)"\s*/>', sym_xml)
            if points:
                xs = [int(x) for x, _ in points]
                ys = [int(y) for _, y in points]
                bbox = (min(xs), min(ys), max(xs), max(ys))

        results.append((raw, bbox))
    return results


def render_ascii_frame(image_path: Path, width: int = 60,
                        bbox: tuple[int, int, int, int] | None = None,
                        found: bool = False,
                        boxes: list[tuple[tuple[int, int, int, int], bool]] | None = None) -> str:
    """Рендерит JPEG в цветной ASCII-арт (RGB → truecolor rich-разметка).

    Режим одной рамки (обратная совместимость, Режимы 3/5 одиночный захват):
    передай bbox — рисует рамку вокруг найденного QR (зелёную при found=True,
    жёлтую если просто виден).

    Режим нескольких рамок (мульти-QR захват по клавише): передай boxes —
    список (bbox, found) пар, каждая рисуется своим цветом независимо.
    Если boxes задан, параметры bbox/found игнорируются.

    Без Pillow возвращает заглушку с текстовым уведомлением — рамка по
    координатам всё равно не нарисуется без пиксельных данных, но статус
    останется информативным вместо падения.
    """
    if not PIL_AVAILABLE:
        return "[dim](для ASCII-превью нужен Pillow: pip install pillow)[/dim]"

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        return f"[red](не удалось открыть кадр: {e})[/red]"

    orig_w, orig_h = img.size
    # Символы терминала примерно в 2 раза выше, чем широкие — уменьшаем высоту вдвое
    aspect_correction = 0.5
    height = max(1, int(width * (orig_h / orig_w) * aspect_correction))
    small = img.resize((width, height))
    pixels = small.load()

    ramp = " .:-=+*#%@"
    scale_x = orig_w / width
    scale_y = orig_h / height

    # Нормализуем в единый список рамок для отрисовки, независимо от того,
    # какой режим вызова использовался.
    box_list: list[tuple[tuple[int, int, int, int], bool]] = []
    if boxes:
        box_list = [b for b in boxes if b[0]]
    elif bbox:
        box_list = [(bbox, found)]

    lines = []
    for row in range(height):
        line_parts = []
        for col in range(width):
            r, g, b = pixels[col, row]
            brightness = (r * 299 + g * 587 + b * 114) // 1000  # luma
            char = ramp[min(len(ramp) - 1, brightness * len(ramp) // 256)]

            # Если несколько рамок накладываются на одну ASCII-клетку,
            # приоритет у "found" (зелёный), чтобы принятый кусок был
            # заметнее непринятого при перекрытии.
            border_found = None
            for box_bbox, box_found in box_list:
                x_min, y_min, x_max, y_max = box_bbox
                px, py = col * scale_x, row * scale_y
                margin_x, margin_y = scale_x, scale_y
                near_left_right = abs(px - x_min) < margin_x or abs(px - x_max) < margin_x
                near_top_bottom = abs(py - y_min) < margin_y or abs(py - y_max) < margin_y
                within_x = x_min - margin_x <= px <= x_max + margin_x
                within_y = y_min - margin_y <= py <= y_max + margin_y
                if (near_left_right and within_y) or (near_top_bottom and within_x):
                    if box_found:
                        border_found = True
                        break
                    elif border_found is None:
                        border_found = False

            if border_found is not None:
                color = "bold bright_green" if border_found else "bold yellow"
                line_parts.append(f"[{color}]#[/{color}]")
            else:
                # Реальный цвет пикселя через rich truecolor-разметку
                line_parts.append(f"[rgb({r},{g},{b})]{char}[/rgb({r},{g},{b})]")
        lines.append("".join(line_parts))
    return "\n".join(lines)


def _capture_frame_for_scan(camera_id: str = "0") -> tuple[Path | None, Path | None, float, str]:
    """Снимает кадр камерой и готовит улучшенную копию для zbar (апскейл +
    автоконтраст + резкость). Общая часть для одиночного и мульти-QR захвата.

    Возвращает (snapshot_path_или_None, scan_target_path_или_None,
    scale_factor, статус). При ошибке snapshot/scan_target — None, статус
    объясняет причину ("camera_fail: ...", "ok").
    """
    snapshot = TEMP_DIR / f"file_to_qr_scan_{os.getpid()}.jpg"
    _t0 = time.monotonic()
    result = subprocess.run(
        ["termux-camera-photo", "-c", camera_id, str(snapshot)],
        capture_output=True,
        timeout=15,
    )
    _t_camera = time.monotonic() - _t0
    if result.returncode != 0 or not snapshot.exists():
        err = result.stderr.decode("utf-8", errors="replace").strip()
        return None, None, 1.0, f"camera_fail: {err or 'no output file'}"
    if snapshot.stat().st_size == 0:
        return None, None, 1.0, "camera_fail: empty photo file"

    scan_target = snapshot
    scale_factor = 1.0
    _t1 = time.monotonic()
    if PIL_AVAILABLE:
        try:
            enhanced_path = TEMP_DIR / f"file_to_qr_scan_enhanced_{os.getpid()}.png"
            img = Image.open(snapshot).convert("L")
            if max(img.size) < 1600:
                factor = 1600 / max(img.size)
                img = img.resize(
                    (int(img.width * factor), int(img.height * factor)),
                    Image.LANCZOS,
                )
                scale_factor = factor
            img = ImageOps.autocontrast(img, cutoff=1)
            img = img.filter(ImageFilter.SHARPEN)
            img.save(enhanced_path)
            scan_target = enhanced_path
        except Exception:
            scan_target = snapshot
            scale_factor = 1.0
    _t_enhance = time.monotonic() - _t1
    if FILE_TO_QR_DEBUG_TIMING:
        sys.stderr.write(f"[timing] camera={_t_camera:.2f}s enhance={_t_enhance:.2f}s\n")

    return snapshot, scan_target, scale_factor, "ok"


def _capture_and_scan_qr(camera_id: str = "0") -> tuple[str | None, str, Path | None, tuple | None]:
    """Снимает кадр камерой, улучшает его и пытается распознать ЛЮБОЙ QR-код
    (не только формата QRVAULT). Возвращает (raw_текст_или_None, статус,
    путь_к_кадру_или_None, bbox_или_None).

    Это общая низкоуровневая функция для двух режимов: сборки файла из QR
    (Режим 3, где raw парсится как QRVAULT|...) и обычного сканера QR
    (Режим 5, где raw — это просто содержимое кода как есть: ссылка,
    текст, вход в Telegram-аккаунт и т.д., включая цветные/кастомные QR —
    zbarimg распознаёт по контрасту модулей и не зависит от цвета).
    """
    try:
        snapshot, scan_target, scale_factor, status = _capture_frame_for_scan(camera_id)
        if snapshot is None:
            return None, status, None, None

        _t2 = time.monotonic()
        zresult = subprocess.run(
            ["zbarimg", "-q", "--xml", "--polygon", str(scan_target)],
            capture_output=True,
            timeout=15,
        )
        _t_zbar = time.monotonic() - _t2
        if FILE_TO_QR_DEBUG_TIMING:
            sys.stderr.write(f"[timing] zbar={_t_zbar:.2f}s\n")
        if scan_target != snapshot:
            try:
                scan_target.unlink()
            except OSError:
                pass
        if zresult.returncode != 0:
            # zbarimg returncode 4 = QR не найден на кадре (это нормально, не ошибка)
            if zresult.returncode == 4:
                return None, "no_qr_found", snapshot, None
            err = zresult.stderr.decode("utf-8", errors="replace").strip()
            return None, f"zbar_fail: {err or 'unknown'}", snapshot, None

        xml_out = zresult.stdout.decode("utf-8", errors="replace")
        raw, bbox = _parse_zbar_xml(xml_out)
        if not raw:
            return None, "no_qr_found", snapshot, None
        # bbox пришёл в системе координат scan_target (может быть увеличенной
        # версией snapshot) — пересчитываем обратно в координаты snapshot,
        # на котором строится ASCII-превью, иначе рамка рисуется не в том месте.
        if bbox and scale_factor != 1.0:
            x_min, y_min, x_max, y_max = bbox
            bbox = (
                int(x_min / scale_factor),
                int(y_min / scale_factor),
                int(x_max / scale_factor),
                int(y_max / scale_factor),
            )
        return raw, "ok", snapshot, bbox
    except subprocess.TimeoutExpired:
        return None, "timeout", None, None


def read_any_qr_from_camera(camera_id: str = "0") -> tuple[str | None, str, Path | None, tuple | None]:
    """Снимает кадр и распознаёт ЛЮБОЙ QR (не только QRVAULT) — обычная
    ссылка, текст, вход в аккаунт и т.д. Тонкая обёртка над
    _capture_and_scan_qr без парсинга под формат вольта."""
    return _capture_and_scan_qr(camera_id)


def capture_and_scan_multi_qr(camera_id: str = "0") -> tuple[list[tuple[str, tuple | None]], str, Path | None]:
    """Снимает ОДИН кадр камерой и распознаёт ВСЕ QR-коды на нём разом
    (например, лист с 5-8 напечатанными кодами). Используется в режиме
    захвата по нажатию клавиши, где пользователь сам решает, когда снимать,
    а не в постоянном автоцикле.

    Возвращает (список (raw_данные, bbox_или_None) по каждому найденному
    коду, статус, путь_к_кадру_или_None). Порядок в списке — это порядок
    обнаружения zbar на кадре, НЕ гарантированно порядок index в данных;
    сборка файла всегда идёт по chunk.index, а не по этому порядку.
    """
    try:
        snapshot, scan_target, scale_factor, status = _capture_frame_for_scan(camera_id)
        if snapshot is None:
            return [], status, None

        zresult = subprocess.run(
            ["zbarimg", "-q", "--xml", "--polygon", str(scan_target)],
            capture_output=True,
            timeout=15,
        )
        if scan_target != snapshot:
            try:
                scan_target.unlink()
            except OSError:
                pass
        if zresult.returncode != 0:
            if zresult.returncode == 4:
                return [], "no_qr_found", snapshot
            err = zresult.stderr.decode("utf-8", errors="replace").strip()
            return [], f"zbar_fail: {err or 'unknown'}", snapshot

        xml_out = zresult.stdout.decode("utf-8", errors="replace")
        found = _parse_zbar_xml_multi(xml_out)
        if not found:
            return [], "no_qr_found", snapshot

        # Пересчитываем каждый bbox обратно в координаты snapshot (см. комментарий
        # в _capture_and_scan_qr про scan_target vs snapshot).
        results = []
        for raw, bbox in found:
            if bbox and scale_factor != 1.0:
                x_min, y_min, x_max, y_max = bbox
                bbox = (
                    int(x_min / scale_factor),
                    int(y_min / scale_factor),
                    int(x_max / scale_factor),
                    int(y_max / scale_factor),
                )
            results.append((raw, bbox))
        return results, "ok", snapshot
    except subprocess.TimeoutExpired:
        return [], "timeout", None


def read_multi_qrvault_from_camera(camera_id: str = "0") -> tuple[list[tuple[QRChunk | None, str]], list[tuple], str, Path | None]:
    """Снимает один кадр и парсит все найденные QR как куски QRVAULT
    (Режим 3, множественный захват). Куски, которые не парсятся под формат
    QRVAULT (например, случайно попавший в кадр посторонний QR), возвращаются
    как (None, "parse_fail: ...") — вызывающий код решает, показывать ли
    предупреждение, но это не останавливает обработку остальных кодов в кадре.

    Возвращает (список (chunk_или_None, статус) по каждому найденному коду,
    список bbox в ТОМ ЖЕ порядке — используется для подсветки на превью,
    общий статус кадра, путь к снимку).
    """
    found, status, snapshot = capture_and_scan_multi_qr(camera_id)
    if not found:
        return [], [], status, snapshot
    parsed = []
    bboxes = []
    for raw, bbox in found:
        bboxes.append(bbox)
        try:
            chunk = parse_qr_payload(raw)
            if chunk is None:
                parsed.append((None, "not_qrvault_format"))
            else:
                parsed.append((chunk, "ok"))
        except Exception as e:
            parsed.append((None, f"parse_fail: {e}"))
    return parsed, bboxes, "ok", snapshot


# ---------------------------------------------------------------------------
# Сборка кусков
# ---------------------------------------------------------------------------

@dataclass
class AssemblyState:
    chunks: dict = field(default_factory=dict)  # index -> QRChunk
    total: int | None = None
    filehash: str | None = None

    def add(self, chunk: QRChunk) -> tuple[bool, str]:
        """Добавляет кусок. Возвращает (успех, сообщение)."""
        if self.total is None:
            if chunk.total < 1:
                return False, f"Некорректный total в QR: {chunk.total}"
            self.total = chunk.total
            self.filehash = chunk.filehash
        else:
            if chunk.total != self.total:
                return False, f"Кусок {chunk.index} имеет другое общее число кусков ({chunk.total} vs {self.total})"
            if chunk.filehash != self.filehash:
                return False, f"Кусок {chunk.index} принадлежит другому файлу (хэш {chunk.filehash} vs {self.filehash})"
        # Индекс должен попадать в диапазон 1..total — иначе битый/чужой скан
        # может тихо застрять в словаре под несуществующим номером, и
        # is_complete() никогда не станет True (или того хуже, соберётся
        # мусор, который потом непонятно почему не расшифровывается).
        if not (1 <= chunk.index <= self.total):
            return False, f"Кусок с некорректным индексом {chunk.index} (ожидается 1..{self.total})"
        if chunk.index in self.chunks:
            if self.chunks[chunk.index].data != chunk.data:
                return False, f"Кусок {chunk.index} уже есть, но с другими данными — возможно, битый скан"
            return False, "duplicate"
        self.chunks[chunk.index] = chunk
        return True, "ok"

    def missing_indices(self) -> list[int]:
        if self.total is None:
            return []
        return [i for i in range(1, self.total + 1) if i not in self.chunks]

    def is_complete(self) -> bool:
        return self.total is not None and not self.missing_indices()

    def assembled_base64(self) -> str:
        return "".join(self.chunks[i].data for i in range(1, self.total + 1))


def assemble_chunks(chunks: list[QRChunk]) -> AssemblyState:
    """Собирает список QRChunk в AssemblyState с валидацией."""
    state = AssemblyState()
    errors = []
    for chunk in chunks:
        ok, msg = state.add(chunk)
        if not ok and msg != "duplicate":
            errors.append(msg)
    if errors:
        for e in errors:
            console.print(f"[{_C_ERR}]⚠ {e}[/{_C_ERR}]")
    return state


def build_decrypted_source_from_state(state: AssemblyState, dest_encrypted: Path) -> bool:
    """Декодирует base64 из собранного состояния, пишет зашифрованный файл, сверяет хэш."""
    b64_data = state.assembled_base64()
    try:
        raw = base64.b64decode(b64_data)
    except Exception as e:
        _err(f"Ошибка декодирования base64: {e}")
        return False
    dest_encrypted.write_bytes(raw)
    actual_hash = sha256_short(dest_encrypted)
    if actual_hash != state.filehash:
        console.print(
            f"[{_C_ERR}]⚠ Хэш собранного файла ({actual_hash}) не совпадает с ожидаемым ({state.filehash}). "
            f"Данные могут быть повреждены.[/{_C_ERR}]"
        )
        return Confirm.ask(f"[{_C_WARN}]Продолжить расшифровку несмотря на несовпадение хэша?[/{_C_WARN}]", default=False)
    return True


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

# Крупный блочный ASCII-шрифт 5 строк высотой — свой, самодельный (не QR-похожий
# паттерн, чтобы не создавать путаницу с реальными QR-кодами на экране).
# Каждая буква — список из 5 строк по 5-6 символов.
_BLOCK_FONT = {
    "U": ["██  ██", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", " ████ "],
    "D": ["█████ ", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", "█████ "],
    "N": ["██  ██", "███ ██", "██ ███", "██  ██", "██  ██", "██  ██", "██  ██"],
    "M": ["██  ██", "██████", "██████", "██  ██", "██  ██", "██  ██", "██  ██"],
    "P": ["██████", "██  ██", "██  ██", "██████", "██    ", "██    ", "██    "],
    "A": [" ████ ", "██  ██", "██  ██", "██████", "██  ██", "██  ██", "██  ██"],
    " ": ["      ", "      ", "      ", "      ", "      ", "      ", "      "],
}
_FONT_ROWS = 7

_BANNER_TEXT = "UDNMPA"

# Палитра для переливающегося градиента по буквам — идём по кругу через эти
# цвета кадр за кадром, создавая эффект "бегущей" подсветки надписи.
_BANNER_PALETTE = [
    "bright_green", "green", "cyan", "bright_cyan", "bright_blue", "blue",
    "bright_magenta", "magenta", "bright_green",
]


def _render_block_text(text: str, letter_colors: list[str] | None = None) -> str:
    """Собирает блочный ASCII-рендер строки text высотой _FONT_ROWS строк.
    letter_colors, если задан, красит каждую букву индивидуально (для
    градиента/пульсации) — длина должна совпадать с длиной text; при None
    вся надпись одним цветом."""
    letters = [_BLOCK_FONT.get(ch.upper(), _BLOCK_FONT[" "]) for ch in text]
    lines = []
    for row in range(_FONT_ROWS):
        parts = []
        for i, letter in enumerate(letters):
            glyph_row = letter[row]
            if letter_colors:
                color = letter_colors[i]
                parts.append(f"[bold {color}]{glyph_row}[/bold {color}]")
            else:
                parts.append(glyph_row)
            parts.append(" ")  # зазор между буквами
        lines.append("".join(parts))
    return "\n".join(lines)


def show_intro_animation():
    """Анимированная заставка при старте — крупная надпись UDNMPA:

      Фаза 1: буквы проявляются по одной слева направо (столбик за столбиком),
               достаточно медленно, чтобы эффект был заметен, а не мелькнул.
      Фаза 2: несколько секунд "бегущей" цветовой подсветки по буквам —
               переливающийся градиент, кадр за кадром сдвигается по палитре.
      Фаза 3: подпись-тэглайн печатается посимвольно под надписью.

    Полностью декоративный слой поверх Live() — обёрнут best-effort: если
    терминал не тянет анимацию, просто печатает надпись статично и едет дальше,
    ничего не ломая в остальной программе.
    """
    try:
        # refresh_per_second поднят с 20 до 30: шаг фазы 2 (0.09с между
        # кадрами) требует Live успевать перерисовываться быстрее раза в
        # 50мс, иначе на медленных терминалах Termux кадры градиента иногда
        # схлопываются и "бег" по буквам читается рывками, а не плавной
        # волной. 30 Гц даёт запас без ощутимой нагрузки на CPU телефона.
        with Live(console=console, refresh_per_second=30, transient=True) as live:
            # Фаза 1: буквы проявляются одна за другой слева направо
            for n in range(1, len(_BANNER_TEXT) + 1):
                partial = _BANNER_TEXT[:n] + " " * (len(_BANNER_TEXT) - n)
                rendered = _render_block_text(partial, [_C_OK] * len(_BANNER_TEXT))
                live.update(Panel(rendered, expand=False, border_style="green"))
                time.sleep(0.18)

            # Фаза 2: переливающийся градиент "бежит" по буквам несколько циклов
            palette_len = len(_BANNER_PALETTE)
            cycles = 2
            total_shifts = palette_len * cycles
            for shift in range(total_shifts):
                colors = [
                    _BANNER_PALETTE[(i + shift) % palette_len]
                    for i in range(len(_BANNER_TEXT))
                ]
                rendered = _render_block_text(_BANNER_TEXT, colors)
                live.update(Panel(rendered, expand=False, border_style=_C_OK))
                time.sleep(0.09)

            # Финальный чистый кадр перед выходом из Live
            final = _render_block_text(_BANNER_TEXT, [_C_OK] * len(_BANNER_TEXT))
            live.update(Panel(final, expand=False, border_style=_C_OK))
            time.sleep(0.4)

        # Live(transient=True) стирает панель на выходе — печатаем финальный
        # кадр ещё раз статично, чтобы надпись осталась на экране.
        final = _render_block_text(_BANNER_TEXT, [_C_OK] * len(_BANNER_TEXT))
        console.print(Panel(final, expand=False, border_style=_C_OK))
        console.print()
    except Exception:
        # Анимация — не критичный путь. Если что-то в терминале не
        # поддерживается, просто печатаем надпись статично и едем дальше.
        try:
            console.print(Panel(_render_block_text(_BANNER_TEXT), expand=False, border_style="green"))
            console.print()
        except Exception:
            pass


# Пункты меню: (номер, иконка, текст, цвет, описание). Цвет привязан к
# смыслу пункта и подобран в тон палитре баннера UDNMPA.
_MENU_ITEMS = [
    ("1", "🔐", "Зашифровать файл → QR", "bright_green", "источник → шифр → набор QR"),
    ("2", "📂", "Собрать файл из QR (из папки)", "cyan", "PNG-файлы → расшифровка"),
    ("3", "📷", "Собрать файл из QR (камера)", "bright_cyan", "съёмка по Enter, любой порядок"),
    ("4", "⚙ ", "Настройки", "yellow", "размер куска, коррекция ошибок"),
    ("5", "🔎", "Сканировать любой QR", "bright_magenta", "ссылки, текст — без расшифровки"),
    ("0", "🚪", "Выход", "grey70", ""),
]

_MENU_DIVIDER = "[bright_black]" + "─" * 44 + "[/bright_black]"


def _menu_row(num: str, icon: str, text: str, color: str, desc: str, cursor: bool = False) -> str:
    """Одна строка пункта меню + приглушённая подпись-описание под ней."""
    marker = f"[bold {color}]▸[/bold {color}]" if cursor else " "
    head = f"{marker} [bold {color}]{icon}  {num}[/bold {color}]  [{color}]{text}[/{color}]"
    if not desc:
        return head
    return f"{head}\n     [bright_black]{desc}[/bright_black]"


def _menu_panel(rows: list[str], footer: str | None = None) -> Panel:
    body = "\n".join(rows) if rows else " "
    if footer:
        body += f"\n{_MENU_DIVIDER}\n{footer}"
    return Panel(
        body,
        title="[bold bright_green]◆ FILE TO QR[/bold bright_green] [bright_black]v1.0 · офлайн[/bright_black]",
        subtitle="[bright_black]cold storage[/bright_black]",
        border_style="bright_green",
        expand=False,
        padding=(1, 2),
    )


def _menu_footer() -> str:
    return "[bright_black]Выбор цифрой + Enter[/bright_black]"


def show_main_menu(animate: bool = False):
    """Печатает главное меню. При animate=True пункты проявляются построчно
    через Live, с бегущим курсором ▸ по текущей строке (только при первом
    показе за сессию — в цикле main() при последующих заходах используется
    animate=False, чтобы не надоедать анимацией на каждом возврате из
    под-режима)."""
    footer = _menu_footer()
    full_rows = [_menu_row(*item) for item in _MENU_ITEMS]

    if not animate:
        console.print(_menu_panel(full_rows, footer))
        return

    try:
        with Live(console=console, refresh_per_second=30, transient=True) as live:
            shown: list[str] = []
            for item in _MENU_ITEMS:
                # Кадр 1: строка появляется с курсором ▸ (акцент "печатается сейчас")
                shown.append(_menu_row(*item, cursor=True))
                live.update(_menu_panel(shown))
                time.sleep(0.075)
                # Кадр 2: курсор гаснет, строка оседает в обычном виде
                shown[-1] = _menu_row(*item)
                live.update(_menu_panel(shown))
                time.sleep(0.035)
            live.update(_menu_panel(shown, footer))
            time.sleep(0.2)
        console.print(_menu_panel(full_rows, footer))
    except Exception:
        # Анимация не критична — при любом сбое просто печатаем статично.
        console.print(_menu_panel(full_rows, footer))


# ---------------------------------------------------------------------------
# Режим 1: Зашифровать файл → QR
# ---------------------------------------------------------------------------

def menu_encrypt(config: Config):
    console.print(_section_panel("🔐 Зашифровать файл → QR"))

    default_path = str(DEFAULT_DOWNLOADS) if DEFAULT_DOWNLOADS.exists() else str(Path.home())
    file_str = Prompt.ask("Путь к файлу для шифрования", default=default_path)
    input_path = Path(file_str).expanduser()

    if not input_path.exists() or not input_path.is_file():
        _err(f"Файл не найден: {input_path}")
        return

    size = input_path.stat().st_size
    if size > MAX_INPUT_FILE_BYTES:
        _err(
            f"Файл слишком большой: {size} байт (лимит {MAX_INPUT_FILE_BYTES} байт / 52 КБ). "
            f"Уменьши архив или раздели его."
        )
        return
    _ok(f"Файл найден: {input_path} ({size} байт)")

    # Пароль
    while True:
        password = getpass("Пароль: ")
        password2 = getpass("Повтори пароль: ")
        if password != password2:
            _warn("Пароли не совпадают, попробуй ещё раз")
            continue
        if password == "":
            if not Confirm.ask("[yellow]Пароль пустой. Точно без пароля?[/yellow]", default=False):
                continue
        break

    # Размер куска
    chunk_size = config.chunk_size
    if Confirm.ask(f"Использовать размер куска по умолчанию ({chunk_size} байт)?", default=True):
        pass
    else:
        chunk_size = int(Prompt.ask("Размер куска (байт)", default=str(chunk_size)))
    warn_if_chunk_too_big(chunk_size, config.error_correction)

    tmp_encrypted = TEMP_DIR / f"file_to_qr_{os.getpid()}.enc"
    try:
        status = console.status(f"[bold {_C_OK}]🔐 Шифрование файла...[/bold {_C_OK}]", spinner="bouncingBar")
        with status:
            try:
                encrypt_file(input_path, tmp_encrypted, password)
            except CryptoError as e:
                _err(f"Ошибка шифрования: {e}")
                return
            finally:
                del password
                del password2

            # Хэширование зашифрованных данных — на маленьких файлах (лимит
            # 52 КБ) это доли секунды, но без явного статуса это выглядит
            # как секундная заминка между "Шифрование..." и таблицей
            # результата. Обновляем текст того же статуса вместо того чтобы
            # открывать второй Live одновременно (rich не разрешает
            # параллельные Live-дисплеи в одной консоли).
            status.update(f"[{_C_INFO}]Вычисление хэша...[/{_C_INFO}]")
            filehash = sha256_short(tmp_encrypted)

        out_dir_name = f"{input_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}"
        out_dir = Path(config.output_dir).expanduser() / out_dir_name

        try:
            png_paths = generate_qr_chunks(
                tmp_encrypted, filehash, chunk_size, out_dir, config.error_correction
            )
        except CryptoError as e:
            _err(str(e))
            _warn("Попробуй уменьшить размер куска и повторить.")
            return

    finally:
        try:
            if tmp_encrypted.exists():
                tmp_encrypted.unlink()
        except OSError:
            pass

    table = _styled_table("Результат шифрования")
    table.add_column("Параметр", style=_C_INFO)
    table.add_column("Значение", style=_C_OK)
    table.add_row("Кусков сгенерировано", str(len(png_paths)))
    table.add_row("Размер куска", f"{chunk_size} байт")
    table.add_row("Хэш файла (для проверки)", filehash)
    table.add_row("Папка сохранения", str(out_dir))
    console.print(table)

    if Confirm.ask("Показать список сгенерированных файлов?", default=False):
        for p in png_paths:
            console.print(f"  [{_C_DIM}]{p}[/{_C_DIM}]")


# ---------------------------------------------------------------------------
# Режим 2: Собрать файл из QR (из папки)
# ---------------------------------------------------------------------------

def menu_decrypt_folder(config: Config):
    console.print(_section_panel("📂 Собрать файл из QR (из папки)"))

    folder_str = Prompt.ask("Путь к папке с PNG QR-кодов", default=config.output_dir)
    folder = Path(folder_str).expanduser()
    if not folder.exists() or not folder.is_dir():
        _err(f"Папка не найдена: {folder}")
        return

    png_files = sorted(folder.glob("*.png"))
    if not png_files:
        _err(f"В папке нет PNG-файлов: {folder}")
        return

    chunks: list[QRChunk] = []
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"), console=console,
    ) as progress:
        task = progress.add_task("Сканирование QR из файлов...", total=len(png_files))
        for png in png_files:
            chunk = read_qr_from_file(png)
            if chunk is not None:
                chunks.append(chunk)
            progress.update(task, advance=1)

    if not chunks:
        _err("Не удалось распознать ни одного QR-кода формата QRVAULT")
        return

    state = assemble_chunks(chunks)

    table = _styled_table("Статус сборки")
    table.add_column("Параметр", style=_C_INFO)
    table.add_column("Значение")
    table.add_row("Всего кусков ожидается", str(state.total))
    table.add_row("Найдено", str(len(state.chunks)))
    missing = state.missing_indices()
    if missing:
        table.add_row("Не хватает", f"[{_C_ERR}]{', '.join(map(str, missing))}[/{_C_ERR}]")
    else:
        table.add_row("Статус", f"[{_C_OK}]Все куски найдены[/{_C_OK}]")
    console.print(table)

    if missing:
        _err("Не все куски найдены, сборка невозможна")
        return

    _finish_assembly(state, config)


# ---------------------------------------------------------------------------
# Режим 3: Собрать файл из QR (камера)
# ---------------------------------------------------------------------------

CAMERA_PROGRESS_FILE = TEMP_DIR / "file_to_qr_camera_progress.json"


def _save_camera_progress(state: AssemblyState):
    try:
        data = {
            "total": state.total,
            "filehash": state.filehash,
            "chunks": {str(i): c.data for i, c in state.chunks.items()},
        }
        CAMERA_PROGRESS_FILE.write_text(json.dumps(data))
    except OSError:
        pass


def _load_camera_progress() -> AssemblyState | None:
    if not CAMERA_PROGRESS_FILE.exists():
        return None
    try:
        data = json.loads(CAMERA_PROGRESS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    state = AssemblyState(total=data["total"], filehash=data["filehash"])
    for idx_str, chunk_data in data["chunks"].items():
        idx = int(idx_str)
        state.chunks[idx] = QRChunk(index=idx, total=state.total, filehash=state.filehash, data=chunk_data)
    return state


def _clear_camera_progress():
    try:
        if CAMERA_PROGRESS_FILE.exists():
            CAMERA_PROGRESS_FILE.unlink()
    except OSError:
        pass


def menu_decrypt_camera(config: Config):
    console.print(_section_panel("📷 Собрать файл из QR (камера)"))

    if not check_environment(require_camera=True):
        return

    _info(
        "Съёмка по нажатию Enter — сам решаешь, когда фоткать. "
        "Можно держать в кадре сразу несколько QR (например, лист с 5-8 кодами), "
        "программа найдёт и распознает все за один снимок."
    )

    state = None
    if CAMERA_PROGRESS_FILE.exists():
        if Confirm.ask("Найден незавершённый прогресс сканирования. Продолжить с него?", default=True):
            state = _load_camera_progress()
    if state is None:
        state = AssemblyState()

    camera_id = Prompt.ask("ID камеры (0 = тыловая, 1 = фронтальная)", default="0")

    ascii_width = 60 if PIL_AVAILABLE else 0
    if not PIL_AVAILABLE:
        console.print(
            f"[{_C_DIM}]Подсказка: установи 'pip install pillow' для ASCII-превью кадра с камеры.[/{_C_DIM}]"
        )

    consecutive_camera_fails = 0

    try:
        while True:
            if state.total:
                found_n = len(state.chunks)
                missing = state.missing_indices()
                console.print(f"[{_C_OK}]✓ Найдено {found_n}/{state.total}[/{_C_OK}]" +
                              (f"  [{_C_WARN}]Не хватает: {', '.join(map(str, missing))}[/{_C_WARN}]" if missing else ""))
            input("Наведи камеру на QR (один или несколько) и нажми Enter для снимка (Ctrl+C — выход)... ")

            parsed, bboxes, status, snapshot_path = read_multi_qrvault_from_camera(camera_id)

            if status.startswith("camera_fail"):
                consecutive_camera_fails += 1
                console.print(f"[red]⚠ Камера не отвечает: {status}[/red]")
                if consecutive_camera_fails >= 3:
                    console.print(
                        f"[yellow]Попробуй другой ID камеры (сейчас: {camera_id}) "
                        f"или проверь разрешение камеры для Termux:API.[/yellow]"
                    )
                continue
            consecutive_camera_fails = 0

            if status == "no_qr_found":
                console.print(f"[{_C_DIM}]QR не найден в кадре, попробуй ещё раз.[/{_C_DIM}]")
                if snapshot_path:
                    try:
                        snapshot_path.unlink()
                    except OSError:
                        pass
                continue
            if status == "timeout":
                console.print("[red]⚠ Камера не ответила вовремя (timeout).[/red]")
                continue

            # Для каждого найденного на кадре кода решаем: принят (зелёный),
            # уже есть/дубликат (жёлтый), или ошибка формата/чужой QR (жёлтый,
            # не мешает остальным кодам в этом же кадре).
            box_list = []
            accepted_count = 0
            for (chunk, parse_status), bbox in zip(parsed, bboxes):
                if chunk is None:
                    if parse_status != "not_qrvault_format":
                        console.print(f"[yellow]⚠ Код в кадре не распознан: {parse_status}[/yellow]")
                    else:
                        console.print("[yellow]⚠ Код в кадре не в формате QRVAULT — пропущен.[/yellow]")
                    box_list.append((bbox, False))
                    continue
                ok, msg = state.add(chunk)
                if ok:
                    _save_camera_progress(state)
                    accepted_count += 1
                    box_list.append((bbox, True))
                elif msg == "duplicate":
                    box_list.append((bbox, True))  # уже был принят раньше — тоже зелёный
                else:
                    console.print(f"[red]⚠ Кусок {chunk.index}: {msg}[/red]")
                    box_list.append((bbox, False))

            if ascii_width and snapshot_path:
                ascii_frame = render_ascii_frame(snapshot_path, width=ascii_width, boxes=box_list)
                console.print(Panel(ascii_frame, title=f"Снимок — найдено QR: {len(parsed)}, принято новых: {accepted_count}"))
            else:
                console.print(f"[{_C_INFO}]Найдено QR в кадре: {len(parsed)}, принято новых: {accepted_count}[/{_C_INFO}]")

            if snapshot_path:
                try:
                    snapshot_path.unlink()
                except OSError:
                    pass

            if state.total and state.is_complete():
                break
    except KeyboardInterrupt:
        console.print("\n[yellow]Сканирование прервано пользователем.[/yellow]")
        if state.chunks:
            found_n = len(state.chunks)
            total_str = str(state.total) if state.total else "?"
            console.print(f"[{_C_INFO}]Прогресс сохранён: {found_n}/{total_str} кусков. Запусти Режим 3 снова, чтобы продолжить.[/{_C_INFO}]")
        return

    _fx_flash_success("✓ Все куски собраны!")
    _clear_camera_progress()
    _finish_assembly(state, config)


# ---------------------------------------------------------------------------
# Общий финал сборки (Режимы 2 и 3): декодирование + расшифровка + сохранение
# ---------------------------------------------------------------------------

def _finish_assembly(state: AssemblyState, config: Config):
    tmp_encrypted = TEMP_DIR / f"file_to_qr_assembled_{os.getpid()}.enc"
    tmp_decrypted = TEMP_DIR / f"file_to_qr_decrypted_{os.getpid()}.out"
    try:
        if not build_decrypted_source_from_state(state, tmp_encrypted):
            _err("Сборка отменена.")
            return

        password = getpass("Пароль для расшифровки: ")
        try:
            # bright_cyan вместо старого bright_green намеренно: тот же
            # смысловой код, что и в заголовках подрежимов (_C_ACCENT) — так
            # операция "чтение/восстановление" визуально отличается от
            # "создание" (шифрование, зелёное) на 🔐, оставаясь в одной
            # палитре с остальным UI, а не отдельным случайным цветом.
            with console.status(f"[bold {_C_ACCENT}]🔓 Расшифровка...[/bold {_C_ACCENT}]", spinner="bouncingBar"):
                try:
                    decrypt_file(tmp_encrypted, tmp_decrypted, password)
                except CryptoError as e:
                    _err(str(e))
                    return
        finally:
            del password

        default_out = str(DEFAULT_DOWNLOADS) if DEFAULT_DOWNLOADS.exists() else str(Path.home())
        dest_str = Prompt.ask("Куда сохранить расшифрованный файл (папка)", default=default_out)
        dest_name = Prompt.ask("Имя файла", default="restored_file")
        dest_path = Path(dest_str).expanduser() / dest_name
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(tmp_decrypted, dest_path)

        _ok(f"Файл сохранён: {dest_path}")
        _warn(
            "Внимание: этот файл лежит на диске незашифрованным. "
            "Перемести его в надёжное место и не храни открытым долго."
        )
    finally:
        for p in (tmp_encrypted, tmp_decrypted):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Режим 5: Сканировать любой QR (ссылки, текст, кастомные/цветные QR)
# ---------------------------------------------------------------------------

def _print_scanned_value(value: str):
    """Показывает распознанное содержимое QR и, если это похоже на URL,
    предлагает удобные действия."""
    console.print(Panel(
        value, title=f"[bold {_C_OK}]Содержимое QR[/bold {_C_OK}]",
        border_style=_C_OK, expand=False, padding=(0, 1),
    ))
    looks_like_url = value.strip().lower().startswith(("http://", "https://", "tg://"))
    if looks_like_url:
        console.print(f"[{_C_DIM}]Похоже на ссылку. Скопируй и открой в браузере/приложении.[/{_C_DIM}]")


def menu_scan_any_qr(config: Config):
    """Сканирует произвольный QR-код — не только созданные этим скриптом.
    Подходит для обычных ссылок, текста, а также кастомных/цветных QR
    (например специальных QR из Telegram) — zbarimg декодирует по контрасту
    модулей и не зависит от цвета самого кода."""
    console.print(_section_panel("🔎 Сканировать любой QR"))

    source = Prompt.ask(
        "Источник QR",
        choices=["камера", "файл"],
        default="камера",
    )

    if source == "файл":
        path_str = Prompt.ask("Путь к изображению с QR")
        image_path = Path(path_str).expanduser()
        if not image_path.exists():
            _err(f"Файл не найден: {image_path}")
            return
        values, status = read_any_qr_from_file(image_path)
        if status == "no_qr_found":
            _warn("QR-код на изображении не найден.")
            return
        if status != "ok":
            _err(f"Ошибка распознавания: {status}")
            return
        for i, value in enumerate(values, 1):
            if len(values) > 1:
                console.print(f"[{_C_DIM}]— QR #{i} —[/{_C_DIM}]")
            _print_scanned_value(value)
        return

    # Источник — камера
    if not check_environment(require_camera=True):
        return

    _warn(
        "Примечание: это не честное живое видео, а цикл снимков раз в ~0.3 сек. "
        "Держи телефон ровно, следи за освещением."
    )
    camera_id = Prompt.ask("ID камеры (0 = тыловая, 1 = фронтальная)", default="0")
    _info("Сканирую... (Ctrl+C для остановки)")

    ascii_width = 60 if PIL_AVAILABLE else 0
    if not PIL_AVAILABLE:
        console.print(
            f"[{_C_DIM}]Подсказка: установи 'pip install pillow' для ASCII-превью кадра с камеры.[/{_C_DIM}]"
        )

    consecutive_camera_fails = 0
    last_status = "starting"

    try:
        with Live(console=console, refresh_per_second=2) as live:
            while True:
                raw, status, snapshot_path, bbox = read_any_qr_from_camera(camera_id)
                last_status = status
                found_now = raw is not None

                if status.startswith("camera_fail"):
                    consecutive_camera_fails += 1
                    if consecutive_camera_fails >= 3:
                        live.update(Panel(
                            f"[{_C_ERR}]⚠ Камера не отвечает 3 раза подряд: {status}[/{_C_ERR}]\n"
                            f"[{_C_WARN}]Попробуй другой ID камеры (сейчас: {camera_id}) "
                            f"или проверь разрешение камеры для Termux:API.[/{_C_WARN}]",
                            title="Сканирование QR", border_style=_C_ERR,
                        ))
                        time.sleep(2)
                    else:
                        time.sleep(0.5)
                else:
                    consecutive_camera_fails = 0

                ascii_frame = ""
                if snapshot_path and ascii_width:
                    ascii_frame = render_ascii_frame(
                        snapshot_path, width=ascii_width, bbox=bbox, found=found_now
                    )
                if snapshot_path:
                    try:
                        snapshot_path.unlink()
                    except OSError:
                        pass

                status_line = f"[{_C_DIM}]Ищу QR... (последний статус: {last_status})[/{_C_DIM}]"
                body = f"{ascii_frame}\n\n{status_line}" if ascii_frame else status_line
                live.update(Panel(body, title="Сканирование QR", border_style=_C_ACCENT))

                if raw is not None:
                    break

                time.sleep(0.3)
    except KeyboardInterrupt:
        console.print(f"\n[{_C_WARN}]Сканирование прервано пользователем.[/{_C_WARN}]")
        return

    _ok("QR распознан")
    _print_scanned_value(raw)


# ---------------------------------------------------------------------------
# Режим 4: Настройки
# ---------------------------------------------------------------------------

def menu_settings(config: Config):
    console.print(_section_panel("⚙ Настройки"))

    table = _styled_table("Текущие настройки")
    table.add_column("Параметр", style=_C_INFO)
    table.add_column("Значение", style=_C_OK)
    table.add_row("Папка вывода", config.output_dir)
    table.add_row("Размер куска по умолчанию", f"{config.chunk_size} байт")
    table.add_row("Уровень коррекции ошибок", config.error_correction)
    console.print(table)

    if not Confirm.ask("Изменить настройки?", default=False):
        return

    config.output_dir = Prompt.ask("Папка вывода", default=config.output_dir)

    new_chunk = Prompt.ask("Размер куска по умолчанию (байт)", default=str(config.chunk_size))
    try:
        config.chunk_size = int(new_chunk)
    except ValueError:
        _warn("Некорректное число, оставляю прежнее значение")

    _info(
        "Уровень коррекции: L (наименее надёжный, больше данных на QR) / "
        "M (баланс, рекомендуется) / Q / H (самый надёжный, меньше данных на QR)"
    )
    level = Prompt.ask("Уровень коррекции ошибок", choices=["L", "M", "Q", "H"], default=config.error_correction)
    config.error_correction = level

    warn_if_chunk_too_big(config.chunk_size, config.error_correction)

    # Короткий спиннер на сохранение — сама запись JSON мгновенна, но без
    # какого-либо фидбека переход от прежней таблицы к финальному "✓
    # Настройки сохранены" был слишком резким скачком. Лёгкая пауза с явным
    # статусом делает момент сохранения ощутимым, а не мгновенным морганием.
    with console.status(f"[{_C_INFO}]Сохранение...[/{_C_INFO}]", spinner="dots"):
        config.save()
        time.sleep(0.2)

    _ok("Настройки сохранены")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _cleanup_stale_temp_files():
    """Подчищает temp-файлы file_to_qr_* от предыдущих запусков (например,
    оставшиеся после Ctrl+C посреди захвата кадра камерой). Файлы текущего
    процесса не могут попасть под удаление — они всегда содержат os.getpid()
    активного запуска. Не критично для работы, просто гигиена TEMP_DIR —
    ошибки молча игнорируются, это не должно мешать запуску программы.

    ВАЖНО: файл прогресса сборки (file_to_qr_camera_progress.json) НЕ трогаем —
    он должен переживать перезапуск программы, это его прямое назначение
    (см. _save_camera_progress / _load_camera_progress).
    """
    try:
        for p in TEMP_DIR.glob("file_to_qr_*"):
            if p == CAMERA_PROGRESS_FILE:
                continue
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def main():
    _cleanup_stale_temp_files()
    if not check_environment(require_camera=False):
        _err("Установи недостающие зависимости и запусти скрипт снова.")
        sys.exit(1)

    show_intro_animation()

    config = Config.load()

    first_menu_show = True
    while True:
        try:
            # Экран очищается перед КАЖДЫМ повторным показом меню (но не
            # перед первым — чтобы не срезать интро-баннер, который уже
            # только что отрисовался). Раньше экран просто рос вниз с каждым
            # возвратом из подрежима — на маленьком терминале Termux это
            # быстро превращалось в нечитаемую простыню из старых таблиц,
            # промптов и логов прошлых операций. console.clear() тут не
            # "дёргает" картинку — предыдущий результат уже отработал и
            # прочитан, а следующий кадр (меню) рисуется на чистом месте.
            if not first_menu_show:
                console.clear()
            console.print()
            show_main_menu(animate=first_menu_show)
            first_menu_show = False
            choice = Prompt.ask("Выбор", choices=["0", "1", "2", "3", "4", "5"], default="0")

            if choice == "0":
                _info("До встречи!")
                break
            elif choice == "1":
                menu_encrypt(config)
            elif choice == "2":
                menu_decrypt_folder(config)
            elif choice == "3":
                menu_decrypt_camera(config)
            elif choice == "4":
                menu_settings(config)
            elif choice == "5":
                menu_scan_any_qr(config)

        except KeyboardInterrupt:
            console.print(f"\n[{_C_WARN}]Прервано. Возврат в главное меню (Ctrl+C ещё раз для выхода).[/{_C_WARN}]")
            continue


if __name__ == "__main__":
    main()
