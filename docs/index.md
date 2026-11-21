
<!--- mdformat-toc start --slug=github --->

<!---
!!! IF EDITING THE README, ENSURE TO COPY THE WHOLE FILE TO index.md in `/docs/` AND REMOVE THE REFERENCES TO ReadTheDocs THERE.
!!! ENSURE TO UPDATE LINKS AND REMOVE HTML FORMATTING
--->

# MusicDownloaderPaludo

Este fork é baseado no [spotDL / spotify-downloader](https://github.com/spotDL/spotify-downloader),
responsável pelo downloader original de terminal. Créditos aos spotDL Developers
e colaboradores; a licença MIT original está preservada em
[LICENSES/spotdl-MIT](https://github.com/tpklge/MusicDownloaderPaludo/blob/main/LICENSES/spotdl-MIT).
O MusicDownloaderPaludo mantém a licença GPL v3 do repositório.

O [README do projeto](https://github.com/tpklge/MusicDownloaderPaludo#readme)
contém as instruções de instalação da interface desktop. A documentação abaixo
inclui as funcionalidades de terminal herdadas do spotDL.

## Base original: spotDL v4

**spotDL** finds songs from Spotify playlists on YouTube, Internet Archive and SoundCloud and downloads them - along with album art, lyrics and metadata.

[![MIT License](https://img.shields.io/github/license/spotdl/spotify-downloader?color=44CC11&style=flat-square)](https://github.com/spotDL/spotify-downloader/blob/master/LICENSE)
[![PyPI version](https://img.shields.io/pypi/pyversions/spotDL?color=%2344CC11&style=flat-square)](https://pypi.org/project/spotdl/)
[![PyPi downloads](https://img.shields.io/pypi/dw/spotDL?label=downloads@pypi&color=344CC11&style=flat-square)](https://pypi.org/project/spotdl/)
![GitHub Repo stars](https://img.shields.io/github/stars/spotDL/spotify-downloader)
![Contributors](https://img.shields.io/github/contributors/spotDL/spotify-downloader?style=flat-square)
[![Discord](https://img.shields.io/discord/771628785447337985?label=discord&logo=discord&style=flat-square)](https://discord.gg/xCa23pwJWY)

> spotDL: The fastest, easiest and most accurate command-line music downloader.


## Installation

Refer to our [Installation Guide](installation.md) for more details.

### Python (Recommended Method)

- _spotDL_ can be installed by running `pip install spotdl`.
- To update spotDL run `pip install --upgrade spotdl`

  > On some systems you might have to change `pip` to `pip3`.

### Other options

- Prebuilt executable
    - Download the latest version from the [Releases Tab](https://github.com/spotDL/spotify-downloader/releases).
- On Termux
    - `curl -L https://raw.githubusercontent.com/spotDL/spotify-downloader/master/scripts/termux.sh | sh`
- Arch
    - There is an [Arch User Repository (AUR) package](https://aur.archlinux.org/packages/spotdl/) for spotDL.
- Docker
    - Build image:

      ```bash
      docker build -t spotdl .
      ```

    - Launch container with spotDL parameters (see section below). You need to create mapped
      volume to access song files

      ```bash
      docker run --rm -v $(pwd):/music spotdl download [trackUrl]
      ```

      If you bind-mount `$(pwd):/music`, that host directory must be writable by the container
      `UID`/`GID`.

    - Use Docker Compose if you want Docker to manage permissions for you:

      ```bash
      # Set your user ID and group ID (recommended)
      # This ensures downloaded files are owned by your user instead of root
      # If you don't set this, files will be owned by user 1000
      export PUID=$(id -u)
      export PGID=$(id -g)

      # Build and download
      docker compose build
      docker compose run --rm spotdl download [trackUrl]
      ```

      Docker Compose mounts `spotdl_music:/music` and stores downloads in that volume.
      Export files:

      ```bash
      docker compose up --no-start spotdl
      mkdir -p downloads
      docker compose cp spotdl:/music/. ./downloads/
      ```

- Build from source

    ```bash
    git clone https://github.com/spotDL/spotify-downloader && cd spotify-downloader
    pip install uv
    uv sync
    uv run scripts/build.py
    ```

    An executable is created in `spotify-downloader/dist/`.

### Installing FFmpeg

FFmpeg is required for spotDL. If using FFmpeg only for spotDL, you can simply install FFmpeg to your spotDL installation directory:
`spotdl --download-ffmpeg`

We recommend the above option, but if you want to install FFmpeg system-wide,
follow these instructions

- [Windows Tutorial](https://windowsloop.com/install-ffmpeg-windows-10/)
- OSX - `brew install ffmpeg`
- Linux - `sudo apt install ffmpeg` or use your distro's package manager

## Usage

### Native desktop interface

Install desktop support with `uv sync --extra desktop`, then run
`.venv/bin/python -m spotdl.gui` (Windows: `.venv\Scripts\python -m spotdl.gui`).
You can also double-click `iniciar_interface.command` on macOS or
`iniciar_interface.bat` on Windows.

Music Downloader opens a native desktop window with light and dark themes,
thumbnail previews, individual song progress bars and 1–32 adjustable workers.
It downloads YouTube videos/playlists and Spotify music, and keeps files and
history in your chosen output folder. See the
[desktop interface guide](usage.md#interface-gráfica-local-music-downloader)
for download controls, quality choices and cookies.

Using SpotDL without options:

```sh
spotdl [urls]
```

You can run _spotDL_ as a package if running it as a script doesn't work:

```sh
python -m spotdl [urls]
```

General usage:

```sh
spotdl [operation] [options] QUERY
```

There are different **operations** spotDL can perform. The _default_ is `download`, which simply downloads the songs from YouTube and embeds metadata.

The **query** for spotDL is usually a list of Spotify URLs, but for some operations like **sync**, only a single link or file is required.
For a list of all **options** use ```spotdl -h```

### Refer to [Usage](usage.md) for more info.

## Music Sourcing and Audio Quality

spotDL tries YouTube, Internet Archive and SoundCloud in that order by default. It moves to the next source when searching or downloading fails. You can choose providers and their order with `--audio`; existing configuration files retain their selected providers. Audio is downloaded from these sources, with Spotify supplying playlist and song metadata.

> **Note**
> Users are responsible for their actions and potential legal consequences. We do not support unauthorized downloading of copyrighted material and take no responsibility for user actions.

### Audio Quality

For YouTube sources, spotDL is designed to download the highest possible bitrate; which is 128 kbps for regular users and 256 kbps for YouTube Music premium users.

Check the [Audio Formats](usage.md#audio-formats-and-quality) page for more info.

## Contributing

Interested in contributing? Check out our [CONTRIBUTING.md](CONTRIBUTING.md) to find
resources around contributing along with a guide on how to set up a development environment.

### Join our amazing community as a code contributor

<a href="https://github.com/spotDL/spotify-downloader/graphs/contributors">
  <img class="dark-light" src="https://contrib.rocks/image?repo=spotDL/spotify-downloader&anon=0&columns=25&max=100&r=true" />
</a>

## License

This project is Licensed under the [MIT](https://github.com/spotDL/spotify-downloader/blob/master/LICENSE) License.
