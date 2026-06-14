FROM alpine:latest as builder

# v6.0.0-beta-2 (download token is "v6.0.0b2"); see
# https://kb.nssurge.com/surge-knowledge-base/release-notes/snell
ENV SNELL_VERSION 'v6.0.0b2'

WORKDIR /tmp

RUN apk --no-cache add unzip && \
    wget https://dl.nssurge.com/snell/snell-server-${SNELL_VERSION}-linux-amd64.zip -O snell.zip && \
    unzip snell.zip && \
    chmod +x snell-server

FROM frolvlad/alpine-glibc

LABEL maintainer="an0sunshy <an0sunshy@gmail.com>"
LABEL org.opencontainers.image.source=https://github.com/an0sunshy/dockerfiles

COPY --from=builder /tmp/snell-server /usr/bin/snell-server

RUN apk add --no-cache libstdc++ 

CMD ["snell-server"]