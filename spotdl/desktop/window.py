"""Native desktop controls with per-song progress, independent of a web server."""

# Qt extension classes and grouped widget construction are intentional.
# pylint: disable=no-name-in-module,attribute-defined-outside-init

import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from spotdl.desktop.theme import apply_theme
from spotdl.utils.config import DOWNLOADER_OPTIONS, get_spotdl_path
from spotdl.web.media import TERMINAL, DownloadManager

OWNER = "desktop"
STATUS = {
    "queued": "Na fila",
    "running": "Em andamento",
    "waiting": "Nova tentativa",
    "completed": "Concluído",
    "failed": "Falhou",
    "partial": "Com falhas",
    "cancelled": "Cancelado",
}


def speed_text(value):
    """Format bytes per second without hiding unknown measurements."""
    if not value:
        return "—"
    return (
        f"{value / 1024 / 1024:.1f} MB/s"
        if value >= 1024 * 1024
        else f"{value / 1024:.0f} KB/s"
    )


def time_text(value):
    """Format a source's estimated remaining time."""
    if value is None:
        return "—"
    seconds = max(0, int(value))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class ProgressDelegate(QStyledItemDelegate):
    """Draw real per-row progress bars without thousands of child widgets."""

    def paint(self, painter, option, index):
        """Render a per-row progress control using the native palette."""
        progress_option = QStyleOptionProgressBar()
        progress_option.rect = option.rect.adjusted(5, 9, -5, -9)
        progress_option.minimum, progress_option.maximum = 0, 100
        progress_option.progress = int(index.data(Qt.ItemDataRole.UserRole) or 0)
        progress_option.text = f"{progress_option.progress}%"
        progress_option.textVisible = True
        progress_option.palette = option.palette
        progress_option.state = option.state
        QApplication.style().drawControl(
            QStyle.ControlElement.CE_ProgressBar, progress_option, painter
        )


