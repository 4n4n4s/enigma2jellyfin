FROM python:3.13.5-slim-bookworm
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app.py .

EXPOSE 8080

CMD ["python", "app.py"]
