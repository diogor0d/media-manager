# Third-Party Notices

The MIT License in [`LICENSE`](LICENSE) covers the original Media Manager source
code and documentation in this repository. Third-party dependencies remain
under their respective licenses.

The provided Dockerfile installs FFmpeg and codec libraries from Alpine Linux. That
FFmpeg build includes GPL-licensed components such as libx264. Anyone who
redistributes a prebuilt image is responsible for complying with the licenses
and source-availability requirements of the exact packages included in that
image.

Relevant upstream licensing and source information:

- [FFmpeg legal information](https://ffmpeg.org/legal.html)
- [Alpine FFmpeg package](https://pkgs.alpinelinux.org/package/edge/community/x86_64/ffmpeg)
- [x264 licensing](https://www.videolan.org/developers/x264.html)

The Dockerfile and lockfile remain public to identify and reproduce the
third-party components used by the project. They do not replace any additional
license obligations that may apply to a redistributed artifact.

The optional Telegram Dockerfile builds the official
[Telegram Bot API server](https://github.com/tdlib/telegram-bot-api), pinned at
`e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1`, with its pinned TDLib submodule.
These projects use the Boost Software License 1.0; the server license is included
in the optional image at `/usr/share/licenses/telegram-bot-api/LICENSE`.
