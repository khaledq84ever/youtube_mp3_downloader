FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server .

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 13000

CMD ["python", "app.py"]