class MainWindow(QMainWindow):
    """Keep every widget mutation on the Qt thread; workers publish snapshots."""

    def __init__(
        self,
        output: Path | None = None,
        workers: int | None = None,
        cookie_file: str | None = None,
        preferences: QSettings | None = None,
    ):
        super().__init__()
        self.preferences = preferences or QSettings(
            str(get_spotdl_path() / "desktop.ini"), QSettings.Format.IniFormat
        )
        self.setWindowTitle("Music Downloader")
        self.resize(1320, 900)
        self.setMinimumSize(1000, 700)
        self.output = (
            output
            or Path(
                str(self.preferences.value("output", str(Path.cwd() / "downloads")))
            )
        ).resolve()
        self.manager = DownloadManager(
            self.output,
            DOWNLOADER_OPTIONS,
            workers=workers or int(str(self.preferences.value("workers", 12))),
        )
        self.background = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="preview"
        )
        self.messages: queue.SimpleQueue = queue.SimpleQueue()
        self.preview = None
        self.preview_sequence = 0
        self.rows: dict[tuple[str, int], QTreeWidgetItem] = {}
        self.parents: dict[str, QTreeWidgetItem] = {}
        self.snapshots: dict[tuple[str, int], tuple] = {}
        self.closing = False
        self._build()
        self.worker_spin.setValue(self.manager.workers)
        if cookie_file:
            self.cookies.setText(cookie_file)
        self._configure()
        self._theme(self.preferences.value("theme", "dark"))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    @staticmethod
    def _label(text, name=""):
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        if name:
            label.setObjectName(name)
        return label

    @staticmethod
    def _button(text, callback, primary=False):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(callback)
        return button

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(16)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(self._label("Music Downloader", "heading"))
        heading.addWidget(
            self._label(
                "Sua biblioteca, no seu computador. Áudio, vídeos e playlists.", "muted"
            )
        )
        header.addLayout(heading, 1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Tema escuro", "dark")
        self.theme_combo.addItem("Tema claro", "light")
        self.theme_combo.setAccessibleName("Tema da interface")
        self.theme_combo.currentIndexChanged.connect(
            lambda: self._theme(self.theme_combo.currentData())
        )
        header.addWidget(self.theme_combo)
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(splitter, 1)
        composer = QFrame()
        composer.setObjectName("card")
        upper = QHBoxLayout(composer)
        upper.setContentsMargins(18, 16, 18, 16)
        left = QVBoxLayout()
        left.addWidget(self._label("Adicionar à fila", "section"))
        left.addWidget(
            self._label(
                "Cole um ou vários links, um por linha. YouTube e Spotify.", "muted"
            )
        )
        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText(
            "https://www.youtube.com/watch?v=…\nhttps://open.spotify.com/playlist/…"
        )
        self.urls.setAccessibleName("Links para baixar, um por linha")
        self.urls.setMaximumHeight(82)
        self.urls.textChanged.connect(self._invalidate_preview)
        left.addWidget(self.urls)
        choices = QHBoxLayout()
        self.format_combo = QComboBox()
        for text, value in [
            ("MP3 · áudio", "mp3"),
            ("M4A · áudio", "m4a"),
            ("FLAC · áudio", "flac"),
            ("MP4 · vídeo + áudio", "mp4"),
        ]:
            self.format_combo.addItem(text, value)
        self.format_combo.setAccessibleName("Formato de saída")
        self.quality = QComboBox()
        for text, value in [
            ("Até 1080p", "1080"),
            ("Até 720p", "720"),
            ("Melhor disponível", "best"),
        ]:
            self.quality.addItem(text, value)
        self.quality.setAccessibleName("Qualidade do vídeo")
        self.quality.setToolTip("Disponível ao selecionar o formato MP4.")
        self.quality.setEnabled(False)
        self.format_combo.currentIndexChanged.connect(
            lambda: self.quality.setEnabled(self.format_combo.currentData() == "mp4")
        )
        self.scope = QComboBox()
        for text, value in [
            ("Vídeo individual / detectar link", "auto"),
            ("Playlist inteira", "playlist"),
        ]:
            self.scope.addItem(text, value)
        self.scope.setAccessibleName("Vídeo individual ou playlist inteira")
        self.scope.currentIndexChanged.connect(self._invalidate_preview)
        choices.addWidget(self.format_combo)
        choices.addWidget(self.quality)
        choices.addWidget(self.scope)
        left.addLayout(choices)
        buttons = QHBoxLayout()
        self.preview_button = self._button("Carregar miniatura", self.load_preview)
        buttons.addWidget(self.preview_button)
        buttons.addWidget(self._button("Adicionar aos downloads", self.add_links, True))
        left.addLayout(buttons)
        self.error = self._label("", "error")
        self.error.hide()
        left.addWidget(self.error)
        upper.addLayout(left, 3)
        preview = QVBoxLayout()
        self.cover = self._label(
            "Prévia somente por miniatura\nSem reprodução de áudio ou vídeo", "muted"
        )
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setAccessibleName("Miniatura do conteúdo")
        self.cover.setMinimumSize(240, 132)
        self.cover.setMaximumHeight(150)
        self.cover.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        preview.addWidget(self.cover)
        self.preview_title = self._label("O conteúdo do primeiro link aparece aqui.")
        self.preview_title.setMaximumWidth(310)
        preview.addWidget(self.preview_title)
        self.preview_author = self._label("", "muted")
        preview.addWidget(self.preview_author)
        preview.addStretch()
        upper.addLayout(preview, 1)
        splitter.addWidget(composer)

        bottom = QWidget()
        body = QHBoxLayout(bottom)
        body.setContentsMargins(0, 0, 0, 0)
        settings = self._settings_panel()
        settings.setMinimumWidth(245)
        settings.setMaximumWidth(290)
        body.addWidget(settings)
        queue_panel = QVBoxLayout()
        toolbar = QHBoxLayout()
        toolbar.addWidget(self._label("Progresso por música", "section"), 1)
        self.filter_combo = QComboBox()
        for text, value in [
            ("Todos", "all"),
            ("Em andamento", "active"),
            ("Finalizados", "history"),
        ]:
            self.filter_combo.addItem(text, value)
        self.filter_combo.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.filter_combo)
        queue_panel.addLayout(toolbar)
        self.overall = QProgressBar()
        self.overall.setAccessibleName("Progresso geral das músicas")
        self.overall.setValue(0)
        queue_panel.addWidget(self.overall)
        self.summary = self._label("Sua fila está pronta para começar.", "muted")
        queue_panel.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                "Música / coleção",
                "Estado",
                "Progresso",
                "Etapa / detalhes",
                "Worker",
                "Tentativa",
                "Velocidade",
                "Restante",
            ]
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setItemDelegateForColumn(2, ProgressDelegate(self.tree))
        self.tree.setAccessibleName(
            "Fila de downloads com progresso individual por música"
        )
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 95)
        self.tree.setColumnWidth(2, 130)
        self.tree.setColumnWidth(3, 190)
        for column, width in enumerate((60, 75, 85, 75), start=4):
            self.tree.setColumnWidth(column, width)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(False)
        queue_panel.addWidget(self.tree, 1)
        actions = QHBoxLayout()
        actions.addWidget(self._button("Cancelar selecionados", self.cancel_selected))
        actions.addWidget(self._button("Tentar novamente", self.retry_selected))
        actions.addWidget(self._button("Abrir arquivo / pasta", self.open_selected))
        actions.addStretch()
        actions.addWidget(self._button("Cancelar tudo", self.cancel_all))
        queue_panel.addLayout(actions)
        body.addLayout(queue_panel, 1)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 510])
        self.destination = self._label(f"Destino: {self.output}", "muted")
        self.destination.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.destination)
        self.statusBar().showMessage("Pronto · janela desktop nativa")

    def _settings_panel(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 12, 0)
        group = QGroupBox("Execução")
        form = QFormLayout(group)
        form.setVerticalSpacing(8)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.worker_spin = QSpinBox()
        self.worker_spin.setRange(1, 32)
        self.worker_spin.setToolTip(
            "Quantidade global de músicas processadas ao mesmo tempo. "
            "Pode ser alterada durante o download."
        )
        form.addRow("Workers", self.worker_spin)
        self.youtube_spin = QSpinBox()
        self.youtube_spin.setRange(1, 8)
        self.youtube_spin.setValue(
            int(str(self.preferences.value("youtube_concurrency", 2)))
        )
        self.youtube_spin.setToolTip(
            "Limite de operações simultâneas no YouTube, compartilhado por todos os workers."
        )
        form.addRow("Operações YouTube", self.youtube_spin)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0, 60)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(float(str(self.preferences.value("interval", 5))))
        form.addRow("Intervalo mínimo", self.interval_spin)
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setValue(int(str(self.preferences.value("retries", 4))))
        form.addRow("Novas tentativas", self.retry_spin)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(1, 300)
        self.delay_spin.setSuffix(" s")
        self.delay_spin.setValue(int(str(self.preferences.value("retry_delay", 10))))
        form.addRow("Espera inicial", self.delay_spin)
        self.bitrate = QComboBox()
        self.bitrate.addItems(["128k", "192k", "256k", "320k"])
        self.bitrate.setCurrentText(str(self.preferences.value("bitrate", "192k")))
        form.addRow("Bitrate de áudio", self.bitrate)
        layout.addWidget(group)
        layout.addWidget(
            self._label(
                "Workers e limites de rede mudam agora. "
                "Tentativas, bitrate e cookies valem para novos links.",
                "muted",
            )
        )
        self.network = self._label("", "muted")
        layout.addWidget(self.network)
        layout.addWidget(self._button("Escolher pasta…", self.choose_output))
        self.cookies = QLineEdit(str(self.preferences.value("cookies", "")))
        self.cookies.setPlaceholderText("Arquivo de cookies (opcional)")
        self.cookies.setAccessibleName("Caminho do arquivo de cookies")
        self.cookies.setToolTip(
            "Cookies Netscape do YouTube. Deixe vazio para não usar autenticação."
        )
        layout.addWidget(self.cookies)
        layout.addWidget(self._button("Selecionar cookies…", self.choose_cookies))
        layout.addWidget(
            self._label(
                "FLAC preserva o áudio recebido; não recupera qualidade perdida na fonte.",
                "muted",
            )
        )
        layout.addStretch()
        for spin in (
            self.worker_spin,
            self.youtube_spin,
            self.interval_spin,
            self.retry_spin,
            self.delay_spin,
        ):
            spin.valueChanged.connect(self._configure)
        self.bitrate.currentTextChanged.connect(self._configure)
        self.cookies.editingFinished.connect(self._configure)
        scroll.setWidget(widget)
        return scroll

    def _configure(self):
        try:
            cookies = self.cookies.text().strip()
            if cookies and not Path(cookies).is_file():
                raise ValueError("Arquivo de cookies não encontrado.")
            self.manager.configure(
                self.worker_spin.value(),
                self.retry_spin.value(),
                self.delay_spin.value(),
                self.youtube_spin.value(),
                self.interval_spin.value(),
                self.bitrate.currentText(),
                cookies or None,
            )
            for name, value in {
                "workers": self.worker_spin.value(),
                "youtube_concurrency": self.youtube_spin.value(),
                "interval": self.interval_spin.value(),
                "retries": self.retry_spin.value(),
                "retry_delay": self.delay_spin.value(),
                "bitrate": self.bitrate.currentText(),
                "cookies": cookies,
            }.items():
                self.preferences.setValue(name, value)
            self._error("")
            return True
        except ValueError as error:
            self._error(str(error))
            return False

    def _theme(self, theme):
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, theme)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(0 if theme == "dark" else 1)
        self.theme_combo.blockSignals(False)
        self.preferences.setValue("theme", theme)

    def _error(self, message):
        self.error.setText(message)
        self.error.setVisible(bool(message))

    def _invalidate_preview(self):
        self.preview_sequence += 1
        self.preview = None
        self.preview_button.setEnabled(True)
        self.cover.setPixmap(QPixmap())
        self.cover.setText(
            "Prévia somente por miniatura\nSem reprodução de áudio ou vídeo"
        )
        self.preview_title.setText("O conteúdo do primeiro link aparece aqui.")
        self.preview_author.clear()

    def load_preview(self):
        """Load metadata and a thumbnail outside the UI thread."""
        links = self.urls.toPlainText().strip().splitlines()
        if not links:
            self._error("Cole um link para carregar a miniatura.")
            return
        if not self._configure():
            return
        self.preview_sequence += 1
        sequence = self.preview_sequence
        url, scope = links[0].strip(), self.scope.currentData()
        self.preview_button.setEnabled(False)
        self.preview_title.setText("Buscando miniatura…")

        def fetch():
            try:
                data = self.manager.preview(url, scope)
                image = b""
                try:
                    if data.get("thumbnail", "").startswith("https://"):
                        with requests.get(
                            data["thumbnail"], timeout=(5, 15), stream=True
                        ) as response:
                            response.raise_for_status()
                            chunks = []
                            size = 0
                            for chunk in response.iter_content(65536):
                                size += len(chunk)
                                if size > 5 * 1024 * 1024:
                                    break
                                chunks.append(chunk)
                            else:
                                image = b"".join(chunks)
                except requests.RequestException:
                    pass
                self.messages.put((sequence, data, image, ""))
            except Exception as error:
                self.messages.put((sequence, None, b"", str(error)))

        self.background.submit(fetch)

    def add_links(self):
        """Queue every pasted URL without blocking on network I/O."""
        if not self._configure():
            return
        links = [
            line.strip()
            for line in self.urls.toPlainText().splitlines()
            if line.strip()
        ]
        if not links:
            self._error("Cole pelo menos um link para baixar.")
            return
        errors = []
        for url in links:
            try:
                meta = (
                    self.preview if self.preview and self.preview["url"] == url else {}
                )
                self.manager.submit(
                    OWNER,
                    url,
                    self.scope.currentData(),
                    self.format_combo.currentData(),
                    self.quality.currentData(),
                    meta.get("title", ""),
                    meta.get("thumbnail", ""),
                )
            except ValueError as error:
                errors.append(f"{url}: {error}")
        self._error("\n".join(errors))
        self.statusBar().showMessage(
            f"{len(links) - len(errors)} link(s) adicionado(s) à fila.", 6000
        )
        self.refresh()

    def refresh(self):
        """Poll snapshots instead of touching Qt widgets from worker threads."""
        while not self.messages.empty():
            sequence, data, image, error = self.messages.get_nowait()
            if sequence != self.preview_sequence:
                continue
            self.preview_button.setEnabled(True)
            if error:
                self.preview_title.setText("Não foi possível carregar a prévia.")
                self._error(error)
                continue
            self.preview = data
            self.preview_title.setText(data["title"])
            self.preview_author.setText(
                data["author"]
                + (f" · {data['count']} itens" if data.get("count") else "")
            )
            pixmap = QPixmap()
            if image and pixmap.loadFromData(image):
                self.cover.setPixmap(
                    pixmap.scaled(
                        280,
                        145,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                self.cover.setText("Miniatura indisponível")
        jobs = self.manager.list_jobs(OWNER)
        total, finished, progress, failures = 0, 0, 0.0, 0
        for job_index, job in enumerate(jobs):
            parent = self.parents.get(job["id"])
            if parent is None:
                parent = QTreeWidgetItem()
                parent.setData(0, Qt.ItemDataRole.UserRole, (job["id"], -1))
                self.tree.insertTopLevelItem(job_index, parent)
                self.parents[job["id"]] = parent
                parent.setExpanded(True)
            parent.setText(0, job["title"])
            parent.setToolTip(0, job["url"])
            parent.setText(1, STATUS.get(job["status"], job["status"]))
            parent.setData(2, Qt.ItemDataRole.UserRole, job["progress"])
            parent.setText(3, job["message"])
            parent.setToolTip(3, job["message"])
            rows = job.get("items", [])
            total += len(rows)
            for row in rows:
                key = (job["id"], row["index"])
                child = self.rows.get(key)
                if child is None:
                    child = QTreeWidgetItem(parent)
                    child.setData(0, Qt.ItemDataRole.UserRole, key)
                    self.rows[key] = child
                message = row["message"]
                if row["status"] == "waiting" and row.get("retry_at"):
                    message = (
                        f"Tentativa em {max(0, int(row['retry_at'] - time.monotonic()))}s · "
                        + message.partition(" · ")[2]
                    )
                values = (
                    row["title"],
                    STATUS.get(row["status"], row["status"]),
                    row["progress"],
                    message,
                    str(row["worker"] or "—"),
                    f"{row['attempt']}/{job.get('options', {}).get('retries', 4) + 1}",
                    speed_text(row.get("speed")),
                    time_text(row.get("eta")),
                )
                if values != self.snapshots.get(key):
                    for column, value in enumerate(values):
                        if column == 2:
                            child.setData(column, Qt.ItemDataRole.UserRole, value)
                        else:
                            child.setText(column, value)
                    child.setToolTip(0, row["url"])
                    child.setToolTip(3, message)
                    self.snapshots[key] = values
                finished += row["status"] in TERMINAL
                failures += row["status"] == "failed"
                progress += 100 if row["status"] in TERMINAL else row["progress"]
            mode = self.filter_combo.currentData()
            parent.setHidden(
                (mode == "active" and job["status"] in TERMINAL)
                or (mode == "history" and job["status"] not in TERMINAL)
            )
        self.overall.setValue(round(progress / total) if total else 0)
        self.summary.setText(
            f"{finished}/{total} músicas processadas · {failures} falhas · "
            f"{len(self.manager.active_workers)}/{self.manager.workers} workers ocupados"
        )
        active, cooldown = self.manager.limiter.state
        self.network.setText(
            f"YouTube: {active}/{self.youtube_spin.value()} operações"
            + (f" · pausa {int(cooldown)}s" if cooldown else "")
        )
        if (
            self.closing
            and all(job["status"] in TERMINAL for job in jobs)
            and not self.manager.active_workers
        ):
            self.close()

    def cancel_selected(self):
        """Cancel selected songs or entire collections."""
        for selected in self.tree.selectedItems():
            job_id, index = selected.data(0, Qt.ItemDataRole.UserRole)
            if index < 0:
                self.manager.cancel(job_id, OWNER)
            else:
                self.manager.cancel_item(job_id, index, OWNER)

    def cancel_all(self):
        """Request cancellation for all active collections."""
        for job in self.manager.list_jobs(OWNER):
            if job["status"] not in TERMINAL:
                self.manager.cancel(job["id"], OWNER)

    def retry_selected(self):
        """Retry failed songs independently or requeue a collection."""
        if not self._configure():
            return
        seen = set()
        for selected in self.tree.selectedItems():
            job_id, index = selected.data(0, Qt.ItemDataRole.UserRole)
            job = self.manager.owned_job(job_id, OWNER)
            row = job["items"][index] if index >= 0 else job
            if row["status"] not in {"failed", "cancelled", "partial"}:
                continue
            url = row["url"]
            if url in seen:
                continue
            seen.add(url)
            try:
                self.manager.submit(
                    OWNER,
                    url,
                    "playlist" if index < 0 and job["kind"] == "playlist" else "auto",
                    job["format"],
                    job["quality"],
                )
            except ValueError as error:
                self._error(str(error))

    def open_selected(self):
        """Open a completed file or output folder with the operating system."""
        selected = self.tree.selectedItems()
        path = self.output
        if selected:
            job_id, index = selected[0].data(0, Qt.ItemDataRole.UserRole)
            job = self.manager.owned_job(job_id, OWNER)
            filename = job["items"][index].get("file") if index >= 0 else None
            if filename:
                candidate = (self.output / filename).resolve()
                if candidate.is_relative_to(self.output) and candidate.exists():
                    path = candidate
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def choose_cookies(self):
        """Select an optional Netscape cookie file."""
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Selecionar cookies Netscape",
            "",
            "Cookies (*.txt);;Todos os arquivos (*)",
        )
        if filename:
            self.cookies.setText(filename)
            self._configure()

    def choose_output(self):
        """Switch the output directory when no downloads are active."""
        if any(job["status"] not in TERMINAL for job in self.manager.list_jobs(OWNER)):
            self._error("Aguarde ou cancele os downloads antes de trocar a pasta.")
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Escolher pasta de downloads", str(self.output)
        )
        if not folder:
            return
        try:
            manager = DownloadManager(
                Path(folder), DOWNLOADER_OPTIONS, self.worker_spin.value()
            )
        except OSError as error:
            self._error(f"Não foi possível abrir a pasta: {error}")
            return
        self.preview_sequence += 1
        self.manager.close()
        self.manager = manager
        self.output = manager.root
        self.preferences.setValue("output", str(self.output))
        self.tree.clear()
        self.parents.clear()
        self.rows.clear()
        self.snapshots.clear()
        self.destination.setText(f"Destino: {self.output}")
        self._configure()
        self.refresh()

    def closeEvent(self, event: QCloseEvent):  # pylint: disable=invalid-name
        """Cancel cooperatively and keep Qt alive while active workers wind down."""
        active = any(
            job["status"] not in TERMINAL for job in self.manager.list_jobs(OWNER)
        )
        if active and not self.closing:
            answer = QMessageBox.question(
                self,
                "Encerrar downloads?",
                "Há downloads em andamento. Cancelar e fechar? "
                "Os arquivos concluídos serão mantidos.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.closing = True
            central = self.centralWidget()
            if central:
                central.setEnabled(False)
            self.statusBar().showMessage("Encerrando operações em andamento…")
            self.manager.close()
            event.ignore()
            return
        if active:
            event.ignore()
            return
        self.timer.stop()
        self.preferences.setValue("output", str(self.output))
        self.preferences.sync()
        self.manager.close()
        self.background.shutdown(wait=False, cancel_futures=True)
        event.accept()
