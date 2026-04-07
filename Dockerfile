FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py generator.py image.py bot.py rubrics.json tone_profiles.json ./
RUN mkdir -p archive/images

CMD ["python", "bot.py"]
