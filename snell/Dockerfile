FROM alpine:latest as builder

ENV SNELL_VERSION 'v4.0.1'

WORKDIR /tmp

RUN apk --no-cache add unzip && \
    wget https://dl.nssurge.com/snell/snell-server-${SNELL_VERSION}-linux-amd64.zip -O snell.zip && \
    unzip snell.zip && \
    chmod +x snell-server

FROM frolvlad/alpine-glibc

COPY --from=builder /tmp/snell-server /usr/bin/snell-server

RUN apk add --no-cache libstdc++ 

CMD ["snell-server"]