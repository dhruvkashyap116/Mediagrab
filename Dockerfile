FROM python:3.12-slim

# Install ffmpeg and nodejs (required for yt-dlp JS challenges)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create downloads directory
RUN mkdir -p downloads

# Use environment variable for port (default 5000)
ENV PORT=5000

EXPOSE ${PORT}

CMD ["python", "server.py"]
