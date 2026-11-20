FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY fake_iothub.py /app/
EXPOSE 8883
VOLUME ["/data"]
ENV CERT_DIR=/data
CMD ["python", "-u", "fake_iothub.py"]
