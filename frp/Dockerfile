FROM alpine:latest

LABEL maintainer="an0sunshy <an0sunshy@gmail.com>"

ENV FRP_VERSION 0.16.0
ENV FRP_DOWNLOAD_URL https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz

RUN set -e \ 
        && apk upgrade --update \
        && apk add curl tar \
        && curl -sSLO ${FRP_DOWNLOAD_URL} \
        && tar -zxf frp_${FRP_VERSION}_linux_amd64.tar.gz --strip 1 

COPY ./entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
VOLUME /config
