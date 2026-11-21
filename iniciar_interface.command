#!/bin/zsh
# Abra com dois cliques no macOS.
cd -- "$(dirname -- "$0")" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print 'Ambiente Python não encontrado. Execute uv sync --extra desktop nesta pasta primeiro.'
  read -r '?Pressione Enter para fechar. '
  exit 1
fi
exec .venv/bin/python -m spotdl.gui "$@"
