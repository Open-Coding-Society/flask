FROM python:3.12-slim

WORKDIR /app

# Install python deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r /app/requirements.txt

# Copy service code
COPY . /app

EXPOSE 8591

# Run the websocket service
CMD ["python", "runner.py"]
