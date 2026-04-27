FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Hypercorn (not uvicorn) so the container can speak h2c when Cloud Run
# is deployed with --use-http2. HTTP/2 end-to-end lifts the 32 MB ingress
# request body cap that uvicorn (HTTP/1.1 only) was forcing on us.
CMD ["hypercorn", "api.main:app", "--bind", "0.0.0.0:8080"]
