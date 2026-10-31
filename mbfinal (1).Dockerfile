FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg gcc curl unzip ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Kolkata
# YouTube needs a JavaScript runtime for yt-dlp (deno 2.3+)
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh
WORKDIR /app
COPY mbfinal_requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# YouTube changes constantly: always build with the newest yt-dlp (nightly)
RUN pip install --no-cache-dir -U --pre "yt-dlp[default]"
COPY mbfinal_bot.py music_bot.py
COPY mbfinal_panel.py panel.py
COPY mbfinal_songs.csv songs.csv
EXPOSE 8080
CMD ["python", "music_bot.py"]
