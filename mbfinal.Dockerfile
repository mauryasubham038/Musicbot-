FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gcc curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*
# YouTube needs a JavaScript runtime for yt-dlp (deno 2.3+)
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh
WORKDIR /app
COPY mbfinal_requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY mbfinal_bot.py music_bot.py
COPY mbfinal_songs.csv songs.csv
CMD ["python", "music_bot.py"]
