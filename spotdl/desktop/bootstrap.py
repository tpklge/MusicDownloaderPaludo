"""Prepare native Qt plug-in discovery before QApplication is constructed."""

# Keep Qt optional until the desktop launcher requests it.
# pylint: disable=import-outside-toplevel,no-name-in-module

import os
import stat
import sys
from pathlib import Path


def prepare_qt_plugins():
    """Handle macOS installations whose Qt plug-ins have the hidden file flag.

    Qt's factory loader excludes hidden files even when their explicit paths
    are readable. Clear only that flag in Qt's own plug-in directory; retain
    all other flags and permissions. Other platforms need no adjustment.
    """
    if sys.platform != "darwin":
        return
    from PySide6.QtCore import (  # pylint: disable=import-outside-toplevel,no-name-in-module
        QLibraryInfo,
    )

    root = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    for path in (root, *root.rglob("*")):
        flags = path.stat().st_flags
        if flags & stat.UF_HIDDEN:
            os.chflags(path, flags & ~stat.UF_HIDDEN)
