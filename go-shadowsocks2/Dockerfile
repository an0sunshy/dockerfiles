FROM golang:alpine as builder
MAINTAINER an0sunshy <an0sunshy@gmail.com>

ENV SS_VERSION v0.1.3
ENV SS_REPO github.com/shadowsocks/go-shadowsocks2
ENV GO111MODULE=on

RUN go get ${SS_REPO}@${SS_VERSION}  \
    && cd $GOPATH/pkg/mod/${SS_REPO}@${SS_VERSION} \
    && go build -o $GOPATH/bin/go-shadowsocks2

FROM alpine:latest

COPY --from=builder /go/bin/go-shadowsocks2 /usr/bin/go-shadowsocks2

ENTRYPOINT ["go-shadowsocks2"]
CMD ["--help"]
