"""Launch the native desktop interface: python -m spotdl.gui."""

import argparse
import sys
from pathlib import Path
from typing import cast

from spotdl.desktop.bootstrap import prepare_qt_plugins
from spotdl.utils.ffmpeg import is_ffmpeg_installed


def main():
    """Open a native window without starting a browser or local HTTP server."""
    parser = argparse.ArgumentParser(
        description="Music Downloader · aplicativo desktop"
    )
    parser.add_argument("--output", type=Path, help="Pasta permanente de downloads")
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, 33),
        metavar="1-32",
        help="Músicas processadas simultaneamente (padrão: 12 ou preferência salva)",
    )
    parser.add_argument(
        "--cookie-file", type=Path, help="Cookies Netscape para o YouTube"
    )
    args = parser.parse_args()
    if args.cookie_file and not args.cookie_file.is_file():
        parser.error("Arquivo de cookies não encontrado.")
    if not is_ffmpeg_installed():
        parser.error(
            "FFmpeg não encontrado. Execute: python -m spotdl --download-ffmpeg"
        )
    try:
        from PySide6.QtWidgets import (  # pylint: disable=import-outside-toplevel
            QApplication,
        )

        from spotdl.desktop.window import (  # pylint: disable=import-outside-toplevel
            MainWindow,
        )
    except ImportError as error:
        parser.error(
            f"Interface desktop indisponível ({error}). Instale: uv sync --extra desktop"
        )
    try:
        prepare_qt_plugins()
    except OSError as error:
        parser.error(f"Não foi possível preparar os componentes Qt: {error}")
    app = cast(QApplication, QApplication.instance()) or QApplication([sys.argv[0]])
    app.setApplicationName("Music Downloader")
    app.setOrganizationName("MusicDownloader")
    app.setStyle("Fusion")
    try:
        window = MainWindow(
            args.output,
            args.workers,
            str(args.cookie_file.resolve()) if args.cookie_file else None,
        )
    except OSError as error:
        parser.error(f"Não foi possível abrir a pasta de downloads: {error}")
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
