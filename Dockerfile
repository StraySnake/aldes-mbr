FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin aldes \
 && mkdir -p /data /logs && chown aldes:aldes /data /logs
COPY fake_iothub.py healthcheck.py docker-entrypoint.sh /app/
EXPOSE 8883
VOLUME ["/data"]
ENV CERT_DIR=/data
# Healthy = broker loop alive and connected to Mosquitto (see healthcheck.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "/app/healthcheck.py"]
# Starts as root only to fix volume ownership, then runs as user "aldes" (uid 1000).
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-u", "fake_iothub.py"]
