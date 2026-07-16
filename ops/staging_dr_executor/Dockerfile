FROM alpine:3.20

RUN apk add --no-cache \
    aws-cli \
    bash \
    coreutils \
    curl \
    jq \
    openssl \
    postgresql16-client

WORKDIR /dr
COPY contracts/ /dr/contracts/
COPY scripts/ /dr/scripts/
RUN sed -i 's/\r$//' /dr/scripts/*.sh && chmod +x /dr/scripts/*.sh

CMD ["/bin/bash", "/dr/scripts/idle.sh"]
