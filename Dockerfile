# SEDAR-Scrape: FastAPI web + serial download worker + real Chrome under Xvfb.
#
# One image, two roles. By default `start.sh` runs BOTH the worker (under Xvfb)
# and the web server in one container -- simplest single-service Railway deploy.
# To split them into two Railway services from the same image, override the
# start command: `python -m app.worker` (run under xvfb) vs `uvicorn app.main:app`.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

# Chrome + Xvfb + the libs headed Chrome needs on a slim base.
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg ca-certificates xvfb \
      fonts-liberation libnss3 libxss1 libasound2 libatk-bridge2.0-0 \
      libgtk-3-0 libgbm1 libu2f-udev xdg-utils \
 && wget -q -O /tmp/chrome.deb \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# undetected-chromedriver ships only an sdist; disable build isolation so it
# installs against the image's setuptools.
RUN pip install --no-build-isolation -r requirements.txt

COPY . .
RUN chmod +x start.sh && mkdir -p /data

ENV CHROME_BINARY=/usr/bin/google-chrome \
    PORT=8000
EXPOSE 8000
VOLUME ["/data"]

CMD ["./start.sh"]
