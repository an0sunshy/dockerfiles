# Run for frpc
# docker run -d -v your_config_file:/config/frpc.ini an0sunshy/frp -c
#
# Run for frps
# docker run -d -v your_config_file:/config/frps.ini an0sunshy/frp -s 

FROM alpine:latest

LABEL maintainer="an0sunshy <an0sunshy@gmail.com>"
LABEL org.opencontainers.image.source=https://github.com/an0sunshy/dockerfiles


ENV FRP_VERSION 0.58.0
ENV FRP_DOWNLOAD_URL https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_amd64.tar.gz

VOLUME /config

ADD ${FRP_DOWNLOAD_URL} /tmp/

RUN  tar -zxf /tmp/frp_${FRP_VERSION}_linux_amd64.tar.gz --strip 1 \
    && rm /tmp/frp_${FRP_VERSION}_linux_amd64.tar.gz \
    && rm *.toml

ADD  entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
