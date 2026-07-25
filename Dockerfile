FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY scripts ./scripts
# Fixed uid/gid so the owner can chown the bind-mounted ./data directory on
# the host to match (docker-compose mounts it over /data, which makes any
# in-image chown of /data itself moot at runtime — see README).
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /data \
    && chown -R bot:bot /app /data
USER bot
CMD ["python", "-m", "src.main"]
