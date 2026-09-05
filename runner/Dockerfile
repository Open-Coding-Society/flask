FROM python:3.12-slim

WORKDIR /app

# Install Python deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir -r /app/requirements.txt

# Copy service code
COPY . /app

EXPOSE 8591

# Run Flask with Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8591", "--workers", "2", "runner:runner"]
