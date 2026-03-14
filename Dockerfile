FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py dashboard.py setup_credentials.py start.sh ./
CMD ["bash", "start.sh"]