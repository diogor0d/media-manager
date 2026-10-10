# syntax=docker/dockerfile:1.7
# Official Bot API and its recursive TDLib submodule are pinned by this commit.
FROM python:3.12-alpine3.24@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a AS build
ARG TELEGRAM_API_REVISION=e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1
RUN apk add --no-cache git cmake make g++ gperf openssl-dev zlib-dev linux-headers \
    && git init /source \
    && git -C /source remote add origin https://github.com/tdlib/telegram-bot-api.git \
    && git -C /source fetch --depth=1 origin "$TELEGRAM_API_REVISION" \
    && git -C /source checkout --detach FETCH_HEAD \
    && test "$(git -C /source rev-parse HEAD)" = "$TELEGRAM_API_REVISION" \
    && git -C /source submodule update --init --recursive --depth=1 \
    && cmake -S /source -B /source/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /source/build --parallel 2 --target telegram-bot-api

FROM python:3.12-alpine3.24@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a
RUN apk add --no-cache ca-certificates libstdc++ libgcc openssl zlib \
    && adduser -D -u 10001 -s /sbin/nologin bot \
    && mkdir -m 0700 /telegram \
    && chown 10001:10001 /telegram
COPY --from=build /source/build/telegram-bot-api /usr/local/bin/telegram-bot-api
COPY --from=build /source/LICENSE_1_0.txt /usr/share/licenses/telegram-bot-api/LICENSE
COPY deploy/telegram/service.py /usr/local/bin/telegram-service.py
USER 10001:10001
WORKDIR /telegram
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/usr/local/bin/telegram-service.py"]
