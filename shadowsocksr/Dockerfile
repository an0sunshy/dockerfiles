# Run shadowsocksr-python with a config file
#
# docker run -p port:port -v your_config_file:/client.json \
#       -d --name ssr \
#       an0sunshy/shadowsocksr

FROM alpine:latest

ENV VER=3.2.2
ENV SSR_SRC=https://github.com/an0sunshy/shadowsocksr/archive/

RUN apk add libsodium python3 wget && \
    wget $SSR_SRC/$VER.zip -O shadowsocksr.zip && \
    unzip shadowsocksr.zip && rm -f shadowsocksr.zip

WORKDIR shadowsocksr-$VER/shadowsocks

ENTRYPOINT ["python3", "local.py", "-c", "/client.json"]
