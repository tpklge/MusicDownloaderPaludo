# MusicDownloaderPaludo

Aplicativo desktop para baixar músicas, vídeos e playlists, com temas claro e
escuro, progresso individual por música e controle de workers.

> **Créditos ao downloader original:** este projeto é baseado no
> [spotDL / spotify-downloader](https://github.com/spotDL/spotify-downloader),
> desenvolvido pelos **spotDL Developers**. O mecanismo original de download e
> sua interface de linha de comando (terminal) são do spotDL. Este fork acrescenta
> a interface desktop nativa e as melhorias descritas abaixo.

## Recursos

- Janela desktop nativa em Qt, com temas claro e escuro.
- Download direto de vídeos e playlists do YouTube.
- Download de músicas, álbuns e playlists a partir de links do Spotify.
- Prévia somente pela miniatura, sem reprodução de áudio ou vídeo.
- MP3, M4A e FLAC para áudio; MP4 para vídeos do YouTube.
- Resolução de até 720p, até 1080p ou a melhor disponível.
- Barras de progresso individuais, com etapa, worker e tentativa por música.
- Velocidade e tempo restante quando informados pela fonte.
- De 1 a 32 workers, ajustáveis durante os downloads.
- Limite separado de operações no YouTube, intervalo entre requisições,
  novas tentativas e bitrate configuráveis.
- Cancelamento e nova tentativa por música ou coleção.
- Seleção de pasta e arquivo de cookies por diálogos nativos.
- Arquivos, preferências e histórico persistentes.
- Interface de terminal e interface web disponíveis separadamente.

## Instalação

Requisitos: Python 3.11 ou superior, dentro da faixa suportada no `pyproject.toml`,
[uv](https://docs.astral.sh/uv/) e FFmpeg.

```bash
git clone https://github.com/tpklge/MusicDownloaderPaludo.git
cd MusicDownloaderPaludo
uv sync --extra desktop
```

Se o FFmpeg ainda não estiver instalado:

```bash
uv run --extra desktop python -m spotdl --download-ffmpeg
```

O Deno também pode ser instalado para os vídeos do YouTube que precisam dele:

```bash
uv run --extra desktop python -m spotdl --download-deno
```

## Abrir o aplicativo

Após instalar as dependências, abra com dois cliques:

- **macOS:** `iniciar_interface.command`.
- **Windows:** `iniciar_interface.bat`.

Ou execute, em qualquer sistema suportado:

```bash
uv run --extra desktop python -m spotdl.gui
```

Para definir uma pasta e a quantidade inicial de workers:

```bash
uv run --extra desktop python -m spotdl.gui --output "downloads" --workers 8
```

Cole um ou vários links, um por linha, escolha o formato e adicione à fila.
A miniatura corresponde ao primeiro link informado. Em links que contêm vídeo e
playlist, selecione se deseja baixar o vídeo individual ou a playlist inteira.

Os workers e os limites de rede podem mudar durante a execução. Tentativas,
bitrate e cookies são aplicados aos novos links. Ao reduzir os workers, as
operações atuais terminam antes de novos itens começarem.

Os arquivos ficam em `downloads/` por padrão. A pasta pode ser alterada na janela;
o aplicativo lembra a última escolha. Os downloads concluídos são mantidos
quando uma música é cancelada ou o programa é fechado.

Veja o [guia de uso](docs/usage.md#interface-gráfica-local-music-downloader)
para detalhes dos controles, cookies e formatos. FLAC preserva o áudio recebido
da fonte; converter áudio comprimido não recupera qualidade perdida.

## Uso pelo terminal

O CLI herdado do spotDL continua disponível:

```bash
uv run python -m spotdl download "URL_DO_SPOTIFY"
```

Para o fluxo de playlists com controle de workers e tentativas:

```bash
uv run python scripts/download_playlists.py "URL_DA_PLAYLIST" --workers 8
```

A interface web pode ser iniciada separadamente com `uv run python -m spotdl web`.

## Desenvolvimento e testes

```bash
uv sync --extra desktop
uv run --extra desktop pytest -m "not live"
```

Os testes incluem a interface Qt, o progresso individual, a alteração de workers
em execução, cancelamento, tentativas e persistência do histórico. Os testes
marcados como `live` dependem dos serviços externos e não fazem parte desse comando.

## Créditos e licença

- **Downloader original e CLI:** [spotDL Developers — spotify-downloader](https://github.com/spotDL/spotify-downloader).
- **Extração de mídia do YouTube:** [yt-dlp](https://github.com/yt-dlp/yt-dlp).
- **Interface desktop:** [Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/).
- **Este fork e suas adaptações:** [tpklge / Paludo](https://github.com/tpklge).

Este repositório mantém a licença **GNU GPL v3** escolhida para o
MusicDownloaderPaludo, disponível em [LICENSE](LICENSE). O código original do
spotDL mantém seu aviso de copyright e sua licença **MIT**, reproduzidos
integralmente em [LICENSES/spotdl-MIT](LICENSES/spotdl-MIT). O histórico de
contribuições do projeto original também é preservado.

Consulte [NOTICE.md](NOTICE.md) para a atribuição ao projeto de origem.
