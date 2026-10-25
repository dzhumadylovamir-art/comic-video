#!/bin/bash
# Установка Comic Video: проверяет зависимости, собирает приложение и кладёт его в «Программы».
set -e
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

say_step() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

say_step "Инструменты разработчика (swift, python3)"
if ! xcode-select -p >/dev/null 2>&1 || ! command -v swiftc >/dev/null; then
  echo "Не найдены инструменты разработчика. Сейчас откроется окно установки."
  xcode-select --install || true
  echo "Дождитесь окончания установки и запустите ./install.sh ещё раз."
  exit 1
fi
echo "ок"

say_step "Библиотека Pillow для python3"
if ! /usr/bin/python3 -c "import PIL" >/dev/null 2>&1; then
  /usr/bin/python3 -m pip install --user --quiet pillow
fi
/usr/bin/python3 -c "import PIL; print('ок, Pillow', PIL.__version__)"

say_step "ffmpeg"
if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then
    brew install ffmpeg
  else
    echo "Нужен Homebrew, чтобы поставить ffmpeg. Установите его командой с сайта https://brew.sh"
    echo "и запустите ./install.sh ещё раз."
    exit 1
  fi
fi
echo "ок, $(ffmpeg -version | head -1)"

say_step "Распознавание манги (manga-ocr: около 2–3 ГБ, скачивается один раз)"
V="$HOME/Library/Application Support/ComicVideo/manga-ocr-venv"
if [ "$1" = "--no-manga-ocr" ]; then
  echo "пропущено (--no-manga-ocr): комиксы с вертикальным текстом распознаваться не будут"
else
  if ! "$V/bin/python" -c "import manga_ocr, cv2" >/dev/null 2>&1; then
    /usr/bin/python3 -m venv "$V"
    "$V/bin/python" -m pip install --quiet --upgrade pip
    "$V/bin/python" -m pip install --quiet manga-ocr opencv-python-headless
  fi
  "$V/bin/python" -c "from manga_ocr import MangaOcr; MangaOcr()" >/dev/null 2>&1  # скачивает модель
  echo "ок"
fi

say_step "Сборка Comic Video.app"
./build_app.sh

say_step "Установка в «Программы»"
rm -rf "/Applications/Comic Video.app"
ditto "Comic Video.app" "/Applications/Comic Video.app"
rm -rf "Comic Video.app"
echo "ок: /Applications/Comic Video.app"

say_step "VOICEVOX (японский голос)"
if [ -d "/Applications/VOICEVOX.app" ]; then
  echo "ок, установлен"
else
  echo "VOICEVOX не найден. Скачайте его с https://voicevox.hiroshiba.jp и перетащите в «Программы»."
  echo "Без него программа работает только с голосом macOS (хуже по качеству)."
fi

printf "\n\033[1mГотово!\033[0m Запустите «Comic Video» из папки «Программы» или через Spotlight.\n"
